from .base import Broker, Fill


class PaperBroker(Broker):
    """Fake-money fills at the latest price, nudged by a little slippage so results aren't rosy."""

    name = "paper"
    live = False

    def __init__(self, slippage_bps=5):
        self.slip = slippage_bps / 10_000

    def buy(self, ticker, shares, ref_price):
        return Fill(ticker, "buy", shares, round(ref_price * (1 + self.slip), 4))

    def sell(self, ticker, shares, ref_price):
        return Fill(ticker, "sell", shares, round(ref_price * (1 - self.slip), 4))
