# Stock City

A neon 3D city where every building is a trading bot for one sector. Click a building to see what it holds, what it traded and why, and to change its settings.

| Building | Sector | Trades |
|---|---|---|
| Tech Tower | Technology | AAPL, MSFT, NVDA, GOOGL, META, AVGO, AMD, … |
| Energy Works | Energy | XOM, CVX, COP, EOG, SLB, NEE, FSLR, … |
| Finance Bank | Financials | JPM, BAC, GS, MS, V, MA, SCHW, … |
| Consumer Mall | Consumer | AMZN, TSLA, HD, MCD, COST, WMT, KO, … |

City Hall in the middle shows the whole city's value.

## How each bot trades

Each building splits its money **80% momentum / 20% AI picks** (adjustable per building).

- **Momentum (80%)**: once a week it ranks its stocks by their 6-month and 3-month returns, keeps only the ones above their 50-day average, and holds the top 3 in equal amounts. Anything that drops 10% below what the bot paid is sold the same day (stop loss).
- **AI picks (20%)**: once a week Claude looks at the same stocks' numbers (returns, volatility, distance from highs) and picks up to 2, with a short reason for each. If Claude isn't set up or fails, that 20% simply waits in cash.

**Do-not-buy list** (`config/exclusions.json`): no healthcare and no private prisons (GEO Group, CoreCivic, and the prison food contractor Aramark), plus a name/industry keyword check. Every buy is checked against it, including AI picks and any ticker you add yourself. The city also refuses to add a blocked ticker from the settings screen.

Bots only buy whole shares (Schwab's API can't trade fractions), so each building needs enough money that one share of its priciest stock fits in a slot. The paper default is $10,000 per building.

## Where it runs (always on)

One small server runs 24/7 ([Railway](https://railway.com), about $5/month). It:

- shows the city website, with prices refreshed every 2 minutes while the market is open (every 15 minutes otherwise),
- checks stop losses and the do-not-buy list every 5 minutes during market hours,
- runs each building's full trading day at 3:35pm New York time on market days,
- lets you change settings and press **Run now** from the website, protected by a password.

The server is a standard Docker container (`Dockerfile`), so it also runs on Fly.io, a Hetzner/DigitalOcean server, or your own computer.

### Setup on Railway

1. Sign up at railway.com with your GitHub account and choose the Hobby plan.
2. **New Project → Deploy from GitHub repo → PotSauce/StockCity.**
3. In the service, open **Variables** and add:
   - `APP_PASSWORD`: a password you'll type on the website to change settings
   - `ANTHROPIC_API_KEY`: from console.anthropic.com, turns on AI picks (about $1–3 a month)
4. Right-click the service → **Attach volume**, mount path `/data`. This keeps settings and trade history safe across restarts and updates.
5. **Settings → Networking → Generate Domain.** That link is your city.

Every push to `main` redeploys automatically.

### Adding new markets (prediction markets, crypto)

Each building names its market in `config/bots.json` (`"market": "us_stocks"`). A market is a small plug-in in `engine/markets/` that says when it's open, where its prices come from, and which broker trades it. A Kalshi prediction-market building would be a new market plug-in, a broker for Kalshi orders, and a strategy suited to yes/no contracts.

## Going live with Schwab

Paper trading is the default. To trade real money:

1. Create an app at developer.schwab.com (approval can take a few days) and note its app key and secret.
2. On your computer: `pip install schwab-py` then `python -m engine.schwab_login`. Add the printed token as a `SCHWAB_TOKEN_JSON` variable on the server, plus `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. Add `SCHWAB_ACCOUNT_NUMBER` if you have more than one account.
3. In the settings file (`/data/bots.json` on the server, seeded from `config/bots.json`) set `"broker": "schwab"` **and** `"live_trading_confirmed": true`. Both are required; either one alone stops the run with an error.
4. Set each building's money to what you actually want it to use. Real-money runs also cap buys at the cash Schwab says is available.

Schwab expires the login every **7 days**, so step 2 has to be repeated weekly. Live mode never trades while the market is closed, even with "force".

## Run it yourself

```bash
pip install -r requirements.txt
python -m pytest -q tests          # safety and strategy tests
python -m engine.run --simulate 90 # preview on fake prices
APP_PASSWORD=test uvicorn server.app:app --port 8000          # the full city on real prices
MARKET_DATA=simulated APP_PASSWORD=test uvicorn server.app:app # same, on fake prices
```

## Files

- `config/bots.json` – each building's settings (what the settings tab edits)
- `config/exclusions.json` – the do-not-buy list
- `engine/` – trading code: `city.py` (one building's day), `strategy/` (momentum, AI picks), `brokers/` (paper, Schwab)
- `engine/service.py` – the always-on city (price refresh, stop-loss guard, daily trading)
- `engine/markets/` – market plug-ins (US stocks today)
- `server/app.py` – web server and API
- `/data` on the server – live settings (`bots.json`) and each building's cash, holdings and trades (`ledger.json`)
- `docs/` – the city website
