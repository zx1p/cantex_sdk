# Copyright (c) 2026 CaviarNine
# SPDX-License-Identifier: MIT OR Apache-2.0

"""
Cantex SDK -- Swap / Scalp Bot  (multi-account edition)

────────────────────────────────────────────────────────────────────────────
MULTI-ACCOUNT MODE  (recommended)
────────────────────────────────────────────────────────────────────────────
Create one sub-folder per account inside the ``accounts/`` directory:

    accounts/
        account1/
            config.json        ← strategy config for this account
            .env               ← CANTEX_OPERATOR_KEY, CANTEX_TRADING_KEY
            secrets/
                api_key.txt
        account2/
            config.json
            .env
            secrets/
                api_key.txt

All accounts run concurrently in the same process.  Each account uses its
own credentials and its own strategy configuration independently.  Logs are
prefixed with a magenta [account_name] tag so you can tell them apart.

────────────────────────────────────────────────────────────────────────────
SINGLE-ACCOUNT MODE  (backward-compatible)
────────────────────────────────────────────────────────────────────────────
If no ``accounts/`` directory exists the bot falls back to reading
``config.json`` and ``.env`` (or shell environment variables) from the
project root – exactly as before.

────────────────────────────────────────────────────────────────────────────
STRATEGIES  (set ``"strategy"`` in each account's config.json)
────────────────────────────────────────────────────────────────────────────
  "strategy": "swap"    -- randomised-interval swap loop (original)
  "strategy": "scalp"   -- price-threshold scalping loop (new)

────────────────────────────────────────────────────────────────────────────
SWAP strategy
────────────────────────────────────────────────────────────────────────────
  - Resolves live InstrumentId objects for token_a and token_b at startup.
  - Checks live balances each cycle: sells token_a if it has enough, otherwise
    falls back to selling token_b (reverse swap).
  - Picks a random sell amount between amount_min and the available balance.
  - Fetches a quote and skips the swap if the network fee >= max_network_fee.
  - Waits a random interval (interval_min_minutes – interval_max_minutes)
    between every cycle, whether the swap ran or was skipped.

────────────────────────────────────────────────────────────────────────────
SCALP strategy
────────────────────────────────────────────────────────────────────────────
  - Polls the pool price every interval_min_seconds – interval_max_seconds.
  - Two-state machine: WATCHING (no position) <-> HOLDING (have token_a).
  - BUY  when state is WATCHING: spends full token_b balance immediately.
  - SELL when state is HOLDING and any exit condition fires (checked in order):
        1. stop-loss      price <= entry * (1 - stop_loss_pct    / 100)
        2. profit target  price >= entry * (1 + profit_target_pct / 100)
     At least one of profit_target_pct or stop_loss_pct must be set.
  - Price metric: pool_price_before_trade from a token_a -> token_b quote,
    i.e. "how many token_b per one token_a".
  - On startup a non-zero token_a balance is treated as an existing position
    (entry price unknown) so the bot survives restarts cleanly.
  - Tracks and logs realised P&L per trade and cumulatively in token_b.

────────────────────────────────────────────────────────────────────────────
Required .env variables per account:

    CANTEX_OPERATOR_KEY   Ed25519 private key hex
    CANTEX_TRADING_KEY    secp256k1 private key hex

Optional:

    CANTEX_BASE_URL       API base URL (default: https://api.testnet.cantex.io)

Edit each account's config.json to configure instruments, amounts,
thresholds, intervals, and the fee threshold, then run:

    python main.py
"""

import asyncio
import json
import logging
import os
import random
import sys
from decimal import ROUND_DOWN, Decimal

from dotenv import dotenv_values, load_dotenv

