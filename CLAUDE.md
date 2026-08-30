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

A second market, **PSX (Pakistan Stock Exchange equities)**, runs through the
*same* scanner with `MARKET=psx` (daily + weekly only) on its own workflow —
see "PSX market" below.

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Scan core. Fetches symbols/klines, detects sweeps, computes trade levels, sends Telegram, logs signals. Also holds the shared signal-log + `evaluate_outcome` helpers. |
| `evaluate.py` | Resolves logged signals (win/loss/expired + realized R) and builds/sends the performance digest. Imports `scanner` for fetchers + helpers. |
| `backtest.py` | Read-only historical backtest: replays candles, fires the live `analyze_sweep` per bar, grades with `evaluate_outcome`, reports win% / expectancy by timeframe/direction/exchange. Run locally on demand; tune via `BACKTEST_*` env vars. Never writes signals.jsonl or Telegram. |
| `signals.jsonl` | Append-only signal log, committed back by the Action each run. One JSON object per signal (levels + outcome). Created on first run; **not** gitignored. |
| `signals_archive.txt` | Human-readable archive: a copy of every message in plain `SYMBOL @ price` style (`fmt_section_simple`), appended each run, committed back. Telegram uses `fmt_section` (`⭐score SYMBOL @ price`, best-first); both render from the same blocks via `render_message()`. Entry/stop/target are logged to `signals.jsonl` but shown in neither message. |
| `.github/workflows/psx_scan.yml` | PSX workflow: crons `0 13 * * 1-5` (daily 18:00 PKT Mon–Fri) and `15 13 * * 5` (weekly Fri 18:15 PKT) + manual dropdown (daily/weekly). Runs `scanner.py` with `MARKET=psx`, then `evaluate.py`, then commits `signals.jsonl`, `signals_archive.txt` **and `psx_cache/`** back. Shares the `sweep-scan-signals` concurrency group with the crypto workflow. |
| `psx_cache/<SYMBOL>.csv` | Committed per-symbol PSX daily history (`date,open,high,low,close,volume`, ≤650 rows ≈ 2.6y). Refreshed incrementally each run (current month from the portal); one-time backfill came from SCSTrade and was verified identical to the portal. ~500 files / ~13 MB. |
| `.github/workflows/scan.yml` | Three crons (daily `15 0 * * *`, weekly `15 0 * * 1`, monthly `15 0 1 * *`) + manual `workflow_dispatch` with a timeframe dropdown. Steps: resolve timeframe → run scanner → run evaluate → commit `signals.jsonl` back. `permissions: contents: write` + a `concurrency` group (serializes the commit-back). Weekly run sets `DIGEST=1`. |
| `README.md` | End-user setup guide (Telegram bot, GitHub secrets, manual trigger). |
| `IMPROVEMENTS.md` | Backlog of strategy upgrades (some now built — see markers). |

## Signal logic — DO NOT change unless the user explicitly asks

Implemented in `scanner.py`; treat as fixed spec (identical for crypto and PSX):

- **Swings = ATR-filtered ZigZag** (`compute_atr` 14-period + `detect_pivots`): a
  candidate high/low keeps extending while price makes new extremes and is only
  *confirmed* once price CLOSES against it by `ATR_MULT × ATR` (default 2.0).
  Smaller wiggles never become pivots. Pivots alternate H/L and carry the bar at
  which they became known (`confirmed`) — no lookahead.
- **Level selection** (`last_intact_swing`): the most recent confirmed swing that
  is still sweepable — confirmed before the last candle, no candle has *closed*
  beyond it since the pivot, and (with `REQUIRE_UNTAPPED`, default on) no earlier
  *wick* has traded through it either (the signal candle must be the FIRST to run
  the level). A broken nearer level falls through to the next older intact one.
- **Sweep trigger** on the **last closed** candle (`analyze_sweep`):
  - **Bullish**: `low < swing_low` AND `close > swing_low`
  - **Bearish**: `high > swing_high` AND `close < swing_high`
  Entry = close, stop = the sweep wick extreme, target = `TARGET_R`× risk.
  `check_sweep()` is a thin wrapper returning `"bull"`/`"bear"`/`None`.
- `score_signal(sig, k)` is a post-detection **ranking** layer (0–100: volume
  spike, rejection wick, close location, reclaim distance, swing size in ATRs). It
  never changes detection. `MIN_SCORE` (default 0) is an opt-in filter in `scan()`.
- Fetchers return a `Klines` namedtuple `(highs, lows, closes, opens, oprices, vols)`;
  `opens` are candle open/start times in ms and serve as each candle's id.
- Backtest note (IMPROVEMENTS.md): none of the level-finders tested beat ~29% win
  at 2R; the deployed ZigZag+untapped variant was kept by the owner's decision.

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

## PSX market (`MARKET=psx`)

- **Universe**: every listed PSX equity from the portal's `/symbols` (excludes
  debt instruments, ETFs, and rights/preference-share instruments by name tag),
  ~750 tickers of which ~440 trade on a given day. `PSX_MIN_TURNOVER` (PKR, 20-day
  average close×volume; default 0 = off) is an optional liquidity floor.
- **Data**: official **PSX Data Portal** `https://dps.psx.com.pk` — free, no key,
  raw *unadjusted* EOD OHLCV via `POST /historical {month, year, symbol}` (HTML
  table, ONE month per request; `/timeseries/eod/<SYM>` has no high/low so it is
  not used). To keep runs ~10 min, `psx_cache/<SYMBOL>.csv` is kept and committed
  back; each run fetches only the months since the cache's last date (≤4). Empty
  or >100-day-stale caches are rebuilt from **SCSTrade**
  (`POST scstrade.com/stockscreening/SS_CompanySnapShotHP.aspx/chart`, whole
  history in one call; its `/Date(ms)/` stamps are PKT midnight → convert with
  UTC+5). SCSTrade is also the automatic fallback when the portal errors; if it has
  no data for a symbol, the last 3 portal months are used. Yahoo `.KA` was rejected
  (null bars, blocks runners).
- **Candles**: daily = one portal row per session; **weekly is aggregated locally**
  from daily bars over Mon–Fri ISO weeks (id = Monday 00:00 UTC in ms). Monthly is
  not supported for PSX. Forming-candle safety: a same-day bar is dropped before
  16:45 PKT; the current week is dropped until Friday 17:00 PKT.
- **Session guard** (`psx_session_check`, via `PSX_STATE["latest_date"]`): only
  signals on the just-closed session (daily) / just-completed week (weekly) are
  reported; on a holiday/no-new-candle day a one-line note is sent instead, so
  stale candles never re-alert. Signals are logged with `exchange:"PSX"` into the
  shared `signals.jsonl`; `evaluate.py` scores them through `klines_for("PSX", …)`.
- **Caveat**: prices are unadjusted — bonus/rights issues create price gaps that
  can look like sweeps. Timezone: all PSX dates are PKT (UTC+5, no DST).

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
| `MARKET` (env) | crypto | `crypto` or `psx` — selects the exchange registry (`CRYPTO_EXCHANGES` / `PSX_EXCHANGES`), session calendar and message label. |
| `ATR_MULT` (env) | 2.0 | ATR multiple a close must reverse by to confirm a ZigZag swing (1.5 = more swings, 2.5–3 = only major). |
| `REQUIRE_UNTAPPED` (env) | 1 | `0` to allow sweeps of levels a prior wick already ran. |
| `PSX_MIN_TURNOVER` (env) | 0 | PSX only: min 20-day avg traded value (PKR) to include a stock; 0 = every equity. |
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
