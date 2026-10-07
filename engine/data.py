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

    def intraday(self, tickers):
        """Today's 1-minute closes and volumes (two DataFrames, one column per ticker)."""
        import yfinance as yf

        syms = sorted(set(tickers))
        df = yf.download(syms, period="1d", interval="1m", progress=False, threads=True, auto_adjust=True, prepost=False)
        if df.empty:
            return pd.DataFrame(), pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            close, vol = df["Close"], df["Volume"]
        else:
            close, vol = df[["Close"]].set_axis(syms, axis=1), df[["Volume"]].set_axis(syms, axis=1)
        idx = pd.to_datetime(close.index)
        idx = (idx.tz_convert("America/New_York") if idx.tz is not None else idx).tz_localize(None)
        close.index = vol.index = idx
        return close.ffill(), vol.fillna(0)

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

    def live_quotes(self, tickers):
        """Last close nudged by a small time-based wiggle, so a test server looks alive."""
        import time

        tick = int(time.time() // 60)
        out = {}
        for t in tickers:
            s = self._series(t)
            h = int(hashlib.sha256(f"{t}:{tick}".encode()).hexdigest()[:6], 16) / 0xFFFFFF
            out[t] = round(float(s.iloc[-1]) * (1 + (h - 0.5) * 0.01), 2)
        return out

    def intraday(self, tickers, day=None, until=None):
        """Seeded 1-minute bars for one day, starting from the previous close, with trending
        stretches so intraday signals actually fire. `until` (a time) cuts the day short."""
        import datetime as dt

        day = pd.Timestamp(day or self.end).normalize()
        start = day + pd.Timedelta(hours=9, minutes=30)
        end = day + pd.Timedelta(hours=16)
        if until is not None:
            end = min(end, day + pd.Timedelta(hours=until.hour, minutes=until.minute))
        idx = pd.date_range(start, end, freq="1min", inclusive="left")
        closes, vols = {}, {}
        for t in sorted(set(tickers)):
            s = self._series(t)
            prev = s[s.index < day]
            base = float(prev.iloc[-1]) if len(prev) else float(s.iloc[0])
            h = int(hashlib.sha256(f"{self.seed}:{t}:{day.date()}".encode()).hexdigest()[:8], 16)
            rng = np.random.default_rng(h)
            n = 390
            drift = np.repeat(rng.normal(0, 0.00012, n // 30 + 1), 30)[:n]  # mild half-hour trends
            rets = rng.normal(0, 0.0009, n) + drift
            path = base * np.exp(np.cumsum(rets))
            closes[t] = np.round(path[: len(idx)], 2)
            vols[t] = rng.integers(2_000, 40_000, n)[: len(idx)]
        return pd.DataFrame(closes, index=idx), pd.DataFrame(vols, index=idx)

    def trading_days(self, n):
        return list(pd.bdate_range(end=self.end, periods=n))
