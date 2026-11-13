#!/usr/bin/env python3
"""
india-stock-signals: breakout / candlestick signal scanner for Indian stocks (daily candles, swing horizon).

Pipeline
  1. Universe   : NSE index lists (nifty50, next50, nifty100, nifty200, nifty500, midcap100, bank, it)
                  or any symbols you pass with --symbols
  2. Prices     : 2 years of daily OHLCV from Yahoo Finance (free, NSE .NS tickers; Yahoo's BSE history is
                  too patchy to scan, and every large BSE stock is also listed on NSE)
  3. Technicals : every stock gets a confluence score from
                    - triggers : 20/55-day & 52-week breakouts, Bollinger squeeze breakouts, candlestick patterns
                                 (engulfing, hammer / shooting star, morning / evening star, piercing / dark cloud,
                                 marubozu, three soldiers / crows), MACD cross, SMA 50 reclaim, golden / death cross
                    - confirmation : relative volume, SMA 50/200 trend, ADX, RSI, where the candle closed
                    - penalties : thin volume, overextension, exhausted RSI, counter-trend
                  A stock is listed only when it has a fresh trigger, volume >= 1.2x the 20-day average and
                  score >= MIN_SCORE.
  4. Levels     : entry = last close, stop under the breakout level / pattern low (1 to 2.5 ATR), target = 2x risk
  5. News       : Google News RSS for the stock (7 days) and its sector (3 days); headlines get a simple keyword
                  sentiment and event flags (results, regulator, rating changes ...). It is a pointer to read the
                  headlines, not a verdict.

Usage
  python signal_scanner.py                          # Nifty 50 ("top 50"), strong signals only
  python signal_scanner.py --batch 2                # Nifty Next 50 ("next 50")
  python signal_scanner.py --universe nifty500
  python signal_scanner.py --symbols RELIANCE,TCS,INFY
  python signal_scanner.py --json                   # machine-readable
  python signal_scanner.py --min-score 5 --no-news

Not investment advice. Signals are mechanical pattern matches on delayed free data.
"""
import argparse
import csv
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

__version__ = "1.0.0"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "india-stock-signals"
IST = ZoneInfo("Asia/Kolkata")
_NSE = "https://nsearchives.nseindia.com/content/indices/ind_{}list.csv"
UNIVERSES = {
    "nifty50": ("Nifty 50", _NSE.format("nifty50")),
    "next50": ("Nifty Next 50", _NSE.format("niftynext50")),
    "nifty100": ("Nifty 100", _NSE.format("nifty100")),
    "nifty200": ("Nifty 200", _NSE.format("nifty200")),
    "nifty500": ("Nifty 500", _NSE.format("nifty500")),
    "midcap100": ("Nifty Midcap 100", _NSE.format("niftymidcap100")),
    "bank": ("Nifty Bank", _NSE.format("niftybank")),
    "it": ("Nifty IT", _NSE.format("niftyit")),
}
BATCHES = {1: "nifty50", 2: "next50"}      # "top 50" first, "next 50" on request
SUFFIX = ".NS"                             # Yahoo Finance suffix for NSE
MIN_SCORE = 7          # "strong" threshold
MIN_RVOL = 1.2         # breakout / pattern must come with above-average volume
REWARD_R = 2.0         # target = 2x risk
UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------- universe
def load_universe(universe: str) -> tuple[str, list[dict]]:
    if universe not in UNIVERSES:
        raise ValueError(f"Unknown universe '{universe}'. Choose from: {', '.join(UNIVERSES)}")
    name, url = UNIVERSES[universe]
    cache = CACHE_DIR / f"{universe}.csv"
    text = None
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        if "Symbol" in r.text[:200]:
            text = r.text
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
    except (requests.RequestException, OSError):
        pass
    if text is None:
        if not cache.exists():
            raise RuntimeError(f"Could not download the {name} list from NSE and there is no cached copy")
        text = cache.read_text()
    rows = csv.DictReader(io.StringIO(text))
    return name, [{"symbol": r["Symbol"].strip(), "company": r["Company Name"].strip(),
                   "industry": (r.get("Industry") or "").strip()} for r in rows if r.get("Symbol")]


