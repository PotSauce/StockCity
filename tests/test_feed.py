"""Batch price snapshots, the minute tape, and keeping a running city's ticker lists current."""
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from engine.brokers import PaperBroker
from engine.city import BotDay, new_ledger
from engine.config import CONFIG_PATH, load_exclusions
from engine.data import YahooPrices, parse_quotes
from engine.exclusions import Exclusions
from engine.tape import MinuteTape

from test_engine import frame, intraday_bot, minute_bars, picker_returning

NY_11 = datetime(2026, 10, 7, 11, 0)


def snap(**prices_and_volumes):
    return {t: {"price": p, "volume": v} for t, (p, v) in prices_and_volumes.items()}


def test_tape_turns_snapshots_into_minute_bars(tmp_path):
    tape = MinuteTape(tmp_path / "tape.pkl")
    tape.record(snap(AAA=(10.0, 1000), BBB=(20.0, 500)), datetime(2026, 10, 7, 10, 0, 5))
    tape.record(snap(AAA=(10.5, 1600), BBB=(20.0, 500)), datetime(2026, 10, 7, 10, 1, 7))
    # 10:02 was missed; 10:03 arrives
    tape.record(snap(AAA=(11.0, 2600), BBB=(19.0, 900)), datetime(2026, 10, 7, 10, 3, 2))
    close, vol = tape.frames(["AAA", "BBB"])
    assert list(close.index.strftime("%H:%M")) == ["10:00", "10:01", "10:02", "10:03"]
    assert close["AAA"].tolist() == [10.0, 10.5, 10.5, 11.0]  # the missed minute repeats the last price
    assert vol["AAA"].tolist() == [0, 600, 0, 1000]  # each minute's volume is the change in the day's volume
    assert vol["BBB"].tolist() == [0, 0, 0, 400]

    # survives a restart, but not into the next day
    again = MinuteTape(tmp_path / "tape.pkl")
    assert again.frames(["AAA"])[0]["AAA"].iloc[-1] == 11.0
    assert again.frames(["AAA"], day=datetime(2026, 10, 8).date())[0].empty
    again.record(snap(AAA=(12.0, 50)), datetime(2026, 10, 8, 9, 31))
    assert len(again.frames(["AAA"])[0]) == 1


def test_parse_yahoo_quote_response():
    raw = {
        "quoteResponse": {
            "result": [
                {"symbol": "AAPL", "regularMarketPrice": 251.3, "regularMarketVolume": 12_000_000},
                {"symbol": "DEAD", "regularMarketPrice": None},
                {"symbol": "F", "regularMarketPrice": 11.02},
            ],
            "error": None,
        }
    }
    assert parse_quotes(raw) == {"AAPL": {"price": 251.3, "volume": 12_000_000.0}, "F": {"price": 11.02, "volume": 0.0}}
    assert parse_quotes({}) == {}


def test_yahoo_intraday_uses_one_snapshot_per_minute(tmp_path, monkeypatch):
    prices = YahooPrices(tape_path=tmp_path / "tape.pkl")
    calls = []

    def fake_snapshot(tickers):
        calls.append(len(tickers))
        n = len(calls)
        return {t: {"price": 100.0 + n, "volume": 1000.0 * n} for t in tickers}

    monkeypatch.setattr(prices, "snapshot", fake_snapshot)
    tickers = [f"T{i}" for i in range(500)]
    for minute in range(3):
        close, vol = prices.intraday(tickers, now_ny=NY_11.replace(minute=minute), is_open=True)
    assert calls == [500, 500, 500]  # one batch call per poll, not one per stock
    assert close.shape == (3, 500) and close["T7"].iloc[-1] == 103.0
    # with the market closed it just reads the tape
    close, _ = prices.intraday(tickers, now_ny=NY_11.replace(hour=17), is_open=False)
    assert len(calls) == 3 and close.shape == (3, 500)


def test_feed_status_says_whether_the_batch_request_worked(tmp_path, monkeypatch):
    from engine.markets.us_stocks import UsStocks

    prices = YahooPrices(tape_path=tmp_path / "tape.pkl")
    market = UsStocks(prices)
    assert market.feed_status() is None  # nothing asked yet
    monkeypatch.setattr(prices, "_snapshot", lambda t: {"AAA": {"price": 1.0, "volume": 5.0}})
    prices.quotes(["AAA", "BBB"])
    assert market.feed_status()["ok"] and market.feed_status()["got"] == 1 and market.feed_status()["asked"] == 2
    monkeypatch.setattr(prices, "_snapshot", lambda t: (_ for _ in ()).throw(RuntimeError("Invalid Crumb")))
    try:
        prices.quotes(["AAA"])
    except RuntimeError:
        pass
    assert market.feed_status()["ok"] is False and "Invalid Crumb" in market.feed_status()["error"]


def test_failed_snapshot_falls_back_to_charts_for_holdings_first(tmp_path, monkeypatch):
    prices = YahooPrices(tape_path=tmp_path / "tape.pkl")
    monkeypatch.setattr(prices, "snapshot", lambda t: (_ for _ in ()).throw(RuntimeError("429")))
    asked = []
    monkeypatch.setattr(prices, "_chart_bars", lambda t: asked.extend(t) or (pd.DataFrame(), pd.DataFrame()))
    prices.intraday([f"T{i:03d}" for i in range(500)], now_ny=NY_11, is_open=True, priority=["ZZZ"])
    assert asked[0] == "ZZZ" and len(asked) == 80


