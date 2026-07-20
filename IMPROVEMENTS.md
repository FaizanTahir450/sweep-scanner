# Strategy Improvements — Backlog

Ideas for making the sweep/SFP scanner more *powerful* — i.e. higher-quality,
ranked, actionable signals rather than just more of them. The core signal logic
(`last_valid_pivot` / `check_sweep`) stays intact; these wrap **context and
scoring** around it.

**Recommended order:** #1 + #3 first (quality scoring + trade levels), then #4
(backtest to validate/tune), then #2 (confluence), then #5 (regime filter).

**Status:** ✅ **#3 (trade levels)** and ✅ **#6 (signal logging & evaluation)**
are **DONE** — signals carry entry/stop/target, are logged to `signals.jsonl`
(committed back by the Action), scored win/loss/expired by `evaluate.py`, and a
weekly Telegram performance digest reports win rate & avg R. Still open: #1
(quality score), #2 (confluence), #4 (backtest), #5 (regime filter).

---

## 1. Quality score + filters  ⭐ biggest win (fewer false positives)
Today every sweep is treated equally. Score each one and only alert on strong ones,
ranked best-first.

- **Volume spike** — sweep candle volume ≥ ~1.5× the 20-candle average (a real
  liquidity grab, not a quiet wick). Requires pulling volume (kline index 5) —
  currently discarded in the adapters.
- **Rejection strength** — wick length vs candle range / vs ATR. Long wick that
  closes near the opposite extreme = strong rejection; shallow sweeps filtered.
- **Close location** — bullish sweep should close in the upper third of its
  range (mirror for bearish).
- Output: a composite 0–100 score; show only signals above a threshold, sorted.

## 2. Multi-timeframe confluence (infra already exists — we scan 1d/1w/1M)
A daily sweep that aligns with a **weekly/monthly** swing level or trend is far
stronger than a random daily wick.
- Flag "⭐ confluence" when a lower-TF sweep sits at/near a higher-TF swing level.
- Could run all timeframes in one pass and cross-reference, or cache HTF levels.

## 3. Trade levels + risk/reward filter (makes alerts actionable)  ✅ DONE (levels)
Attach to each alert:
- **Entry** = current close
- **Stop** = just beyond the sweep wick (the swept extreme)
- **Target** = next opposing swing (or a fixed R multiple)
- **R:R** — only alert when R:R ≥ ~2.
Turns each message from "a wick happened" into a tradeable setup, and self-filters
weak setups.

## 4. Backtest mode  ⭐ the meta-upgrade (makes tuning data-driven)
Replay history and compute the pattern's **forward win rate / average R** — per
timeframe and per filter combination.
- Tells us which filters above actually improve results (vs guessing).
- Implementation: iterate historical candles, detect signals as of each bar,
  measure forward outcome (e.g. hit target before stop within N candles).
- Do this early if we want to validate the edge before adding complexity.

## 5. Market regime filter (cheap, avoids fighting the tape)
Don't take longs when the whole market is breaking down.
- Gate signals by **BTC** trend (e.g. BTC above/below its 200-period MA), or a
  simple market-breadth check (% of coins above their MA).
- Optionally only surface counter-trend sweeps at range extremes.

## 6. Signal logging & evaluation  ✅ DONE — the data to actually judge the strategy
Without a durable, structured record of signals + outcomes we can't compute a
win rate. Telegram chat and GitHub Actions logs are informal only (Actions logs
auto-delete after ~90 days). This is the foundation for tuning everything above.

**Design (serverless, no database):**
1. **Structured signal log** — each run appends to a repo-tracked `signals.jsonl`,
   one JSON object per signal:
   `{ts, timeframe, exchange, symbol, direction, entry, level, stop, target}`.
2. **Commit-back** — the Action commits the updated `signals.jsonl` back to the
   repo each run (`git add signals.jsonl && git commit && git push` with the
   built-in `GITHUB_TOKEN`). Free, versioned, no external store.
   - Guard against noisy commits (skip if nothing changed).
   - Beware the scheduled-workflow 60-day dormancy pause is reset by these commits
     — a nice side effect.
3. **Outcome tracker** — a periodic job re-reads open entries in `signals.jsonl`,
   pulls candles since the signal, and marks each **win/loss/open** (did price hit
   target before stop within N candles?) + forward return / realized R.
4. **Weekly hit-rate summary** — Telegram a digest: win rate & avg R per timeframe
   / exchange / direction, so the strategy's edge is visible over time.

**Live forward log vs backtest (#4):** the forward log is real out-of-sample truth
but takes weeks to accumulate; the backtest gives instant historical stats. Build
the backtest first to validate, run the forward log in parallel to confirm.

**Notes:** needs stop/target from #3 to score outcomes; needs volume/ATR from #1
for richer scoring. Keep `signals.jsonl` append-only and small (rotate by
year if it grows).

---

## Other / smaller ideas
- **Confirmation candle** (optional): wait for the next candle to close further in
  the signal direction before alerting — fewer fakeouts, one candle of lag.
- **Alert de-duplication / state**: remember which signals already fired; track
  outcomes to build a live hit-rate log.
- **Indicator confluence**: RSI divergence at the sweep, or equal highs/lows
  (liquidity pools) preceding the sweep.
- **Ranking + top-N**: cap each message to the top N by score to cut alert fatigue.

---

## Notes for implementation
- Volume is currently dropped in `binance_style_klines` / `kucoin_klines` — most
  of #1 needs it re-included (Binance/MEXC kline idx 5 = base volume; KuCoin idx 5
  = volume, idx 6 = turnover/quote volume).
- ATR/MA helpers would be small pure functions alongside `check_sweep` (keep the
  existing signal logic untouched; add scoring as a separate layer).
- Keep everything dependency-free (`requests` only) unless we deliberately choose
  to add numpy/pandas for the backtest.
