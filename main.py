# Copyright (c) 2026 CaviarNine
# SPDX-License-Identifier: MIT OR Apache-2.0

"""
Cantex SDK -- Swap / Scalp Bot

Reads configuration from config.json and credentials from environment
variables (or a .env file).  Set ``"strategy"`` in config.json to pick
which mode to run:

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
  - BUY  when price <= buy_price_threshold and state is WATCHING.
  - SELL when state is HOLDING and any exit condition fires (checked in order):
        1. stop-loss      price <= entry * (1 - stop_loss_pct    / 100)
        2. profit target  price >= entry * (1 + profit_target_pct / 100)
        3. fixed ceiling  price >= sell_price_threshold
     At least one of sell_price_threshold or profit_target_pct must be set.
  - Price metric: pool_price_before_trade from a token_a -> token_b quote,
    i.e. "how many token_b per one token_a".
  - On startup a non-zero token_a balance is treated as an existing position
    (entry price unknown) so the bot survives restarts cleanly.
  - Tracks and logs realised P&L per trade and cumulatively in token_b.

────────────────────────────────────────────────────────────────────────────
Required environment variables (or entries in a .env file):

    CANTEX_OPERATOR_KEY   Ed25519 private key hex
    CANTEX_TRADING_KEY    secp256k1 private key hex

Optional:

    CANTEX_BASE_URL       API base URL (default: https://api.testnet.cantex.io)

Edit config.json to configure instruments, amounts, thresholds, intervals,
and the fee threshold, then run:

    python main.py
"""

import asyncio
import json
import logging
import os
import random
import sys
from decimal import ROUND_DOWN, Decimal

from dotenv import load_dotenv

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
    """Colorized, timestamp-free log formatter."""

    _RST = "\033[0m"

    _STYLES: dict[int, tuple[str, str]] = {
        logging.DEBUG: ("\033[90m", "DEBUG"),
        logging.INFO: ("\033[96m", "INFO "),
        logging.WARNING: ("\033[93m", "WARN "),
        logging.ERROR: ("\033[91m", "ERROR"),
        logging.CRITICAL: ("\033[1;91m", "CRIT "),
    }

    def format(self, record: logging.LogRecord) -> str:
        lc, label = self._STYLES.get(record.levelno, ("\033[97m", record.levelname[:5]))
        return f"{lc}[{label}]{self._RST}  {record.getMessage()}"


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
_VALID_STRATEGIES = ("swap", "scalp")


def _require_fields(
    section: dict,
    prefix: str,
    fields: tuple[str, ...],
    path: str,
) -> None:
    """Exit with a clear message if any of *fields* are absent from *section*."""
    for field in fields:
        if field not in section:
            log.error("Config missing field  %s.%s  (%s)", prefix, field, path)
            sys.exit(1)


def _require_token_fields(section: dict, prefix: str, path: str) -> None:
    """Validate that token_a / token_b are present and non-empty strings."""
    for key in ("token_a", "token_b"):
        if key not in section:
            log.error("Config missing field  %s.%s  (%s)", prefix, key, path)
            sys.exit(1)
        if not isinstance(section[key], str) or not section[key].strip():
            log.error(
                'Config  %s.%s  must be a non-empty string  e.g. "CC"', prefix, key
            )
            sys.exit(1)


def _validate_swap_config(swap: dict, path: str) -> None:
    _require_token_fields(swap, "swap", path)
    _require_fields(
        swap,
        "swap",
        (
            "amount_min",
            "interval_min_minutes",
            "interval_max_minutes",
            "max_network_fee",
        ),
        path,
    )
    iv_min = float(swap["interval_min_minutes"])
    iv_max = float(swap["interval_max_minutes"])
    if iv_min > iv_max:
        log.error(
            "swap.interval_min_minutes (%s) must be <= interval_max_minutes (%s)",
            iv_min,
            iv_max,
        )
        sys.exit(1)


