# Stock City

A neon 3D city where every building is a trading bot for one sector. Click a building to see what it holds, what it traded and why, and to change its settings.

| Building | Sector | Trades (125 heavily traded stocks each, 500 in all) |
|---|---|---|
| Tech Tower | Technology, internet, telecom | AAPL, NVDA, AMD, INTC, PLTR, fast movers like SOUN and IONQ, … |
| Energy Works | Oil and gas, utilities, clean energy, uranium | XOM, CVX, OXY, RIG, PLUG, CCJ, NEE, … |
| Finance Bank | Banks, brokers, fintech, insurance | JPM, BAC, SOFI, HOOD, COIN, NU, V, … |
| Consumer Mall | Retail, autos, travel, food and drink | AMZN, TSLA, F, NIO, CCL, KO, WMT, … |

City Hall in the middle shows the whole city's value.

## How each bot trades

Each building splits its money **80% day trading / 20% AI picks** (adjustable per building) and checks for trades **every 3 minutes** while the market is open (9:45am–3:55pm New York time; adjustable from 2 minutes to once a day). On every check it can buy and sell.

- **Day trading (80%)**: looks at today's minute-by-minute prices. It buys a stock that is up at least 0.2% over the last 15 minutes and trading above VWAP (today's volume-weighted average price), up to 2 at a time. It sells at **+0.8% take profit**, at a **0.5% stop**, when the run fades (back below VWAP or the 15-minute move turns negative), and always by **3:50pm**, so nothing is held overnight. No new buys after 3:40pm, and it waits 20 minutes before buying the same stock again.
- **AI picks (20%)**: every 2 hours Claude looks at the building's stocks (returns, volatility, distance from highs) and picks 1, with a short reason. These are held for days, protected by a 10% stop loss and a 7% trailing stop. If Claude isn't set up or fails, that 20% simply waits in cash.

Each building can switch to **Swing** style on its settings tab instead: it holds the top 3 momentum stocks (6-month, 3-month and 1-week returns, above their 50-day average) for days or weeks.

**Risk slider**: each building's settings tab has a slider from less risky to more risky. It sets the numbers below for you:

| Level | Buy when up | Take profit | Stop | Day trades at once | AI pick stop / trailing stop |
|---|---|---|---|---|---|
| 1 Careful | 0.3% | +0.6% | −0.35% | 3 | 6% / 4% |
| 2 Steady | 0.25% | +0.7% | −0.45% | 2 | 8% / 5% |
| 3 Balanced (default) | 0.2% | +0.8% | −0.5% | 2 | 10% / 7% |
| 4 Bold | 0.15% | +1.2% | −0.8% | 1 | 12% / 9% |
| 5 Aggressive | 0.1% | +2% | −1.2% | 1 | 15% / 12% |

The exact numbers can still be changed under **Fine-tune**, which switches the slider to Custom. The levels live in `RISK_LEVELS` in `engine/config.py`.

**Guardrails** (all adjustable per building):

- **At most 30 buys a day** per building. Selling is never capped, so stops and the close-out always go through.
- A stopped-out stock isn't bought back the same day.
- **Cash-account rule**: in a cash account, money from a sale can't be used again until it settles the next trading day. Each building only spends settled cash, so it never triggers a good-faith violation. This is on for paper too (`"cash_account_rules": "always"` in the settings file), so a paper trial trades the way the real account will. Set it to `"live"` to apply it only with real money, or `"off"` for a margin account.

What that means for a $1,000–2,000 cash account: each dollar can be spent once a day, so expect about 3–4 buys per building per day (without the rule, 13–30). More positions at once (smaller trades) means more trades from the same money. Margin accounts under $25,000 are limited to 3 day trades per 5 days by the pattern day trader rule, which is why a cash account is the right fit here.

**Do-not-buy list** (`config/exclusions.json`): no healthcare and no private prisons (GEO Group, CoreCivic, and the prison food contractor Aramark), plus a name/industry keyword check. Every buy is checked against it, including AI picks and any ticker you add yourself. The city also refuses to add a blocked ticker from the settings screen. Each stock's sector is looked up once in the background and saved; a stock isn't bought until that check has run.

**Stock lists**: each building can trade the stocks listed on its settings tab, where you can add or remove any. When the lists in `config/bots.json` grow (its `universe_version` goes up), a running city adds the new stocks to each building on its next restart and keeps the ones you added. Claude reviews the strongest 40 of a building's affordable stocks each time it picks.

Bots only buy whole shares (Schwab's API can't trade fractions), so a stock is skipped when one share costs more than its slot. Each building starts with $500 ($2,000 for the city), which puts slots near $200 for day trades and $100 for the AI pick; pricier stocks such as MSFT or META are skipped until a building has more money.

## Where it runs (always on)

One small server runs 24/7 ([Railway](https://railway.com), about $5/month). It:

- shows the city website, updating itself every 15 seconds,
- refreshes prices every minute while the market is open (every 15 minutes otherwise). It asks Yahoo for one snapshot of all 500 stocks at a time (about 100 per request, so 5 requests a minute) and builds each stock's minute-by-minute prices from those snapshots. Asking for each stock's chart separately would be 500 requests a minute, which Yahoo blocks.
- runs each building's trade check every 3 minutes during market hours, buying and selling on minute-by-minute prices,
- lets you change settings and press **Run now** from the website, protected by a password. Run now makes every building check immediately; while the market is closed it only refreshes the rankings and never trades.

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
python -m engine.run --simulate 10 --check-minutes 3  # preview on fake minute prices
APP_PASSWORD=test uvicorn server.app:app --port 8000          # the full city on real prices
MARKET_DATA=simulated APP_PASSWORD=test uvicorn server.app:app # same, on fake prices
```

## Files

- `config/bots.json` – each building's settings (what the settings tab edits)
- `config/exclusions.json` – the do-not-buy list
- `engine/` – trading code: `city.py` (one building's trade check), `strategy/` (intraday, momentum, AI picks), `brokers/` (paper, Schwab)
- `engine/service.py` – the always-on city (price refresh, stop guard, trade checks)
- `engine/markets/` – market plug-ins (US stocks today)
- `server/app.py` – web server and API
- `/data` on the server – live settings (`bots.json`) and each building's cash, holdings and trades (`ledger.json`)
- `docs/` – the city website
