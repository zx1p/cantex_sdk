# Cantex Swap Bot

An automated swap bot for the [Cantex](https://cantex.io) decentralised exchange. Credentials come from a `.env` file and swap parameters from `config.json`.

> **Fork note:** Built directly on top of [caviarnine/cantex_sdk](https://github.com/caviarnine/cantex_sdk).

## License

MIT OR Apache-2.0  
See [LICENSE-MIT](LICENSE-MIT) and [LICENSE-APACHE](LICENSE-APACHE).

---

## Installation

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
```

---

## Setup

Create a `.env` file in the project root:

```bash
CANTEX_OPERATOR_KEY=your_operator_key_hex
CANTEX_TRADING_KEY=your_intent_key_hex
CANTEX_BASE_URL=https://api.testnet.cantex.io   # optional, defaults to testnet
```

Then edit `config.json` to set your swap parameters (see below).

---

## config.json reference

```json
{
  "api_key_path": "secrets/api_key.txt",

  "swap": {
    "token_a": "CC",
    "token_b": "USDCx",

    "amount_min": "10",
    "amount_decimal_places": 6,

    "interval_min_minutes": 5,
    "interval_max_minutes": 10,

    "max_network_fee": "0.1"
  }
}
```

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `api_key_path` | No | `"secrets/api_key.txt"` | Path to cache the authenticated API key on disk between restarts. Set to `null` to disable. |
| `swap` | Yes | — | Swap bot parameters (see below). |

### `swap` block

| Field | Required | Default | Description |
| --- | --- | --- | --- |
| `token_a` | Yes | — | Symbol or instrument ID of the primary token (e.g. `"CC"`). |
| `token_b` | Yes | — | Symbol or instrument ID of the secondary token (e.g. `"USDCx"`). |
| `amount_min` | Yes | — | Minimum sell amount per swap (e.g. `"10"`). |
| `amount_decimal_places` | No | `6` | Decimal places used when generating a random sell amount. |
| `interval_min_minutes` | Yes | — | Minimum wait time between cycles (minutes). |
| `interval_max_minutes` | Yes | — | Maximum wait time between cycles (minutes). |
| `max_network_fee` | Yes | — | Swap is skipped if the quoted network fee is >= this value. |

---

## Running the bot

```bash
uv run main.py
```

Stop at any time with `Ctrl+C`.

---

## How it works

Each cycle the bot:

1. **Resolves instruments** — on startup, looks up live instrument IDs for `token_a` and `token_b` by matching symbols against the account's token list (case-insensitive). Exits with a clear error if either token is not found.

2. **Checks live balances** — decides the swap direction:
   - If `token_a` balance ≥ `amount_min`: sells `token_a` for `token_b`.
   - Otherwise, if `token_b` balance ≥ `amount_min`: sells `token_b` for `token_a` (reverse swap).
   - If neither balance meets the minimum: logs an error and skips the cycle.

3. **Picks a random amount** — uniformly distributed between `amount_min` and the available sell-token balance, rounded to `amount_decimal_places`.

4. **Fetches a quote** — if the quoted network fee ≥ `max_network_fee`, the swap is skipped.

5. **Executes the swap** — submits the swap and logs the result.

6. **Waits** — sleeps for a random duration between `interval_min_minutes` and `interval_max_minutes` before the next cycle.

The bot logs cumulative swap counts, fee-skip counts, and balance-skip counts each cycle.