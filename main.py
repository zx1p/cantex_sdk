# Copyright (c) 2026 CaviarNine
# SPDX-License-Identifier: MIT OR Apache-2.0

"""
Cantex SDK -- Swap Bot

Reads swap configuration from config.json and credentials from environment
variables (or a .env file). Runs an automated swap loop:

  - Resolves live InstrumentId objects for token_a and token_b at startup.
  - Checks live balances each cycle: sells token_a if it has enough, otherwise
    falls back to selling token_b (reverse swap).
  - Picks a random sell amount between amount_min and the available balance.
  - Fetches a quote and skips the swap if the network fee >= max_network_fee.
  - Waits a random interval (interval_min_minutes – interval_max_minutes)
    between every cycle, whether the swap ran or was skipped.

Required environment variables (or entries in a .env file):

    CANTEX_OPERATOR_KEY   Ed25519 private key hex
    CANTEX_TRADING_KEY    secp256k1 private key hex

Optional:

    CANTEX_BASE_URL       API base URL (default: https://api.testnet.cantex.io)

Edit config.json to configure instruments, amounts, intervals, and the fee
threshold, then run:

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

    # (level-tag color, label text)
    _STYLES: dict[int, tuple[str, str]] = {
        logging.DEBUG: ("\033[90m", "DEBUG"),
        logging.INFO: ("\033[96m", "INFO"),
        logging.WARNING: ("\033[93m", "WARN"),
        logging.ERROR: ("\033[91m", "ERROR"),
        logging.CRITICAL: ("\033[1;91m", "CRIT"),
    }

    # message body color per level
    _MSG_COLOR: dict[int, str] = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[97m",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[1;91m",
    }

    def format(self, record: logging.LogRecord) -> str:
        lc, label = self._STYLES.get(record.levelno, ("\033[97m", record.levelname[:5]))
        mc = self._MSG_COLOR.get(record.levelno, "")
        return f"{lc}[{label}]{self._RST}  {mc}{record.getMessage()}{self._RST}"


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
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = "config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load and return the bot configuration from *path*."""
    if not os.path.exists(path):
        log.error("Config file not found  :  %s", path)
        sys.exit(1)

    with open(path, "r") as fh:
        cfg = json.load(fh)

    if "swap" not in cfg:
        log.error("Missing required key 'swap' in config  (%s)", path)
        sys.exit(1)

    swap = cfg["swap"]
    for key in ("token_a", "token_b"):
        if key not in swap:
            log.error("Config missing field  swap.%s  (%s)", key, path)
            sys.exit(1)
        if not isinstance(swap[key], str) or not swap[key].strip():
            log.error('swap.%s must be a non-empty string  e.g. "CC"', key)
            sys.exit(1)

    for field in (
        "amount_min",
        "interval_min_minutes",
        "interval_max_minutes",
        "max_network_fee",
    ):
        if field not in swap:
            log.error("Config missing field  swap.%s  (%s)", field, path)
            sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# Swap helpers
# ---------------------------------------------------------------------------


def random_amount(
    amount_min: Decimal,
    amount_max: Decimal,
    decimal_places: int,
) -> Decimal:
    """Return a uniformly random ``Decimal`` in [amount_min, amount_max]."""
    raw = random.uniform(float(amount_min), float(amount_max))
    step = Decimal(10) ** -decimal_places
    return Decimal(str(raw)).quantize(step, rounding=ROUND_DOWN)


async def resolve_instruments(
    sdk: CantexSDK,
    token_a_symbol: str,
    token_b_symbol: str,
) -> tuple[InstrumentId, InstrumentId]:
    """
    Fetch real ``InstrumentId`` objects from the live API by matching token
    symbols/IDs (case-insensitive) against the account's token list.

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
      1. token_a → token_b  when token_a balance >= amount_min
      2. token_b → token_a  when token_b balance >= amount_min  (fallback)
      3. ``None``            when neither balance meets amount_min → skip cycle.
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
# Main swap loop
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

    if interval_min > interval_max:
        log.error(
            "interval_min_minutes (%s) must be <= interval_max_minutes (%s)",
            interval_min,
            interval_max,
        )
        sys.exit(1)

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

        await run_swap_loop(sdk, cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
