"""The 80% sleeve: buy the sector's strongest trending stocks, re-check on a schedule."""
import pandas as pd


def rank(closes: pd.DataFrame, settings: dict) -> list[dict]:
    """Score every ticker with enough history. Higher score = stronger momentum.

    score = average of the long and short lookback returns.
    A ticker only qualifies when it's above its trend average and its score is positive.
    """
    long_n = settings["lookback_days"]
    short_n = settings["short_lookback_days"]
    sma_n = settings["trend_sma_days"]
    rows = []
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) <= max(long_n, sma_n):
            continue
        price = float(s.iloc[-1])
        r_long = price / float(s.iloc[-1 - long_n]) - 1
        r_short = price / float(s.iloc[-1 - short_n]) - 1
        sma = float(s.tail(sma_n).mean())
        score = (r_long + r_short) / 2
        rows.append(
            {
                "ticker": t,
                "price": round(price, 2),
                "score": round(score, 4),
                "ret_long": round(r_long, 4),
                "ret_short": round(r_short, 4),
                "above_trend": price > sma,
                "qualifies": price > sma and score > 0,
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def picks(ranking: list[dict], top_n: int) -> list[str]:
    return [r["ticker"] for r in ranking if r["qualifies"]][:top_n]
