"""
Signal outcome evaluator + performance digest.
-----------------------------------------------
Reads signals.jsonl, resolves each still-open signal against fresh candles
(win / loss / expired + realized R), writes the file back, and prints a
performance summary. Set DIGEST=1 to also send the summary to Telegram.

A signal wins if price reaches its target before its stop within EVAL_HORIZON
candles, loses if the stop is hit first (stop wins same-candle ties), and
expires (marked-to-market) if neither happens within the horizon.

Env vars: TELEGRAM_* (only if DIGEST=1); SIGNALS_LOG, EVAL_HORIZON, TARGET_R
are shared with scanner.py.
"""

import os
import time
import scanner as sc


def resolve_open_signals(rows):
    """Evaluate every open signal; mutate rows in place. Returns count resolved."""
    open_rows = [r for r in rows if r.get("status") == "open"]
    print(f"{len(rows)} logged, {len(open_rows)} open to evaluate")
    cache, resolved = {}, 0
    for r in open_rows:
        key = (r["exchange"], r["symbol"], r["timeframe"])
        try:
            if key not in cache:
                cache[key] = sc.klines_for(r["exchange"], r["symbol"], r["timeframe"])
                time.sleep(sc.REQUEST_PAUSE)
            k = cache[key]
            res = sc.evaluate_outcome(r, k.highs, k.lows, k.closes, k.opens)
            if res:
                r.update(res)
                r["closed_ts"] = int(time.time())
                resolved += 1
        except Exception as e:
            print(f"  eval failed {r['id']}: {type(e).__name__}")
    return resolved


def _stats(subset):
    n = len(subset)
    if not n:
        return "no closed trades"
    wins = sum(1 for r in subset if r["status"] == "win")
    avg_r = sum((r.get("realized_r") or 0) for r in subset) / n
    return f"{n} trades | win {100 * wins / n:.0f}% | avg {avg_r:+.2f}R"


def build_summary(rows):
    closed = [r for r in rows if r.get("status") in ("win", "loss")]
    lines = ["📈 Sweep Strategy — Performance Digest"]
    lines.append(f"Overall: {_stats(closed)}")
    for tf, label in [("1d", "Daily"), ("1w", "Weekly"), ("1M", "Monthly")]:
        sub = [r for r in closed if r["timeframe"] == tf]
        if sub:
            lines.append(f"{label}: {_stats(sub)}")
    for d, label in [("bull", "🟢 Bull"), ("bear", "🔴 Bear")]:
        sub = [r for r in closed if r["direction"] == d]
        if sub:
            lines.append(f"{label}: {_stats(sub)}")
    n_open = sum(1 for r in rows if r.get("status") == "open")
    n_exp = sum(1 for r in rows if r.get("status") == "expired")
    lines.append(f"Open: {n_open} | Expired: {n_exp} | Logged total: {len(rows)}")
    return "\n".join(lines)


def main():
    rows = sc.load_signals()
    if not rows:
        print("No signals logged yet — nothing to evaluate.")
        return
    resolved = resolve_open_signals(rows)
    if resolved:
        sc.save_signals(rows)
    print(f"Resolved {resolved} signal(s).")
    summary = build_summary(rows)
    print("\n" + summary)
    if os.environ.get("DIGEST") == "1":
        sc.send_telegram(summary)
        print("Digest sent to Telegram.")


if __name__ == "__main__":
    main()
