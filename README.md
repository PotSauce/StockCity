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

## Where it runs (free)

- **GitHub Actions** runs the bots every weekday at 3:35pm New York time (2:35pm in winter) and saves the results to the repo.
- **GitHub Pages** serves the city website from the `docs/` folder.

Free GitHub Pages needs a **public** repo, which means anyone with the link could see the holdings (never your Schwab login; that stays in encrypted secrets). If you want it private, either pay for GitHub Pro ($4/month) or host `docs/` on Cloudflare Pages for free.

## Setup

1. Create an empty repo on GitHub (for example `stock-city`) and push this code to it.
2. **Settings → Pages**: Source "Deploy from a branch", branch `main`, folder `/docs`. Your city appears at `https://<you>.github.io/stock-city/`.
3. **Settings → Actions → General → Workflow permissions**: choose "Read and write permissions".
4. **Settings → Secrets and variables → Actions**: add `ANTHROPIC_API_KEY` to turn on AI picks (about $1–3 a month at 4 buildings × weekly picks).
5. **Actions → Trade → Run workflow** (or wait for the weekday schedule). The first run replaces the simulated preview data with real prices.

To change settings from the city itself, click **Connect** and paste a fine-grained GitHub token limited to this repo with *Contents: read and write* and *Actions: read and write*. The token is kept only in that browser. Without it, the settings tab gives you the JSON to paste into `config/bots.json`.

## Going live with Schwab

Paper trading is the default. To trade real money:

1. Create an app at developer.schwab.com (approval can take a few days) and note its app key and secret.
2. On your computer: `pip install schwab-py` then `python -m engine.schwab_login`. Paste the printed token into a `SCHWAB_TOKEN_JSON` secret, and add `SCHWAB_APP_KEY` and `SCHWAB_APP_SECRET`. Add `SCHWAB_ACCOUNT_NUMBER` if you have more than one account.
3. In `config/bots.json` set `"broker": "schwab"` **and** `"live_trading_confirmed": true`. Both are required; either one alone stops the run with an error.
4. Set each building's money to what you actually want it to use. Real-money runs also cap buys at the cash Schwab says is available.

Schwab expires the login every **7 days**, so step 2 has to be repeated weekly. Live mode never trades while the market is closed, even with "force".

## Run it yourself

```bash
pip install -r requirements.txt
python -m pytest -q tests          # safety and strategy tests
python -m engine.run --simulate 90 # preview on fake prices
python -m engine.run               # real prices, today's trades
python -m http.server -d docs      # then open http://localhost:8000
```

## Files

- `config/bots.json` – each building's settings (what the settings tab edits)
- `config/exclusions.json` – the do-not-buy list
- `engine/` – trading code: `city.py` (one building's day), `strategy/` (momentum, AI picks), `brokers/` (paper, Schwab)
- `state/ledger.json` – each building's cash, holdings and trade history (written by the bots)
- `docs/` – the city website; `docs/data/state.json` is what it displays
