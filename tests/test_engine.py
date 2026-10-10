import copy
import math

import numpy as np
import pandas as pd
import pytest

from engine.brokers import PaperBroker, make_broker
from engine.city import BotDay, new_ledger
from engine.config import held_share, load_config, load_exclusions, normalize_bot
from engine.exclusions import Exclusions
from engine.strategy import momentum

EXCL = Exclusions(load_exclusions())
BOT = normalize_bot(
    {
        "id": "t",
        "name": "Test",
        "sector": "Technology",
        "starting_cash": 10000,
        "day_share": 0,
        "ai_share": 0.2,
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
    assert led["trades"][-1]["cost"] == 1e6  # so the screen can show the sale lost money


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
    assert bot["day_share"] == 0 and held_share(bot) == 0


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


# ---- day trades ----------------------------------------------------------------------

def minute_bars(paths, start="2026-10-07 09:30"):
    """paths: {ticker: list of 1-minute closes}"""
    n = max(len(p) for p in paths.values())
    idx = pd.date_range(start, periods=n, freq="1min")
    close = pd.DataFrame({t: p + [p[-1]] * (n - len(p)) for t, p in paths.items()}, index=idx)
    return close, pd.DataFrame(1000, index=idx, columns=close.columns)


def intraday_bot(**over):
    bot = copy.deepcopy(BOT)
    bot.update(day_share=1.0, ai_share=0, starting_cash=1000)
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
    sale = led["trades"][-1]
    assert sale["price"] > sale["cost"] and "cost" not in led["trades"][0]


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


def test_ai_pick_can_use_unsettled_cash_and_is_held_until_it_settles():
    bot = copy.deepcopy(BOT)
    bot.update(day_share=0.8, ai_share=0.2, starting_cash=1000)
    bot["universe"] = ["RUN", "FLAT", "UP1"]
    bot["ai"]["max_picks"] = 1
    led = new_ledger(1000)
    closes = frame(["RUN", "FLAT", "UP1"], slopes={"UP1": 0.002, "RUN": 0.0, "FLAT": 0.0})
    led["cash"] = 150.0  # the rest is already spent; a sale this morning left $150 unsettled
    led["unsettled"] = [{"amount": 150.0, "settles": "2026-10-08"}]
    led["contributed"] = 1000
    day = BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning("UP1"), now="2026-10-07T11:00:00", settle=True)
    day._buy("UP1", "ai", 1, "New pick", led["cash"])
    pos = led["positions"]["ai:UP1"]
    assert pos["locked_until"] == "2026-10-08"
    assert sum(u["amount"] for u in led["unsettled"]) == pytest.approx(150 - pos["avg_cost"], abs=0.01)
    assert day.spendable() == pytest.approx(0, abs=0.01)  # day trades still can't touch it
    # a day trade can't use unsettled money
    day._buy("FLAT", "intraday", 1, "x", led["cash"])
    assert "intraday:FLAT" not in led["positions"]
    # a crash the same day doesn't sell it (that would be a good faith violation)
    day.prices["UP1"] = pos["avg_cost"] * 0.5
    day.stop_losses()
    assert "ai:UP1" in led["positions"]
    # the next trading day it can be sold again
    nxt = BotDay(bot, led, closes, "2026-10-08", PaperBroker(), EXCL, picker_returning(), now="2026-10-08T10:00:00", settle=True)
    nxt.prices["UP1"] = pos["avg_cost"] * 0.5
    nxt.stop_losses()
    assert "ai:UP1" not in led["positions"]


# ---- the three-way split: day trades, held stocks, AI picks ----------------------------

def test_old_style_setting_moves_to_the_three_way_split():
    base = {"id": "x", "name": "X", "sector": "Tech"}
    day = normalize_bot({**base, "style": "intraday", "ai_share": 0.3})
    assert (day["day_share"], day["ai_share"], held_share(day)) == (0.2, 0.3, 0.5) and "style" not in day
    assert normalize_bot(day) == day  # normalizing again changes nothing
    assert normalize_bot({**base, "ai_share": 0.3})["day_share"] == 0.2  # saved with no style: it was day trading
    swing = normalize_bot({**base, "style": "swing", "ai_share": 0.2})
    assert swing["day_share"] == 0 and held_share(swing) == 0.8 and "style" not in swing
    assert normalize_bot({**base, "style": "intraday", "ai_share": 0.9})["day_share"] == 0.1
    # a saved split is kept; when it adds up to more than everything, the AI share wins
    assert normalize_bot({**base, "day_share": 0.5, "ai_share": 0.3})["day_share"] == 0.5
    over = normalize_bot({**base, "day_share": 0.8, "ai_share": 0.6})
    assert (over["day_share"], over["ai_share"], held_share(over)) == (0.4, 0.6, 0)
    assert normalize_bot({**base, "day_share": 7})["day_share"] == 0.7  # the default AI share is 0.3
    assert normalize_bot({**base, "day_share": -1})["day_share"] == 0


def test_saved_settings_move_to_the_split_when_the_city_starts(tmp_path):
    import json

    from engine import service
    from engine.config import CONFIG_PATH

    saved = json.loads(CONFIG_PATH.read_text())
    for b in saved["bots"]:
        b.pop("day_share")
        b.update(style="intraday", ai_share=0.3)
    saved["bots"][1]["style"] = "swing"
    (tmp_path / "bots.json").write_text(json.dumps(saved))
    city = service.City(tmp_path, price_source="simulated")
    tech, energy = city.cfg["bots"][0], city.cfg["bots"][1]
    assert tech["day_share"] == 0.2 and held_share(tech) == 0.5 and "style" not in tech
    assert energy["day_share"] == 0 and held_share(energy) == 0.7
    city.update_bot("tech", {"enabled": False})  # a save writes the new shape
    raw = json.loads((tmp_path / "bots.json").read_text())["bots"][0]
    assert raw["day_share"] == 0.2 and "style" not in raw


def split_bot(**over):
    bot = copy.deepcopy(BOT)
    bot.update({"day_share": 0.2, "ai_share": 0.3, "starting_cash": 10000, **over})
    bot["universe"] = ["UP1", "UP2", "UP3", "UP4", "DOWN", "RUN", "FLAT"]
    bot["ai"]["max_picks"] = 1
    return bot


def split_closes():
    slopes = {"UP1": 0.003, "UP2": 0.0025, "UP3": 0.002, "UP4": 0.0015, "RUN": 0.0, "FLAT": 0.0}
    return frame(["UP1", "UP2", "UP3", "UP4", "DOWN", "RUN", "FLAT"], slopes=slopes)


RUNNING = minute_bars({"RUN": [100 + i * 0.02 for i in range(40)], "FLAT": [50.0] * 40})


def value(led, sleeve):
    return {p["ticker"]: p["shares"] * p["avg_cost"] for p in led["positions"].values() if p["sleeve"] == sleeve}


def test_20_50_30_split_fills_each_slot():
    bot = split_bot()
    led = new_ledger(10000)
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning("UP4"), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    day, held, ai = value(led, "intraday"), value(led, "momentum"), value(led, "ai")
    slot = 10000 * 0.5 / 3
    assert set(held) == {"UP1", "UP2", "UP3"}  # the three strongest, held for days
    assert all(slot - 200 < v <= slot * 1.001 for v in held.values())
    assert list(ai) == ["UP4"] and 3000 - 150 < ai["UP4"] <= 3000 * 1.001
    assert list(day) == ["RUN"] and 1000 - 110 < day["RUN"] <= 1000 * 1.001  # one of two $1,000 day-trade slots
    assert "DOWN" not in bought(led) and "FLAT" not in bought(led)
    assert led["cash"] >= 0 and sum(t["value"] for t in led["trades"]) <= 10000
    # day trades buy first, then held stocks, then the AI pick
    assert [t["sleeve"] for t in led["trades"]] == ["intraday", "momentum", "momentum", "momentum", "ai"]


def test_split_never_spends_more_than_its_cash():
    bot = split_bot()
    led = new_ledger(10000)
    led["cash"] = 2500.0
    # the rest is in an AI pick bought 10 minutes ago (too soon to trim)
    led["positions"]["ai:FLAT"] = {"ticker": "FLAT", "sleeve": "ai", "shares": 150, "avg_cost": 50.0, "opened": "2026-10-07", "opened_at": "2026-10-07T10:00:00", "high": 50.0}
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning("FLAT"), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert [t["side"] for t in led["trades"]] == ["buy", "buy"]
    assert [t["sleeve"] for t in led["trades"]] == ["intraday", "momentum"]
    assert led["cash"] >= 0 and sum(t["value"] for t in led["trades"]) <= 2500
    assert any(n["text"] == "Not enough cash to buy UP2" for n in led["notes"])


def test_held_share_of_zero_rotates_out_leftover_held_stocks():
    bot = split_bot(day_share=0.7)
    led = new_ledger(10000)
    led["positions"]["momentum:UP1"] = {"ticker": "UP1", "sleeve": "momentum", "shares": 5, "avg_cost": 150.0, "opened": "2026-10-01", "high": 150.0}
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning("UP4"), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert not value(led, "momentum") and value(led, "intraday") and value(led, "ai")


def test_held_stocks_pass_over_a_stock_one_share_of_which_costs_more_than_a_slot():
    bot = split_bot(starting_cash=900)  # $150 slots: one share of UP1 ($182) or UP2 ($164) doesn't fit
    led = new_ledger(900)
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert set(value(led, "momentum")) == {"UP3", "UP4"}
    bot = split_bot(starting_cash=400)
    led = new_ledger(400)
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert not value(led, "momentum")
    assert [n["text"] for n in led["notes"] if n["text"].startswith("Held stocks")] == [
        "Held stocks: one share of every stock in an uptrend costs more than the $67 slot, so that money stays in cash"
    ]


def test_held_stocks_can_use_unsettled_cash_and_are_held_until_it_settles():
    bot = split_bot(ai_share=0, starting_cash=1000)
    bot["intraday"]["max_positions"] = 1
    bot["momentum"]["top_n"] = 1
    led = new_ledger(1000)
    led["unsettled"] = [{"amount": 1000.0, "settles": "2026-10-08"}]  # all of it is from this morning's sales
    BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING, settle=True).run()
    pos = led["positions"]["momentum:UP1"]
    assert pos["locked_until"] == "2026-10-08"
    # RUN qualified for a day trade, but day trades need settled cash
    assert "intraday:RUN" not in led["positions"]
    assert any(n["text"].startswith("Waiting for cash to settle") for n in led["notes"])
    assert sum(u["amount"] for u in led["unsettled"]) == pytest.approx(led["cash"], abs=0.01)
    # later the same day: not rotated out, trimmed or stopped out (each would be a good faith violation)
    later = BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T14:00:00", minute_bars=RUNNING, settle=True, force=True)
    assert later.plan_sleeve("momentum", [], 0) == ([], [])
    assert later.plan_sleeve("momentum", ["UP1"], pos["avg_cost"]) == ([], [])
    later.prices["UP1"] = pos["avg_cost"] * 0.5  # past the stop loss
    later.run()
    later.sell_blocked()
    assert "momentum:UP1" in led["positions"] and not [t for t in led["trades"] if t["side"] == "sell"]
    # the next settlement day it can be sold
    nxt = BotDay(bot, led, split_closes(), "2026-10-08", PaperBroker(), EXCL, picker_returning(), now="2026-10-08T10:00:00", settle=True)
    assert not led["unsettled"]
    nxt.prices["UP1"] = led["positions"]["momentum:UP1"]["avg_cost"] * 0.5
    nxt.stop_losses()
    assert "momentum:UP1" not in led["positions"]


