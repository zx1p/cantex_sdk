# Copyright (c) 2026 CaviarNine
# SPDX-License-Identifier: MIT OR Apache-2.0

"""
Cantex SDK -- Swap Bot

Reads all configuration from config.json and runs an automated swap loop:

  - Picks a random sell amount between amount_min and amount_max.
  - Checks live balances: sells token_a (CC) if it has enough, otherwise
    falls back to selling token_b (USDCx) and buying token_a instead.
  - Fetches a quote and skips the swap if the network fee >= max_network_fee.
  - Waits a random interval (interval_min_minutes – interval_max_minutes)
    between every cycle, whether the swap ran or was skipped.

Edit config.json to configure keys, instruments, amounts, intervals and the
fee threshold before running:

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("cantex_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = "config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    """Load and return the bot configuration from *path*."""
    if not os.path.exists(path):
        log.error("Configuration file not found: %s", path)
        sys.exit(1)
    with open(path, "r") as fh:
        cfg = json.load(fh)

    # Basic sanity checks so we fail early with a clear message.
    for top_key in ("operator_key", "trading_key", "swap"):
        if top_key not in cfg:
            log.error("Missing required top-level key '%s' in %s", top_key, path)
            sys.exit(1)

    swap = cfg["swap"]
    for key in ("token_a", "token_b"):
        if key not in swap:
            log.error("swap.%s is missing from %s", key, path)
            sys.exit(1)
        if not isinstance(swap[key], str) or not swap[key].strip():
            log.error('swap.%s must be a non-empty string (e.g. "CC")', key)
            sys.exit(1)

    for field in (
        "amount_min",
        "interval_min_minutes",
        "interval_max_minutes",
        "max_network_fee",
    ):
        if field not in swap:
            log.error("swap.%s is missing from %s", field, path)
            sys.exit(1)

    return cfg


# ---------------------------------------------------------------------------
# Signer helpers
# ---------------------------------------------------------------------------


def _build_signer(key_cfg: dict, cls, label: str):
    """
    Build a signer from a config block::

        {"source": "env",  "env_var":   "CANTEX_OPERATOR_KEY"}
        {"source": "file", "file_path": "secrets/operator.key", "file_type": "hex"}
    """
    source = key_cfg.get("source", "env")

    if source == "env":
        env_var = key_cfg.get("env_var", f"CANTEX_{label.upper()}_KEY")
        value = os.environ.get(env_var)
        if not value:
            log.error("Environment variable '%s' is not set", env_var)
            sys.exit(1)
        return cls.from_hex(value)

    if source == "file":
        file_path = key_cfg.get("file_path")
        if not file_path:
            log.error("%s key: 'file_path' is required when source='file'", label)
            sys.exit(1)
        file_type = key_cfg.get("file_type", "hex")
        return cls.from_file(file_path, key_type=file_type)

    log.error("%s key: unknown source '%s' (expected 'env' or 'file')", label, source)
    sys.exit(1)


def build_operator_signer(cfg: dict) -> OperatorKeySigner:
    return _build_signer(cfg["operator_key"], OperatorKeySigner, "OPERATOR")  # type: ignore[return-value]


def build_trading_signer(cfg: dict) -> IntentTradingKeySigner:
    return _build_signer(cfg["trading_key"], IntentTradingKeySigner, "TRADING")  # type: ignore[return-value]


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
    info = await sdk.get_account_info()

    mapping: dict[str, InstrumentId] = {}
    for tok in info.tokens:
        # Match against both the instrument id and the display symbol.
        mapping[tok.instrument.id.lower()] = tok.instrument
        if tok.instrument_symbol:
            mapping[tok.instrument_symbol.lower()] = tok.instrument

    result: dict[str, InstrumentId] = {}
    for symbol in (token_a_symbol, token_b_symbol):
        match = mapping.get(symbol.lower())
        if match is None:
            available = [(t.instrument.id, t.instrument_symbol) for t in info.tokens]
            log.error(
                "Token '%s' not found in account. Available: %s",
                symbol,
                available,
            )
            sys.exit(1)
        result[symbol.lower()] = match

    token_a = result[token_a_symbol.lower()]
    token_b = result[token_b_symbol.lower()]
    log.info(
        "Resolved  %s → admin=%.32s...",
        token_a.id,
        token_a.admin,
    )
    log.info(
        "Resolved  %s → admin=%.32s...",
        token_b.id,
        token_b.admin,
    )
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

    ``token_a`` and ``token_b`` must already be real ``InstrumentId`` objects
    resolved at startup via ``resolve_instruments``.

    Priority:
      1. token_a → token_b  when token_a balance >= amount_min
      2. token_b → token_a  when token_b balance >= amount_min  (fallback)
      3. ``None``            when neither balance meets amount_min → skip cycle.
    """
    info = await sdk.get_account_info()
    bal_a = info.get_balance(token_a)
    bal_b = info.get_balance(token_b)

    log.info(
        "Balances  %s=%s  |  %s=%s  |  min=%s",
        token_a.id,
        bal_a,
        token_b.id,
        bal_b,
        amount_min,
    )

    if bal_a >= amount_min:
        amount = random_amount(amount_min, bal_a, decimal_places)
        log.info("Direction: %s → %s  (amount=%s)", token_a.id, token_b.id, amount)
        return token_a, token_b, amount

    log.warning(
        "%s balance %s is below minimum %s — checking %s for reverse swap",
        token_a.id,
        bal_a,
        amount_min,
        token_b.id,
    )

    if bal_b >= amount_min:
        amount = random_amount(amount_min, bal_b, decimal_places)
        log.info(
            "Direction (reversed): %s → %s  (amount=%s)",
            token_b.id,
            token_a.id,
            amount,
        )
        return token_b, token_a, amount

    log.error(
        "Insufficient balance in both tokens  %s=%s  %s=%s  (minimum %s) — skipping cycle",
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

    log.info("=" * 64)
    log.info("Cantex Swap Bot started")
    log.info("  Token A         : %s", token_a)
    log.info("  Token B         : %s", token_b)
    log.info(
        "  Amount min      : %s  (decimal_places=%d)",
        amount_min,
        decimal_places,
    )
    log.info("  Interval range  : %.1f – %.1f min", interval_min, interval_max)
    log.info("  Max network fee : %s", max_network_fee)
    log.info("=" * 64)

    swap_count = 0
    fee_skips = 0
    bal_skips = 0
    cycle = 0

    while True:
        cycle += 1

        log.info("─" * 64)
        log.info(
            "[Cycle %d]  (swaps=%d  fee_skips=%d  bal_skips=%d)",
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
            log.warning("Could not fetch account info: %s — skipping cycle", exc)
            direction = None

        if direction is None:
            bal_skips += 1
            wait = random.uniform(interval_min, interval_max)
            log.info("Waiting %.1f min before next attempt", wait)
            await asyncio.sleep(wait * 60)
            continue

        sell_inst, buy_inst, actual_amount = direction

        # ── Step 2: get quote and apply fee guard ──────────────────────
        try:
            quote = await sdk.get_swap_quote(
                sell_amount=actual_amount,
                sell_instrument=sell_inst,
                buy_instrument=buy_inst,
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.warning("Quote request failed: %s — skipping cycle", exc)
            wait = random.uniform(interval_min, interval_max)
            await asyncio.sleep(wait * 60)
            continue

        network_fee = quote.fees.network_fee.amount
        log.info(
            "Quote  %s %s → %s %s  |  network_fee=%s  fee_pct=%s%%  slippage=%s",
            actual_amount,
            sell_inst.id,
            quote.returned_amount,
            buy_inst.id,
            network_fee,
            quote.fees.fee_percentage,
            quote.slippage,
        )

        if network_fee >= max_network_fee:
            fee_skips += 1
            log.warning(
                "Network fee %s >= threshold %s — SKIPPING swap  (fee_skips so far: %d)",
                network_fee,
                max_network_fee,
                fee_skips,
            )
            wait = random.uniform(interval_min, interval_max)
            log.info("Waiting %.1f min before next attempt", wait)
            await asyncio.sleep(wait * 60)
            continue

        # ── Step 3: execute swap ───────────────────────────────────────
        log.info(
            "Executing swap: %s %s → %s  (fee %s < threshold %s)",
            actual_amount,
            sell_inst.id,
            buy_inst.id,
            network_fee,
            max_network_fee,
        )
        try:
            result = await sdk.swap(
                sell_amount=actual_amount,
                sell_instrument=sell_inst,
                buy_instrument=buy_inst,
            )
            swap_count += 1
            log.info("Swap #%d completed: %s", swap_count, result)
        except CantexAuthError as exc:
            log.error(
                "Authentication error during swap (HTTP %d): %s",
                exc.status,
                exc.body[:200],
            )
        except (CantexAPIError, CantexTimeoutError) as exc:
            log.error("Swap execution failed: %s", exc)

        # ── Step 4: wait random interval ──────────────────────────────
        wait = random.uniform(interval_min, interval_max)
        log.info(
            "Next cycle in %.1f min  (swaps=%d  fee_skips=%d  bal_skips=%d)",
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

    operator = build_operator_signer(cfg)
    trading = build_trading_signer(cfg)

    # CANTEX_BASE_URL env var takes priority over config.json
    base_url = os.environ.get("CANTEX_BASE_URL") or cfg.get(
        "base_url", "https://api.testnet.cantex.io"
    )

    async with CantexSDK(
        operator,
        trading,
        base_url=base_url,
        api_key_path=cfg.get("api_key_path", "secrets/api_key.txt"),
    ) as sdk:
        log.info("Authenticating...")
        api_key = await sdk.authenticate()
        log.info("Authenticated  (key prefix: %s...)", api_key[:8])

        await run_swap_loop(sdk, cfg)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped by user")
