import copy

import numpy as np
import pandas as pd
import pytest

from engine.brokers import PaperBroker, make_broker
from engine.city import BotDay, new_ledger
from engine.config import load_config, load_exclusions, normalize_bot
from engine.exclusions import Exclusions
from engine.strategy import momentum

EXCL = Exclusions(load_exclusions())
BOT = normalize_bot(
    {
        "id": "t",
        "name": "Test",
        "sector": "Technology",
        "starting_cash": 10000,
        "universe": ["UP1", "UP2", "UP3", "UP4", "DOWN"],
    }
)


def frame(tickers, days=200, end="2026-10-07", slopes=None):
    idx = pd.bdate_range(end=end, periods=days)
    data = {}
    for t in tickers:
        slope = (slopes or {}).get(t, 0.002 if t.startswith("UP") else -0.002)
        data[t] = 100 * np.exp(np.arange(days) * slope)
    return pd.DataFrame(data, index=idx)


def picker_returning(*tickers):
    def picker(sector, cands, max_picks, avoid):
        return {"picks": [{"ticker": t, "confidence": 0.9, "reason": "x"} for t in tickers], "market_view": "", "model": "test"}

    return picker


def bought(led):
    return {t["ticker"] for t in led["trades"] if t["side"] == "buy"}


def test_prison_and_healthcare_tickers_are_blocked():
    for t in ["GEO", "CXW", "UNH", "LLY", "PFE"]:
        assert EXCL.is_blocked(t)
    assert EXCL.is_blocked("ZZZ", sector="Healthcare")
    assert EXCL.is_blocked("ZZZ", name="Acme Corrections Inc")
    assert EXCL.is_blocked("ZZZ", industry="Biotechnology")
    assert not EXCL.is_blocked("AAPL", name="Apple Inc.", sector="Technology")


def test_blocked_ticker_in_universe_is_never_bought():
    bot = copy.deepcopy(BOT)
    bot["universe"] = ["GEO", "CXW", "UNH", "UP1"]
    closes = frame(["GEO", "CXW", "UNH", "UP1"], slopes={"GEO": 0.01, "CXW": 0.01, "UNH": 0.01})
    led = new_ledger(10000)
    BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning("GEO", "UNH")).run()
    assert bought(led) == {"UP1"}
    assert {r["ticker"] for r in led["blocked_in_universe"]} == {"GEO", "CXW", "UNH"}


def test_ai_cannot_pick_blocked_or_outside_universe():
    closes = frame(BOT["universe"] + ["CXW", "MSFT"])
    led = new_ledger(10000)
    BotDay(BOT, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning("CXW", "MSFT", "UP4")).run()
    assert "CXW" not in bought(led) and "MSFT" not in bought(led)
    assert [p["ticker"] for p in led["ai"]["picks"]] == ["UP4"]
    assert {r["ticker"] for r in led["ai"]["rejected"]} == {"CXW", "MSFT"}


def test_80_20_split_and_downtrend_skipped():
    closes = frame(BOT["universe"])
    led = new_ledger(10000)
    BotDay(BOT, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning("UP4")).run()
    mom = sum(p["shares"] * p["avg_cost"] for p in led["positions"].values() if p["sleeve"] == "momentum")
    ai = sum(p["shares"] * p["avg_cost"] for p in led["positions"].values() if p["sleeve"] == "ai")
    assert "DOWN" not in bought(led)
    assert 7400 < mom <= 8020
    assert 800 < ai <= 1010  # one pick fills one of two AI slots (20% / 2)
    assert led["cash"] >= -0.01


def test_stop_loss_sells():
    closes = frame(["UP1"])
    led = new_ledger(10000)
    led["positions"]["momentum:UP1"] = {"ticker": "UP1", "sleeve": "momentum", "shares": 10, "avg_cost": 1e6, "opened": "x"}
    led["last_momentum"] = led["last_ai"] = "2026-10-07"
    bot = copy.deepcopy(BOT)
    bot["universe"] = ["UP1"]
    BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning()).run()
    assert "momentum:UP1" not in led["positions"]
    assert led["trades"][-1]["reason"].startswith("Stop loss")


def test_paused_bot_does_not_trade():
    bot = copy.deepcopy(BOT)
    bot["enabled"] = False
    led = new_ledger(10000)
    BotDay(bot, led, frame(bot["universe"]), "2026-10-07", PaperBroker(), EXCL, picker_returning("UP1")).run()
    assert led["trades"] == []


def test_ai_failure_keeps_holdings_and_retries():
    def broken(*_):
        raise RuntimeError("boom")

    led = new_ledger(10000)
    BotDay(BOT, led, frame(BOT["universe"]), "2026-10-07", PaperBroker(), EXCL, broken).run()
    assert led["ai"]["status"] == "error"
    assert led["last_ai"] is None


def test_capital_change_is_applied():
    bot = copy.deepcopy(BOT)
    bot["starting_cash"] = 15000
    bot["enabled"] = False
    led = new_ledger(10000)
    BotDay(bot, led, frame(bot["universe"]), "2026-10-07", PaperBroker(), EXCL, picker_returning()).run()
    assert led["cash"] == 15000 and led["contributed"] == 15000


def test_momentum_rank_orders_by_strength():
    closes = frame(["UP1", "UP2"], slopes={"UP1": 0.001, "UP2": 0.003})
    assert momentum.picks(momentum.rank(closes, BOT["momentum"]), 2) == ["UP2", "UP1"]


def test_config_values_are_clamped():
    bot = normalize_bot({"id": "x", "name": "x", "sector": "x", "ai_share": 5, "momentum": {"top_n": 99, "stop_loss_pct": 0}})
    assert bot["ai_share"] == 1.0 and bot["momentum"]["top_n"] == 10 and bot["momentum"]["stop_loss_pct"] == 0.01


def test_schwab_needs_explicit_confirmation():
    cfg = load_config()
    cfg["broker"] = "schwab"
    cfg["live_trading_confirmed"] = False
    with pytest.raises(RuntimeError, match="live_trading_confirmed"):
        make_broker(cfg)


def test_shipped_config_has_no_blocked_tickers():
    for bot in load_config()["bots"]:
        assert not [t for t in bot["universe"] if EXCL.is_blocked(t)], bot["id"]
