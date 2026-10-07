from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from engine.markets.us_stocks import UsStocks


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MARKET_DATA", "simulated")
    monkeypatch.setenv("APP_PASSWORD", "secret")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import importlib

    import server.app as app_module

    importlib.reload(app_module)
    app_module.city.refresh_prices()
    with TestClient(app_module.app) as c:
        c.city = app_module.city
        yield c


def test_state_and_site(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    st = r.json()
    assert {b["id"] for b in st["bots"]} == {"tech", "energy", "finance", "consumer"}
    assert st["server"]["mode"] == "live"
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_settings_need_password(client):
    assert client.put("/api/bots/tech", json={"enabled": False}).status_code == 401
    r = client.put("/api/bots/tech", json={"enabled": False}, headers={"X-City-Password": "secret"})
    assert r.status_code == 200 and r.json()["enabled"] is False
    # persisted to the data dir and reloaded
    assert '"enabled": false' in (client.city.config_path.read_text())


def test_cannot_add_blocked_ticker_via_api(client):
    bot = client.get("/api/state").json()["bots"][0]["settings"]
    bot["universe"] = bot["universe"] + ["GEO"]
    r = client.put(f"/api/bots/{bot['id']}", json=bot, headers={"X-City-Password": "secret"})
    assert r.status_code == 400 and "GEO" in r.json()["detail"]


def test_run_now_trades_on_paper(client):
    r = client.post("/api/run", headers={"X-City-Password": "secret"})
    assert r.status_code == 200
    st = client.get("/api/state").json()
    assert any(b["trades"] for b in st["bots"])
    # AI is off without a key, so the AI sleeve holds nothing
    for b in st["bots"]:
        assert not [p for p in b["positions"] if p["sleeve"] == "ai"]


def test_market_hours():
    from engine.data import SyntheticPrices

    m = UsStocks(prices=SyntheticPrices())
    assert m.is_open(datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc))  # 11am NY, Wednesday
    assert not m.is_open(datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))  # 5pm NY
    assert not m.is_open(datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc))  # Saturday
    assert not m.is_open(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc))  # Thanksgiving


def test_each_building_checks_on_its_own_interval(client):
    from datetime import timedelta

    city = client.city
    now = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
    tech = city.cfg["bots"][0]
    city.ledger["bots"][tech["id"]]["last_check"] = (now - timedelta(minutes=2)).isoformat()
    assert not city.bot_due(tech, now)  # default 3 minutes
    assert city.bot_due(tech, now + timedelta(minutes=1))