# ---------------------------------------------------------------- prices
def download_daily(tickers: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """{ticker: daily OHLCV}. Yahoo sometimes leaves the latest session's Close empty for NSE; that close is
    then taken from the last 5-minute candle of the same day."""
    import yfinance as yf
    kw = dict(group_by="ticker", auto_adjust=False, progress=False, threads=True)
    raw = yf.download(tickers, period=period, interval="1d", **kw)
    out = {}
    for t in tickers:
        try:
            sub = raw[t].dropna(subset=["Open", "High", "Low"]).copy()
        except KeyError:
            continue
        if len(sub):
            out[t] = sub
    holes = [t for t, sub in out.items() if pd.isna(sub["Close"].iloc[-1])]
    if holes:
        try:
            intra = yf.download(holes, period="1d", interval="5m", **kw)
        except Exception:
            intra = None
        for t in holes:
            try:
                last = intra[t]["Close"].dropna()
                if len(last) and last.index[-1].date() == out[t].index[-1].date():
                    out[t].loc[out[t].index[-1], "Close"] = float(last.iloc[-1])
            except Exception:
                pass
    return {t: sub.dropna(subset=["Close"]) for t, sub in out.items()}


# ---------------------------------------------------------------- indicators
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df: daily Open/High/Low/Close/Volume. Returns one row per day with everything the scorer needs.
    All 'level' columns (hi20, lo55, vol20 ...) use only PRIOR days, so a comparison with today's close is fair."""
    o, h, l, c, v = (df[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    x = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "v": v})
    pc = c.shift(1)
    for n in (20, 50, 200):
        x[f"sma{n}"] = c.rolling(n).mean()

    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    x["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    x["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()

    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    x["atr"] = atr
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0).ewm(alpha=1 / 14, adjust=False).mean()
    ndm = dn.where((dn > up) & (dn > 0), 0.0).ewm(alpha=1 / 14, adjust=False).mean()
    pdi, ndi = 100 * pdm / atr, 100 * ndm / atr
    x["adx"] = (100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)).ewm(alpha=1 / 14, adjust=False).mean()
    x["di"] = np.sign(pdi - ndi)

    sd = c.rolling(20).std(ddof=0)
    x["bb_up"], x["bb_lo"] = x["sma20"] + 2 * sd, x["sma20"] - 2 * sd
    # was the band unusually narrow (bottom 20% of the last 6 months) just before today?
    x["squeeze"] = ((4 * sd / x["sma20"]).rolling(120).rank(pct=True).shift(1) <= 0.20)

    x["rvol"] = v / v.shift(1).rolling(20).mean()
    for n in (20, 55):
        x[f"hi{n}"] = h.shift(1).rolling(n).max()
        x[f"lo{n}"] = l.shift(1).rolling(n).min()
    x["hi252"] = h.shift(1).rolling(250, min_periods=200).max()
    x["lo252"] = l.shift(1).rolling(250, min_periods=200).min()

    # ---- candlestick patterns
    rng = (h - l).replace(0, np.nan)
    body = (c - o).abs()
    green, red = c > o, c < o
    upw, loww = h - np.maximum(c, o), np.minimum(c, o) - l
    o1, c1, body1, green1, red1 = o.shift(1), c.shift(1), body.shift(1), green.shift(1, fill_value=False), red.shift(1, fill_value=False)
    o2, c2, body2 = o.shift(2), c.shift(2), body.shift(2)
    x["clv"] = (c - l) / rng                                   # where the day closed inside its range (1 = high)
    after_dip = l.rolling(3).min() <= l.rolling(10).min()      # 10-day low made within the last 3 candles
    after_rally = h.rolling(3).max() >= h.rolling(10).max()

    bull = [
        (after_dip & red1 & green & (o <= c1) & (c >= o1) & (body > body1), "Bullish engulfing", 2),
        (after_dip & (c2 < o2) & (body2 > 0.6 * atr) & (body1 < 0.35 * body2) & green & (c > (o2 + c2) / 2), "Morning star", 2),
        (green & green1 & (c.shift(2) > o.shift(2)) & (c > c1) & (c1 > c2) & (body > 0.5 * atr) & (body1 > 0.5 * atr) & (body2 > 0.5 * atr)
         & (upw < 0.3 * body), "Three white soldiers", 2),
        (after_dip & (loww >= 2 * body) & (upw <= 0.25 * rng) & (body > 0) & (rng > 0.8 * atr), "Hammer", 1),
        (after_dip & red1 & green & (o < c1) & (c > (o1 + c1) / 2) & (c < o1) & (body1 > 0.5 * atr), "Piercing line", 1),
        (green & (body >= 0.85 * rng) & (rng >= 1.2 * atr), "Bullish marubozu", 1),
    ]
    bear = [
        (after_rally & green1 & red & (o >= c1) & (c <= o1) & (body > body1), "Bearish engulfing", 2),
        (after_rally & (c2 > o2) & (body2 > 0.6 * atr) & (body1 < 0.35 * body2) & red & (c < (o2 + c2) / 2), "Evening star", 2),
        (red & red1 & (c.shift(2) < o.shift(2)) & (c < c1) & (c1 < c2) & (body > 0.5 * atr) & (body1 > 0.5 * atr) & (body2 > 0.5 * atr)
         & (loww < 0.3 * body), "Three black crows", 2),
        (after_rally & (upw >= 2 * body) & (loww <= 0.25 * rng) & (body > 0) & (rng > 0.8 * atr), "Shooting star", 1),
        (after_rally & green1 & red & (o > c1) & (c < (o1 + c1) / 2) & (c > o1) & (body1 > 0.5 * atr), "Dark cloud cover", 1),
        (red & (body >= 0.85 * rng) & (rng >= 1.2 * atr), "Bearish marubozu", 1),
    ]
    for side, pats in (("bull", bull), ("bear", bear)):
        conds = [p[0].fillna(False).to_numpy(bool) for p in pats]
        x[f"pat_{side}"] = np.select(conds, [p[1] for p in pats], default="")
        x[f"pat_{side}_pts"] = np.select(conds, [p[2] for p in pats], default=0)
    return x