from cantex_sdk import (
    CantexAPIError,
    CantexAuthError,
    CantexSDK,
    CantexTimeoutError,
    InstrumentId,
    IntentTradingKeySigner,
    OperatorKeySigner,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _ColoredFormatter(logging.Formatter):
    """Colorized, timestamp-free log formatter with optional account prefix."""

    _RST = "\033[0m"
    _ACC = "\033[35m"  # magenta for the [account] tag

    _STYLES: dict[int, tuple[str, str]] = {
        logging.DEBUG: ("\033[90m", "DEBUG"),
        logging.INFO: ("\033[96m", "INFO "),
        logging.WARNING: ("\033[93m", "WARN "),
        logging.ERROR: ("\033[91m", "ERROR"),
        logging.CRITICAL: ("\033[1;91m", "CRIT "),
    }

    def format(self, record: logging.LogRecord) -> str:
        lc, label = self._STYLES.get(record.levelno, ("\033[97m", record.levelname[:5]))
        # "cantex_bot.account1" -> show "[account1]" prefix; "cantex_bot" -> no prefix
        parts = record.name.split(".", 1)
        account_prefix = (
            f"  {self._ACC}[{parts[1]}]{self._RST}" if len(parts) > 1 else ""
        )
        return f"{lc}[{label}]{self._RST}{account_prefix}  {record.getMessage()}"


def _setup_logging() -> None:
    if sys.platform == "win32":
        os.system("")  # enable ANSI escape codes in Windows terminals

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColoredFormatter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


_setup_logging()
log = logging.getLogger("cantex_bot")

# ---------------------------------------------------------------------------
# Config loading & validation
# ---------------------------------------------------------------------------

CONFIG_PATH = "config.json"
ACCOUNTS_DIR = "accounts"
_VALID_STRATEGIES = ("swap", "scalp")


def _require_fields(cfg: dict, keys: list[str], section: str, path: str) -> None:
    for key in keys:
        if key not in cfg:
            log.error("Missing required key '%s' in [%s]  (%s)", key, section, path)
            sys.exit(1)


def _require_token_fields(cfg: dict, section: str, path: str) -> None:
    _require_fields(cfg, ["token_a", "token_b"], section, path)


def _validate_swap_config(swap_cfg: dict, path: str) -> None:
    required = [
        "token_a",
        "token_b",
        "amount_min",
        "interval_min_minutes",
        "interval_max_minutes",
        "max_network_fee",
    ]
    _require_fields(swap_cfg, required, "swap", path)
    if float(swap_cfg["interval_min_minutes"]) > float(
        swap_cfg["interval_max_minutes"]
    ):
        log.error("swap.interval_min_minutes > swap.interval_max_minutes  (%s)", path)
        sys.exit(1)


def _validate_scalp_config(scalp_cfg: dict, path: str) -> None:
    required = [
        "token_a",
        "token_b",
        "max_network_fee",
        "interval_min_seconds",
        "interval_max_seconds",
    ]
    _require_fields(scalp_cfg, required, "scalp", path)

    if float(scalp_cfg["interval_min_seconds"]) > float(
        scalp_cfg["interval_max_seconds"]
    ):
        log.error("scalp.interval_min_seconds > scalp.interval_max_seconds  (%s)", path)
        sys.exit(1)

    # watch_interval is optional — only validate if explicitly provided
    w_min = scalp_cfg.get("watch_interval_min_seconds")
    w_max = scalp_cfg.get("watch_interval_max_seconds")
    if w_min is not None and w_max is not None:
        if float(w_min) > float(w_max):
            log.error(
                "scalp.watch_interval_min_seconds (%s) must be <= "
                "watch_interval_max_seconds (%s)  (%s)",
                w_min,
                w_max,
                path,
            )
            sys.exit(1)

    profit_target = Decimal(str(scalp_cfg.get("profit_target_pct", "0")))
    stop_loss = Decimal(str(scalp_cfg.get("stop_loss_pct", "0")))
    if profit_target <= 0 and stop_loss <= 0:
        log.error(
            "scalp config must set at least one of 'profit_target_pct' or "
            "'stop_loss_pct' to a positive value  (%s)",
            path,
        )
        sys.exit(1)


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load, validate, and return the bot configuration from *path*."""
    if not os.path.exists(path):
        log.error("Config file not found  :  %s", path)
        sys.exit(1)

    with open(path, "r") as fh:
        cfg = json.load(fh)

    strategy = cfg.get("strategy", "swap")
    if strategy not in _VALID_STRATEGIES:
        log.error(
            "Unknown strategy '%s'  —  valid options: %s",
            strategy,
            ", ".join(_VALID_STRATEGIES),
        )
        sys.exit(1)

    if strategy == "swap":
        if "swap" not in cfg:
            log.error("Missing required key 'swap' in config  (%s)", path)
            sys.exit(1)
        _validate_swap_config(cfg["swap"], path)

    elif strategy == "scalp":
        if "scalp" not in cfg:
            log.error("Missing required key 'scalp' in config  (%s)", path)
            sys.exit(1)
        _validate_scalp_config(cfg["scalp"], path)

    return cfg


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    """Round *value* down to *decimal_places* decimal places."""
    step = Decimal(10) ** -decimal_places
    return value.quantize(step, rounding=ROUND_DOWN)


def random_amount(
    amount_min: Decimal,
    amount_max: Decimal,
    decimal_places: int,
) -> Decimal:
    """Return a uniformly random ``Decimal`` in [amount_min, amount_max]."""
    raw = random.uniform(float(amount_min), float(amount_max))
    return _quantize(Decimal(str(raw)), decimal_places)


async def resolve_instruments(
    sdk: CantexSDK,
    token_a_symbol: str,
    token_b_symbol: str,
    *,
    account_log: logging.Logger = log,
) -> tuple[InstrumentId, InstrumentId]:
    """
    Fetch real ``InstrumentId`` objects from the live API by matching token
    symbols / IDs (case-insensitive) against the account's token list.

    Exits the process with a clear error if either token is not found.
    """
    account_log.info("Resolving instruments from account...")
    info = await sdk.get_account_info()

    mapping: dict[str, InstrumentId] = {}
    for tok in info.tokens:
        mapping[tok.instrument.id.lower()] = tok.instrument
        if tok.instrument_symbol:
            mapping[tok.instrument_symbol.lower()] = tok.instrument

    result: dict[str, InstrumentId] = {}
    for symbol in (token_a_symbol, token_b_symbol):
        match = mapping.get(symbol.lower())
        if match is None:
            available = [(t.instrument.id, t.instrument_symbol) for t in info.tokens]
            account_log.error("Token not found  :  '%s'", symbol)
            account_log.error("Available tokens :  %s", available)
            sys.exit(1)
        result[symbol.lower()] = match

    token_a = result[token_a_symbol.lower()]
    token_b = result[token_b_symbol.lower()]
    account_log.info("%-16s  ->  admin: %.20s...", token_a.id, token_a.admin)
    account_log.info("%-16s  ->  admin: %.20s...", token_b.id, token_b.admin)
    return token_a, token_b


# ---------------------------------------------------------------------------
# Swap strategy helpers
# ---------------------------------------------------------------------------


async def resolve_direction(
    sdk: CantexSDK,
    token_a: InstrumentId,
    token_b: InstrumentId,
    amount_min: Decimal,
    decimal_places: int,
    *,
    account_log: logging.Logger = log,
) -> tuple[InstrumentId, InstrumentId, Decimal] | None:
    """
    Fetch live balances, decide the swap direction, and pick a random sell
    amount in ``[amount_min, available_balance]``.

    Priority:
      1. token_a -> token_b  when token_a balance >= amount_min
      2. token_b -> token_a  when token_b balance >= amount_min  (fallback)
      3. ``None``            when neither balance meets amount_min -> skip cycle.
    """
    info = await sdk.get_account_info()
    bal_a = info.get_balance(token_a)
    bal_b = info.get_balance(token_b)

    account_log.info("Balances")
    account_log.info("%-16s  :  %s", token_a.id, bal_a)
    account_log.info("%-16s  :  %s", token_b.id, bal_b)
    account_log.info("%-16s  :  %s", "Min required", amount_min)

    if bal_a >= amount_min:
        amount = random_amount(amount_min, bal_a, decimal_places)
        account_log.info(
            "Selling  %s  ->  %s   amount: %s", token_a.id, token_b.id, amount
        )
        return token_a, token_b, amount

    account_log.warning(
        "%s balance (%s) is below minimum (%s)  —  trying reverse swap",
        token_a.id,
        bal_a,
        amount_min,
    )

    if bal_b >= amount_min:
        amount = random_amount(amount_min, bal_b, decimal_places)
        account_log.info(
            "Selling  %s  ->  %s   amount: %s  [reversed]",
            token_b.id,
            token_a.id,
            amount,
        )
        return token_b, token_a, amount

    account_log.error("Insufficient balance in both tokens  —  skipping cycle")
    account_log.error(
        "%s: %s   |   %s: %s   |   min: %s",
        token_a.id,
        bal_a,
        token_b.id,
        bal_b,
        amount_min,
    )
    return None


# ---------------------------------------------------------------------------
# Swap strategy loop
# ---------------------------------------------------------------------------


async def run_swap_loop(
    sdk: CantexSDK,
    cfg: dict,
    *,
    account_log: logging.Logger = log,
) -> None:
    swap_cfg = cfg["swap"]

    token_a, token_b = await resolve_instruments(
        sdk,
        swap_cfg["token_a"],
        swap_cfg["token_b"],
        account_log=account_log,
    )

    amount_min = Decimal(str(swap_cfg["amount_min"]))
    decimal_places = int(swap_cfg.get("amount_decimal_places", 6))
    interval_min = float(swap_cfg["interval_min_minutes"])
    interval_max = float(swap_cfg["interval_max_minutes"])
    max_network_fee = Decimal(str(swap_cfg["max_network_fee"]))

    account_log.info("=" * 60)
    account_log.info("Cantex Swap Bot  —  Ready")
    account_log.info(
        "%-14s  :  %s  (admin: %.16s...)", "Token A", token_a.id, token_a.admin
    )
    account_log.info(
        "%-14s  :  %s  (admin: %.16s...)", "Token B", token_b.id, token_b.admin
    )
    account_log.info(
        "%-14s  :  %s  (%d decimal places)", "Min amount", amount_min, decimal_places
    )
    account_log.info(
        "%-14s  :  %.1f – %.1f min", "Interval", interval_min, interval_max
    )
    account_log.info("%-14s  :  %s", "Max net fee", max_network_fee)
    account_log.info("=" * 60)

    swap_count = 0
    fee_skips = 0
    bal_skips = 0
    cycle = 0

    while True:
        cycle += 1

        account_log.info(
            "─── Cycle %d   Swaps: %d  ·  Fee Skips: %d  ·  Bal Skips: %d",
            cycle,
            swap_count,
            fee_skips,
            bal_skips,
        )

        # ── Step 1: resolve direction and amount from live balances ────
        try:
            direction = await resolve_direction(
                sdk,
                token_a,
                token_b,
                amount_min,
                decimal_places,
                account_log=account_log,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            account_log.warning(
                "Could not fetch account info: %s  —  skipping cycle", exc
            )
            direction = None

        if direction is None:
            bal_skips += 1
            wait = random.uniform(interval_min, interval_max)
            account_log.info("Retrying in %.1f min", wait)
            await asyncio.sleep(wait * 60)
            continue

        sell_inst, buy_inst, actual_amount = direction

        # ── Step 2: get quote and apply fee guard ──────────────────────
        account_log.info("Fetching swap quote...")
        try:
            quote = await sdk.get_swap_quote(
                sell_amount=actual_amount,
                sell_instrument=sell_inst,
                buy_instrument=buy_inst,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            account_log.warning("Quote request failed: %s  —  skipping cycle", exc)
            wait = random.uniform(interval_min, interval_max)
            await asyncio.sleep(wait * 60)
            continue

        network_fee = quote.fees.network_fee.amount
        account_log.info("Quote")
        account_log.info("%-12s  :  %s %s", "Sell", actual_amount, sell_inst.id)
        account_log.info(
            "%-12s  :  %s %s", "Receive", quote.returned_amount, buy_inst.id
        )
        account_log.info("%-12s  :  %s", "Network fee", network_fee)
        account_log.info("%-12s  :  %s%%", "Fee", quote.fees.fee_percentage)
        account_log.info("%-12s  :  %s", "Slippage", quote.slippage)

        if network_fee >= max_network_fee:
            fee_skips += 1
            account_log.warning(
                "Network fee %s >= threshold %s  —  swap skipped  (total fee skips: %d)",
                network_fee,
                max_network_fee,
                fee_skips,
            )
            wait = random.uniform(interval_min, interval_max)
            account_log.info("Retrying in %.1f min", wait)
            await asyncio.sleep(wait * 60)
            continue

        # ── Step 3: execute swap ───────────────────────────────────────
        account_log.info(
            "Executing swap #%d  —  %s %s  ->  %s",
            swap_count + 1,
            actual_amount,
            sell_inst.id,
            buy_inst.id,
        )
        try:
            result = await sdk.swap(
                sell_amount=actual_amount,
                sell_instrument=sell_inst,
                buy_instrument=buy_inst,
            )
            swap_count += 1
            account_log.info("Swap #%d complete  ->  %s", swap_count, result)
        except CantexAuthError as exc:
            account_log.error(
                "Auth error during swap  (HTTP %d):  %s", exc.status, exc.body[:200]
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            account_log.error("Swap failed  :  %s", exc)

        # ── Step 4: wait random interval ──────────────────────────────
        wait = random.uniform(interval_min, interval_max)
        account_log.info(
            "Next cycle in %.1f min  —  Swaps: %d  ·  Fee Skips: %d  ·  Bal Skips: %d",
            wait,
            swap_count,
            fee_skips,
            bal_skips,
        )
        await asyncio.sleep(wait * 60)


# ---------------------------------------------------------------------------
# Scalp strategy loop
# ---------------------------------------------------------------------------


async def run_scalp_loop(
    sdk: CantexSDK,
    cfg: dict,
    *,
    account_log: logging.Logger = log,
) -> None:
    """
    Scalping strategy: always holds a position in token_a and manages it
    with a profit target and/or stop-loss. No price threshold is needed to
    enter — the bot immediately spends the full token_b balance on each buy
    and re-enters right after every sell.

    Cycle flow
    ----------
    WATCHING  ->  spend entire token_b balance to buy token_a  ->  HOLDING
    HOLDING   ->  poll price every interval; sell entire token_a balance when
                  stop-loss OR profit-target fires               ->  WATCHING
    (repeat)

    Price metric
    ------------
    ``pool_price_before_trade`` from a 1-unit token_a -> token_b probe each
    cycle. Using the same direction every cycle keeps entry_price and
    current_price directly comparable (both are "token_b per token_a").

    Exit conditions  (stop-loss checked first; profit target second)
    ----------------------------------------------------------------
    stop-loss      price <= entry_price * (1 - stop_loss_pct    / 100)
    profit target  price >= entry_price * (1 + profit_target_pct / 100)
    At least one must be configured.

    Restart safety
    --------------
    If a token_a balance >= min_position_amount is detected on startup the bot
    starts in HOLDING state and rebaselines entry_price to the current price
    so both exit conditions work immediately.

    P&L tracking
    ------------
    Per-trade P&L  =  token_b received on sell  -  token_b spent on buy.
    Cumulative P&L is printed in every cycle header.
    """
    scalp_cfg = cfg["scalp"]

    token_a, token_b = await resolve_instruments(
        sdk,
        scalp_cfg["token_a"],
        scalp_cfg["token_b"],
        account_log=account_log,
    )

    decimal_places = int(scalp_cfg.get("amount_decimal_places", 6))
    max_network_fee = Decimal(str(scalp_cfg["max_network_fee"]))
    interval_min = float(scalp_cfg["interval_min_seconds"])
    interval_max = float(scalp_cfg["interval_max_seconds"])
    # Separate interval for the WATCHING state (re-entry attempts).
    # Defaults to 4x the holding interval to avoid rate-limiting.
    watch_interval_min = float(
        scalp_cfg.get("watch_interval_min_seconds", interval_min * 4)
    )
    watch_interval_max = float(
        scalp_cfg.get("watch_interval_max_seconds", interval_max * 4)
    )
    min_position = Decimal(str(scalp_cfg.get("min_position_amount", "0.001")))

    _raw_profit = str(scalp_cfg.get("profit_target_pct", "0"))
    profit_target_pct: Decimal | None = (
        Decimal(_raw_profit) if Decimal(_raw_profit) > 0 else None
    )

    _raw_stop = str(scalp_cfg.get("stop_loss_pct", "0"))
    stop_loss_pct: Decimal | None = (
        Decimal(_raw_stop) if Decimal(_raw_stop) > 0 else None
    )

    # ── Detect initial position; rebase entry_price if already holding ─
    account_log.info("Checking initial balances for position state...")
    try:
        init_info = await sdk.get_account_info()
        init_bal_a = init_info.get_balance(token_a)
        init_bal_b = init_info.get_balance(token_b)
    except (CantexAPIError, CantexTimeoutError) as exc:
        account_log.error("Failed to fetch initial account info: %s", exc)
        sys.exit(1)

    holding: bool = init_bal_a >= min_position
    entry_price: Decimal | None = None
    entry_cost_b: Decimal | None = None

    # ── Stats ──────────────────────────────────────────────────────────
    buy_count = 0
    sell_count = 0
    fee_skips = 0
    total_realized_pnl = Decimal("0")
    cycle = 0

    # ── Summary banner ─────────────────────────────────────────────────
    account_log.info("=" * 60)
    account_log.info("Cantex Scalp Bot  —  Ready")
    account_log.info(
        "%-22s  :  %s  (admin: %.16s...)", "Token A", token_a.id, token_a.admin
    )
    account_log.info(
        "%-22s  :  %s  (admin: %.16s...)", "Token B", token_b.id, token_b.admin
    )
    account_log.info(
        "%-22s  :  %s%%", "Profit target", profit_target_pct or "—  (disabled)"
    )
    account_log.info("%-22s  :  %s%%", "Stop loss", stop_loss_pct or "—  (disabled)")
    account_log.info(
        "%-22s  :  %.1f – %.1f sec", "Hold interval", interval_min, interval_max
    )
    account_log.info(
        "%-22s  :  %.1f – %.1f sec",
        "Watch interval",
        watch_interval_min,
        watch_interval_max,
    )
    account_log.info("%-22s  :  %s", "Max net fee", max_network_fee)
    account_log.info("%-22s  :  %s", "Min position", min_position)
    account_log.info(
        "%-22s  :  %s  (%s %s  |  %s %s)",
        "Initial balances",
        "HOLDING" if holding else "WATCHING",
        init_bal_a,
        token_a.id,
        init_bal_b,
        token_b.id,
    )
    account_log.info("=" * 60)

    while True:
        cycle += 1

        # ── Step 1: consistent price probe (1 token_a -> token_b) ─────
        try:
            probe = await sdk.get_swap_quote(
                sell_amount=Decimal("1"),
                sell_instrument=token_a,
                buy_instrument=token_b,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            account_log.warning(
                "Price probe failed: %s  —  retrying after interval", exc
            )
            await asyncio.sleep(random.uniform(watch_interval_min, watch_interval_max))
            continue

        current_price = probe.pool_price_before_trade

        # ── Rebase entry_price on the first cycle when already holding ─
        if holding and entry_price is None:
            entry_price = current_price
            account_log.warning(
                "Existing %s position detected  —  rebasing entry price to "
                "current pool price: %s",
                token_a.id,
                entry_price,
            )
            account_log.warning(
                "Exit levels are now relative to the rebase price, "
                "not the original purchase price."
            )

        # ── Compute live exit levels for display ───────────────────────
        profit_level = (
            f"{entry_price * (1 + profit_target_pct / 100):.6f}"
            if profit_target_pct and entry_price
            else "—"
        )
        stop_level = (
            f"{entry_price * (1 - stop_loss_pct / 100):.6f}"
            if stop_loss_pct and entry_price
            else "—"
        )

        account_log.info(
            "─── Cycle %d  ·  Price: %s  ·  %s  "
            "·  Buys: %d  ·  Sells: %d  ·  P&L: %s %s",
            cycle,
            current_price,
            "HOLDING " if holding else "WATCHING",
            buy_count,
            sell_count,
            total_realized_pnl,
            token_b.id,
        )

        # ══════════════════════════════════════════════════════════════
        # Path A — WATCHING: spend entire token_b balance to enter
        # ══════════════════════════════════════════════════════════════
        if not holding:
            # Fetch live token_b balance
            try:
                info = await sdk.get_account_info()
                bal_b = info.get_balance(token_b)
            except (CantexAPIError, CantexTimeoutError) as exc:
                account_log.warning("Balance check failed: %s  —  retrying", exc)
                await asyncio.sleep(
                    random.uniform(watch_interval_min, watch_interval_max)
                )
                continue

            buy_amount = _quantize(bal_b, decimal_places)

            if buy_amount <= 0:
                account_log.warning(
                    "No %s balance to enter with  —  waiting",
                    token_b.id,
                )
                await asyncio.sleep(
                    random.uniform(watch_interval_min, watch_interval_max)
                )
                continue

            # Get buy-direction quote (token_b -> token_a) for fee check
            try:
                buy_quote = await sdk.get_swap_quote(
                    sell_amount=buy_amount,
                    sell_instrument=token_b,
                    buy_instrument=token_a,
                )
            except (CantexAPIError, CantexTimeoutError) as exc:
                account_log.warning("Buy quote failed: %s  —  retrying", exc)
                await asyncio.sleep(
                    random.uniform(watch_interval_min, watch_interval_max)
                )
                continue

            buy_fee = buy_quote.fees.network_fee.amount
            account_log.info(
                "Buy quote  |  spend: %s %s  ->  ~%s %s  |  fee: %s  |  slippage: %s",
                buy_amount,
                token_b.id,
                buy_quote.returned_amount,
                token_a.id,
                buy_fee,
                buy_quote.slippage,
            )

            if buy_fee >= max_network_fee:
                fee_skips += 1
                account_log.warning(
                    "Buy fee %s >= max %s  --  skipping entry  (fee skips: %d)",
                    buy_fee,
                    max_network_fee,
                    fee_skips,
                )
            else:
                account_log.info(
                    "Entering position #%d  --  spending %s %s  ->  %s",
                    buy_count + 1,
                    buy_amount,
                    token_b.id,
                    token_a.id,
                )
                try:
                    result = await sdk.swap(
                        sell_amount=buy_amount,
                        sell_instrument=token_b,
                        buy_instrument=token_a,
                    )
                    buy_count += 1
                    entry_price = current_price
                    entry_cost_b = buy_amount
                    holding = True
                    account_log.info("BUY #%d complete  ->  %s", buy_count, result)
                    account_log.info(
                        "Entry price: %s  |  Cost: %s %s  |  Target: %s  |  Stop: %s",
                        entry_price,
                        entry_cost_b,
                        token_b.id,
                        profit_level,
                        stop_level,
                    )
                except CantexAuthError as exc:
                    account_log.error(
                        "Auth error on BUY  (HTTP %d):  %s", exc.status, exc.body[:200]
                    )
                except (CantexAPIError, CantexTimeoutError) as exc:
                    account_log.error("BUY failed  :  %s", exc)

        # ================================================================
        # Path B -- HOLDING: watch price, exit on stop-loss or profit target
        # ================================================================
        else:
            sell_reason: str | None = None

            # 1. Stop-loss (highest urgency)
            if stop_loss_pct is not None and entry_price is not None:
                floor = entry_price * (1 - stop_loss_pct / Decimal("100"))
                if current_price <= floor:
                    sell_reason = (
                        f"stop-loss  (price {current_price} <= floor {floor:.6f}"
                        f"  [entry {entry_price} - {stop_loss_pct}%])"
                    )

            # 2. Profit target
            if (
                sell_reason is None
                and profit_target_pct is not None
                and entry_price is not None
            ):
                target = entry_price * (1 + profit_target_pct / Decimal("100"))
                if current_price >= target:
                    sell_reason = (
                        f"profit target  (price {current_price} >= target {target:.6f}"
                        f"  [entry {entry_price} + {profit_target_pct}%])"
                    )

            if sell_reason is None:
                account_log.info(
                    "Holding  --  price: %s  |  entry: %s  |  target: %s  |  stop: %s",
                    current_price,
                    entry_price,
                    profit_level,
                    stop_level,
                )
            else:
                account_log.info("SELL signal  --  %s", sell_reason)

                # Fetch live token_a balance to sell the full position
                try:
                    info = await sdk.get_account_info()
                    bal_a = info.get_balance(token_a)
                except (CantexAPIError, CantexTimeoutError) as exc:
                    account_log.warning(
                        "Balance check failed: %s  --  holding, retrying next cycle",
                        exc,
                    )
                    await asyncio.sleep(random.uniform(interval_min, interval_max))
                    continue

                if bal_a < min_position:
                    account_log.warning(
                        "%s balance too low to sell (%s)  --  resetting state",
                        token_a.id,
                        bal_a,
                    )
                    holding = False
                    entry_price = None
                    entry_cost_b = None
                else:
                    sell_amt = _quantize(bal_a, decimal_places)

                    try:
                        sell_quote = await sdk.get_swap_quote(
                            sell_amount=sell_amt,
                            sell_instrument=token_a,
                            buy_instrument=token_b,
                        )
                    except (CantexAPIError, CantexTimeoutError) as exc:
                        account_log.warning(
                            "Sell quote failed: %s  --  holding, retrying next cycle",
                            exc,
                        )
                        await asyncio.sleep(random.uniform(interval_min, interval_max))
                        continue

                    sell_fee = sell_quote.fees.network_fee.amount
                    account_log.info(
                        "Sell quote  |  sell: %s %s  ->  ~%s %s  |  fee: %s  |  slippage: %s",
                        sell_amt,
                        token_a.id,
                        sell_quote.returned_amount,
                        token_b.id,
                        sell_fee,
                        sell_quote.slippage,
                    )

                    if sell_fee >= max_network_fee:
                        fee_skips += 1
                        account_log.warning(
                            "Sell fee %s >= max %s  --  holding  (fee skips: %d)",
                            sell_fee,
                            max_network_fee,
                            fee_skips,
                        )
                    else:
                        account_log.info(
                            "Executing SELL #%d  --  %s %s  ->  %s",
                            sell_count + 1,
                            sell_amt,
                            token_a.id,
                            token_b.id,
                        )
                        try:
                            result = await sdk.swap(
                                sell_amount=sell_amt,
                                sell_instrument=token_a,
                                buy_instrument=token_b,
                            )
                            sell_count += 1

                            # P&L
                            received_b = sell_quote.returned_amount
                            if entry_cost_b is not None:
                                trade_pnl = received_b - entry_cost_b
                                total_realized_pnl += trade_pnl
                                sign = "+" if trade_pnl >= 0 else ""
                                account_log.info(
                                    "SELL #%d complete  ->  %s", sell_count, result
                                )
                                account_log.info(
                                    "Trade P&L: %s%s %s  |  Cumulative P&L: %s %s",
                                    sign,
                                    trade_pnl,
                                    token_b.id,
                                    total_realized_pnl,
                                    token_b.id,
                                )
                            else:
                                account_log.info(
                                    "SELL #%d complete  ->  %s  "
                                    "(P&L unknown -- no recorded entry cost)",
                                    sell_count,
                                    result,
                                )

                            # Reset state; next cycle re-enters immediately
                            holding = False
                            entry_price = None
                            entry_cost_b = None

                        except CantexAuthError as exc:
                            account_log.error(
                                "Auth error on SELL  (HTTP %d):  %s",
                                exc.status,
                                exc.body[:200],
                            )
                        except (CantexAPIError, CantexTimeoutError) as exc:
                            account_log.error("SELL failed  :  %s", exc)

        # Wait before next poll — use the appropriate interval for the current state.
        # HOLDING: short hold interval (price monitoring cadence).
        # WATCHING: longer watch interval (re-entry cooldown, avoids rate-limiting).
        if holding:
            wait = random.uniform(interval_min, interval_max)
        else:
            wait = random.uniform(watch_interval_min, watch_interval_max)
        account_log.info(
            "Next poll in %.1fs  (%s)",
            wait,
            "hold interval" if holding else "watch interval",
        )
        await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Multi-account support
# ---------------------------------------------------------------------------


def discover_accounts() -> list[tuple[str, str]]:
    """
    Scan the ``accounts/`` directory and return [(name, abs_path), ...] for
    every immediate sub-folder found, sorted alphabetically by name.

    Returns an empty list when the directory does not exist, so callers can
    fall back to single-account mode transparently.
    """
    if not os.path.isdir(ACCOUNTS_DIR):
        return []
    entries = []
    for entry in os.scandir(ACCOUNTS_DIR):
        if entry.is_dir():
            entries.append((entry.name, os.path.abspath(entry.path)))
    entries.sort(key=lambda t: t[0])
    return entries


async def run_account(account_name: str, account_dir: str) -> None:
    """
    Bootstrap and run the full bot loop for a single account.

    Credentials are loaded from ``<account_dir>/.env`` using
    ``dotenv_values()`` (returns a plain dict, never touches ``os.environ``)
    so multiple accounts running concurrently cannot overwrite each other's
    keys.

    Strategy config is loaded from ``<account_dir>/config.json``.

    The ``api_key_path`` in config.json is resolved relative to
    ``account_dir`` when it is not an absolute path.
    """
    account_log = logging.getLogger(f"cantex_bot.{account_name}")

    # --- credentials -------------------------------------------------------
    env_path = os.path.join(account_dir, ".env")
    env = dotenv_values(env_path)  # dict, no os.environ side-effects

    operator_hex = env.get("CANTEX_OPERATOR_KEY") or os.environ.get(
        "CANTEX_OPERATOR_KEY"
    )
    trading_hex = env.get("CANTEX_TRADING_KEY") or os.environ.get("CANTEX_TRADING_KEY")
    base_url = (
        env.get("CANTEX_BASE_URL")
        or os.environ.get("CANTEX_BASE_URL")
        or "https://api.testnet.cantex.io"
    )

    if not operator_hex:
        account_log.error(
            "CANTEX_OPERATOR_KEY is not set  (checked %s and env)", env_path
        )
        return
    if not trading_hex:
        account_log.error(
            "CANTEX_TRADING_KEY is not set  (checked %s and env)", env_path
        )
        return

    # --- config ------------------------------------------------------------
    config_path = os.path.join(account_dir, "config.json")
    cfg = load_config(config_path)

    # Resolve api_key_path relative to account_dir when not absolute
    raw_key_path = cfg.get("api_key_path", "secrets/api_key.txt")
    api_key_path = (
        raw_key_path
        if os.path.isabs(raw_key_path)
        else os.path.join(account_dir, raw_key_path)
    )

    # --- run ---------------------------------------------------------------
    operator = OperatorKeySigner.from_hex(operator_hex)
    trading = IntentTradingKeySigner.from_hex(trading_hex)

    async with CantexSDK(
        operator,
        trading,
        base_url=base_url,
        api_key_path=api_key_path,
    ) as sdk:
        account_log.info("Connecting to Cantex API  (%s)", base_url)
        try:
            api_key = await sdk.authenticate()
        except (CantexAuthError, CantexAPIError, CantexTimeoutError) as exc:
            account_log.error("Authentication failed  :  %s", exc)
            return

        account_log.info("Authenticated  --  key: %s...", api_key[:8])

        strategy = cfg.get("strategy", "swap")
        account_log.info("Strategy  :  %s", strategy)

        try:
            if strategy == "swap":
                await run_swap_loop(sdk, cfg, account_log=account_log)
            elif strategy == "scalp":
                await run_scalp_loop(sdk, cfg, account_log=account_log)
        except Exception as exc:  # noqa: BLE001
            account_log.error(
                "Unhandled error in strategy loop  :  %s", exc, exc_info=True
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    accounts = discover_accounts()

    if accounts:
        # ── Multi-account mode ─────────────────────────────────────────
        log.info("Multi-account mode  --  %d account(s) found:", len(accounts))
        for name, path in accounts:
            log.info("  %-20s  ->  %s", name, path)

        results = await asyncio.gather(
            *[run_account(name, path) for name, path in accounts],
            return_exceptions=True,
        )
        for (name, _), result in zip(accounts, results):
            if isinstance(result, BaseException):
                log.error(
                    "Account '%s' raised an unhandled exception: %s", name, result
                )
    else:
        # ── Single-account mode (backward-compatible) ──────────────────
        log.info("No accounts/ directory found  --  running in single-account mode")
        load_dotenv()

        operator_hex = os.environ.get("CANTEX_OPERATOR_KEY")
        if not operator_hex:
            log.error("Environment variable 'CANTEX_OPERATOR_KEY' is not set")
            sys.exit(1)

        trading_hex = os.environ.get("CANTEX_TRADING_KEY")
        if not trading_hex:
            log.error("Environment variable 'CANTEX_TRADING_KEY' is not set")
            sys.exit(1)

        operator = OperatorKeySigner.from_hex(operator_hex)
        trading = IntentTradingKeySigner.from_hex(trading_hex)
        base_url = os.environ.get("CANTEX_BASE_URL", "https://api.testnet.cantex.io")

        cfg = load_config()

        raw_key_path = cfg.get("api_key_path", "secrets/api_key.txt")
        async with CantexSDK(
            operator,
            trading,
            base_url=base_url,
            api_key_path=raw_key_path,
        ) as sdk:
            log.info("Connecting to Cantex API  (%s)", base_url)
            api_key = await sdk.authenticate()
            log.info("Authenticated  --  key: %s...", api_key[:8])

            strategy = cfg.get("strategy", "swap")
            log.info("Strategy  :  %s", strategy)

            if strategy == "swap":
                await run_swap_loop(sdk, cfg)
            elif strategy == "scalp":
                await run_scalp_loop(sdk, cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
