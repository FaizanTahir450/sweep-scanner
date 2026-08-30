"""
Liquidity Sweep (SFP) Scanner — Multi-Exchange, Multi-Timeframe
---------------------------------------------------------------
Scans liquid USDT spot pairs across several exchanges (plus Binance USDT-M
futures) on daily/weekly/monthly candles and reports liquidity sweeps / Swing
Failure Patterns. Each signal carries trade levels (entry/stop/target) and a
0-100 quality score, and is logged to signals.jsonl for later outcome
evaluation (see evaluate.py).

Logic per symbol:
  * Build ATR-filtered ZigZag swings: a low/high only counts as a swing once
    price has CLOSED away from it by ATR_MULT x ATR. Minor fractal wiggles
    never reach that threshold, so only significant levels — where stop-loss
    liquidity actually rests — survive (see detect_pivots).
  * Take the most recent confirmed swing whose level is still sweepable:
    no candle CLOSED beyond it since the pivot, and (with REQUIRE_UNTAPPED)
    no wick has traded through it either — the signal candle must be the
    FIRST to run the level. Broken levels fall through to older intact ones.
  * Signal on the last closed candle:
      Bullish sweep : low  < swing_low  AND close > swing_low
      Bearish sweep : high > swing_high AND close < swing_high
Sends one consolidated Telegram message per run.

Markets: MARKET=crypto (default: Binance/MEXC/KuCoin spot + Binance futures) or
MARKET=psx (Pakistan Stock Exchange equities, daily/weekly, official PSX Data
Portal + committed psx_cache/). Detection is identical for both.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required); MARKET, TIMEFRAME,
SIGNALS_LOG, TARGET_R, EVAL_HORIZON, MIN_SCORE, ATR_MULT, REQUIRE_UNTAPPED,
PSX_MIN_TURNOVER (optional).
"""

import os
import json
import time
from collections import namedtuple

import re
from datetime import datetime, date, timedelta, timezone

import requests

# ── Config ─────────────────────────────────────────────────────────
ATR_PERIOD       = 14          # ATR lookback used by the swing filter
ATR_MULT         = float(os.environ.get("ATR_MULT", "2.0"))
                               # close-reversal (in ATRs) needed to confirm a swing:
                               # 1.5 = more/smaller swings · 2.5-3 = only major ones
REQUIRE_UNTAPPED = os.environ.get("REQUIRE_UNTAPPED", "1") != "0"
                               # True → only FIRST-time sweeps (no prior wick through level)
CANDLES          = 120         # candles of history to fetch (per timeframe)
MIN_QUOTE_VOLUME = 500_000     # skip pairs with < $500k 24h volume
QUOTE_ASSET      = "USDT"
SCAN_FUTURES     = True         # Binance USDT-M futures (geo-blocked on US runners)
REQUEST_PAUSE    = 0.08        # seconds between kline requests (rate-limit safety)

# Market: "crypto" (default) scans the crypto exchanges below; "psx" scans
# Pakistan Stock Exchange equities (daily/weekly). Same detection logic either
# way — only the data adapter, session calendar and message label differ.
MARKET = os.environ.get("MARKET", "crypto").strip().lower()

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
    if exchange == "PSX":
        return psx_klines(symbol, timeframe)
    if exchange == "BINANCE FUT":
        return binance_style_klines(FUT_BASE, "/fapi/v1/klines", symbol, BINANCE_INTERVALS[timeframe])
    raise ValueError(f"unknown exchange: {exchange}")


