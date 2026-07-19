"""
Daily Liquidity Sweep (SFP) Scanner — Multi-Exchange
----------------------------------------------------
Scans liquid USDT spot pairs across several exchanges (plus Binance USDT-M
futures) on daily candles and reports liquidity sweeps / Swing Failure Patterns.

Logic per symbol (daily candles):
  * Find the most recent CONFIRMED swing low/high (strength N bars each side).
  * Level must still be intact (no candle CLOSED beyond it since the pivot).
  * Signal on the last closed candle:
      Bullish sweep : low  < swing_low  AND close > swing_low
      Bearish sweep : high > swing_high AND close < swing_high
Sends one consolidated Telegram message per run.

Env vars required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import time
import requests

# ── Config ─────────────────────────────────────────────────────────
SWING_STRENGTH   = 5           # bars each side to confirm a swing point
CANDLES          = 120         # candles of history to fetch (per timeframe)
MIN_QUOTE_VOLUME = 500_000     # skip pairs with < $500k 24h volume
QUOTE_ASSET      = "USDT"
SCAN_FUTURES     = True         # Binance USDT-M futures (geo-blocked on US runners)
REQUEST_PAUSE    = 0.08        # seconds between kline requests (rate-limit safety)

# Timeframe: daily / weekly / monthly. Set via TIMEFRAME env (1d/1w/1M or a
# friendly name); defaults to daily. Note: the volume filter always uses 24h
# quote volume for liquidity regardless of the scan timeframe.
_TF_ALIASES = {"1d": "1d", "d": "1d", "daily": "1d", "day": "1d",
               "1w": "1w", "w": "1w", "weekly": "1w", "week": "1w",
               "1M": "1M", "monthly": "1M", "month": "1M"}
TIMEFRAME = _TF_ALIASES.get(os.environ.get("TIMEFRAME", "1d").strip(), "1d")
TF_LABEL  = {"1d": "Daily", "1w": "Weekly", "1M": "Monthly"}[TIMEFRAME]

# Per-exchange interval / candle-type strings for the chosen timeframe.
BINANCE_INTERVAL = {"1d": "1d",   "1w": "1w",    "1M": "1M"}[TIMEFRAME]   # spot + futures
MEXC_INTERVAL    = {"1d": "1d",   "1w": "1W",    "1M": "1M"}[TIMEFRAME]   # MEXC uses "1W"
KUCOIN_TYPE      = {"1d": "1day", "1w": "1week", "1M": "1month"}[TIMEFRAME]

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


# ── Kline fetchers (return highs, lows, closes; ascending, closed only) ──
def binance_style_klines(base, path, symbol, interval):
    """Binance & MEXC share this exact kline format (works for 1d/1w/1M)."""
    data = get_json(f"{base}{path}",
                    params={"symbol": symbol, "interval": interval, "limit": CANDLES})
    now_ms = int(time.time() * 1000)
    closed = [k for k in data if int(k[6]) <= now_ms]    # k[6] = close time
    highs  = [float(k[2]) for k in closed]
    lows   = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    return highs, lows, closes


def _kucoin_candle_closed(start, now_s):
    """KuCoin candles give only a start time. Decide if the period has ended."""
    if TIMEFRAME == "1M":                                # calendar month (variable length)
        st, nw = time.gmtime(start), time.gmtime(now_s)
        return (st.tm_year, st.tm_mon) < (nw.tm_year, nw.tm_mon)
    dur = 86400 if TIMEFRAME == "1d" else 604800         # day / week: fixed duration
    return start + dur <= now_s


def kucoin_klines(symbol):
    """KuCoin candles are newest-first: [start, open, close, high, low, vol, turnover]."""
    data = get_json(f"{KUCOIN_BASE}/api/v1/market/candles",
                    params={"type": KUCOIN_TYPE, "symbol": symbol}).get("data") or []
    now_s = int(time.time())
    rows = [r for r in reversed(data)                    # -> ascending, closed only
            if _kucoin_candle_closed(int(r[0]), now_s)]
    highs  = [float(r[3]) for r in rows]
    lows   = [float(r[4]) for r in rows]
    closes = [float(r[2]) for r in rows]
    return highs, lows, closes


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


# ── Scan runner ────────────────────────────────────────────────────
def scan(name, symbols, fetch_klines):
    bulls, bears, errors = [], [], 0
    for i, sym in enumerate(symbols, 1):
        try:
            highs, lows, closes = fetch_klines(sym)
            signal = check_sweep(highs, lows, closes)
            if signal == "bull":
                bulls.append((sym, closes[-1]))
            elif signal == "bear":
                bears.append((sym, closes[-1]))
        except Exception:
            errors += 1
        if i % 50 == 0:
            print(f"[{name}] {i}/{len(symbols)} scanned...")
        time.sleep(REQUEST_PAUSE)
    print(f"[{name}] done: {len(bulls)} bull / {len(bears)} bear / {errors} errors")
    return bulls, bears, errors


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


def fmt_price(p):
    return f"{p:.8f}".rstrip("0").rstrip(".") if p < 1 else f"{p:,.4f}".rstrip("0").rstrip(".")


def fmt_section(title, rows):
    if not rows:
        return f"{title}: none"
    lines = [f"{title} ({len(rows)}):"]
    lines += [f"  • {sym} @ {fmt_price(px)}" for sym, px in rows]
    return "\n".join(lines)


# ── Exchange registry (spot) ───────────────────────────────────────
# (display name, symbol-listing fn, kline fn)
SPOT_EXCHANGES = [
    ("BINANCE", get_binance_spot_symbols,
     lambda s: binance_style_klines(SPOT_BASE, "/api/v3/klines", s, BINANCE_INTERVAL)),
    ("MEXC", get_mexc_symbols,
     lambda s: binance_style_klines(MEXC_BASE, "/api/v3/klines", s, MEXC_INTERVAL)),
    ("KUCOIN", get_kucoin_symbols, kucoin_klines),
]


# ── Main ───────────────────────────────────────────────────────────
def main():
    date_str = time.strftime("%d %b %Y", time.gmtime())
    print(f"Timeframe: {TF_LABEL} ({TIMEFRAME})")
    parts = [f"📊 {TF_LABEL} Sweep Scan — {date_str} ({TIMEFRAME} close)"]

    # Spot exchanges — one failing (e.g. geo-block) never blocks the others
    for display, list_symbols, fetch_klines in SPOT_EXCHANGES:
        try:
            syms = list_symbols()
            print(f"[{display}] symbols to scan: {len(syms)}")
            bulls, bears, _ = scan(display, syms, fetch_klines)
            parts.append(f"— {display} (SPOT) —")
            parts.append(fmt_section("🟢 Bullish sweeps", bulls))
            parts.append(fmt_section("🔴 Bearish sweeps", bears))
        except Exception as e:
            print(f"[{display}] scan failed: {type(e).__name__}: {e}")
            parts.append(f"⚠️ {display} spot scan unavailable ({type(e).__name__}).")

    # Binance USDT-M futures (may be geo-blocked on some runners)
    if SCAN_FUTURES:
        try:
            fut_syms = get_futures_symbols()
            print(f"[BINANCE FUT] symbols to scan: {len(fut_syms)}")
            f_bull, f_bear, _ = scan(
                "BINANCE FUT", fut_syms,
                lambda s: binance_style_klines(FUT_BASE, "/fapi/v1/klines", s, BINANCE_INTERVAL))
            parts.append("— BINANCE FUTURES (USDT-M) —")
            parts.append(fmt_section("🟢 Bullish sweeps", f_bull))
            parts.append(fmt_section("🔴 Bearish sweeps", f_bear))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            parts.append(f"⚠️ Binance futures unavailable (HTTP {code} — likely geo-block on runner). Spot results above are complete.")
        except Exception as e:
            parts.append(f"⚠️ Binance futures scan failed: {type(e).__name__}")

    send_telegram("\n\n".join(parts))
    print("Telegram message sent.")


if __name__ == "__main__":
    main()