# ---------------------------------------------------------------- scoring
def score_at(x: pd.DataFrame, i: int, d: int) -> dict | None:
    """Confluence score for candle i. d = +1 scores the bullish case, -1 the bearish case."""
    if i < 60:
        return None
    r, p = x.iloc[i], x.iloc[i - 1]
    if not (r.atr > 0 and r.c > 0) or pd.isna(r.sma50):
        return None
    bull = d > 0
    beyond = (lambda a, b: a > b) if bull else (lambda a, b: a < b)
    hl, key = ("high", "hi") if bull else ("low", "lo")
    pts, why, warn = 0, [], []
    trigger, level = False, None

    # ---- triggers (fresh today)
    for n, w in ((55, 3), (20, 2)):
        if beyond(r.c, r[f"{key}{n}"]) and not beyond(p.c, p[f"{key}{n}"]):
            pts += w
            trigger, level = True, float(r[f"{key}{n}"])
            why.append(f"Close {'above' if bull else 'below'} the {n}-day {hl} ({'breakout' if bull else 'breakdown'})")
            if beyond(r.c, r[f"{key}252"]):
                pts += 1
                why.append(f"New 52-week {hl}")
            break
    if bool(r.squeeze) and beyond(r.c, r.bb_up if bull else r.bb_lo):
        pts += 2
        trigger = True
        why.append("Bollinger squeeze released " + ("upward" if bull else "downward"))
    pat, pat_pts = (r.pat_bull, int(r.pat_bull_pts)) if bull else (r.pat_bear, int(r.pat_bear_pts))
    if pat:
        pts += pat_pts
        trigger = True
        why.append(f"{pat} candle")
    hist = x["macd_hist"].iloc[i - 3:i + 1].to_numpy() * d
    if hist[-1] > 0 and (hist[:-1] <= 0).any():
        pts += 1
        why.append(f"MACD {'bullish' if bull else 'bearish'} crossover")
    if beyond(r.c, r.sma50) and not beyond(p.c, p.sma50):
        pts += 1
        why.append(f"{'Reclaimed' if bull else 'Lost'} the 50-day average")
    if not pd.isna(r.sma200):
        back = x.iloc[i - 5]
        if beyond(r.sma50, r.sma200) and not beyond(back.sma50, back.sma200):
            pts += 1
            why.append("Golden cross (50 over 200-day)" if bull else "Death cross (50 under 200-day)")

    # ---- confirmation / penalties
    rvol = float(r.rvol) if not pd.isna(r.rvol) else 0.0
    if rvol >= 2:
        pts += 3
    elif rvol >= 1.5:
        pts += 2
    elif rvol >= 1.2:
        pts += 1
    elif rvol < 0.8:
        pts -= 1
        warn.append("Below-average volume")
    if rvol >= 1.2:
        why.append(f"Volume {rvol:.1f}x the 20-day average")
    if not pd.isna(r.sma200):
        if beyond(r.c, r.sma50) and beyond(r.sma50, r.sma200):
            pts += 2
            why.append("Trend aligned (price / 50-day / 200-day)")
        elif beyond(r.c, r.sma200):
            pts += 1
        else:
            pts -= 1
            warn.append(f"Against the long-term trend ({'below' if bull else 'above'} the 200-day average)")
    if r.adx >= 25 and r.di == d:
        pts += 1
        why.append(f"ADX {r.adx:.0f} (trending)")
    rsi = float(r.rsi) if not pd.isna(r.rsi) else 50.0
    if (55 <= rsi <= 75) if bull else (25 <= rsi <= 45):
        pts += 1
    elif (rsi > 80) if bull else (rsi < 20):
        pts -= 1
        warn.append(f"RSI {rsi:.0f} - {'overbought' if bull else 'oversold'}, move may be exhausted")
    clv = float(r.clv) if not pd.isna(r.clv) else 0.5
    pos = clv if bull else 1 - clv
    if pos >= 0.7:
        pts += 1
        why.append(f"Closed near the day's {hl}")
    elif pos < 0.4:
        pts -= 1
        warn.append("Closed well off the day's extreme (rejection wick)")
    if (r.c - r.sma20) * d > 3 * r.atr:
        pts -= 1
        warn.append("Extended: more than 3 ATR from the 20-day average")

    # ---- trade levels
    entry, atr = float(r.c), float(r.atr)
    if level is not None:
        struct = max(level, float(r.l)) if bull else min(level, float(r.h))
    else:
        struct = min(float(r.l), float(p.l)) if bull else max(float(r.h), float(p.h))
    risk = min(max((entry - struct) * d + 0.2 * atr, 1.0 * atr), 2.5 * atr)
    stop, target = entry - d * risk, entry + d * REWARD_R * risk
    return {
        "side": "BUY" if bull else "SELL", "score": int(pts), "trigger": trigger, "rvol": round(rvol, 2),
        "strong": bool(trigger and rvol >= MIN_RVOL and pts >= MIN_SCORE),
        "grade": "A" if pts >= MIN_SCORE + 3 else "B" if pts >= MIN_SCORE else "C",
        "reasons": why, "warnings": warn, "pattern": pat or None,
        "close": round(entry, 2), "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
        "stop_pct": round(risk / entry * 100, 2), "target_pct": round(REWARD_R * risk / entry * 100, 2),
        "rr": REWARD_R, "rsi": round(rsi, 1), "atr_pct": round(atr / entry * 100, 2),
        "chg_1d_pct": round((r.c / p.c - 1) * 100, 2),
    }


