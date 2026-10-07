"""Registry of markets the city can trade. Add new ones (e.g. a Kalshi prediction market) here."""
from .base import Market
from .us_stocks import UsStocks

MARKETS = {
    "us_stocks": UsStocks,
}


def make_market(market_id, **kwargs) -> Market:
    if market_id not in MARKETS:
        raise ValueError(f"Unknown market {market_id!r}. Known: {', '.join(MARKETS)}")
    return MARKETS[market_id](**kwargs)


__all__ = ["Market", "MARKETS", "make_market"]