def test_held_buys_pay_with_settled_cash_first_like_the_broker():
    bot = split_bot(starting_cash=1000)
    led = new_ledger(1000)
    led["unsettled"] = [{"amount": 400.0, "settles": "2026-10-08"}]  # $600 settled, $400 from this morning's sales
    day = BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING, settle=True)
    day._buy("UP3", "momentum", 3, "New pick", led["cash"])  # about $447: settled cash covers it
    assert "locked_until" not in led["positions"]["momentum:UP3"]
    assert led["unsettled"][0]["amount"] == 400 and day.spendable() == pytest.approx(600 - 3 * 148.97, abs=1)
    day._buy("UP4", "momentum", 2, "New pick", led["cash"])  # about $270: the rest of the settled cash, then sale money
    assert led["positions"]["momentum:UP4"]["locked_until"] == "2026-10-08"
    assert day.spendable() == pytest.approx(0, abs=0.01)  # nothing settled is left for day trades
    assert sum(u["amount"] for u in led["unsettled"]) == pytest.approx(led["cash"], abs=0.01)


def test_day_trades_and_shares_bought_with_unsettled_cash_never_mix():
    bot = split_bot(starting_cash=1000)
    led = new_ledger(1000)
    led["unsettled"] = [{"amount": 800.0, "settles": "2026-10-08"}]  # only $200 is settled
    day = BotDay(bot, led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING, settle=True)
    # while a day trade holds FLAT ($50), a held buy of FLAT pays with settled cash only (and so isn't locked)
    day._buy("FLAT", "intraday", 1, "x", led["cash"])
    day._buy("FLAT", "momentum", 5, "x", led["cash"])
    assert led["positions"]["momentum:FLAT"]["shares"] == 2 and "locked_until" not in led["positions"]["momentum:FLAT"]
    assert led["unsettled"][0]["amount"] == 800
    day._buy("RUN", "momentum", 2, "New pick", led["cash"])  # paid with sale money
    assert led["positions"]["momentum:RUN"]["locked_until"] == "2026-10-08"
    # RUN is running up, but a day trade in it would be sold today and the broker can't tell which shares were sold
    led["unsettled"] = [{"amount": 500.0, "settles": "2026-10-08"}]  # pretend some cash settled, so only the lock stops it
    _, buys = day.plan_intraday(["RUN", "FLAT"], 1000)
    assert buys == []
    day._buy("RUN", "intraday", 1, "x", led["cash"])
    assert "intraday:RUN" not in led["positions"]
    # nor are another sleeve's shares of the same stock sold until then
    led["positions"]["ai:RUN"] = {"ticker": "RUN", "sleeve": "ai", "shares": 1, "avg_cost": 100.0, "opened": "2026-10-01", "high": 100.0}
    assert day._locked(led["positions"]["ai:RUN"])


