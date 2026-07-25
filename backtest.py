"""
Historical backtest for the sweep/SFP strategy.
-----------------------------------------------
Replays candle history and, at each past bar, runs the SAME detection
(analyze_sweep -> check_sweep) as the live scanner, then grades the outcome
with the SAME engine (evaluate_outcome): win (target before stop within
EVAL_HORIZON candles), loss (stop first), or expired (marked-to-market).

Reports win rate, expectancy (avg R per signal), and trade counts — overall
and broken down by timeframe, direction, and exchange. Read-only: it does NOT
touch signals.jsonl or send Telegram.

Tuning via env:
  BACKTEST_TIMEFRAMES  default "1d,1w,1M"
  BACKTEST_MAX_SYMBOLS default 0 (all liquid symbols; set e.g. 40 for a quick run)
  BACKTEST_CANDLES     default 500 (history depth for Binance/MEXC; KuCoin ~100)
  BACKTEST_EXCHANGES   default "BINANCE,MEXC,KUCOIN"
"""

import os
import time
import scanner as sc

TIMEFRAMES  = os.environ.get("BACKTEST_TIMEFRAMES", "1d,1w,1M").split(",")
MAX_SYMBOLS = int(os.environ.get("BACKTEST_MAX_SYMBOLS", "0"))
EXCHANGES   = os.environ.get("BACKTEST_EXCHANGES", "BINANCE,MEXC,KUCOIN").split(",")
sc.CANDLES  = int(os.environ.get("BACKTEST_CANDLES", "500"))   # deepen history (Binance/MEXC)

MIN_LEN = sc.ATR_PERIOD + 10   # matches analyze_sweep's minimum history requirement
LISTERS = {"BINANCE": sc.get_binance_spot_symbols,
           "MEXC": sc.get_mexc_symbols,
           "KUCOIN": sc.get_kucoin_symbols}


def backtest_symbol(exchange, symbol, timeframe):
    """Every historical sweep on this symbol, graded. Returns list of trades."""
    k = sc.klines_for(exchange, symbol, timeframe)
    highs, lows, closes, opens = k.highs, k.lows, k.closes, k.opens
    trades = []
    for t in range(MIN_LEN - 1, len(closes) - 1):           # t = "last closed" bar
        sig = sc.analyze_sweep(highs[:t + 1], lows[:t + 1], closes[:t + 1])
        if not sig:
            continue
        kw = sc.Klines(highs[:t + 1], lows[:t + 1], closes[:t + 1],
                       opens[:t + 1], k.oprices[:t + 1], k.vols[:t + 1])
        score, _ = sc.score_signal(sig, kw)
        rec = {"direction": sig["direction"], "entry": sig["entry"],
               "stop": sig["stop"], "target": sig["target"], "candle_ts": opens[t]}
        res = sc.evaluate_outcome(rec, highs, lows, closes, opens)
        if res:                                             # resolved or expired
            trades.append({"exchange": exchange, "timeframe": timeframe,
                           "direction": sig["direction"], "score": score, **res})
    return trades


def stats(trades):
    """(count, win% of decisive, expectancy R over all resolved)."""
    n = len(trades)
    if not n:
        return "no trades"
    wins = sum(1 for t in trades if t["status"] == "win")
    losses = sum(1 for t in trades if t["status"] == "loss")
    exp = sum(1 for t in trades if t["status"] == "expired")
    decisive = wins + losses
    win_rate = (100 * wins / decisive) if decisive else float("nan")
    expectancy = sum((t.get("realized_r") or 0) for t in trades) / n
    return (f"{n:5d} trades | win {win_rate:5.1f}% | exp {expectancy:+.2f}R "
            f"| W/L/exp {wins}/{losses}/{exp}")


def main():
    print(f"Backtest — timeframes={TIMEFRAMES} exchanges={EXCHANGES} "
          f"candles={sc.CANDLES} max_symbols={MAX_SYMBOLS or 'all'} "
          f"(TARGET_R={sc.TARGET_R:g}, horizon={sc.EVAL_HORIZON})\n")
    all_trades = []
    for tf in TIMEFRAMES:
        for ex in EXCHANGES:
            try:
                syms = LISTERS[ex]()
            except Exception as e:
                print(f"[{tf} {ex}] symbol list failed: {type(e).__name__}"); continue
            if MAX_SYMBOLS:
                syms = syms[:MAX_SYMBOLS]
            tf_ex_trades, errors = [], 0
            for sym in syms:
                try:
                    tf_ex_trades += backtest_symbol(ex, sym, tf)
                except Exception:
                    errors += 1
                time.sleep(sc.REQUEST_PAUSE)
            all_trades += tf_ex_trades
            print(f"[{tf:>2} {ex:<8}] {len(syms):4d} symbols, {errors} errors -> {stats(tf_ex_trades)}")
        print()

    print("=" * 72)
    print(f"OVERALL:                     {stats(all_trades)}")
    for tf in TIMEFRAMES:
        print(f"  timeframe {tf:<3}              {stats([t for t in all_trades if t['timeframe'] == tf])}")
    for d in ("bull", "bear"):
        print(f"  direction {d:<5}            {stats([t for t in all_trades if t['direction'] == d])}")
    print("-" * 72)
    print("BY QUALITY SCORE (does the score predict wins?):")
    for lo, hi in [(0, 40), (40, 55), (55, 70), (70, 101)]:
        bucket = [t for t in all_trades if lo <= t.get("score", 0) < hi]
        print(f"  score {lo:>2}-{hi - 1 if hi <= 100 else 100:<3}            {stats(bucket)}")
    print("=" * 72)
    print("Note: win% is of decisive (win+loss) trades; expectancy R averages all "
          "resolved incl. expired.\nIf higher score buckets show higher win%/expectancy, "
          "the score adds edge — set MIN_SCORE to filter.")


if __name__ == "__main__":
    main()
