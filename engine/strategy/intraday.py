"""Intraday momentum: buy stocks that are running up right now, sell within minutes or hours.

Every check (every few minutes) it looks at today's 1-minute prices:
  - Entry: the stock is up at least `entry_pct` over the last `lookback_minutes` and trading
    above VWAP (today's volume-weighted average price, the usual intraday trend line).
  - Exit: take profit, stop, or the run fading (back below VWAP or the short-term move turned
    negative), and everything is sold before the close so nothing is held overnight.
"""
import pandas as pd


def signals(close: pd.DataFrame, volume: pd.DataFrame, settings: dict) -> list[dict]:
    n = settings["lookback_minutes"]
    rows = []
    for t in close.columns:
        c = close[t].dropna()
        if len(c) <= n:
            continue
        v = volume[t].reindex(c.index).fillna(0) if t in volume.columns else pd.Series(1, index=c.index)
        vwap = float((c * v).sum() / v.sum()) if v.sum() > 0 else float(c.mean())
        price = float(c.iloc[-1])
        move = price / float(c.iloc[-1 - n]) - 1
        rows.append(
            {
                "ticker": t,
                "price": round(price, 2),
                "move": round(move, 4),
                "vwap": round(vwap, 2),
                "above_vwap": price > vwap,
                "qualifies": price > vwap and move >= settings["entry_pct"],
                "fading": price < vwap or move < 0,
            }
        )
    rows.sort(key=lambda r: r["move"], reverse=True)
    return rows