def test_sales_settle_on_the_next_settlement_day():
    from datetime import date

    from engine.markets.us_stocks import next_settlement_day, next_trading_day

    d = date
    assert next_settlement_day(d(2026, 10, 7)) == d(2026, 10, 8)
    # Columbus Day: the market is open but nothing settles, so a Friday sale settles Tuesday
    assert next_settlement_day(d(2026, 10, 9)) == d(2026, 10, 13) and next_trading_day(d(2026, 10, 9)) == d(2026, 10, 12)
    assert next_settlement_day(d(2026, 10, 10)) == next_settlement_day(d(2026, 10, 11)) == d(2026, 10, 13)  # weekend
    assert next_settlement_day(d(2026, 10, 12)) == d(2026, 10, 13)  # a sale on Columbus Day itself
    assert next_settlement_day(d(2026, 11, 10)) == d(2026, 11, 12)  # Veterans Day
    assert next_settlement_day(d(2026, 11, 25)) == next_settlement_day(d(2026, 11, 26)) == d(2026, 11, 27)  # Thanksgiving
    assert next_settlement_day(d(2027, 10, 8)) == d(2027, 10, 12) and next_settlement_day(d(2027, 11, 10)) == d(2027, 11, 12)

    led = new_ledger(10000)
    led["positions"]["momentum:UP1"] = {"ticker": "UP1", "sleeve": "momentum", "shares": 10, "avg_cost": 100.0, "opened": "2026-10-01", "high": 100.0}
    BotDay(BOT, led, frame(["UP1"]), "2026-10-09", PaperBroker(), EXCL, picker_returning(), settle=True)._sell("momentum:UP1", 10, "x")
    assert led["unsettled"][-1]["settles"] == "2026-10-13"
    monday = BotDay(BOT, led, frame(["UP1"]), "2026-10-12", PaperBroker(), EXCL, picker_returning(), settle=True)
    assert monday.spendable() == pytest.approx(10000)