# ── PSX (Pakistan Stock Exchange) adapter ──────────────────────────
# Data: the official PSX Data Portal (dps.psx.com.pk) — free, no key, raw
# (unadjusted) EOD OHLCV via POST /historical, ONE MONTH per request. To keep
# runs fast a per-symbol CSV history cache (psx_cache/<SYMBOL>.csv, committed
# back by the Action) is maintained; each run fetches only the current month
# (plus any months the cache is missing). SCSTrade (identical numbers, whole
# history in one call) is the automatic fallback and the one-time backfill
# source. PSX trades Mon-Fri; weekly candles are aggregated here from daily
# bars (Mon-Fri weeks). Monthly is not supported for PSX.
PSX_BASE  = "https://dps.psx.com.pk"
SCS_BASE  = "https://www.scstrade.com"
PSX_CACHE = os.environ.get("PSX_CACHE_DIR", "psx_cache")
PSX_KEEP  = 650                      # daily rows kept per symbol (~2.6y ≈ 120 weekly candles)
PSX_MIN_TURNOVER = float(os.environ.get("PSX_MIN_TURNOVER", "0"))  # PKR 20d avg close×volume; 0 = every equity
PKT = timezone(timedelta(hours=5))
PSX_STATE = {"latest_date": None}    # newest session date seen this run (holiday / staleness guard)
# Non-equity instruments that share the equity list: rights "(R)/(Right)", preference
# shares "(Pref)/(PRS)", participation term certificates "(Ptc)". Matched on name.
_PSX_NON_EQUITY = re.compile(r"\((?:r\d*|right|prs|ptc|[^)]*pref[^)]*)\)", re.I)
_PSX_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_PSX_TD = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)


def _pkt_now():
    return datetime.now(PKT)


def _date_ms(d):
    """'YYYY-MM-DD' -> ms at 00:00 UTC — the candle id used in the signal log."""
    y, m, dd = (int(x) for x in d.split("-"))
    return int(datetime(y, m, dd, tzinfo=timezone.utc).timestamp() * 1000)


def _psx_cache_path(sym):
    return os.path.join(PSX_CACHE, f"{sym}.csv")


def _psx_cache_read(sym):
    """Cached daily rows (date, o, h, l, c, v), ascending."""
    p = _psx_cache_path(sym)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        next(f, None)                                    # header
        for line in f:
            c = line.strip().split(",")
            if len(c) == 6:
                out.append((c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4]), int(float(c[5]))))
    return out


def _fmt_px(x):
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _psx_cache_write(sym, rows):
    os.makedirs(PSX_CACHE, exist_ok=True)
    with open(_psx_cache_path(sym), "w", encoding="utf-8", newline="") as f:
        f.write("date,open,high,low,close,volume\n")
        for d, o, h, l, c, v in rows[-PSX_KEEP:]:
            f.write(f"{d},{_fmt_px(o)},{_fmt_px(h)},{_fmt_px(l)},{_fmt_px(c)},{v}\n")


def _psx_portal_month(sym, year, month):
    """Official portal: one month of EOD OHLCV (HTML table) -> rows."""
    r = session.post(f"{PSX_BASE}/historical",
                     data={"month": month, "year": year, "symbol": sym}, timeout=30)
    r.raise_for_status()
    rows = []
    for tr in _PSX_TR.findall(r.text):
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in _PSX_TD.findall(tr)]
        if len(c) < 6 or c[0].upper() == "DATE":
            continue
        try:
            d = datetime.strptime(c[0], "%b %d, %Y").date().isoformat()
            o, h, l, cl = (float(x.replace(",", "")) for x in c[1:5])
            v = int(float(c[5].replace(",", "") or 0))
        except ValueError:
            continue
        if cl > 0:
            rows.append((d, o, h, l, cl, v))
    return rows


