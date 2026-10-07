import copy
import math

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
        "style": "swing",
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


def test_rank_buffer_keeps_holding_that_slipped_slightly():
    ranking = [{"ticker": t, "qualifies": True, "score": s} for t, s in [("A", 5), ("B", 4), ("C", 3), ("D", 2), ("E", 1)]]
    assert momentum.picks_with_buffer(ranking, 2, held=["C"], buffer=1) == ["C", "A"]
    assert momentum.picks_with_buffer(ranking, 2, held=["E"], buffer=1) == ["A", "B"]


def test_min_hold_blocks_quick_rotation():
    bot = copy.deepcopy(BOT)
    bot["universe"] = ["UP1", "UP2"]
    bot["momentum"]["top_n"] = 1
    bot["momentum"]["rank_buffer"] = 0
    bot["ai_share"] = 0
    led = new_ledger(10000)
    led["positions"]["momentum:UP1"] = {"ticker": "UP1", "sleeve": "momentum", "shares": 50, "avg_cost": 50, "opened": "2026-10-07", "opened_at": "2026-10-07T10:00:00", "high": 50}
    closes = frame(["UP1", "UP2"], slopes={"UP1": 0.0005, "UP2": 0.004})
    BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:20:00").run()
    assert "momentum:UP1" in led["positions"]  # held 20 min < 30 min
    BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:45:00").run()
    assert "momentum:UP1" not in led["positions"] and "momentum:UP2" in led["positions"]


def test_trailing_stop_sells_winner_that_drops_from_high():
    closes = frame(["UP1"])
    price = float(closes["UP1"].iloc[-1])
    led = new_ledger(10000)
    led["positions"]["momentum:UP1"] = {"ticker": "UP1", "sleeve": "momentum", "shares": 5, "avg_cost": price * 0.8, "opened": "x", "high": price * 1.2}
    bot = copy.deepcopy(BOT)
    bot["universe"] = ["UP1"]
    day = BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning())
    day.stop_losses()
    assert "momentum:UP1" not in led["positions"]
    assert led["trades"][-1]["reason"].startswith("Trailing stop")


def test_daily_buy_cap():
    bot = copy.deepcopy(BOT)
    bot["max_buys_per_day"] = 2
    led = new_ledger(10000)
    BotDay(bot, led, frame(bot["universe"]), "2026-10-07", PaperBroker(), EXCL, picker_returning("UP4")).run()
    assert len([t for t in led["trades"] if t["side"] == "buy"]) == 2


# ---- intraday style ------------------------------------------------------------------

def minute_bars(paths, start="2026-10-07 09:30"):
    """paths: {ticker: list of 1-minute closes}"""
    n = max(len(p) for p in paths.values())
    idx = pd.date_range(start, periods=n, freq="1min")
    close = pd.DataFrame({t: p + [p[-1]] * (n - len(p)) for t, p in paths.items()}, index=idx)
    return close, pd.DataFrame(1000, index=idx, columns=close.columns)


def intraday_bot(**over):
    bot = copy.deepcopy(BOT)
    bot.update(style="intraday", ai_share=0, starting_cash=1000)
    bot["universe"] = ["RUN", "FLAT"]
    bot["intraday"].update(over)
    return bot


def run_at(bot, led, bars, hhmm, settle=False):
    closes = frame(["RUN", "FLAT"])
    BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning(), now=f"2026-10-07T{hhmm}:00", minute_bars=bars, settle=settle).run()


def test_intraday_buys_a_stock_running_up_and_takes_profit():
    bot = intraday_bot()
    led = new_ledger(1000)
    up = [100 + i * 0.02 for i in range(40)]  # +0.8% over 40 minutes, steady climb
    run_at(bot, led, minute_bars({"RUN": up, "FLAT": [50.0] * 40}), "10:10")
    assert [t["ticker"] for t in led["trades"] if t["side"] == "buy"] == ["RUN"]
    assert led["positions"]["intraday:RUN"]["shares"] == math.floor(1000 / 2 / up[-1])
    more = up + [up[-1] * (1 + i * 0.001) for i in range(1, 12)]  # +1.1% more
    run_at(bot, led, minute_bars({"RUN": more, "FLAT": [50.0] * len(more)}), "10:21")
    assert "intraday:RUN" not in led["positions"]
    assert led["trades"][-1]["reason"].startswith("Take profit")


def test_intraday_closes_everything_before_the_bell():
    bot = intraday_bot()
    led = new_ledger(1000)
    up = [100 + i * 0.02 for i in range(40)]
    run_at(bot, led, minute_bars({"RUN": up, "FLAT": [50.0] * 40}), "10:10")
    run_at(bot, led, minute_bars({"RUN": up + [up[-1]] * 2, "FLAT": [50.0] * 42}), "15:51")
    assert not led["positions"]
    assert led["trades"][-1]["reason"].startswith("End-of-day close-out")


def test_cash_account_cannot_rebuy_with_unsettled_money():
    bot = intraday_bot(cooldown_minutes=0, max_positions=1)
    led = new_ledger(1000)
    up = [100 + i * 0.02 for i in range(40)]
    run_at(bot, led, minute_bars({"RUN": up, "FLAT": [50.0] * 40}), "10:10", settle=True)
    assert "intraday:RUN" in led["positions"]
    spent = 1000 - led["cash"]
    more = up + [up[-1] * (1 + i * 0.001) for i in range(1, 12)]
    run_at(bot, led, minute_bars({"RUN": more, "FLAT": [50.0] * len(more)}), "10:21", settle=True)  # take profit
    assert led["unsettled"] and led["unsettled"][0]["settles"] == "2026-10-08"
    # still running: it would buy again, but only the never-spent cash is usable
    again = more + [more[-1] * (1 + i * 0.0005) for i in range(1, 20)]
    run_at(bot, led, minute_bars({"RUN": again, "FLAT": [50.0] * len(again)}), "10:40", settle=True)
    bought_again = [t for t in led["trades"] if t["side"] == "buy"][1:]
    assert sum(t["value"] for t in bought_again) <= 1000 - spent + 0.01