def test_old_ledger_dates_on_a_bank_holiday_move_to_the_real_settlement_day():
    led = new_ledger(1000)
    # written by the old code for Friday's sales, which counted Columbus Day as a settlement day
    led["unsettled"] = [{"amount": 100.0, "settles": "2026-10-12"}, {"amount": 50.0, "settles": "2026-10-12"}, {"amount": 10.0, "settles": "2026-10-14"}]
    led["positions"]["ai:UP1"] = {"ticker": "UP1", "sleeve": "ai", "shares": 1, "avg_cost": 100.0, "opened": "2026-10-09", "high": 100.0, "locked_until": "2026-10-12"}
    day = BotDay(BOT, led, frame(["UP1"]), "2026-10-12", PaperBroker(), EXCL, picker_returning(), settle=True)
    assert [u["settles"] for u in led["unsettled"]] == ["2026-10-13", "2026-10-13", "2026-10-14"]
    assert led["positions"]["ai:UP1"]["locked_until"] == "2026-10-13"
    assert day.spendable() == pytest.approx(840)
    day.prices["UP1"] = 50.0
    day.stop_losses()
    assert "ai:UP1" in led["positions"]  # still can't be sold on Columbus Day
    BotDay(BOT, led, frame(["UP1"]), "2026-10-13", PaperBroker(), EXCL, picker_returning(), settle=True)
    assert [u["settles"] for u in led["unsettled"]] == ["2026-10-14"]


