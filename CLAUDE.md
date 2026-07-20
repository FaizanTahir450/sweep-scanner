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
| `scanner.py` | Scan core. Fetches symbols/klines, detects sweeps, computes trade levels, sends Telegram, logs signals. Also holds the shared signal-log + `evaluate_outcome` helpers. |
| `evaluate.py` | Resolves logged signals (win/loss/expired + realized R) and builds/sends the performance digest. Imports `scanner` for fetchers + helpers. |
| `backtest.py` | Read-only historical backtest: replays candles, fires the live `analyze_sweep` per bar, grades with `evaluate_outcome`, reports win% / expectancy by timeframe/direction/exchange. Run locally on demand; tune via `BACKTEST_*` env vars. Never writes signals.jsonl or Telegram. |
| `signals.jsonl` | Append-only signal log, committed back by the Action each run. One JSON object per signal (levels + outcome). Created on first run; **not** gitignored. |
| `.github/workflows/scan.yml` | Three crons (daily `15 0 * * *`, weekly `15 0 * * 1`, monthly `15 0 1 * *`) + manual `workflow_dispatch` with a timeframe dropdown. Steps: resolve timeframe → run scanner → run evaluate → commit `signals.jsonl` back. `permissions: contents: write` + a `concurrency` group (serializes the commit-back). Weekly run sets `DIGEST=1`. |
| `README.md` | End-user setup guide (Telegram bot, GitHub secrets, manual trigger). |
| `IMPROVEMENTS.md` | Backlog of strategy upgrades (some now built — see markers). |

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
- `analyze_sweep()` is a **layer on top** of `check_sweep` (does not change it):
  it adds entry/stop/target (stop = the sweep wick extreme, target = `TARGET_R`×
  risk). If you change the level math, do it here, not in `check_sweep`.
- `score_signal()` is another post-detection layer: a 0-100 rank from volume
  spike + rejection wick + close location. It NEVER changes detection. `MIN_SCORE`
  (default 0) is an opt-in filter applied in `scan()`; at 0 nothing is dropped.
  Backtest note: win% is non-monotonic in score (mid-band best, top band regresses
  — big-volume/deep-wick sweeps behave like breakouts). Recalibrate before relying
  on `MIN_SCORE` to filter. Fetchers now return a `Klines` namedtuple that also
  carries `oprices` (open price) and `vols` (volume) for scoring.

## Signal logging & evaluation

- Kline fetchers return `(highs, lows, closes, opens)` — `opens` are candle
  open/start times in ms, used as each candle's id.
- `scan()` returns signal dicts; `log_signals()` appends unseen ones to
  `signals.jsonl` as `status:"open"` (dedup by `timeframe:exchange:symbol:candle_ts`).
- `evaluate.py` re-fetches candles via `klines_for(exchange, symbol, timeframe)`
  (timeframe-parameterized, so it can score any timeframe regardless of the run's
  `TIMEFRAME`), locates the signal candle by `candle_ts`, and walks forward up to
  `EVAL_HORIZON` candles: **win** (target hit first), **loss** (stop first; stop
  wins same-candle ties), or **expired** (mark-to-market R). Writes the file back.
- The digest (win rate / avg R, overall + per timeframe/direction) is sent on the
  weekly run (`DIGEST=1`).

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
| `TIMEFRAME` (env) | 1d | `1d`/`1w`/`1M` (or daily/weekly/monthly). Selects per-exchange interval via `BINANCE_INTERVALS`/`MEXC_INTERVALS`/`KUCOIN_TYPES` (note MEXC weekly is `1W`) and the message label. |
| `TARGET_R` (env) | 2 | Reward multiple for the target; stop = 1R (the sweep wick). |
| `EVAL_HORIZON` (env) | 20 | Candles a signal has to resolve before it expires. |
| `SIGNALS_LOG` (env) | signals.jsonl | Path to the signal log (override to a scratch path for local tests so the repo file isn't touched). |
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
