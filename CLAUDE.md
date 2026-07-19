# CLAUDE.md — sweep-scanner

Guidance for future Claude Code sessions working in this repo.

## What this project is

A single-file Python scanner that runs **on GitHub Actions**, scans liquid USDT
spot pairs across **multiple exchanges (Binance, MEXC, KuCoin)** plus Binance
USDT-M futures on **daily / weekly / monthly** candles, detects **liquidity
sweeps / Swing Failure Patterns (SFP)**, and sends a consolidated **Telegram**
message per run. No server, no manual step.

The **timeframe** is selected at runtime via the `TIMEFRAME` env var
(`1d`/`1w`/`1M`, default daily). The workflow runs three schedules — daily
(00:15 UTC), weekly (Mondays 00:15 UTC), monthly (1st 00:15 UTC) — each setting
`TIMEFRAME` accordingly; a manual `workflow_dispatch` has a timeframe dropdown.

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | The entire program. Fetches symbols, fetches klines, detects sweeps, sends Telegram. |
| `.github/workflows/scan.yml` | Three crons (daily `15 0 * * *`, weekly `15 0 * * 1`, monthly `15 0 1 * *`) + manual `workflow_dispatch` with a timeframe dropdown. A "Resolve timeframe" step maps the cron / input to `TIMEFRAME` (1d/1w/1M) via `github.event.schedule`. Installs `requests`, runs `scanner.py`. |
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
  `check_sweep()` (returns `"bull"`, `"bear"`, or `None`). These are exchange-
  agnostic — they take `(highs, lows, closes)` lists, so adding an exchange never
  touches the signal logic.

## Architecture — adding/removing exchanges

Each exchange has two adapter pieces: a **symbol lister** (`get_<ex>_symbols()`,
applies the volume filter) and a **kline fetcher** returning
`(highs, lows, closes)` as ascending lists of *closed* candles only. Spot
exchanges are wired up in the **`SPOT_EXCHANGES`** registry near the bottom of
`scanner.py`: `(display_name, symbol_lister, kline_fetcher)`. `main()` iterates
it, each exchange wrapped in try/except so one failing (e.g. geo-block) emits a
⚠️ note without blocking the others. Binance/MEXC share `binance_style_klines()`
(identical kline format); KuCoin uses `kucoin_klines()` (newest-first, different
column order). Binance futures is handled as a separate block after the registry.

## Data sources

- **Binance spot**: `https://data-api.binance.vision` — public geo-unrestricted
  mirror (GitHub runners are US-based; the main Binance API blocks US IPs).
- **MEXC**: `https://api.mexc.com` — Binance-compatible API; `status == "1"`
  means trading. Reachable from US runners.
- **KuCoin**: `https://api.kucoin.com` — `volValue` = USDT 24h volume; candles are
  newest-first `[start, open, close, high, low, ...]`; leveraged tokens end in
  `3L/3S/5L/5S`. Candles give only a *start* time, so `_kucoin_candle_closed()`
  decides the forming candle by fixed duration (day/week) or calendar month.
  KuCoin weeks start **Thursday** (Binance/MEXC weeks start Monday) — expected.

**Forming-candle safety:** the current (unclosed) candle is always dropped —
Binance/MEXC via `close_time <= now`, KuCoin via `_kucoin_candle_closed()`.
Signals only ever fire on closed candles, on every timeframe.
- **Binance futures (USDT-M)**: `https://fapi.binance.com` — no geo mirror; usually
  geo-blocked on US runners, so it typically emits a ⚠️ note. To scan it, run from
  a non-US machine. (No proxy support in the code — kept deliberately simple.)

## Config knobs (top of `scanner.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `SWING_STRENGTH` | 5 | Bars each side to confirm a swing point. |
| `TIMEFRAME` (env) | 1d | `1d`/`1w`/`1M` (or daily/weekly/monthly). Sets per-exchange interval (`BINANCE_INTERVAL`/`MEXC_INTERVAL`/`KUCOIN_TYPE` — note MEXC weekly is `1W`, KuCoin uses `1day`/`1week`/`1month`) and the message label. |
| `CANDLES` | 120 | Candles of history fetched per symbol (per timeframe). |
| `MIN_QUOTE_VOLUME` | 500_000 | Skip pairs under $500k 24h quote volume. Applied to every exchange. Lower = more coins (esp. MEXC/KuCoin long tail), more noise. |
| `QUOTE_ASSET` | "USDT" | Quote asset filter. |
| `SCAN_FUTURES` | True | Binance USDT-M futures. Set False to skip. |
| `REQUEST_PAUSE` | 0.08 | Seconds between kline requests (rate-limit safety). |

Leveraged tokens (Binance/MEXC `UP/DOWN/BULL/BEAR` suffixes, KuCoin `3L/3S/5L/5S`)
and stablecoin bases are excluded on every exchange.

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
