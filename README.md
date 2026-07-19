# Daily Liquidity Sweep Scanner (Binance → Telegram)

Scans every liquid USDT pair on Binance (spot + USDT-M futures) once per day
after the daily candle closes, detects liquidity sweeps / Swing Failure
Patterns, and sends you a Telegram alert. Runs free on GitHub Actions —
no PC, no server, nothing manual.

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
- `FUTURES_PROXY` *(optional)* = proxy URL for futures data, e.g.
  `http://user:pass@host:port` or `socks5://user:pass@host:port`. Needed only
  to scan USDT-M futures, because GitHub's US-based runners are geo-blocked
  from `fapi.binance.com`. The proxy's exit must be in a Binance-allowed region.
  Leave it unset for spot-only scanning.

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
| `MIN_QUOTE_VOLUME` | 500,000   | Skip pairs under $500k 24h volume. Lower = more coins, more noise. |
| `CANDLES`          | 120       | Days of history examined.                       |
| `SCAN_FUTURES`     | True      | Set False for spot only.                        |

## Notes
- Spot data uses `data-api.binance.vision`, Binance's public data mirror
  that works from any region (GitHub runners are US-based and the main
  Binance API blocks US IPs).
- Futures has no such mirror; if the runner IP is blocked you'll see a
  clear ⚠️ note in the Telegram message and spot results are unaffected.
  To actually scan futures, set the `FUTURES_PROXY` secret (Step 3) to a proxy
  whose exit is in a Binance-allowed region — that unlocks ~260 extra
  futures-only coins.
- GitHub schedules can drift 5–15 minutes at busy times — normal.
- If the repo has no commits for 60 days, GitHub pauses scheduled
  workflows and emails you; one click re-enables it.
