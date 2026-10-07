"""Every broker (paper or Schwab) looks the same to the bots, so they can be swapped in config."""
from dataclasses import dataclass


@dataclass
class Fill:
    ticker: str
    side: str  # "buy" or "sell"
    shares: int
    price: float


class Broker:
    name = "base"
    live = False

    def buy(self, ticker: str, shares: int, ref_price: float) -> Fill:
        raise NotImplementedError

    def sell(self, ticker: str, shares: int, ref_price: float) -> Fill:
        raise NotImplementedError

    def available_cash(self) -> float | None:
        """Real cash in the account, or None when the broker has no account (paper)."""
        return None