def _psx_scs_history(sym, start="01/01/2023"):
    """SCSTrade fallback/backfill: whole history in one call -> rows (PKT dates)."""
    end = (_pkt_now() + timedelta(days=1)).strftime("%m/%d/%Y")
    r = session.post(f"{SCS_BASE}/stockscreening/SS_CompanySnapShotHP.aspx/chart",
                     json={"par": sym, "date1": start, "date2": end},
                     headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    rows = []
    for x in r.json().get("d") or []:
        ms = int(re.search(r"-?\d+", x["trading_Date"]).group())
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(PKT).date().isoformat()
        o, h, l, c, v = (x.get("trading_open"), x.get("trading_high"), x.get("trading_low"),
                         x.get("trading_close"), x.get("trading_vol"))
        if None in (o, h, l, c) or c <= 0:
            continue
        rows.append((d, float(o), float(h), float(l), float(c), int(v or 0)))
    return rows


def _psx_update_cache(sym):
    """Merge fresh data into the symbol's cache. Returns daily rows, ascending."""
    rows = {r[0]: r for r in _psx_cache_read(sym)}
    today = _pkt_now().date()
    last = max(rows) if rows else None
    fresh = []
    try:
        if last and (today - date.fromisoformat(last)).days <= 100:
            # portal: every month from the last cached month up to the current one (≤4)
            y, m = today.year, today.month
            ly, lm = int(last[:4]), int(last[5:7])
            months = []
            while (y, m) >= (ly, lm) and len(months) < 4:
                months.append((y, m))
                m -= 1
                if m == 0:
                    y, m = y - 1, 12
            for yy, mm in months:
                fresh += _psx_portal_month(sym, yy, mm)
        else:                                            # empty/stale cache: full backfill
            fresh = _psx_scs_history(sym)
            if not fresh:                                # not on SCSTrade: last 3 months from the portal
                y, m = today.year, today.month
                for _ in range(3):
                    fresh += _psx_portal_month(sym, y, m)
                    m -= 1
                    if m == 0:
                        y, m = y - 1, 12
    except Exception:
        try:
            fresh = _psx_scs_history(sym)
        except Exception:
            fresh = []
    changed = False
    for r in fresh:
        old = rows.get(r[0])
        if old is None or any(abs(a - b) > 1e-4 for a, b in zip(old[1:5], r[1:5])) or old[5] != r[5]:
            rows[r[0]] = r
            changed = True
    out = [rows[k] for k in sorted(rows)][-PSX_KEEP:]
    if changed:
        _psx_cache_write(sym, out)
    return out


def get_psx_symbols():
    """All listed PSX equities (no debt instruments / ETFs); optional turnover floor."""
    syms = get_json(f"{PSX_BASE}/symbols")
    eq = sorted(x["symbol"] for x in syms
                if not x.get("isDebt") and not x.get("isETF")
                and not _PSX_NON_EQUITY.search(x.get("name") or ""))
    if PSX_MIN_TURNOVER > 0:
        keep = []
        for s in eq:
            recent = _psx_cache_read(s)[-20:]
            if recent and sum(r[4] * r[5] for r in recent) / len(recent) >= PSX_MIN_TURNOVER:
                keep.append(s)
        eq = keep
    return eq


def psx_klines(symbol, timeframe=TIMEFRAME):
    """Klines for a PSX equity: daily rows from the cache (+fresh month), or Mon-Fri weekly aggregates."""
    if timeframe not in ("1d", "1w"):
        raise ValueError("PSX supports 1d / 1w only")
    rows = _psx_update_cache(symbol)
    now = _pkt_now()
    today = now.date().isoformat()
    if rows and rows[-1][0] == today and (now.hour, now.minute) < (16, 45):
        rows = rows[:-1]                                 # session not over yet — treat as forming
    if rows:
        PSX_STATE["latest_date"] = max(PSX_STATE["latest_date"] or "", rows[-1][0])
    if timeframe == "1d":
        return Klines(highs=[r[2] for r in rows], lows=[r[3] for r in rows],
                      closes=[r[4] for r in rows], opens=[_date_ms(r[0]) for r in rows],
                      oprices=[r[1] for r in rows], vols=[r[5] for r in rows])
    weeks, order = {}, []
    for d, o, h, l, c, v in rows:
        dd = date.fromisoformat(d)
        key = dd.isocalendar()[:2]
        if key not in weeks:
            weeks[key] = [(dd - timedelta(days=dd.weekday())).isoformat(), o, h, l, c, v]
            order.append(key)
        else:
            w = weeks[key]
            w[2] = max(w[2], h); w[3] = min(w[3], l); w[4] = c; w[5] += v
    wk = [weeks[k] for k in order]
    if wk:                                               # drop the forming week (closed after Fri 17:00 PKT)
        mon = date.fromisoformat(wk[-1][0])
        fri_close = datetime(mon.year, mon.month, mon.day, 17, 0, tzinfo=PKT) + timedelta(days=4)
        if now < fri_close:
            wk = wk[:-1]
    return Klines(highs=[w[2] for w in wk], lows=[w[3] for w in wk], closes=[w[4] for w in wk],
                  opens=[_date_ms(w[0]) for w in wk], oprices=[w[1] for w in wk], vols=[w[5] for w in wk])


def psx_session_check():
    """(has_new_candle, wanted_candle_ts, note) for this run, from the newest session seen."""
    latest = PSX_STATE["latest_date"]
    now = _pkt_now()
    today = now.date()
    if not latest:
        return False, None, "⚠️ PSX data unavailable this run (portal and fallback both failed)."
    ld = date.fromisoformat(latest)
    if TIMEFRAME == "1d":
        if ld != today:
            return False, None, f"PSX market closed today (last session {ld:%d %b %Y}) — no new daily candle."
        return True, _date_ms(latest), ""
    mon_latest = ld - timedelta(days=ld.weekday())
    mon_today = today - timedelta(days=today.weekday())
    fri_close = datetime(mon_today.year, mon_today.month, mon_today.day, 17, 0, tzinfo=PKT) + timedelta(days=4)
    if mon_latest != mon_today or now < fri_close:
        return False, None, f"No completed PSX week to scan yet (last session {ld:%d %b %Y})."
    return True, _date_ms(mon_latest.isoformat()), ""


# ── Swing detection (ATR-filtered ZigZag — real pivots, not noise) ──
def compute_atr(highs, lows, closes, period=ATR_PERIOD):
    """Average True Range (simple rolling mean of TR); one value per candle."""
    n = len(closes)
    trs = [highs[0] - lows[0]] if n else []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    atr, s = [], 0.0
    for i, tr in enumerate(trs):
        s += tr
        if i >= period:
            s -= trs[i - period]
            atr.append(s / period)
        else:
            atr.append(s / (i + 1))
    return atr


def detect_pivots(highs, lows, closes, atr_mult=None, atr=None):
    """
    ZigZag swing detector with an ATR noise filter.

    While price keeps making higher highs, the candidate swing high keeps
    extending — so the TRUE extreme is always captured, never an early bump.
    The candidate is only CONFIRMED as a pivot once price CLOSES against it
    by atr_mult * ATR; smaller wiggles never confirm and are ignored.
    Confirming on the CLOSE (not the opposite wick) stops a single huge-range
    candle from faking two pivots at once. Pivots alternate H, L, H, L.

    Returns a time-ordered list of dicts {idx, price, type 'H'/'L', confirmed},
    where `confirmed` is the bar at which the pivot became known (no lookahead).
    """
    n = len(closes)
    if n < 3:
        return []
    if atr is None:
        atr = compute_atr(highs, lows, closes)
    if atr_mult is None:
        atr_mult = ATR_MULT

    piv = []
    trend = 0                     # 0 = warm-up, +1 = up-leg, -1 = down-leg
    cand = hi_i = lo_i = 0        # candidate extreme / warm-up running extremes

    for i in range(1, n):
        if trend == 0:
            # warm-up: wait for the first threshold-sized move
            if highs[i] > highs[hi_i]:
                hi_i = i
            if lows[i] < lows[lo_i]:
                lo_i = i
            if closes[i] - lows[lo_i] >= atr_mult * atr[i]:
                piv.append({"idx": lo_i, "price": lows[lo_i], "type": "L", "confirmed": i})
                seg = range(lo_i + 1, i + 1)
                cand = max(seg, key=lambda j: highs[j]) if len(seg) else i
                trend = 1
            elif highs[hi_i] - closes[i] >= atr_mult * atr[i]:
                piv.append({"idx": hi_i, "price": highs[hi_i], "type": "H", "confirmed": i})
                seg = range(hi_i + 1, i + 1)
                cand = min(seg, key=lambda j: lows[j]) if len(seg) else i
                trend = -1
        elif trend == 1:                                   # tracking a swing HIGH
            if highs[i] > highs[cand]:
                cand = i                                   # higher high → extend
            elif highs[cand] - closes[i] >= atr_mult * atr[i]:
                piv.append({"idx": cand, "price": highs[cand], "type": "H", "confirmed": i})
                cand = min(range(cand + 1, i + 1), key=lambda j: lows[j])
                trend = -1
        else:                                              # tracking a swing LOW
            if lows[i] < lows[cand]:
                cand = i                                   # lower low → extend
            elif closes[i] - lows[cand] >= atr_mult * atr[i]:
                piv.append({"idx": cand, "price": lows[cand], "type": "L", "confirmed": i})
                cand = max(range(cand + 1, i + 1), key=lambda j: highs[j])
                trend = 1
    return piv


def last_intact_swing(pivots, highs, lows, closes, kind):
    """
    Most recent confirmed swing low/high whose level is still sweepable:
      * confirmed BEFORE the last candle (no lookahead);
      * no candle CLOSED beyond it since the pivot (up to, excluding, last);
      * with REQUIRE_UNTAPPED, no wick traded through it either — the signal
        candle must be the FIRST to run the level (equal touches allowed).
    Unlike the old fractal logic, a broken nearer level falls through to the
    next older swing: a deeper low can still hold untouched liquidity.
    Returns (price, idx) or (None, None).
    """
    last = len(closes) - 1
    want = "L" if kind == "low" else "H"
    for p in reversed(pivots):
        if p["type"] != want or p["confirmed"] >= last:
            continue
        i, v = p["idx"], p["price"]
        if kind == "low":
            if any(c <= v for c in closes[i + 1:last]):
                continue                                   # level broken by a close
            if REQUIRE_UNTAPPED and any(lo < v for lo in lows[i + 1:last]):
                continue                                   # already swept once
        else:
            if any(c >= v for c in closes[i + 1:last]):
                continue
            if REQUIRE_UNTAPPED and any(hi > v for hi in highs[i + 1:last]):
                continue
        return v, i
    return None, None


# ── Sweep detection + trade levels (built on the ZigZag swings) ─────
def analyze_sweep(highs, lows, closes):
    """
    Detect a sweep of the nearest intact ZigZag swing on the last closed
    candle. Returns a dict with direction, the swept level, entry (close),
    stop (the sweep wick extreme), target (TARGET_R multiple of risk) plus
    scoring metadata (level_idx, atr), or None.
    """
    n = len(closes)
    if n < ATR_PERIOD + 10:
        return None
    last = n - 1
    atr = compute_atr(highs, lows, closes)
    pivots = detect_pivots(highs, lows, closes, atr=atr)
    entry = closes[last]

    level, pidx = last_intact_swing(pivots, highs, lows, closes, "low")
    if level is not None and lows[last] < level and closes[last] > level:
        stop = lows[last]                        # below the swept low
        risk = entry - stop
        if risk > 0:
            return {"direction": "bull", "level": level, "entry": entry,
                    "stop": stop, "target": entry + TARGET_R * risk,
                    "level_idx": pidx, "atr": atr[last]}

    level, pidx = last_intact_swing(pivots, highs, lows, closes, "high")
    if level is not None and highs[last] > level and closes[last] < level:
        stop = highs[last]                       # above the swept high
        risk = stop - entry
        if risk > 0:
            return {"direction": "bear", "level": level, "entry": entry,
                    "stop": stop, "target": entry - TARGET_R * risk,
                    "level_idx": pidx, "atr": atr[last]}
    return None


def check_sweep(highs, lows, closes):
    """Return 'bull', 'bear', or None for the last closed candle."""
    sig = analyze_sweep(highs, lows, closes)
    return sig["direction"] if sig else None


def score_signal(sig, k):
    """
    0-100 quality score for a detected signal — pure ranking metadata, never
    changes detection. Blends: volume spike, rejection wick, close location,
    reclaim distance beyond the level (in ATRs), and the size of the swept
    swing (in ATRs — a bigger swing means more resting liquidity under it).
    Returns (score, reason).
    """
    highs, lows, closes, oprices, vols = k.highs, k.lows, k.closes, k.oprices, k.vols
    i = len(closes) - 1
    hi, lo, cl, op = highs[i], lows[i], closes[i], oprices[i]
    rng = (hi - lo) or 1e-9
    a = sig.get("atr") or 1e-9
    level, pidx = sig["level"], sig["level_idx"]

    prior = vols[max(0, i - VOL_LOOKBACK):i]
    avg_v = (sum(prior) / len(prior)) if prior else 0.0
    v_ratio = (vols[i] / avg_v) if avg_v > 0 else 1.0

    if sig["direction"] == "bull":
        close_loc = (cl - lo) / rng              # 1 = closed at the high
        rej = (min(op, cl) - lo) / rng           # lower-wick fraction (rejection of low)
        reclaim = (cl - level) / a               # how far back ABOVE the level (ATRs)
        swing = (max(highs[pidx:i]) - level) / a # size of the swept swing (ATRs)
    else:
        close_loc = (hi - cl) / rng              # 1 = closed at the low
        rej = (hi - max(op, cl)) / rng           # upper-wick fraction (rejection of high)
        reclaim = (level - cl) / a
        swing = (level - min(lows[pidx:i])) / a

    s_vol = min(v_ratio / 2.0, 1.0)              # 2x average volume → full marks
    s_rej = min(max(rej, 0.0) / 0.5, 1.0)        # 50% wick → full marks
    s_loc = min(max(close_loc, 0.0), 1.0)        # closes at extreme → full marks
    s_rec = min(max(reclaim, 0.0) / 0.5, 1.0)    # closed 0.5 ATR beyond level → full marks
    s_swg = min(max(swing, 0.0) / 4.0, 1.0)      # 4 ATR swing → full marks
    score = round(100 * (0.30 * s_vol + 0.25 * s_rej + 0.15 * s_loc
                         + 0.15 * s_rec + 0.15 * s_swg))
    reason = (f"vol {v_ratio:.1f}x·rej {rej:.2f}·loc {close_loc:.2f}"
              f"·rcl {reclaim:.2f}A·swg {swing:.1f}A")
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
                score, reason = score_signal(sig, k)
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
    """Simple 'SYMBOL @ price' + ⭐rank — used for the Telegram message."""
    if not sigs:
        return f"{title}: none"
    sigs = sorted(sigs, key=lambda s: s.get("score", 0), reverse=True)  # best-first
    lines = [f"{title} ({len(sigs)}):"]
    lines += [f"  • ⭐{s.get('score', 0)} {s['symbol']} @ {fmt_price(s['entry'])}" for s in sigs]
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
CRYPTO_EXCHANGES = [
    ("BINANCE", get_binance_spot_symbols, lambda s: klines_for("BINANCE", s, TIMEFRAME)),
    ("MEXC",    get_mexc_symbols,         lambda s: klines_for("MEXC", s, TIMEFRAME)),
    ("KUCOIN",  get_kucoin_symbols,       lambda s: klines_for("KUCOIN", s, TIMEFRAME)),
]
PSX_EXCHANGES = [
    ("PSX", get_psx_symbols, lambda s: klines_for("PSX", s, TIMEFRAME)),
]
SPOT_EXCHANGES = PSX_EXCHANGES if MARKET == "psx" else CRYPTO_EXCHANGES


# ── Main ───────────────────────────────────────────────────────────
def main():
    date_str = time.strftime("%d %b %Y", time.gmtime())
    print(f"Timeframe: {TF_LABEL} ({TIMEFRAME}) | MIN_SCORE={MIN_SCORE}")
    if MARKET == "psx" and TIMEFRAME not in ("1d", "1w"):
        print("PSX supports daily/weekly only — nothing to do.")
        return
    market = "PSX " if MARKET == "psx" else ""
    hdr = f"📊 {market}{TF_LABEL} Sweep Scan — {date_str} ({TIMEFRAME} close)"
    if MIN_SCORE:
        hdr += f"  [min score {MIN_SCORE}]"
    blocks = []           # collected once, rendered twice (Telegram + archive)
    all_signals = []

    # Spot exchanges — one failing (e.g. geo-block) never blocks the others
    kind = "EQUITIES" if MARKET == "psx" else "SPOT"
    for display, list_symbols, fetch_klines in SPOT_EXCHANGES:
        try:
            syms = list_symbols()
            print(f"[{display}] symbols to scan: {len(syms)}")
            sigs, _ = scan(display, syms, fetch_klines)
            all_signals += sigs
            blocks.append({"label": f"{display} ({kind})",
                           "bulls": [s for s in sigs if s["direction"] == "bull"],
                           "bears": [s for s in sigs if s["direction"] == "bear"]})
        except Exception as e:
            print(f"[{display}] scan failed: {type(e).__name__}: {e}")
            blocks.append({"note": f"⚠️ {display} spot scan unavailable ({type(e).__name__})."})

    # PSX: only report the just-closed session/week; on holidays send a one-line note
    if MARKET == "psx":
        ok, want_ts, note = psx_session_check()
        if not ok:
            print(note)
            send_telegram(f"{hdr}\n\n{note}")
            append_archive(f"{hdr}\n\n{note}")
            return
        for b in blocks:
            if "bulls" in b:
                b["bulls"] = [s for s in b["bulls"] if s["candle_ts"] == want_ts]
                b["bears"] = [s for s in b["bears"] if s["candle_ts"] == want_ts]
        all_signals = [s for s in all_signals if s["candle_ts"] == want_ts]

    # Binance USDT-M futures (may be geo-blocked on some runners)
    if SCAN_FUTURES and MARKET == "crypto":
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