def test_whole_shares_dont_cause_churn():
    closes = frame(["UP1"])
    price = float(closes["UP1"].iloc[-1])
    bot = copy.deepcopy(BOT)
    bot["universe"] = ["UP1"]

    def plan(shares, slot):
        led = new_ledger(10000)
        led["positions"]["ai:UP1"] = {"ticker": "UP1", "sleeve": "ai", "shares": shares, "avg_cost": price, "opened": "2026-10-01", "opened_at": "2026-10-01T10:00:00", "high": price}
        return BotDay(bot, led, closes, "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T11:00:00").plan_sleeve("ai", ["UP1"], slot)

    trim = "Trimmed back to target size"
    assert plan(1, price * 0.95) == ([], [])  # one share priced just over its slot is kept, not sold and bought back
    assert plan(1, price * 0.5) == ([], [])  # a pick always keeps one share
    assert plan(115, price * 100.5) == ([], [])  # 15% over its slot: left alone
    assert plan(130, price * 100.5) == ([("ai:UP1", 30, trim)], [])  # 30% over: trimmed to target
    assert plan(3, price * 0.9) == ([("ai:UP1", 2, trim)], [])  # but never below one share
    # top-ups still wait until a holding is more than 10% under its slot
    assert plan(95, price * 100.5) == ([], [])
    assert plan(85, price * 100.5) == ([], [("UP1", "ai", 15, "Topped up to target size")])


def test_a_day_trade_exit_doesnt_keep_a_stock_out_of_held_stocks():
    sale = {"date": "2026-10-07", "time": "10:00", "ticker": "UP1", "side": "sell", "shares": 1, "price": 180.0, "value": 180.0, "protective": True}
    led = new_ledger(10000)
    led["trades"] = [{**sale, "sleeve": "intraday", "reason": "Take profit: up 0.80%"}]
    BotDay(split_bot(), led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert "UP1" in value(led, "momentum")
    led = new_ledger(10000)
    led["trades"] = [{**sale, "sleeve": "momentum", "reason": "Stop loss: down 10.0% from cost"}]
    BotDay(split_bot(), led, split_closes(), "2026-10-07", PaperBroker(), EXCL, picker_returning(), now="2026-10-07T10:10:00", minute_bars=RUNNING).run()
    assert "UP1" not in value(led, "momentum")  # a held stock that hit its stop isn't bought back the same day
