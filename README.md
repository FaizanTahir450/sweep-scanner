# Liquidity Sweep Scanner (Multi-Exchange → Telegram)

Scans every liquid USDT spot pair across **Binance, MEXC, and KuCoin** (plus
Binance USDT-M futures when reachable) on **daily, weekly, and monthly** candles,
detects liquidity sweeps / Swing Failure Patterns, and sends you a Telegram
alert. Runs free on GitHub Actions — no PC, no server, nothing manual.

**Timeframes & schedule (all times UTC):**
- **Daily** — every day at 00:15, after the 1D candle closes.
- **Weekly** — Mondays at 00:15, after the 1W candle closes.
- **Monthly** — the 1st of each month at 00:15, after the 1M candle closes.

Each timeframe sends its own Telegram message. (On a Monday-the-1st all three
fire — that's three messages.)

**Signal logic (same on every timeframe):**
- Bullish sweep: candle wick goes BELOW the most recent confirmed swing low,
  but candle CLOSES back above it.
- Bearish sweep: candle wick goes ABOVE the most recent confirmed swing high,
  but candle CLOSES back below it.
- A swing point needs 5 higher/lower bars on each side to be confirmed, and
  the level is ignored if any candle already closed through it.
- The still-forming (current) candle is always excluded — signals fire only on
  closed candles.

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