def _validate_scalp_config(scalp: dict, path: str) -> None:
    _require_token_fields(scalp, "scalp", path)
    _require_fields(
        scalp,
        "scalp",
        (
            "max_network_fee",
            "interval_min_seconds",
            "interval_max_seconds",
        ),
        path,
    )

    iv_min = float(scalp["interval_min_seconds"])
    iv_max = float(scalp["interval_max_seconds"])
    if iv_min > iv_max:
        log.error(
            "scalp.interval_min_seconds (%s) must be <= interval_max_seconds (%s)",
            iv_min,
            iv_max,
        )
        sys.exit(1)

    has_profit_target = Decimal(str(scalp.get("profit_target_pct", "0"))) > 0
    has_stop_loss = Decimal(str(scalp.get("stop_loss_pct", "0"))) > 0
    if not has_profit_target and not has_stop_loss:
        log.error(
            "scalp config requires at least one of: "
            "'profit_target_pct' (non-zero) or 'stop_loss_pct' (non-zero)"
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
) -> tuple[InstrumentId, InstrumentId]:
    """
    Fetch real ``InstrumentId`` objects from the live API by matching token
    symbols / IDs (case-insensitive) against the account's token list.

    Exits the process with a clear error if either token is not found.
    """
    log.info("Resolving instruments from account...")
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
            log.error("Token not found  :  '%s'", symbol)
            log.error("Available tokens :  %s", available)
            sys.exit(1)
        result[symbol.lower()] = match

    token_a = result[token_a_symbol.lower()]
    token_b = result[token_b_symbol.lower()]
    log.info("%-16s  ->  admin: %.20s...", token_a.id, token_a.admin)
    log.info("%-16s  ->  admin: %.20s...", token_b.id, token_b.admin)
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

    log.info("Balances")
    log.info("%-16s  :  %s", token_a.id, bal_a)
    log.info("%-16s  :  %s", token_b.id, bal_b)
    log.info("%-16s  :  %s", "Min required", amount_min)

    if bal_a >= amount_min:
        amount = random_amount(amount_min, bal_a, decimal_places)
        log.info("Selling  %s  ->  %s   amount: %s", token_a.id, token_b.id, amount)
        return token_a, token_b, amount

    log.warning(
        "%s balance (%s) is below minimum (%s)  —  trying reverse swap",
        token_a.id,
        bal_a,
        amount_min,
    )

    if bal_b >= amount_min:
        amount = random_amount(amount_min, bal_b, decimal_places)
        log.info(
            "Selling  %s  ->  %s   amount: %s  [reversed]",
            token_b.id,
            token_a.id,
            amount,
        )
        return token_b, token_a, amount

    log.error("Insufficient balance in both tokens  —  skipping cycle")
    log.error(
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


async def run_swap_loop(sdk: CantexSDK, cfg: dict) -> None:
    swap_cfg = cfg["swap"]

    token_a, token_b = await resolve_instruments(
        sdk,
        swap_cfg["token_a"],
        swap_cfg["token_b"],
    )

    amount_min = Decimal(str(swap_cfg["amount_min"]))
    decimal_places = int(swap_cfg.get("amount_decimal_places", 6))
    interval_min = float(swap_cfg["interval_min_minutes"])
    interval_max = float(swap_cfg["interval_max_minutes"])
    max_network_fee = Decimal(str(swap_cfg["max_network_fee"]))

    log.info("=" * 60)
    log.info("Cantex Swap Bot  —  Ready")
    log.info("%-14s  :  %s  (admin: %.16s...)", "Token A", token_a.id, token_a.admin)
    log.info("%-14s  :  %s  (admin: %.16s...)", "Token B", token_b.id, token_b.admin)
    log.info(
        "%-14s  :  %s  (%d decimal places)", "Min amount", amount_min, decimal_places
    )
    log.info("%-14s  :  %.1f – %.1f min", "Interval", interval_min, interval_max)
    log.info("%-14s  :  %s", "Max net fee", max_network_fee)
    log.info("=" * 60)

    swap_count = 0
    fee_skips = 0
    bal_skips = 0
    cycle = 0

    while True:
        cycle += 1

        log.info(
            "─── Cycle %d   Swaps: %d  ·  Fee Skips: %d  ·  Bal Skips: %d",
            cycle,
            swap_count,
            fee_skips,
            bal_skips,
        )

        # ── Step 1: resolve direction and amount from live balances ────
        try:
            direction = await resolve_direction(
                sdk, token_a, token_b, amount_min, decimal_places
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.warning("Could not fetch account info: %s  —  skipping cycle", exc)
            direction = None

        if direction is None:
            bal_skips += 1
            wait = random.uniform(interval_min, interval_max)
            log.info("Retrying in %.1f min", wait)
            await asyncio.sleep(wait * 60)
            continue

        sell_inst, buy_inst, actual_amount = direction

        # ── Step 2: get quote and apply fee guard ──────────────────────
        log.info("Fetching swap quote...")
        try:
            quote = await sdk.get_swap_quote(
                sell_amount=actual_amount,
                sell_instrument=sell_inst,
                buy_instrument=buy_inst,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.warning("Quote request failed: %s  —  skipping cycle", exc)
            wait = random.uniform(interval_min, interval_max)
            await asyncio.sleep(wait * 60)
            continue

        network_fee = quote.fees.network_fee.amount
        log.info("Quote")
        log.info("%-12s  :  %s %s", "Sell", actual_amount, sell_inst.id)
        log.info("%-12s  :  %s %s", "Receive", quote.returned_amount, buy_inst.id)
        log.info("%-12s  :  %s", "Network fee", network_fee)
        log.info("%-12s  :  %s%%", "Fee", quote.fees.fee_percentage)
        log.info("%-12s  :  %s", "Slippage", quote.slippage)

        if network_fee >= max_network_fee:
            fee_skips += 1
            log.warning(
                "Network fee %s >= threshold %s  —  swap skipped  (total fee skips: %d)",
                network_fee,
                max_network_fee,
                fee_skips,
            )
            wait = random.uniform(interval_min, interval_max)
            log.info("Retrying in %.1f min", wait)
            await asyncio.sleep(wait * 60)
            continue

        # ── Step 3: execute swap ───────────────────────────────────────
        log.info(
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
            log.info("Swap #%d complete  ->  %s", swap_count, result)
        except CantexAuthError as exc:
            log.error(
                "Auth error during swap  (HTTP %d):  %s", exc.status, exc.body[:200]
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.error("Swap failed  :  %s", exc)

        # ── Step 4: wait random interval ──────────────────────────────
        wait = random.uniform(interval_min, interval_max)
        log.info(
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


async def run_scalp_loop(sdk: CantexSDK, cfg: dict) -> None:
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
    )

    decimal_places = int(scalp_cfg.get("amount_decimal_places", 6))
    max_network_fee = Decimal(str(scalp_cfg["max_network_fee"]))
    interval_min = float(scalp_cfg["interval_min_seconds"])
    interval_max = float(scalp_cfg["interval_max_seconds"])
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
    log.info("Checking initial balances for position state...")
    try:
        init_info = await sdk.get_account_info()
        init_bal_a = init_info.get_balance(token_a)
        init_bal_b = init_info.get_balance(token_b)
    except (CantexAPIError, CantexTimeoutError) as exc:
        log.error("Failed to fetch initial account info: %s", exc)
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
    log.info("=" * 60)
    log.info("Cantex Scalp Bot  —  Ready")
    log.info("%-22s  :  %s  (admin: %.16s...)", "Token A", token_a.id, token_a.admin)
    log.info("%-22s  :  %s  (admin: %.16s...)", "Token B", token_b.id, token_b.admin)
    log.info("%-22s  :  %s%%", "Profit target", profit_target_pct or "—  (disabled)")
    log.info("%-22s  :  %s%%", "Stop loss", stop_loss_pct or "—  (disabled)")
    log.info("%-22s  :  %.1f – %.1f sec", "Interval", interval_min, interval_max)
    log.info("%-22s  :  %s", "Max net fee", max_network_fee)
    log.info("%-22s  :  %s", "Min position", min_position)
    log.info(
        "%-22s  :  %s  (%s %s  |  %s %s)",
        "Initial balances",
        "HOLDING" if holding else "WATCHING",
        init_bal_a,
        token_a.id,
        init_bal_b,
        token_b.id,
    )
    log.info("=" * 60)

    while True:
        cycle += 1

        # ── Step 1: consistent price probe (1 token_a -> token_b) ─────
        # Using a fixed direction and amount every cycle means entry_price
        # and current_price are always in the same units (token_b per token_a)
        # and can be compared directly.
        try:
            probe = await sdk.get_swap_quote(
                sell_amount=Decimal("1"),
                sell_instrument=token_a,
                buy_instrument=token_b,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.warning("Price probe failed: %s  —  retrying after interval", exc)
            await asyncio.sleep(random.uniform(interval_min, interval_max))
            continue

        current_price = probe.pool_price_before_trade

        # ── Rebase entry_price on the first cycle when already holding ─
        if holding and entry_price is None:
            entry_price = current_price
            log.warning(
                "Existing %s position detected  —  rebasing entry price to "
                "current pool price: %s",
                token_a.id,
                entry_price,
            )
            log.warning(
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

        log.info(
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
                log.warning("Balance check failed: %s  —  retrying", exc)
                await asyncio.sleep(random.uniform(interval_min, interval_max))
                continue

            buy_amount = _quantize(bal_b, decimal_places)

            if buy_amount <= 0:
                log.warning(
                    "No %s balance to enter with  —  waiting",
                    token_b.id,
                )
                await asyncio.sleep(random.uniform(interval_min, interval_max))
                continue

            # Get buy-direction quote (token_b -> token_a) for fee check
            try:
                buy_quote = await sdk.get_swap_quote(
                    sell_amount=buy_amount,
                    sell_instrument=token_b,
                    buy_instrument=token_a,
                )
            except (CantexAPIError, CantexTimeoutError) as exc:
                log.warning("Buy quote failed: %s  —  retrying", exc)
                await asyncio.sleep(random.uniform(interval_min, interval_max))
                continue

            buy_fee = buy_quote.fees.network_fee.amount
            log.info(
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
                log.warning(
                    "Buy fee %s >= max %s  —  skipping entry  (fee skips: %d)",
                    buy_fee,
                    max_network_fee,
                    fee_skips,
                )
            else:
                log.info(
                    "Entering position #%d  —  spending %s %s  ->  %s",
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
                    log.info("BUY #%d complete  ->  %s", buy_count, result)
                    log.info(
                        "Entry price: %s  |  Cost: %s %s  "
                        "|  Profit target: %s  |  Stop loss: %s",
                        entry_price,
                        entry_cost_b,
                        token_b.id,
                        profit_level,
                        stop_level,
                    )
                except CantexAuthError as exc:
                    log.error(
                        "Auth error on BUY  (HTTP %d):  %s",
                        exc.status,
                        exc.body[:200],
                    )
                except (CantexAPIError, CantexTimeoutError) as exc:
                    log.error("BUY failed  :  %s", exc)

        # ══════════════════════════════════════════════════════════════
        # Path B — HOLDING: watch price, exit on profit target or stop-loss
        # ══════════════════════════════════════════════════════════════
        else:
            sell_reason: str | None = None

            # 1. Stop-loss — checked first (highest urgency)
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
                log.info(
                    "Holding  —  price: %s  |  entry: %s  |  target: %s  |  stop: %s",
                    current_price,
                    entry_price,
                    profit_level,
                    stop_level,
                )
            else:
                log.info("SELL signal  —  %s", sell_reason)

                # Fetch live token_a balance to sell the full position
                try:
                    info = await sdk.get_account_info()
                    bal_a = info.get_balance(token_a)
                except (CantexAPIError, CantexTimeoutError) as exc:
                    log.warning(
                        "Balance check failed: %s  —  holding, retrying next cycle",
                        exc,
                    )
                    await asyncio.sleep(random.uniform(interval_min, interval_max))
                    continue

                if bal_a < min_position:
                    log.warning(
                        "%s balance too low to sell (%s)  —  resetting state",
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
                        log.warning(
                            "Sell quote failed: %s  —  holding, retrying next cycle",
                            exc,
                        )
                        await asyncio.sleep(random.uniform(interval_min, interval_max))
                        continue

                    sell_fee = sell_quote.fees.network_fee.amount
                    log.info(
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
                        log.warning(
                            "Sell fee %s >= max %s  —  holding  (fee skips: %d)",
                            sell_fee,
                            max_network_fee,
                            fee_skips,
                        )
                    else:
                        log.info(
                            "Executing SELL #%d  —  %s %s  ->  %s",
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

                            # ── P&L ────────────────────────────────────
                            received_b = sell_quote.returned_amount
                            if entry_cost_b is not None:
                                trade_pnl = received_b - entry_cost_b
                                total_realized_pnl += trade_pnl
                                sign = "+" if trade_pnl >= 0 else ""
                                log.info(
                                    "SELL #%d complete  ->  %s", sell_count, result
                                )
                                log.info(
                                    "Trade P&L: %s%s %s  |  Cumulative P&L: %s %s",
                                    sign,
                                    trade_pnl,
                                    token_b.id,
                                    total_realized_pnl,
                                    token_b.id,
                                )
                            else:
                                log.info(
                                    "SELL #%d complete  ->  %s  "
                                    "(P&L unknown — no recorded entry cost)",
                                    sell_count,
                                    result,
                                )

                            # Reset state; next cycle will immediately re-enter
                            holding = False
                            entry_price = None
                            entry_cost_b = None

                        except CantexAuthError as exc:
                            log.error(
                                "Auth error on SELL  (HTTP %d):  %s",
                                exc.status,
                                exc.body[:200],
                            )
                        except (CantexAPIError, CantexTimeoutError) as exc:
                            log.error("SELL failed  :  %s", exc)

        # ── Wait before next poll ──────────────────────────────────────
        wait = random.uniform(interval_min, interval_max)
        log.info("Next poll in %.1fs", wait)
        await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    load_dotenv()

    cfg = load_config()

    # Keys from environment (populated by .env or the shell)
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

    async with CantexSDK(
        operator,
        trading,
        base_url=base_url,
        api_key_path=cfg.get("api_key_path", "secrets/api_key.txt"),
    ) as sdk:
        log.info("Connecting to Cantex API  (%s)", base_url)
        api_key = await sdk.authenticate()
        log.info("Authenticated  —  key: %s...", api_key[:8])

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
