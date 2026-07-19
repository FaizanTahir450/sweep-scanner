# Daily Liquidity Sweep Scanner (Multi-Exchange → Telegram)

Scans every liquid USDT spot pair across **Binance, MEXC, and KuCoin** (plus
Binance USDT-M futures when reachable) once per day after the daily candle
closes, detects liquidity sweeps / Swing Failure Patterns, and sends you a
Telegram alert. Runs free on GitHub Actions — no PC, no server, nothing manual.

**Signal logic (daily candles):**
- Bullish sweep: candle wick goes BELOW the most recent confirmed swing low,
  but candle CLOSES back above it.
- Bearish sweep: candle wick goes ABOVE the most recent confirmed swing high,
  but candle CLOSES back below it.
- A swing point needs 5 higher/lower bars on each side to be confirmed, and
  the level is ignored if any candle already closed through it.

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
Repo → **Actions** tab → "Daily Sweep Scan" → **Run workflow** button.
Watch the logs; within a few minutes you should get a Telegram message.

Done. It now runs automatically every day at **00:15 UTC**
(= 05:15 AM Pakistan time) and messages you the results.

---

## Tuning knobs (top of scanner.py)

| Setting            | Default    | Meaning                                        |
|--------------------|-----------|-------------------------------------------------|
| `SWING_STRENGTH`   | 5         | Bars each side to confirm a swing. Higher = only major highs/lows, fewer signals. |
| `MIN_QUOTE_VOLUME` | 500,000   | Skip pairs under $500k 24h volume. Lower = more coins (esp. MEXC/KuCoin long tail), more noise. |
| `CANDLES`          | 120       | Days of history examined.                       |
| `SCAN_FUTURES`     | True      | Binance USDT-M futures. Set False to skip.      |

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
- GitHub schedules can drift 5–15 minutes at busy times — normal.
- If the repo has no commits for 60 days, GitHub pauses scheduled
  workflows and emails you; one click re-enables it.
