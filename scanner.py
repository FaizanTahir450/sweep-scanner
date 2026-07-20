"""
Liquidity Sweep (SFP) Scanner — Multi-Exchange, Multi-Timeframe
---------------------------------------------------------------
Scans liquid USDT spot pairs across several exchanges (plus Binance USDT-M
futures) on daily/weekly/monthly candles and reports liquidity sweeps / Swing
Failure Patterns. Each signal carries trade levels (entry/stop/target) and a
0-100 quality score, and is logged to signals.jsonl for later outcome
evaluation (see evaluate.py).

Logic per symbol:
  * Find the most recent CONFIRMED swing low/high (strength N bars each side).
  * Level must still be intact (no candle CLOSED beyond it since the pivot).
  * Signal on the last closed candle:
      Bullish sweep : low  < swing_low  AND close > swing_low
      Bearish sweep : high > swing_high AND close < swing_high
Sends one consolidated Telegram message per run.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required); TIMEFRAME,
SIGNALS_LOG, TARGET_R, EVAL_HORIZON, MIN_SCORE (optional).
"""

import os
import json
import time
from collections import namedtuple

import requests

# ── Config ─────────────────────────────────────────────────────────
SWING_STRENGTH   = 5           # bars each side to confirm a swing point
CANDLES          = 120         # candles of history to fetch (per timeframe)
MIN_QUOTE_VOLUME = 500_000     # skip pairs with < $500k 24h volume
QUOTE_ASSET      = "USDT"
SCAN_FUTURES     = True         # Binance USDT-M futures (geo-blocked on US runners)
REQUEST_PAUSE    = 0.08        # seconds between kline requests (rate-limit safety)

# Trade levels & evaluation
TARGET_R     = float(os.environ.get("TARGET_R", "2"))       # reward multiple (stop = 1R)
EVAL_HORIZON = int(os.environ.get("EVAL_HORIZON", "20"))    # candles to resolve a signal
SIGNALS_LOG  = os.environ.get("SIGNALS_LOG", "signals.jsonl")
# Plain-text archive: a copy of every Telegram message, appended each run.
SIGNALS_ARCHIVE = os.environ.get("SIGNALS_ARCHIVE", "signals_archive.txt")

# Quality score: signals are always scored & ranked (best-first). MIN_SCORE is an
# OPT-IN filter — at 0 (default) NOTHING is filtered; every detected signal is
# shown/logged. Raise it to only keep higher-quality setups. The core detection
# (check_sweep) is never affected by scoring.
MIN_SCORE   = int(os.environ.get("MIN_SCORE", "0"))
VOL_LOOKBACK = 20              # candles for the average-volume baseline

# Timeframe: daily / weekly / monthly. Set via TIMEFRAME env (1d/1w/1M or a
# friendly name); defaults to daily. The volume filter always uses 24h quote
# volume for liquidity regardless of the scan timeframe.
_TF_ALIASES = {"1d": "1d", "d": "1d", "daily": "1d", "day": "1d",
               "1w": "1w", "w": "1w", "weekly": "1w", "week": "1w",
               "1M": "1M", "monthly": "1M", "month": "1M"}
TIMEFRAME = _TF_ALIASES.get(os.environ.get("TIMEFRAME", "1d").strip(), "1d")
TF_LABEL  = {"1d": "Daily", "1w": "Weekly", "1M": "Monthly"}[TIMEFRAME]

# Per-exchange interval / candle-type strings, keyed by timeframe.
BINANCE_INTERVALS = {"1d": "1d",   "1w": "1w",    "1M": "1M"}   # Binance spot + futures
MEXC_INTERVALS    = {"1d": "1d",   "1w": "1W",    "1M": "1M"}   # MEXC uses "1W"
KUCOIN_TYPES      = {"1d": "1day", "1w": "1week", "1M": "1month"}
BINANCE_INTERVAL  = BINANCE_INTERVALS[TIMEFRAME]               # for logging