def best_signal(x: pd.DataFrame, i: int | None = None) -> dict | None:
    i = len(x) - 1 if i is None else i
    cands = [s for s in (score_at(x, i, 1), score_at(x, i, -1)) if s]
    return max(cands, key=lambda s: s["score"]) if cands else None


# ---------------------------------------------------------------- news
POS_WORDS = (r"surge|soar|jump|rall(?:y|ies)|gain|rise[sn]?|climb|record high|52-week high|upgrade|outperform|buy rating|"
             r"raises? target|target raised|beats?|strong|robust|profit (?:rises|jumps|up|surges|grows)|order win|wins? order|bags|"
             r"secures|approval|approves|expansion|dividend|bonus|buyback|bullish|breakout|top gainer|positive|record")
NEG_WORDS = (r"fall[s]?|fell|drop|slump|plunge|tumble|slide|sink|decline|crash|52-week low|downgrade|underperform|sell rating|"
             r"cuts? target|target cut|miss(?:es)?|weak|loss|profit (?:falls|drops|down|declines|slips)|probe|fraud|penalty|fine[sd]?|"
             r"raid|sebi notice|show-cause|lawsuit|resign|default|recall|ban|bearish|top loser|negative|concern|warning|pressure|selloff|sell-off")
EVENTS = {
    "Results": r"\bq[1-4]\b|results|earnings|quarterly",
    "Rating change": r"upgrade|downgrade|target price|price target|brokerage",
    "Regulatory / legal": r"sebi|rbi|probe|penalty|court|tax notice|show-cause|cci|\bed\b|raid",
    "Corporate action": r"dividend|bonus|split|buyback|ex-date|record date",
    "Deal": r"acqui|merger|stake|block deal|bulk deal|\bqip\b|fund ?rais",
}
_JUNK = re.compile(r"share price\s*(today|live|-|\|)|stock price (&|and) chart|live nse|price today", re.I)
_POS, _NEG = re.compile(rf"\b(?:{POS_WORDS})", re.I), re.compile(rf"\b(?:{NEG_WORDS})", re.I)


