"""Today's 1-minute bars, built from repeated price snapshots.

Asking Yahoo for every stock's 1-minute chart is one request per stock, which doesn't scale to
hundreds of stocks polled every minute. Instead the server asks for a snapshot of all of them
(price and the day's volume so far, about 100 stocks per request) once a minute and records it
here. Each minute's volume is the change in the day's volume since the previous snapshot.
"""
import pickle
from pathlib import Path

import pandas as pd


class MinuteTape:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.day = None
        self.rows = {}  # minute (naive New York time) -> {symbol: (price, day_volume)}
        if self.path and self.path.exists():
            try:
                saved = pickle.loads(self.path.read_bytes())
                self.day, self.rows = saved["day"], saved["rows"]
            except Exception:  # a damaged file just means starting today's tape over
                self.day, self.rows = None, {}

    def record(self, snapshot, now_ny):
        """snapshot: {symbol: {"price": float, "volume": float}}; now_ny: naive New York datetime."""
        minute = pd.Timestamp(now_ny).floor("min")
        if self.day != minute.date():
            self.day, self.rows = minute.date(), {}
        row = self.rows.setdefault(minute, {})
        row.update({s: (q["price"], q.get("volume") or 0.0) for s, q in snapshot.items()})
        self._save()

    def frames(self, symbols=None, day=None):
        """(closes, volumes) DataFrames with one row per minute and one column per symbol."""
        if not self.rows or (day is not None and self.day != day):
            return pd.DataFrame(), pd.DataFrame()
        minutes = sorted(self.rows)
        price = pd.DataFrame({m: {s: v[0] for s, v in self.rows[m].items()} for m in minutes}).T
        cum = pd.DataFrame({m: {s: v[1] for s, v in self.rows[m].items()} for m in minutes}).T
        if symbols is not None:
            cols = [s for s in symbols if s in price.columns]
            price, cum = price[cols], cum[cols]
        # fill skipped minutes so "the last 15 minutes" means 15 minutes of clock time
        full = pd.date_range(minutes[0], minutes[-1], freq="1min")
        price = price.reindex(full).ffill()
        cum = cum.reindex(full).ffill()
        vol = cum.diff().clip(lower=0).fillna(0)
        return price, vol

    def _save(self):
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps({"day": self.day, "rows": self.rows}))
        tmp.replace(self.path)
