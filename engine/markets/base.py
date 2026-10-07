"""A market is anything the city can watch and trade: US stocks today, prediction markets or crypto later.

Each building names its market in config/bots.json ("market": "us_stocks"). To add a new kind of
market, subclass Market, register it in engine/markets/__init__.py, and give it a broker.
"""
from datetime import datetime

import pandas as pd


class Market:
    id = "base"
    name = "Market"
    source = "none"  # shown on the website, e.g. "yahoo" or "simulated"

    def is_open(self, now: datetime) -> bool:
        """Can orders fill right now?"""
        raise NotImplementedError

    def is_trading_day(self, day) -> bool:
        raise NotImplementedError

    def history(self, symbols: list[str], days: int) -> pd.DataFrame:
        """Daily closing prices, one column per symbol, oldest first."""
        raise NotImplementedError

    def quotes(self, symbols: list[str]) -> dict[str, float]:
        """Latest prices right now (may be a few minutes delayed)."""
        raise NotImplementedError

    def intraday(self, symbols: list[str], now: datetime | None = None, priority=()):
        """Today's 1-minute bars as (closes, volumes) DataFrames. Empty when not supported."""
        return pd.DataFrame(), pd.DataFrame()

    def info(self, symbol: str) -> dict:
        """{"name", "sector", "industry"} when known; used by the do-not-buy check."""
        return {}

    def info_cached(self, symbol: str) -> dict | None:
        """Same as info() but never waits: None means "still looking it up"."""
        return self.info(symbol)

    def prefetch_info(self, symbols: list[str]):
        """Start looking up stocks in the background."""

    def poll_seconds(self, now: datetime) -> int:
        """How often the server should refresh quotes."""
        return 60 if self.is_open(now) else 900