# Exchange REST bases
SPOT_BASE   = "https://data-api.binance.vision"   # geo-unrestricted Binance spot mirror
FUT_BASE    = "https://fapi.binance.com"          # Binance futures (may be geo-blocked)
MEXC_BASE   = "https://api.mexc.com"              # MEXC (Binance-compatible API)
KUCOIN_BASE = "https://api.kucoin.com"            # KuCoin

# Leveraged tokens & stablecoin bases we don't want to scan
EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
KUCOIN_LEV_SUFFIXES = ("3L", "3S", "5L", "5S")   # e.g. BTC3L, ETH3S
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR",
                "EURI", "PAXG", "XUSD", "USDE", "USD1"}

# Parallel per-candle arrays (ascending, closed candles only). `opens` are candle
# open/start times in ms (each candle's id); `oprices` open prices; `vols` volume.
Klines = namedtuple("Klines", "highs lows closes opens oprices vols")

session = requests.Session()
session.headers.update({"User-Agent": "sweep-scanner/1.0"})


# ── Helpers ────────────────────────────────────────────────────────
def get_json(url, params=None, timeout=25):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Symbol listings (one per exchange) ─────────────────────────────
def get_binance_spot_symbols():
    """Active Binance spot USDT symbols passing the volume filter."""
    info = get_json(f"{SPOT_BASE}/api/v3/exchangeInfo")
    tickers = get_json(f"{SPOT_BASE}/api/v3/ticker/24hr")
    vol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
    out = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s.get("status") == "TRADING"
                and s.get("quoteAsset") == QUOTE_ASSET
                and s.get("isSpotTradingAllowed", True)
                and not sym.endswith(EXCLUDE_SUFFIXES)
                and s.get("baseAsset") not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            out.append(sym)
    return sorted(out)


def get_futures_symbols():
    """Active Binance USDT-M perpetual symbols passing the volume filter."""
    info = get_json(f"{FUT_BASE}/fapi/v1/exchangeInfo")
    tickers = get_json(f"{FUT_BASE}/fapi/v1/ticker/24hr")
    vol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}
    out = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == QUOTE_ASSET
                and s.get("baseAsset") not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            out.append(sym)
    return sorted(out)


def get_mexc_symbols():
    """Active MEXC spot USDT symbols passing the volume filter."""
    info = get_json(f"{MEXC_BASE}/api/v3/exchangeInfo")
    tickers = get_json(f"{MEXC_BASE}/api/v3/ticker/24hr")
    vol = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in tickers}
    out = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s.get("status") == "1"                       # MEXC: "1" == trading
                and s.get("quoteAsset") == QUOTE_ASSET
                and s.get("isSpotTradingAllowed", False)
                and not sym.endswith(EXCLUDE_SUFFIXES)
                and s.get("baseAsset") not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            out.append(sym)
    return sorted(out)


