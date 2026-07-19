"""
Daily Liquidity Sweep (SFP) Scanner — Binance Spot + USDT-M Futures
-------------------------------------------------------------------
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
CANDLES          = 120         # daily candles of history to fetch
MIN_QUOTE_VOLUME = 500_000     # skip pairs with < $500k 24h volume
QUOTE_ASSET      = "USDT"
SCAN_FUTURES     = True
REQUEST_PAUSE    = 0.08        # seconds between kline requests (rate-limit safety)

SPOT_BASE = "https://data-api.binance.vision"   # geo-unrestricted public data mirror
FUT_BASE  = "https://fapi.binance.com"          # may be geo-blocked on some runners

# Optional proxy for FUTURES requests only (spot mirror already works from any
# region). Set FUTURES_PROXY to an http/https/socks5 URL pointing at an exit in
# a Binance-allowed region, e.g. "http://user:pass@host:port" or
# "socks5://user:pass@host:port". Provide it ONLY via env var / GitHub secret —
# it may contain credentials, so never hardcode it. Empty → futures go direct.
FUTURES_PROXY = os.environ.get("FUTURES_PROXY", "").strip()
FUT_PROXIES = {"http": FUTURES_PROXY, "https": FUTURES_PROXY} if FUTURES_PROXY else None

# Leveraged tokens & stablecoin bases we don't want to scan
EXCLUDE_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR",
                "EURI", "PAXG", "XUSD", "USDE", "USD1"}

session = requests.Session()
session.headers.update({"User-Agent": "sweep-scanner/1.0"})


# ── Helpers ────────────────────────────────────────────────────────
def get_json(url, params=None, timeout=20, proxies=None):
    r = session.get(url, params=params, timeout=timeout, proxies=proxies)
    r.raise_for_status()
    return r.json()


def get_spot_symbols():
    """Return list of active spot USDT symbols passing the volume filter."""
    info = get_json(f"{SPOT_BASE}/api/v3/exchangeInfo")
    tickers = get_json(f"{SPOT_BASE}/api/v3/ticker/24hr")
    vol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}

    symbols = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s.get("status") == "TRADING"
                and s.get("quoteAsset") == QUOTE_ASSET
                and s.get("isSpotTradingAllowed", True)
                and not sym.endswith(EXCLUDE_SUFFIXES)
                and s.get("baseAsset") not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            symbols.append(sym)
    return sorted(symbols)


def get_futures_symbols():
    """Return list of active USDT-M perpetual symbols passing the volume filter."""
    info = get_json(f"{FUT_BASE}/fapi/v1/exchangeInfo", proxies=FUT_PROXIES)
    tickers = get_json(f"{FUT_BASE}/fapi/v1/ticker/24hr", proxies=FUT_PROXIES)
    vol = {t["symbol"]: float(t.get("quoteVolume", 0)) for t in tickers}

    symbols = []
    for s in info["symbols"]:
        sym = s["symbol"]
        if (s.get("status") == "TRADING"
                and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == QUOTE_ASSET
                and s.get("baseAsset") not in STABLE_BASES
                and vol.get(sym, 0) >= MIN_QUOTE_VOLUME):
            symbols.append(sym)
    return sorted(symbols)


def get_klines(base, path, symbol, proxies=None):
    """Fetch daily klines and drop the still-forming candle."""
    data = get_json(f"{base}{path}",
                    params={"symbol": symbol, "interval": "1d", "limit": CANDLES},
                    proxies=proxies)
    now_ms = int(time.time() * 1000)
    closed = [k for k in data if int(k[6]) <= now_ms]   # k[6] = close time
    highs  = [float(k[2]) for k in closed]
    lows   = [float(k[3]) for k in closed]
    closes = [float(k[4]) for k in closed]
    return highs, lows, closes


# ── Sweep detection ────────────────────────────────────────────────
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


# ── Scan runners ───────────────────────────────────────────────────
def scan_market(name, base, kline_path, symbols, proxies=None):
    bulls, bears, errors = [], [], 0
    for i, sym in enumerate(symbols, 1):
        try:
            highs, lows, closes = get_klines(base, kline_path, sym, proxies=proxies)
            signal = check_sweep(highs, lows, closes)
            if signal == "bull":
                bulls.append((sym, closes[-1]))
            elif signal == "bear":
                bears.append((sym, closes[-1]))
        except Exception:
            errors += 1
        if i % 25 == 0:
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


# ── Main ───────────────────────────────────────────────────────────
def main():
    date_str = time.strftime("%d %b %Y", time.gmtime())
    parts = [f"📊 Daily Sweep Scan — {date_str} (1D close)"]

    # Spot
    spot_syms = get_spot_symbols()
    print(f"Spot symbols to scan: {len(spot_syms)}")
    s_bull, s_bear, _ = scan_market("SPOT", SPOT_BASE, "/api/v3/klines", spot_syms)
    parts.append("— SPOT —")
    parts.append(fmt_section("🟢 Bullish sweeps", s_bull))
    parts.append(fmt_section("🔴 Bearish sweeps", s_bear))

    # Futures (may be geo-blocked on some runners)
    if SCAN_FUTURES:
        try:
            fut_syms = get_futures_symbols()
            print(f"Futures symbols to scan: {len(fut_syms)} "
                  f"(proxy: {'on' if FUT_PROXIES else 'off'})")
            f_bull, f_bear, _ = scan_market("FUT", FUT_BASE, "/fapi/v1/klines",
                                            fut_syms, proxies=FUT_PROXIES)
            parts.append("— FUTURES (USDT-M) —")
            parts.append(fmt_section("🟢 Bullish sweeps", f_bull))
            parts.append(fmt_section("🔴 Bearish sweeps", f_bear))
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            hint = ("check FUTURES_PROXY region/credentials" if FUT_PROXIES
                    else "likely geo-block on runner — set FUTURES_PROXY")
            parts.append(f"⚠️ Futures scan unavailable (HTTP {code} — {hint}). Spot results above are complete.")
        except Exception as e:
            parts.append(f"⚠️ Futures scan failed: {type(e).__name__}")

    send_telegram("\n\n".join(parts))
    print("Telegram message sent.")


if __name__ == "__main__":
    main()
