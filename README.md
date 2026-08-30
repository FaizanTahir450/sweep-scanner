# Liquidity Sweep Scanner (Multi-Exchange → Telegram)

Scans every liquid USDT spot pair across **Binance, MEXC, and KuCoin** (plus
Binance USDT-M futures when reachable) on **daily, weekly, and monthly** candles,
detects liquidity sweeps / Swing Failure Patterns, and sends you a Telegram
alert. Runs free on GitHub Actions — no PC, no server, nothing manual.

The **same strategy also scans every Pakistan Stock Exchange (PSX) equity** on
daily and weekly candles — see *PSX stocks* below.

**Timeframes & schedule (all times UTC):**
- **Daily** — every day at 00:15, after the 1D candle closes.
- **Weekly** — Mondays at 00:15, after the 1W candle closes.
- **Monthly** — the 1st of each month at 00:15, after the 1M candle closes.

Each timeframe sends its own Telegram message. (On a Monday-the-1st all three
fire — that's three messages.)

**PSX stocks (Pakistan Stock Exchange):**
- Universe: all ~750 listed equities (no debt, ETFs, rights or preference shares).
- Data: the official **PSX Data Portal** (free, no key, unadjusted EOD OHLCV), with
  a committed history cache (`psx_cache/`) so each run only fetches the current
  month; SCSTrade is the automatic fallback.
- Schedule (PKT): **daily 18:00 Mon–Fri**, **weekly Fri 18:15** — after the close
  and the portal's EOD publish. Separate Telegram message (`📊 PSX Daily Sweep
  Scan …`). On market holidays you get a one-line "market closed" note.
- Manual run: Actions → **PSX Sweep Scan** → Run workflow → daily / weekly.
- Same signal rules, levels, score and outcome tracking as crypto (`exchange: PSX`
  in `signals.jsonl`). Prices are unadjusted, so bonus/rights gaps can show up as
  sweeps — sanity-check those.

**Signal logic (same on every timeframe):**
- Bullish sweep: candle wick goes BELOW the most recent confirmed swing low,
  but candle CLOSES back above it.
- Bearish sweep: candle wick goes ABOVE the most recent confirmed swing high,
  but candle CLOSES back below it.
- A swing point needs 5 higher/lower bars on each side to be confirmed, and
  the level is ignored if any candle already closed through it.
- The still-forming (current) candle is always excluded — signals fire only on
  closed candles.

Alerts are kept simple — `⭐<score> SYMBOL @ price`, listed **best-score-first**.
The **0–100 quality score** (volume spike + rejection strength + close location)
is a rank indicator only — it never changes which sweeps are detected. Trade
levels (entry, stop = swept wick extreme, target = `TARGET_R`× risk) are computed
and **logged** for evaluation but kept out of the message. `MIN_SCORE` (default
**0**) is an opt-in filter: at 0 nothing is hidden; raise it to keep only
higher-scored setups.

**Performance tracking (automatic):**
- Every signal is logged to `signals.jsonl` in the repo (committed back by the
  Action each run — expect small automated "update signals.jsonl" commits).
- `evaluate.py` resolves each past signal against later candles — **win** (target
  hit before stop), **loss** (stop first), or **expired** — and records realized R.
- On the **weekly** run you get a Telegram **performance digest** (win rate &
  average R, overall and per timeframe / direction).
- A plain-text copy of every message is appended to **`signals_archive.txt`**
  (simple `SYMBOL @ price` style) and committed back, so you have a running
  history of what was sent.

---

## Setup (one time, ~15 minutes)

### Step 1 — Create the Telegram bot
1. In Telegram, open **@BotFather** → send `/newbot` → follow prompts.
2. Copy the **bot token** it gives you (looks like `123456789:AAH...`).
3. Open a chat with your new bot and send it any message (e.g. "hi").
4. Get your **chat ID**: open this URL in a browser (replace TOKEN):
   `https://api.telegram.org/botTOKEN/getUpdates`
   Find `"chat":{"id": 123456789 ...}` — that number is your chat ID.

### Step 2 — Create the GitHub repo
1. Create a new **private** repository on github.com (e.g. `sweep-scanner`).
2. Upload these files keeping the same structure:
   - `scanner.py`
   - `.github/workflows/scan.yml`
   (On the GitHub website: "Add file" → "Upload files". To create the
   workflow folder path, use "Create new file" and type
   `.github/workflows/scan.yml` as the filename, then paste the content.)

### Step 3 — Add secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN` = your bot token
- `TELEGRAM_CHAT_ID`   = your chat ID

### Step 4 — Test it
Repo → **Actions** tab → "Sweep Scan" → **Run workflow** → pick a **timeframe**
(daily / weekly / monthly) → **Run workflow**. Watch the logs; within a few
minutes you should get a Telegram message for that timeframe.

Done. It now runs automatically on schedule (all UTC): **daily** 00:15,
**weekly** Mondays 00:15, **monthly** on the 1st at 00:15
(00:15 UTC = 05:15 AM Pakistan time) and messages you the results.

---

## Tuning knobs (top of scanner.py)

| Setting            | Default    | Meaning                                        |
|--------------------|-----------|-------------------------------------------------|
| `SWING_STRENGTH`   | 5         | Bars each side to confirm a swing. Higher = only major highs/lows, fewer signals. |
| `MIN_QUOTE_VOLUME` | 500,000   | Skip pairs under $500k 24h volume. Lower = more coins (esp. MEXC/KuCoin long tail), more noise. |
| `CANDLES`          | 120       | Candles of history examined (per timeframe).    |
| `SCAN_FUTURES`     | True      | Binance USDT-M futures. Set False to skip.      |
| `TARGET_R`         | 2         | Reward multiple for the target (stop = 1R).     |
| `EVAL_HORIZON`     | 20        | Candles a signal has to hit target/stop before it expires. |
| `MIN_SCORE`        | 0         | Opt-in quality filter (0 = keep all). Raise to hide low-scored signals. |

**Timeframe** is chosen at runtime via the `TIMEFRAME` env var (`1d` / `1w` /
`1M`, or `daily` / `weekly` / `monthly`; defaults to daily). The workflow sets it
automatically per schedule, and the manual "Run workflow" button lets you pick
one. The volume filter always uses 24h quote volume regardless of timeframe.

To add or remove exchanges, edit the `SPOT_EXCHANGES` registry near the bottom
of `scanner.py` — each entry is `(display_name, symbol_lister, kline_fetcher)`.

## Notes
- Spot exchanges scanned: **Binance** (via `data-api.binance.vision`, a public
  mirror that works from any region), **MEXC** (`api.mexc.com`), and **KuCoin**
  (`api.kucoin.com`). If any one exchange is unreachable from the runner, you'll
  see a ⚠️ note for just that exchange and the others still report normally.
- **Binance futures** (`fapi.binance.com`) has no geo-free mirror, so on GitHub's
  US-based runners it's usually geo-blocked — you'll see a ⚠️ note and spot
  results are unaffected. To scan futures, run from a non-US machine.
- Weekly candles: Binance/MEXC weeks start Monday (UTC); KuCoin weeks start
  Thursday. So each exchange reports its own last *closed* weekly candle — the
  boundaries differ slightly by exchange. Monthly candles are calendar-month
  aligned everywhere.
- GitHub schedules can drift 5–15 minutes at busy times — normal.
- If the repo has no commits for 60 days, GitHub pauses scheduled
  workflows and emails you; one click re-enables it.
