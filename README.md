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

Each building splits its money **80% momentum / 20% AI picks** (adjustable per building) and checks for trades **every 3 minutes** while the market is open (9:45am–3:55pm New York time; adjustable from 2 minutes to once a day).

- **Momentum (80%)**: ranks its stocks by a blend of 6-month, 3-month and 1-week returns (the 1-week part uses the live price), keeps only the ones above their 50-day average, and holds the top 3 in equal amounts.
- **AI picks (20%)**: every 2 hours Claude looks at the same stocks' numbers (returns, volatility, distance from highs) and picks up to 2, with a short reason for each. If Claude isn't set up or fails, that 20% simply waits in cash.

**Guardrails against overtrading** (all adjustable per building):

- **Hold at least 30 minutes** before a stock can be rotated out (stops are exempt).
- **At most 20 trades a day** per building. Stops still fire after the cap.
- **Rank buffer of 2**: a holding stays while it's still in the top 3 + 2, so it isn't swapped for a stock that edged past it.
- **10% size tolerance**: a holding isn't trimmed or topped up over small price moves.
- **Stop loss 10%** below cost and **trailing stop 7%** below a winner's high, checked on every trade check. A stopped-out stock isn't bought back the same day.

**Do-not-buy list** (`config/exclusions.json`): no healthcare and no private prisons (GEO Group, CoreCivic, and the prison food contractor Aramark), plus a name/industry keyword check. Every buy is checked against it, including AI picks and any ticker you add yourself. The city also refuses to add a blocked ticker from the settings screen.

Bots only buy whole shares (Schwab's API can't trade fractions), so each building needs enough money that one share of its priciest stock fits in a slot. The paper default is $10,000 per building.

## Where it runs (always on)

One small server runs 24/7 ([Railway](https://railway.com), about $5/month). It:

- shows the city website, updating itself every 15 seconds,
- refreshes prices every minute while the market is open (every 15 minutes otherwise),
- runs each building's trade check every 3 minutes during market hours,
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
