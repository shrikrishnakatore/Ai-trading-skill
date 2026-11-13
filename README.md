# india-stock-signals

A Claude skill that scans Indian stocks for strong technical setups and checks the news around them.

- **Data**: free daily candles from Yahoo Finance (NSE tickers), index constituents from NSE, headlines from
  Google News RSS. No account or API key needed.
- **Analysis**: 20/55-day and 52-week breakouts, Bollinger squeeze breakouts, twelve candlestick patterns,
  MACD and moving-average crosses, confirmed by relative volume, trend (SMA 50/200), ADX, RSI and where the
  candle closed. Each stock gets a confluence score; only strong ones are listed.
- **Output**: side, score, reasons, warnings, entry, stop-loss, target (2x risk), plus stock and sector
  headlines with event flags (results, rating changes, regulatory, corporate actions, deals).
- **Flow**: Nifty 50 first, Nifty Next 50 when you ask for the next 50; other indices or your own symbols
  on request.

## Install

Clone into a Claude skills folder:

```bash
# for every project
git clone https://github.com/shrikrishnakatore/india-stock-signals.git ~/.claude/skills/india-stock-signals
# or for one project
git clone https://github.com/shrikrishnakatore/india-stock-signals.git .claude/skills/india-stock-signals

cd <that folder>
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Then ask Claude something like "scan Indian stocks for breakouts" or "any strong signals in the Nifty 50
today?", and "now the next 50" afterwards.

## Use without Claude

```bash
python scripts/signal_scanner.py                      # Nifty 50
python scripts/signal_scanner.py --batch 2            # Nifty Next 50
python scripts/signal_scanner.py --universe nifty500
python scripts/signal_scanner.py --symbols RELIANCE,TCS,INFY --min-score 5
python scripts/signal_scanner.py --json               # for other programs
```

From Python:

```python
import sys; sys.path.insert(0, "scripts")
import signal_scanner
result = signal_scanner.run_scan(batch=1)     # dict with "signals", "sector_news", "as_of", ...
```

## How reliable is it?

Run `python scripts/backtest.py` to replay the signals yourself. On the Nifty 100 over the 5 years to
November 2026 (entry next open, scanner's stop, 2x-risk target, 15-day time exit, 0.25% costs):

| | Trades | Win rate | Avg net per trade |
|---|---|---|---|
| BUY signals (score >= 7) | 4,263 | 38.4% | +0.04% |
| SELL signals (short) | 2,213 | 34.5% | -0.72% |
| Random long entries | 1,864 | 39.1% | +0.06% |

So: no measurable edge over random entries after costs, and shorting breakdowns in large caps lost money.
Results swung by year (+0.48% per trade in 2023, -0.28% in 2025). Treat the scan as a shortlist of stocks
where something is happening, to be studied alongside the news, not as a trading system. Nothing here is
investment advice.

## Limits

- Daily candles only; the scan is meant to be run after the close (15:30 IST). During market hours the
  unfinished candle is excluded.
- Yahoo data is free and occasionally delayed or missing; skipped symbols are reported. BSE-only stocks are
  not covered because Yahoo's BSE history is too patchy.
- The built-in news sentiment is keyword matching on headlines. Claude (or you) should read them.

## License

MIT
