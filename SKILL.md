---
name: india-stock-signals
description: Scan Indian stocks (NSE - Nifty 50 first, Nifty Next 50 or other indices on request, or any symbols) for strong technical signals - breakouts, candlestick patterns, volume and trend confluence - with entry, stop-loss and target prices, then check stock and sector news that could affect each signal. Use when the user asks for Indian stock signals, breakouts, a Nifty scan, swing trade ideas, technical analysis of NSE stocks, or "next 50".
---

# India stock signals

Finds daily-candle technical setups in Indian stocks and vets them against the news. The numbers come from
`scripts/signal_scanner.py`; your job is to run it, judge the news properly, and report plainly.

## Setup (once)

The scanner needs Python 3.10+ with `yfinance pandas numpy requests`. Use a Python that already has them
(a project `.venv` is fine), otherwise create one next to this file:

```bash
python3 -m venv <skill-dir>/.venv && <skill-dir>/.venv/bin/pip install -r <skill-dir>/requirements.txt
```

## Workflow

1. **Scan.** Start with the top 50 unless the user names something else:
   ```bash
   python <skill-dir>/scripts/signal_scanner.py --batch 1 --json
   ```
   - "next 50" / "more" -> `--batch 2` (Nifty Next 50). Do not run it until the user asks.
   - Other universes: `--universe nifty100|nifty200|nifty500|midcap100|bank|it`
   - Specific stocks: `--symbols RELIANCE,TCS,INFY`
   - The output holds only stocks that pass the strong-signal filter (fresh trigger, volume at least 1.2x the
     20-day average, score >= 7). An empty list is a valid answer: say there is nothing strong today. Lower
     `--min-score` only if the user asks for weaker setups, and label them as weaker.
2. **Check the news for every signal.** The JSON carries Google News headlines with a crude keyword
   `verdict`; treat it as a starting point only. Read the headlines, and if you have a web search tool, search
   each stock (company name + "share" / "results" / "news") and its sector for the last few days. Look for:
   results dates in the next 2 weeks, rating changes, regulatory or legal action, block deals, promoter
   selling, sector policy moves, and whether the price move is already explained by news. State for each
   stock whether the news supports, is neutral to, or conflicts with the signal, with the one headline that
   matters most.
3. **Report.** One table, strongest first: symbol, side, score/grade, what triggered, close, entry,
   stop-loss (and %), target (and %), relative volume, news view. Under it, one line per stock on the news
   and any warnings from the scanner (`warnings` field). Mention the candle date (`as_of`) and, if
   `market_open` is true, that today's unfinished candle was excluded.
4. **Offer the next step** in one line: scan the next 50, or look closer at one stock.

## Reading the output

- `side`: BUY signals are swing longs. SELL signals are bearish setups: in Indian cash equities a short
  cannot be carried overnight, so present them as "avoid / tighten stops if held", not as short trades.
- `entry` is the last close. `stop` sits under the breakout level or pattern low, kept between 1 and 2.5 ATR.
  `target` is 2x the risk. If the stock opens far above `entry`, the risk:reward is gone; say so rather than
  chasing.
- `score` adds up triggers, confirmation and penalties (see the docstring in `scripts/signal_scanner.py`).
  Grade A = score 10+, B = 7 to 9.

## Rules

- Never place, modify or cancel orders. This skill only analyses; execution belongs to the user.
- Be straight about reliability. In a 5-year Nifty 100 backtest (`scripts/backtest.py`) these BUY signals
  averaged about break-even after costs, no better than random entries, and SELL signals lost money. The scan
  is a shortlist for a human to study, not a system with a proven edge. Say this once when presenting
  signals; do not bury it and do not repeat it in every message.
- Prices are free Yahoo Finance data (NSE tickers) and can be delayed or patchy. If the scan reports
  `skipped` symbols, mention them.
- Do not invent levels, news or dates. If the news lookup returned nothing and you cannot search, say the
  news check was not done.
- This is not investment advice; position size and the decision are the user's.
