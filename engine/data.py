"""Price data. Yahoo Finance for real runs, a seeded random walk for previews and tests."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


class YahooPrices:
    name = "yahoo"

    def __init__(self, cache_path=None):
        self.cache_path = Path(cache_path) if cache_path else None
        self._info = {}
        if self.cache_path and self.cache_path.exists():
            self._info = json.loads(self.cache_path.read_text())

    def closes(self, tickers, days=300):
        import yfinance as yf

        df = yf.download(
            sorted(set(tickers)),
            period=f"{int(days * 1.5) + 10}d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        closes = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
        if not isinstance(df.columns, pd.MultiIndex):
            closes.columns = sorted(set(tickers))
        closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
        return closes.dropna(how="all").ffill()

    def info(self, ticker):
        """Company name / sector / industry, cached so we only ask Yahoo once per ticker."""
        if ticker in self._info:
            return self._info[ticker]
        try:
            import yfinance as yf

            raw = yf.Ticker(ticker).info or {}
            data = {
                "name": raw.get("shortName") or raw.get("longName"),
                "sector": raw.get("sector"),
                "industry": raw.get("industry"),
            }
        except Exception:
            data = {"name": None, "sector": None, "industry": None}
        self._info[ticker] = data
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._info, indent=1, sort_keys=True))
        return data


class SyntheticPrices:
    """Deterministic fake prices so the city can be previewed and tested without market data."""

    name = "simulated"

    def __init__(self, end=None, total_days=420, seed=7):
        self.end = pd.Timestamp(end or pd.Timestamp.today()).normalize()
        self.total_days = total_days
        self.seed = seed
        self._cache = {}

    def _series(self, ticker):
        if ticker not in self._cache:
            h = int(hashlib.sha256(f"{self.seed}:{ticker}".encode()).hexdigest()[:8], 16)
            rng = np.random.default_rng(h)
            drift = rng.normal(0.0004, 0.0008)
            vol = rng.uniform(0.012, 0.03)
            regime = np.sin(np.linspace(0, rng.uniform(2, 6), self.total_days)) * 0.0015
            rets = rng.normal(drift, vol, self.total_days) + regime
            start = rng.uniform(30, 400)
            prices = start * np.exp(np.cumsum(rets))
            idx = pd.bdate_range(end=self.end, periods=self.total_days)
            self._cache[ticker] = pd.Series(prices.round(2), index=idx)
        return self._cache[ticker]

    def closes(self, tickers, days=300, as_of=None):
        df = pd.DataFrame({t: self._series(t) for t in sorted(set(tickers))})
        if as_of is not None:
            df = df[df.index <= pd.Timestamp(as_of)]
        return df.tail(days)

    def info(self, ticker):
        return {"name": ticker, "sector": None, "industry": None}

    def trading_days(self, n):
        return list(pd.bdate_range(end=self.end, periods=n))
