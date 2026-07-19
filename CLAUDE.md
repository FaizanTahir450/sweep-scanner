# CLAUDE.md — sweep-scanner

Guidance for future Claude Code sessions working in this repo.

## What this project is

A single-file Python scanner that runs **once daily on GitHub Actions**, scans
every liquid Binance USDT pair (spot + USDT-M futures) on the **daily candle**,
detects **liquidity sweeps / Swing Failure Patterns (SFP)**, and sends a
consolidated **Telegram** message with the results. No server, no manual step.

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | The entire program. Fetches symbols, fetches klines, detects sweeps, sends Telegram. |
| `.github/workflows/scan.yml` | Daily cron (`15 0 * * *` = 00:15 UTC) + manual `workflow_dispatch`. Installs `requests`, runs `scanner.py`. |
| `README.md` | End-user setup guide (Telegram bot, GitHub secrets, manual trigger). |

## Signal logic — DO NOT change unless the user explicitly asks

Implemented in `scanner.py`; treat as fixed spec:

- Find the most recent **confirmed** swing low/high — `SWING_STRENGTH = 5` bars
  on each side.
- The level must still be **intact**: no candle has *closed* beyond it since the
  pivot (checked up to, but excluding, the last closed candle).
- On the **last closed** daily candle:
  - **Bullish sweep**: `low < swing_low` AND `close > swing_low`
  - **Bearish sweep**: `high > swing_high` AND `close < swing_high`
- Key functions: `last_valid_pivot()` (pivot + intact check) and
  `check_sweep()` (returns `"bull"`, `"bear"`, or `None`).

## Data sources

- **Spot**: `https://data-api.binance.vision` — Binance's public, geo-unrestricted
  data mirror. Used because GitHub runners are US-based and the main Binance API
  blocks US IPs.
- **Futures (USDT-M)**: `https://fapi.binance.com` — no geo mirror exists; if the
  runner IP is blocked the run still succeeds and the Telegram message includes a
  clear ⚠️ note. Spot results are unaffected.

## Config knobs (top of `scanner.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `SWING_STRENGTH` | 5 | Bars each side to confirm a swing point. |
| `CANDLES` | 120 | Daily candles of history fetched per symbol. |
| `MIN_QUOTE_VOLUME` | 1_000_000 | Skip pairs under $1M 24h quote volume. |
| `QUOTE_ASSET` | "USDT" | Quote asset filter. |
| `SCAN_FUTURES` | True | Set False for spot only. |
| `REQUEST_PAUSE` | 0.08 | Seconds between kline requests (rate-limit safety). |

Leveraged tokens (`UP/DOWN/BULL/BEAR` suffixes) and stablecoin bases are excluded.

## Secrets / configuration — SECURITY

The Telegram bot token and chat ID are provided **only** via environment
variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

- **Never** hardcode the token/chat ID in code, commits, logs, or docs.
- In production they live as **GitHub Actions repository secrets**.
- For local testing, set them as local environment variables only.

## Running locally

```bash
pip install requests
export TELEGRAM_BOT_TOKEN=...   # PowerShell: $env:TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID=...
python scanner.py
```

A full run scans hundreds of symbols with a small pause between requests, so it
takes several minutes. For a quick smoke test, temporarily slice the symbol
lists and print instead of sending Telegram — **do not commit** such changes.

## Deployment

Pushed to a **private** GitHub repo `sweep-scanner`. GitHub Actions runs the
daily cron automatically. If the repo has no commits for 60 days, GitHub pauses
scheduled workflows and emails the owner; one click re-enables it.
