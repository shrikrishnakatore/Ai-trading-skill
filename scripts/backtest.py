#!/usr/bin/env python3
"""
Honesty check for signal_scanner.py: replay the signals on 5 years of daily candles.

Every signal is entered at the NEXT day's open with the scanner's own stop distance and a 2x-risk target,
closed after 15 trading days if neither is hit. Costs: 0.25% per round trip (delivery STT + charges + slippage).
A random-entry long baseline with the same exits is printed for comparison.

  python backtest.py [--universe nifty100] [--min-score 7]
"""
import argparse
import random

import signal_scanner as sc

COST_PCT, HOLD_DAYS = 0.25, 15


def simulate(x, i: int, d: int, risk: float) -> float | None:
    if i + 1 >= len(x):
        return None
    e = x.o.iloc[i + 1]
    stop, tgt = e - d * risk, e + d * sc.REWARD_R * risk
    last = min(i + HOLD_DAYS, len(x) - 1)
    for j in range(i + 1, last + 1):
        h, l = x.h.iloc[j], x.l.iloc[j]
        if (l <= stop) if d > 0 else (h >= stop):      # stop checked first: conservative when both are in range
            return (stop / e - 1) * 100 * d - COST_PCT
        if (h >= tgt) if d > 0 else (l <= tgt):
            return (tgt / e - 1) * 100 * d - COST_PCT
    return (x.c.iloc[last] / e - 1) * 100 * d - COST_PCT


def line(label: str, v: list[float]) -> str:
    if not v:
        return f"{label:<28} no trades"
    return (f"{label:<28} trades {len(v):5d}   win {sum(a > 0 for a in v) / len(v) * 100:5.1f}%   "
            f"avg net {sum(v) / len(v):+.2f}% per trade")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=list(sc.UNIVERSES), default="nifty100")
    ap.add_argument("--min-score", type=int, default=sc.MIN_SCORE)
    a = ap.parse_args()
    name, stocks = sc.load_universe(a.universe)
    data = sc.download_daily([s["symbol"] + sc.SUFFIX for s in stocks], period="5y")
    random.seed(1)
    res, base, by_year = {"BUY": [], "SELL": []}, [], {}
    for sub in data.values():
        if len(sub) < 300:
            continue
        x = sc.add_indicators(sub)
        for i in range(260, len(x) - 1):
            for d in (1, -1):
                if not x.rvol.iloc[i] >= sc.MIN_RVOL:
                    continue
                s = sc.score_at(x, i, d)
                if s and s["trigger"] and s["score"] >= a.min_score:
                    r = simulate(x, i, d, abs(s["entry"] - s["stop"]))
                    if r is not None:
                        res[s["side"]].append(r)
                        if d > 0:
                            by_year.setdefault(x.index[i].year, []).append(r)
            if random.random() < 0.02:
                s = sc.score_at(x, i, 1)
                r = simulate(x, i, 1, abs(s["entry"] - s["stop"])) if s else None
                if r is not None:
                    base.append(r)
    print(f"{name}: {len(data)} stocks, 5 years, score >= {a.min_score}, costs {COST_PCT}% per round trip\n")
    print(line("BUY signals", res["BUY"]))
    print(line("SELL signals (short)", res["SELL"]))
    print(line("Random long entries", base))
    for y in sorted(by_year):
        print(line(f"  BUY signals {y}", by_year[y]))


if __name__ == "__main__":
    main()