def _short_name(company: str) -> str:
    return re.sub(r"\s+(Ltd\.?|Limited|Corporation Ltd\.?)\s*$", "", company, flags=re.I).strip()


def fetch_headlines(query: str, days: int, limit: int, must: list[str] | None = None) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote_plus(f'{query} when:{days}d')}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        r = requests.get(url, headers=UA, timeout=10)
        r.raise_for_status()
        items = ET.fromstring(r.content).iter("item")
    except Exception:
        return []
    out = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        if must and not any(m.lower() in title.lower() for m in must):
            continue
        if _JUNK.search(title):          # quote pages, not news
            continue
        try:
            when = parsedate_to_datetime(it.findtext("pubDate"))
        except Exception:
            when = None
        s = (1 if _POS.search(title) else 0) - (1 if _NEG.search(title) else 0)
        out.append({"title": title, "source": source, "link": it.findtext("link"),
                    "published": when.astimezone(IST).strftime("%d %b %H:%M") if when else "",
                    "_ts": when.timestamp() if when else 0, "sentiment": s,
                    "events": [k for k, pat in EVENTS.items() if re.search(pat, title, re.I)]})
    out.sort(key=lambda a: a["_ts"], reverse=True)
    for a in out:
        del a["_ts"]
    return out[:limit]


def news_for(sig: dict) -> dict:
    name = _short_name(sig["company"])
    stock = fetch_headlines(f'"{name}" share', 7, 6, must=[sig["symbol"], name.split()[0]])
    net = sum(a["sentiment"] for a in stock)
    d = 1 if sig["side"] == "BUY" else -1
    if not stock:
        verdict = "none"
    elif net * d >= 2:
        verdict = "supports"
    elif net * d <= -2:
        verdict = "conflicts"
    else:
        verdict = "mixed"
    return {"verdict": verdict, "net_sentiment": net, "headlines": stock,
            "events": sorted({e for a in stock for e in a["events"]})}


# ---------------------------------------------------------------- scan
def market_open_now() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and dtime(9, 15) <= now.time() < dtime(15, 30)


