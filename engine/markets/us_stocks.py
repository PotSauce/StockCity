"""US stocks: NYSE/Nasdaq regular hours, prices from Yahoo Finance (free, no key)."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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

# Federal Reserve holidays (Columbus Day, Veterans Day) when the NYSE is open but trades don't
# settle, because the banks behind settlement are closed. Extend each year.
NO_SETTLEMENT = {date(2026, 10, 12), date(2026, 11, 11), date(2027, 10, 11), date(2027, 11, 11)}


def is_trading_day(day):
    return day.weekday() < 5 and day not in HOLIDAYS


def next_trading_day(day):
    """The next day the market is open."""
    d = day + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def is_settlement_day(day):
    return is_trading_day(day) and day not in NO_SETTLEMENT


def next_settlement_day(day):
    """When a sale made on `day` settles (T+1, counting only days when trades settle)."""
    d = day + timedelta(days=1)
    while not is_settlement_day(d):
        d += timedelta(days=1)
    return d


class UsStocks(Market):
    id = "us_stocks"
    name = "US stocks"

    def __init__(self, prices=None):
        self.prices = prices or YahooPrices()
        self.source = self.prices.name

    def is_trading_day(self, day):
        return is_trading_day(day)

    def is_open(self, now):
        ny = now.astimezone(NY)
        return self.is_trading_day(ny.date()) and OPEN <= ny.time() < CLOSE

    def history(self, symbols, days):
        return self.prices.closes(symbols, days)

    def quotes(self, symbols):
        if isinstance(self.prices, SyntheticPrices):
            return self.prices.live_quotes(symbols)
        return self.prices.quotes(symbols)

    def intraday(self, symbols, now=None, priority=()):
        """Today's 1-minute (close, volume) frames."""
        ny = (now or datetime.now(NY)).astimezone(NY)
        if isinstance(self.prices, SyntheticPrices):
            return self.prices.intraday(symbols, day=ny.date(), until=ny.time())
        return self.prices.intraday(symbols, now_ny=ny.replace(tzinfo=None), is_open=self.is_open(ny), priority=priority)

    def info(self, symbol):
        return self.prices.info(symbol)

    def info_cached(self, symbol):
        return self.prices.info_cached(symbol)

    def prefetch_info(self, symbols):
        self.prices.prefetch_info(symbols)

    def feed_status(self):
        return getattr(self.prices, "feed", None)