def test_stocks_are_not_bought_until_their_sector_is_known():
    bot = intraday_bot()
    led = new_ledger(1000)
    up = [100 * (1.0006**i) for i in range(40)]
    bars = minute_bars({"RUN": up, "FLAT": [50.0] * 40})
    pending = lambda t: None if t == "RUN" else {"name": t, "sector": "Technology", "industry": None}
    BotDay(bot, led, frame(["RUN", "FLAT"]), "2026-10-07", PaperBroker(), Exclusions(load_exclusions()), picker_returning(),
           info_fn=pending, now="2026-10-07T10:10:00", minute_bars=bars).run()
    assert not led["positions"]
    assert any("Still checking" in n["text"] for n in led["notes"])
    known = lambda t: {"name": t, "sector": "Technology", "industry": None}
    BotDay(bot, led, frame(["RUN", "FLAT"]), "2026-10-07", PaperBroker(), Exclusions(load_exclusions()), picker_returning(),
           info_fn=known, now="2026-10-07T10:10:00", minute_bars=bars).run()
    assert [p["ticker"] for p in led["positions"].values()] == ["RUN"]


def test_running_city_gets_new_repo_tickers_and_keeps_its_own(tmp_path, monkeypatch):
    from engine import service

    repo = json.loads(CONFIG_PATH.read_text())
    saved = json.loads(CONFIG_PATH.read_text())
    saved.pop("universe_version")
    for b in saved["bots"]:
        b["universe"] = b["universe"][:5]
    saved["bots"][0]["universe"].append("MYPICK")
    saved["bots"][0]["min_hold_minutes"] = 45  # a setting the user changed stays changed
    (tmp_path / "bots.json").write_text(json.dumps(saved))

    city = service.City(tmp_path, price_source="simulated")
    tech = city.cfg["bots"][0]
    assert "MYPICK" in tech["universe"] and tech["min_hold_minutes"] == 45
    assert len(tech["universe"]) == len(repo["bots"][0]["universe"]) + 1
    assert all(len(b["universe"]) >= 125 for b in city.cfg["bots"])
    assert json.loads((tmp_path / "bots.json").read_text())["universe_version"] == repo["universe_version"]

    # a user removing a ticker later isn't undone on the next restart
    raw = json.loads((tmp_path / "bots.json").read_text())
    raw["bots"][0]["universe"].remove("AAPL")
    (tmp_path / "bots.json").write_text(json.dumps(raw))
    assert "AAPL" not in service.City(tmp_path, price_source="simulated").cfg["bots"][0]["universe"]


def test_ticker_lists_are_big_and_clean():
    excl = Exclusions(load_exclusions())
    repo = json.loads(CONFIG_PATH.read_text())
    seen = []
    for b in repo["bots"]:
        assert len(b["universe"]) == 125
        assert not [t for t in b["universe"] if excl.is_blocked(t)]
        seen += b["universe"]
    assert len(seen) == len(set(seen)) == 500


def test_daily_history_is_not_refetched_every_minute(tmp_path, monkeypatch):
    from engine import service

    monkeypatch.setattr(service, "now_utc", lambda: datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc))
    city = service.City(tmp_path, price_source="simulated")
    m = city.market("us_stocks")
    asked = []
    real = m.history
    monkeypatch.setattr(m, "history", lambda syms, days: asked.append(len(syms)) or real(syms, days))
    city.refresh_prices()
    city.refresh_prices()
    assert asked == [500]
    city.update_bot("tech", {"universe": city.cfg["bots"][0]["universe"] + ["NEWCO"]})
    city.refresh_prices()
    assert asked == [500, 1]  # only the new stock


def test_run_now_never_trades_while_the_market_is_closed(tmp_path, monkeypatch):
    from engine import service

    monkeypatch.setattr(service, "now_utc", lambda: datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))  # 5pm NY
    city = service.City(tmp_path, price_source="simulated")
    note = city.trade_cycle(force=True)
    assert "market closed" in note
    for b in city.ledger["bots"].values():
        assert not b["trades"] and b["ranking"]  # rankings refreshed, nothing traded


def test_risk_slider_sets_the_numbers():
    from engine.config import RISK_LEVELS, normalize_bot

    base = {"id": "x", "name": "X", "sector": "Tech"}
    assert normalize_bot(base)["risk"] == 3  # the defaults are the Balanced level
    bold = normalize_bot({**base, "risk": 5})
    assert bold["intraday"]["take_profit_pct"] == RISK_LEVELS[5]["intraday"]["take_profit_pct"]
    assert bold["intraday"]["max_positions"] == 1 and bold["momentum"]["top_n"] == 1
    insane = normalize_bot({**base, "risk": 6})  # 50% more room than Aggressive
    assert insane["risk"] == 6 and insane["intraday"]["stop_pct"] == pytest.approx(1.5 * bold["intraday"]["stop_pct"])
    assert insane["intraday"]["take_profit_pct"] == pytest.approx(1.5 * bold["intraday"]["take_profit_pct"])
    assert normalize_bot({**base, "risk": 9})["risk"] == 6
    careful = normalize_bot({**base, "risk": 1})
    assert careful["intraday"]["stop_pct"] < bold["intraday"]["stop_pct"]
    # the slider's level wins over stale numbers sent with it
    assert normalize_bot({**base, "risk": 4, "intraday": {"take_profit_pct": 0.05}})["intraday"]["take_profit_pct"] == 0.012
    # numbers typed under Fine-tune make it custom and are kept
    custom = normalize_bot({**base, "risk": 0, "intraday": {"take_profit_pct": 0.05}})
    assert custom["risk"] == 0 and custom["intraday"]["take_profit_pct"] == 0.05
    # a building saved before the slider existed gets the level its numbers match, or custom
    assert normalize_bot({**base, "intraday": RISK_LEVELS[4]["intraday"], "momentum": RISK_LEVELS[4]["momentum"]})["risk"] == 4
    assert normalize_bot({**base, "intraday": {"take_profit_pct": 0.05}})["risk"] == 0