def get_kucoin_symbols():
    """Active KuCoin spot USDT symbols passing the volume filter."""
    syms = get_json(f"{KUCOIN_BASE}/api/v1/symbols")["data"]
    tick = get_json(f"{KUCOIN_BASE}/api/v1/market/allTickers")["data"]["ticker"]
    vol = {t["symbol"]: float(t.get("volValue") or 0) for t in tick}  # volValue = USDT vol
    out = []
    for s in syms:
        sym = s["symbol"]                                # e.g. "BTC-USDT"
        base = s.get("baseCurrency", "")
        if (s.get("quoteCurrency") == QUOTE_ASSET
                and s.get("enableTrading")
                and not base.endswith(KUCOIN_LEV_SUFFIXES)
                and base not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            out.append(sym)
    return sorted(out)


# ── Kline fetchers (return Klines; ascending, closed candles only) ──
def binance_style_klines(base, path, symbol, interval):
    """Binance & MEXC share this exact kline format (works for 1d/1w/1M)."""
    data = get_json(f"{base}{path}",
                    params={"symbol": symbol, "interval": interval, "limit": CANDLES})
    now_ms = int(time.time() * 1000)
    closed = [k for k in data if int(k[6]) <= now_ms]    # k[6] = close time
    return Klines(
        highs=[float(k[2]) for k in closed],
        lows=[float(k[3]) for k in closed],
        closes=[float(k[4]) for k in closed],
        opens=[int(k[0]) for k in closed],               # open time (ms) — candle id
        oprices=[float(k[1]) for k in closed],           # open price
        vols=[float(k[5]) for k in closed],              # base volume
    )


def _kucoin_candle_closed(start, now_s, timeframe):
    """KuCoin candles give only a start time. Decide if the period has ended."""
    if timeframe == "1M":                                # calendar month (variable length)
        st, nw = time.gmtime(start), time.gmtime(now_s)
        return (st.tm_year, st.tm_mon) < (nw.tm_year, nw.tm_mon)
    dur = 86400 if timeframe == "1d" else 604800         # day / week: fixed duration
    return start + dur <= now_s


def kucoin_klines(symbol, timeframe=TIMEFRAME):
    """KuCoin candles are newest-first: [start, open, close, high, low, vol, turnover]."""
    data = get_json(f"{KUCOIN_BASE}/api/v1/market/candles",
                    params={"type": KUCOIN_TYPES[timeframe], "symbol": symbol}).get("data") or []
    now_s = int(time.time())
    rows = [r for r in reversed(data)                    # -> ascending, closed only
            if _kucoin_candle_closed(int(r[0]), now_s, timeframe)]
    return Klines(
        highs=[float(r[3]) for r in rows],
        lows=[float(r[4]) for r in rows],
        closes=[float(r[2]) for r in rows],
        opens=[int(r[0]) * 1000 for r in rows],          # start time (ms) — candle id
        oprices=[float(r[1]) for r in rows],             # open price
        vols=[float(r[5]) for r in rows],                # volume
    )


def klines_for(exchange, symbol, timeframe):
    """Timeframe-parameterized fetch dispatch (used by the scan and the evaluator)."""
    if exchange == "BINANCE":
        return binance_style_klines(SPOT_BASE, "/api/v3/klines", symbol, BINANCE_INTERVALS[timeframe])
    if exchange == "MEXC":
        return binance_style_klines(MEXC_BASE, "/api/v3/klines", symbol, MEXC_INTERVALS[timeframe])
    if exchange == "KUCOIN":
        return kucoin_klines(symbol, timeframe)
    if exchange == "BINANCE FUT":
        return binance_style_klines(FUT_BASE, "/fapi/v1/klines", symbol, BINANCE_INTERVALS[timeframe])
    raise ValueError(f"unknown exchange: {exchange}")


# ── Sweep detection (unchanged signal logic) ───────────────────────
def last_valid_pivot(values, closes, strength, kind):
    """
    Most recent confirmed pivot low/high whose level is still intact
    (no candle has CLOSED beyond it after the pivot, up to but excluding
    the last candle). Returns the pivot price or None.
    """
    last = len(values) - 1                       # index of last closed candle
    for p in range(last - strength - 1, strength - 1, -1):
        left  = values[p - strength:p]
        right = values[p + 1:p + strength + 1]
        v = values[p]
        if kind == "low":
            is_pivot = all(v < x for x in left) and all(v <= x for x in right)
            intact   = all(c > v for c in closes[p + 1:last])
        else:
            is_pivot = all(v > x for x in left) and all(v >= x for x in right)
            intact   = all(c < v for c in closes[p + 1:last])
        if is_pivot:
            return v if intact else None         # nearest pivot broken → no signal
    return None


def check_sweep(highs, lows, closes):
    """Return 'bull', 'bear', or None for the last closed candle."""
    if len(closes) < SWING_STRENGTH * 2 + 5:
        return None
    last = len(closes) - 1

    swing_low = last_valid_pivot(lows, closes, SWING_STRENGTH, "low")
    if swing_low is not None and lows[last] < swing_low and closes[last] > swing_low:
        return "bull"

    swing_high = last_valid_pivot(highs, closes, SWING_STRENGTH, "high")
    if swing_high is not None and highs[last] > swing_high and closes[last] < swing_high:
        return "bear"
    return None


# ── Trade levels + quality score (layers on top — detection unchanged) ──
def analyze_sweep(highs, lows, closes):
    """
    Wrap check_sweep with trade levels. Returns a dict with direction, the swept
    level, entry (close), stop (the sweep wick extreme) and target (TARGET_R
    multiple of risk), or None. Detection matches check_sweep exactly.
    """
    signal = check_sweep(highs, lows, closes)
    if signal is None:
        return None
    last = len(closes) - 1
    entry = closes[last]
    if signal == "bull":
        level = last_valid_pivot(lows, closes, SWING_STRENGTH, "low")
        stop  = lows[last]                       # below the swept low
        risk  = entry - stop
        if risk <= 0:
            return None
        target = entry + TARGET_R * risk
    else:
        level = last_valid_pivot(highs, closes, SWING_STRENGTH, "high")
        stop  = highs[last]                      # above the swept high
        risk  = stop - entry
        if risk <= 0:
            return None
        target = entry - TARGET_R * risk
    return {"direction": signal, "level": level, "entry": entry,
            "stop": stop, "target": target}


def score_signal(direction, highs, lows, closes, oprices, vols):
    """
    0-100 quality score for an already-detected signal — pure ranking metadata,
    never changes detection. Blends: volume spike (vs prior avg), rejection
    strength (wick beyond the level), and close location within the candle.
    Returns (score, reason).
    """
    i = len(closes) - 1
    hi, lo, cl, op = highs[i], lows[i], closes[i], oprices[i]
    rng = (hi - lo) or 1e-9

    prior = vols[max(0, i - VOL_LOOKBACK):i]
    avg_v = (sum(prior) / len(prior)) if prior else 0.0
    v_ratio = (vols[i] / avg_v) if avg_v > 0 else 1.0

    if direction == "bull":
        close_loc = (cl - lo) / rng              # 1 = closed at the high
        rej = (min(op, cl) - lo) / rng           # lower-wick fraction (rejection of low)
    else:
        close_loc = (hi - cl) / rng              # 1 = closed at the low
        rej = (hi - max(op, cl)) / rng           # upper-wick fraction (rejection of high)

    s_vol = min(v_ratio / 2.0, 1.0)              # 2x average volume → full marks
    s_rej = min(max(rej, 0.0) / 0.5, 1.0)        # 50% wick → full marks
    s_loc = min(max(close_loc, 0.0), 1.0)        # closes at extreme → full marks
    score = round(100 * (0.40 * s_vol + 0.35 * s_rej + 0.25 * s_loc))
    reason = f"vol {v_ratio:.1f}x·rej {rej:.2f}·loc {close_loc:.2f}"
    return score, reason


# ── Scan runner ────────────────────────────────────────────────────
def scan(name, symbols, fetch_klines):
    """Returns (signals, errors). Each signal is a dict with levels + score."""
    signals, errors = [], 0
    for i, sym in enumerate(symbols, 1):
        try:
            k = fetch_klines(sym)
            sig = analyze_sweep(k.highs, k.lows, k.closes)
            if sig:
                score, reason = score_signal(sig["direction"], k.highs, k.lows,
                                             k.closes, k.oprices, k.vols)
                if score >= MIN_SCORE:           # MIN_SCORE=0 → keep everything
                    sig.update({"exchange": name, "symbol": sym,
                                "timeframe": TIMEFRAME, "candle_ts": k.opens[-1],
                                "score": score, "reason": reason})
                    signals.append(sig)
        except Exception:
            errors += 1
        if i % 50 == 0:
            print(f"[{name}] {i}/{len(symbols)} scanned...")
        time.sleep(REQUEST_PAUSE)
    nb = sum(1 for s in signals if s["direction"] == "bull")
    ns = sum(1 for s in signals if s["direction"] == "bear")
    print(f"[{name}] done: {nb} bull / {ns} bear / {errors} errors")
    return signals, errors


# ── Signal log + outcome evaluation (shared with evaluate.py) ──────
def signal_id(s):
    return f"{s['timeframe']}:{s['exchange']}:{s['symbol']}:{s['candle_ts']}"


def load_signals(path=SIGNALS_LOG):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def save_signals(rows, path=SIGNALS_LOG):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def log_signals(new_signals, path=SIGNALS_LOG):
    """Append never-seen signals as status='open'. Returns count added."""
    rows = load_signals(path)
    seen = {r["id"] for r in rows}
    added = 0
    for s in new_signals:
        sid = signal_id(s)
        if sid in seen:
            continue
        seen.add(sid)
        rows.append({
            "id": sid, "logged_at": int(time.time()),
            "timeframe": s["timeframe"], "exchange": s["exchange"],
            "symbol": s["symbol"], "direction": s["direction"],
            "entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "level": s["level"], "candle_ts": s["candle_ts"],
            "score": s.get("score"), "reason": s.get("reason"),
            "status": "open", "realized_r": None, "closed_ts": None, "bars": None,
        })
        added += 1
    if added:
        save_signals(rows, path)
    return added


def evaluate_outcome(rec, highs, lows, closes, opens):
    """
    Resolve an open signal against fresh candles. Returns an update dict
    (status/realized_r/bars) or None if still open / not locatable.
    """
    try:
        idx = opens.index(rec["candle_ts"])          # locate the signal candle
    except ValueError:
        return None                                  # candle scrolled out of window
    entry, stop, target = rec["entry"], rec["stop"], rec["target"]
    bull = rec["direction"] == "bull"
    risk = abs(entry - stop) or 1e-9
    fh = highs[idx + 1: idx + 1 + EVAL_HORIZON]
    fl = lows[idx + 1: idx + 1 + EVAL_HORIZON]
    for j in range(len(fh)):
        hit_stop = fl[j] <= stop if bull else fh[j] >= stop
        hit_tgt  = fh[j] >= target if bull else fl[j] <= target
        if hit_stop:                                 # conservative: stop wins ties
            return {"status": "loss", "realized_r": -1.0, "bars": j + 1}
        if hit_tgt:
            return {"status": "win", "realized_r": round(TARGET_R, 4), "bars": j + 1}
    if len(fh) >= EVAL_HORIZON:                       # horizon exhausted → mark-to-market
        c = closes[idx + EVAL_HORIZON] if idx + EVAL_HORIZON < len(closes) else closes[-1]
        mtm = (c - entry) / risk * (1 if bull else -1)
        return {"status": "expired", "realized_r": round(mtm, 4), "bars": len(fh)}
    return None                                       # still open, not enough candles yet


# ── Telegram ───────────────────────────────────────────────────────
def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram hard limit is 4096 chars per message — split if needed
    for i in range(0, len(text), 4000):
        resp = session.post(url, json={"chat_id": chat_id,
                                       "text": text[i:i + 4000]},
                            timeout=20)
        resp.raise_for_status()


def append_archive(text, path=SIGNALS_ARCHIVE):
    """Append a copy of the sent Telegram message to the plain-text archive."""
    divider = ("\n" + "=" * 50 + "\n\n") if os.path.exists(path) and os.path.getsize(path) else ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(divider + text.rstrip() + "\n")


def fmt_price(p):
    return f"{p:.8f}".rstrip("0").rstrip(".") if p < 1 else f"{p:,.4f}".rstrip("0").rstrip(".")


def fmt_section(title, sigs):
    """Detailed rendering (score + levels) — used for the Telegram message."""
    if not sigs:
        return f"{title}: none"
    sigs = sorted(sigs, key=lambda s: s.get("score", 0), reverse=True)  # best-first
    lines = [f"{title} ({len(sigs)}):"]
    for s in sigs:
        lines.append(f"  • [{s.get('score', 0):>3}] {s['symbol']}  "
                     f"entry {fmt_price(s['entry'])} | stop {fmt_price(s['stop'])} | "
                     f"tgt {fmt_price(s['target'])} ({TARGET_R:g}R)  {s.get('reason', '')}")
    return "\n".join(lines)


def fmt_section_simple(title, sigs):
    """Plain 'SYMBOL @ price' rendering — used for the archive file."""
    if not sigs:
        return f"{title}: none"
    sigs = sorted(sigs, key=lambda s: s["symbol"])
    lines = [f"{title} ({len(sigs)}):"]
    lines += [f"  • {s['symbol']} @ {fmt_price(s['entry'])}" for s in sigs]
    return "\n".join(lines)


def render_message(header, blocks, formatter):
    """Build a full message from collected blocks using the given section formatter."""
    parts = [header]
    for b in blocks:
        if "note" in b:                              # ⚠️ warning line
            parts.append(b["note"])
        else:
            parts.append(f"— {b['label']} —")
            parts.append(formatter("🟢 Bullish sweeps", b["bulls"]))
            parts.append(formatter("🔴 Bearish sweeps", b["bears"]))
    return "\n\n".join(parts)


# ── Exchange registry (spot) ───────────────────────────────────────
# (display name, symbol-listing fn, kline fn)
SPOT_EXCHANGES = [
    ("BINANCE", get_binance_spot_symbols, lambda s: klines_for("BINANCE", s, TIMEFRAME)),
    ("MEXC",    get_mexc_symbols,         lambda s: klines_for("MEXC", s, TIMEFRAME)),
    ("KUCOIN",  get_kucoin_symbols,       lambda s: klines_for("KUCOIN", s, TIMEFRAME)),
]


# ── Main ───────────────────────────────────────────────────────────
def main():
    date_str = time.strftime("%d %b %Y", time.gmtime())
    print(f"Timeframe: {TF_LABEL} ({TIMEFRAME}) | MIN_SCORE={MIN_SCORE}")
    hdr = f"📊 {TF_LABEL} Sweep Scan — {date_str} ({TIMEFRAME} close)"
    if MIN_SCORE:
        hdr += f"  [min score {MIN_SCORE}]"
    blocks = []           # collected once, rendered twice (Telegram + archive)
    all_signals = []

    # Spot exchanges — one failing (e.g. geo-block) never blocks the others
    for display, list_symbols, fetch_klines in SPOT_EXCHANGES:
        try:
            syms = list_symbols()
            print(f"[{display}] symbols to scan: {len(syms)}")
            sigs, _ = scan(display, syms, fetch_klines)
            all_signals += sigs
            blocks.append({"label": f"{display} (SPOT)",
                           "bulls": [s for s in sigs if s["direction"] == "bull"],
                           "bears": [s for s in sigs if s["direction"] == "bear"]})
        except Exception as e:
            print(f"[{display}] scan failed: {type(e).__name__}: {e}")
            blocks.append({"note": f"⚠️ {display} spot scan unavailable ({type(e).__name__})."})

    # Binance USDT-M futures (may be geo-blocked on some runners)
    if SCAN_FUTURES:
        try:
            fut_syms = get_futures_symbols()
            print(f"[BINANCE FUT] symbols to scan: {len(fut_syms)}")
            sigs, _ = scan("BINANCE FUT", fut_syms, lambda s: klines_for("BINANCE FUT", s, TIMEFRAME))
            all_signals += sigs
            blocks.append({"label": "BINANCE FUTURES (USDT-M)",
                           "bulls": [s for s in sigs if s["direction"] == "bull"],
                           "bears": [s for s in sigs if s["direction"] == "bear"]})
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            blocks.append({"note": f"⚠️ Binance futures unavailable (HTTP {code} — likely geo-block on runner). Spot results above are complete."})
        except Exception as e:
            blocks.append({"note": f"⚠️ Binance futures scan failed: {type(e).__name__}"})

    send_telegram(render_message(hdr, blocks, fmt_section))          # detailed → Telegram
    print("Telegram message sent.")

    append_archive(render_message(hdr, blocks, fmt_section_simple))  # simple → archive file
    print(f"Archived message -> {SIGNALS_ARCHIVE}")

    added = log_signals(all_signals)
    print(f"Signal log: {added} new / {len(all_signals)} found -> {SIGNALS_LOG}")


if __name__ == "__main__":
    main()
