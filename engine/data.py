"""Price data. Yahoo Finance for real runs, a seeded random walk for previews and tests."""
import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd


QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
SNAPSHOT_CHUNK = 100  # stocks per quote request
HISTORY_CHUNK = 100  # stocks per daily-history download
FALLBACK_MAX = 80  # stocks fetched one by one if the batch quote request fails


class YahooPrices:
    name = "yahoo"

    def __init__(self, cache_path=None, tape_path=None):
        from .tape import MinuteTape

        self.cache_path = Path(cache_path) if cache_path else None
        self._info = {}
        if self.cache_path and self.cache_path.exists():
            self._info = json.loads(self.cache_path.read_text())
        self.tape = MinuteTape(tape_path)
        self._info_queue = []
        self._info_lock = threading.Lock()
        self._info_thread = None
        self.feed = None  # how the last batch quote request went, shown on the site

    def closes(self, tickers, days=300):
        """Daily closes, downloaded about 100 stocks at a time."""
        import yfinance as yf

        syms = sorted(set(tickers))
        parts = []
        for i in range(0, len(syms), HISTORY_CHUNK):
            chunk = syms[i : i + HISTORY_CHUNK]
            df = yf.download(
                chunk,
                period=f"{int(days * 1.5) + 10}d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if df.empty:
                continue
            close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].set_axis(chunk, axis=1)
            parts.append(close)
            if i + HISTORY_CHUNK < len(syms):
                time.sleep(1)
        if not parts:
            return pd.DataFrame()
        closes = pd.concat(parts, axis=1)
        closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
        return closes.dropna(how="all").ffill()

    def snapshot(self, tickers):
        """Latest price and the day's volume so far for many stocks, about 100 per request."""
        at = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
        try:
            out = self._snapshot(tickers)
        except Exception as e:
            self.feed = {"ok": False, "at": at, "error": f"{type(e).__name__}: {e}"[:200]}
            raise
        self.feed = {"ok": True, "at": at, "asked": len(set(tickers)), "got": len(out)}
        return out

    def _snapshot(self, tickers):
        from yfinance.data import YfData

        data = YfData()
        out = {}
        syms = sorted(set(tickers))
        for i in range(0, len(syms), SNAPSHOT_CHUNK):
            raw = data.get_raw_json(
                QUOTE_URL,
                params={"symbols": ",".join(syms[i : i + SNAPSHOT_CHUNK]), "formatted": "false", "lang": "en-US", "region": "US"},
            )
            out.update(parse_quotes(raw))
        if syms and not out:
            raise RuntimeError("Yahoo returned no quotes")
        return out

    def quotes(self, tickers):
        return {t: q["price"] for t, q in self.snapshot(tickers).items()}

    def intraday(self, tickers, now_ny=None, is_open=False, priority=()):
        """Today's 1-minute closes and volumes. While the market is open each call adds one
        snapshot to the tape; if the batch request fails, falls back to per-stock charts for
        the `priority` stocks (holdings) and a few more."""
        if is_open:
            try:
                self.tape.record(self.snapshot(tickers), now_ny)
            except Exception as e:
                logging.getLogger("stockcity").warning("Batch quotes failed (%s); using per-stock charts for %d stocks", e, FALLBACK_MAX)
                pick = list(dict.fromkeys([*priority, *sorted(set(tickers))]))[:FALLBACK_MAX]
                return self._chart_bars(pick)
        return self.tape.frames(sorted(set(tickers)), day=now_ny.date() if now_ny else None)

    def _chart_bars(self, tickers):
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
        with self._info_lock:
            self._info[ticker] = data
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._info, indent=1, sort_keys=True))
        return data

    def info_cached(self, ticker):
        """Like info(), but never waits on Yahoo: returns None and looks the stock up in the
        background when it isn't known yet. The bots don't buy a stock until it's known."""
        if ticker in self._info:
            return self._info[ticker]
        self.prefetch_info([ticker])
        return None

    def prefetch_info(self, tickers):
        with self._info_lock:
            self._info_queue += [t for t in tickers if t not in self._info and t not in self._info_queue]
            if self._info_queue and not (self._info_thread and self._info_thread.is_alive()):
                self._info_thread = threading.Thread(target=self._fill_info, daemon=True)
                self._info_thread.start()

    def _fill_info(self):
        while True:
            with self._info_lock:
                if not self._info_queue:
                    return
                t = self._info_queue.pop(0)
            self.info(t)
            time.sleep(1.5)  # gentle on Yahoo; about 500 stocks in 15 minutes, once


def parse_quotes(raw):
    """{symbol: {"price", "volume"}} from Yahoo's quote response."""
    out = {}
    for q in ((raw or {}).get("quoteResponse") or {}).get("result") or []:
        px = q.get("regularMarketPrice")
        if q.get("symbol") and isinstance(px, (int, float)) and px > 0:
            out[q["symbol"]] = {"price": float(px), "volume": float(q.get("regularMarketVolume") or 0)}
    return out


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

    info_cached = info

    def prefetch_info(self, tickers):
        pass

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