def run_scan(batch: int | None = None, universe: str | None = None, symbols: list[str] | None = None,
             with_news: bool = True, min_score: int = MIN_SCORE) -> dict:
    """Scan a universe (or batch 1 / 2, or explicit symbols) and return only the strong signals."""
    if symbols:
        name = "Custom list"
        stocks = [{"symbol": s.strip().upper(), "company": s.strip().upper(), "industry": ""} for s in symbols if s.strip()]
    else:
        if universe is None:
            if (batch or 1) not in BATCHES:
                raise ValueError("batch must be 1 (top 50) or 2 (next 50)")
            universe = BATCHES[batch or 1]
        name, stocks = load_universe(universe)
    sfx = SUFFIX
    data = download_daily([s["symbol"] + sfx for s in stocks])
    live = market_open_now()
    today = datetime.now(IST).date()
    signals, skipped, as_of = [], [], None
    for s in stocks:
        sub = data.get(s["symbol"] + sfx)
        if sub is None or not len(sub):
            skipped.append(s["symbol"])
            continue
        ltp = float(sub["Close"].iloc[-1])
        if live and sub.index[-1].date() == today:
            sub = sub.iloc[:-1]            # today's candle is still forming: its volume / shape would mislead
        if len(sub) < 120:
            skipped.append(s["symbol"])
            continue
        sig = best_signal(add_indicators(sub))
        if not sig:
            continue
        d = sub.index[-1].date()
        as_of = max(as_of, d) if as_of else d
        if sig["trigger"] and sig["rvol"] >= MIN_RVOL and sig["score"] >= min_score:
            signals.append({**s, **sig, "date": d.isoformat(), "ltp": round(ltp, 2)})
    signals.sort(key=lambda a: a["score"], reverse=True)

    sector_news = {}
    if with_news and signals:
        with ThreadPoolExecutor(8) as ex:
            for sig, n in zip(signals, ex.map(news_for, signals)):
                sig["news"] = n
                if n["verdict"] == "conflicts":
                    sig["warnings"].append("Recent headlines lean against this signal - read them first")
                if "Results" in n["events"]:
                    sig["warnings"].append("Results in the news - check the results date before entering")
            inds = sorted({s["industry"] for s in signals if s["industry"]})
            for ind, heads in zip(inds, ex.map(lambda i: fetch_headlines(f"{i} sector stocks India", 3, 3), inds)):
                sector_news[ind] = heads
    return {
        "batch": batch, "universe": name, "exchange": "NSE", "version": __version__, "scanned": len(stocks) - len(skipped), "skipped": skipped,
        "as_of": as_of.isoformat() if as_of else None, "market_open": live, "min_score": min_score,
        "generated_at": datetime.now(IST).strftime("%d %b %Y %H:%M IST"),
        "signals": signals, "sector_news": sector_news,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Breakout / candlestick signal scanner for Indian stocks")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--batch", type=int, choices=[1, 2], help="1 = Nifty 50 (default), 2 = Nifty Next 50")
    g.add_argument("--universe", choices=list(UNIVERSES), help="NSE index to scan")
    g.add_argument("--symbols", help="comma-separated symbols, e.g. RELIANCE,TCS,INFY")
    ap.add_argument("--min-score", type=int, default=MIN_SCORE, help=f"minimum confluence score (default {MIN_SCORE})")
    ap.add_argument("--no-news", action="store_true", help="skip the news lookup")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    a = ap.parse_args()
    res = run_scan(a.batch, a.universe, a.symbols.split(",") if a.symbols else None,
                   not a.no_news, a.min_score)
    if a.json:
        print(json.dumps(res, indent=1))
        return
    print(f"{res['universe']} ({res['exchange']}): {res['scanned']} stocks scanned, candles as of {res['as_of']}"
          f"{' (market open - today excluded)' if res['market_open'] else ''}; {len(res['signals'])} signal(s) with score >= {a.min_score}")
    if res["skipped"]:
        print("No data: " + ", ".join(res["skipped"]))
    for s in res["signals"]:
        print(f"\n{s['side']:<4} {s['symbol']:<12} score {s['score']} ({s['grade']})  close {s['close']:.2f}  "
              f"entry {s['entry']:.2f}  SL {s['stop']:.2f} ({s['stop_pct']}%)  target {s['target']:.2f} ({s['target_pct']}%)")
        print("     " + "; ".join(s["reasons"]))
        for w in s["warnings"]:
            print("     ! " + w)
        n = s.get("news") or {}
        if n:
            print(f"     news: {n['verdict']}" + (f" | events: {', '.join(n['events'])}" if n["events"] else ""))
        for h in n.get("headlines", [])[:3]:
            print(f"     - [{h['published']}] {h['title']} ({h['source']})")


if __name__ == "__main__":
    main()
