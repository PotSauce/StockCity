"""US stocks: NYSE/Nasdaq regular hours, prices from Yahoo Finance (free, no key)."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from ..data import SyntheticPrices, YahooPrices
from .base import Market

NY = ZoneInfo("America/New_York")
OPEN, CLOSE = time(9, 30), time(16, 0)

# NYSE full-day holidays. Extend each year.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
    date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
    date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
}


class UsStocks(Market):
    id = "us_stocks"
    name = "US stocks"

    def __init__(self, prices=None):
        self.prices = prices or YahooPrices()
        self.source = self.prices.name

    def is_trading_day(self, day):
        return day.weekday() < 5 and day not in HOLIDAYS

    def is_open(self, now):
        ny = now.astimezone(NY)
        return self.is_trading_day(ny.date()) and OPEN <= ny.time() < CLOSE

    def history(self, symbols, days):
        return self.prices.closes(symbols, days)

    def quotes(self, symbols):
        if isinstance(self.prices, SyntheticPrices):
            return self.prices.live_quotes(symbols)
        import yfinance as yf

        df = yf.download(sorted(set(symbols)), period="1d", interval="2m", progress=False, threads=True, auto_adjust=True)
        if df.empty:
            return {}
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].set_axis(sorted(set(symbols)), axis=1)
        last = close.ffill().iloc[-1]
        return {t: float(v) for t, v in last.items() if pd.notna(v) and v > 0}

    def info(self, symbol):
        return self.prices.info(symbol)
