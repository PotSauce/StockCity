"""Stock City web server: serves the city, a small API, and runs the bots in the background 24/7.

    APP_PASSWORD=... uvicorn server.app:app --port 8000

Environment:
  APP_PASSWORD       password for changing settings / running the bots from the website
  DATA_DIR           where settings and the ledger are kept (mount a persistent volume here); default ./data
  MARKET_DATA        "yahoo" (default) or "simulated" for testing without market data
  ANTHROPIC_API_KEY  turns on AI picks
  SCHWAB_*           see engine/brokers/schwab.py
"""
import asyncio
import hmac
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine.service import STOP_CHECK_SECONDS, City, now_utc

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("stockcity")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

city = City(os.environ.get("DATA_DIR", ROOT / "data"), price_source=os.environ.get("MARKET_DATA", "yahoo"))


async def heartbeat():
    """Refresh prices, guard stop losses, and run the daily trading cycle when it's due."""
    while True:
        wait = 300
        try:
            await asyncio.to_thread(city.refresh_prices)
            now = now_utc()
            if city.last_stop_check is None or (now - city.last_stop_check).total_seconds() >= STOP_CHECK_SECONDS:
                await asyncio.to_thread(city.guard)
            if city.due_for_trade():
                log.info("Trading cycle: %s", await asyncio.to_thread(city.trade_cycle))
            city.last_error = None
            wait = min(city.market(m).poll_seconds(now) for m in city.market_ids())
        except Exception as e:
            city.last_error = f"{type(e).__name__}: {e}"
            log.error("Heartbeat failed: %s", traceback.format_exc())
            wait = 120
        await asyncio.sleep(wait)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(heartbeat())
    yield
    task.cancel()


app = FastAPI(title="Stock City", lifespan=lifespan)


@app.middleware("http")
async def always_check_for_new_pages(request, call_next):
    """Browsers otherwise keep showing the old page for a while after a deploy. "no-cache" makes them
    ask each time; an unchanged file comes back as a tiny "not modified" reply."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def require_password(given):
    expected = os.environ.get("APP_PASSWORD")
    if not expected:
        raise HTTPException(503, "Set an APP_PASSWORD on the server before changing anything from the website.")
    if not given or not hmac.compare_digest(given, expected):
        raise HTTPException(401, "Wrong password.")


@app.get("/api/health")
def health():
    feeds = {m: city.market(m).feed_status() for m in city.market_ids()}
    return {"ok": True, "last_quote_at": city.last_quote_at, "last_error": city.last_error, "feeds": feeds}


@app.get("/api/state")
def state():
    if not city.closes:
        raise HTTPException(503, "Loading market data, try again in a few seconds.")
    return city.state()


@app.post("/api/login")
def login(x_city_password: str | None = Header(default=None)):
    require_password(x_city_password)
    return {"ok": True}


@app.put("/api/bots/{bot_id}")
def update_bot(bot_id: str, settings: dict, x_city_password: str | None = Header(default=None)):
    require_password(x_city_password)
    try:
        return city.update_bot(bot_id, settings)
    except KeyError:
        raise HTTPException(404, f"No building called {bot_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/run")
async def run_now(x_city_password: str | None = Header(default=None)):
    require_password(x_city_password)
    note = await asyncio.to_thread(city.trade_cycle, True)
    return {"ok": True, "note": note}


@app.get("/")
def index():
    return FileResponse(ROOT / "docs" / "index.html")


app.mount("/", StaticFiles(directory=ROOT / "docs"), name="site")
