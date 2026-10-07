from .base import Broker, Fill
from .paper import PaperBroker


def make_broker(cfg):
    if cfg["broker"] == "paper":
        return PaperBroker()
    if cfg["broker"] == "schwab":
        if not cfg["live_trading_confirmed"]:
            raise RuntimeError(
                "broker is 'schwab' but live_trading_confirmed is false in config/bots.json. "
                "Set it to true only when you are ready to trade real money."
            )
        from .schwab import SchwabBroker

        return SchwabBroker()
    raise ValueError(cfg["broker"])


__all__ = ["Broker", "Fill", "PaperBroker", "make_broker"]
