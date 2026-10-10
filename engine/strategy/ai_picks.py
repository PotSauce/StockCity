"""The 20% sleeve: Claude reviews the sector's numbers and picks a few stocks for the week.

Claude may only choose from the building's own ticker list, and every pick is checked
against the do-not-buy list afterwards, so it can never buy healthcare or prison stocks.
"""
import json
import os

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"


class Pick(BaseModel):
    ticker: str
    confidence: float = Field(description="0 to 1")
    reason: str = Field(description="One or two plain-English sentences")


class PickList(BaseModel):
    market_view: str = Field(description="One sentence on the sector this week")
    picks: list[Pick]


def metrics(closes: pd.DataFrame) -> list[dict]:
    out = []
    for t in closes.columns:
        s = closes[t].dropna()
        if len(s) < 60:
            continue
        p = float(s.iloc[-1])
        rets = s.pct_change().dropna()
        out.append(
            {
                "ticker": t,
                "price": round(p, 2),
                "ret_1w": round(p / float(s.iloc[-6]) - 1, 4),
                "ret_1m": round(p / float(s.iloc[-22]) - 1, 4),
                "ret_3m": round(p / float(s.iloc[-64]) - 1, 4) if len(s) > 64 else None,
                "ret_6m": round(p / float(s.iloc[-127]) - 1, 4) if len(s) > 127 else None,
                "vol_annual": round(float(rets.tail(63).std() * np.sqrt(252)), 4),
                "off_high": round(p / float(s.tail(252).max()) - 1, 4),
                "vs_sma50": round(p / float(s.tail(50).mean()) - 1, 4),
            }
        )
    return out


SYSTEM = (
    "You are the stock picker for one building in a small automated trading city. "
    "Each week you choose stocks for this building's AI sleeve, which holds them for about a week. "
    "Pick only from the candidate list you are given. Prefer a sensible balance of trend, "
    "pullback opportunity and risk; avoid picks you have little conviction in. "
    "It is fine to return fewer picks than allowed, or none, if nothing looks good."
)


def claude_picks(sector: str, candidates: list[dict], max_picks: int, avoid: list[str]) -> dict:
    """Ask Claude for picks. Returns {"picks": [...], "market_view": str, "model": str}."""
    import anthropic

    client = anthropic.Anthropic()
    prompt = (
        f"Sector: {sector}\n"
        f"Choose up to {max_picks} stocks for the coming week.\n"
        f"The momentum sleeve of this building holds or is buying: {', '.join(avoid) or 'nothing'}. "
        "You may overlap with it, but diversifying is usually better.\n\n"
        "Candidates (returns are decimals, e.g. 0.05 = +5%):\n"
        + json.dumps(candidates, indent=1)
    )
    resp = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=PickList,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise RuntimeError(f"Claude returned no picks (stop_reason={resp.stop_reason})")
    out = resp.parsed_output
    return {
        "market_view": out.market_view,
        "picks": [p.model_dump() for p in out.picks],
        "model": resp.model,
    }


def simulated_picks(sector: str, candidates: list[dict], max_picks: int, avoid: list[str]) -> dict:
    """Stand-in used for previews/tests: favours pullbacks in uptrends. Clearly labelled as simulated."""
    scored = [
        c
        for c in candidates
        if c["ticker"] not in avoid and (c["ret_3m"] or 0) > 0 and c["ret_1w"] < 0
    ] or [c for c in candidates if c["ticker"] not in avoid]
    scored.sort(key=lambda c: (c["ret_3m"] or 0) - c["ret_1w"], reverse=True)
    return {
        "market_view": "Simulated AI picks (no Claude API key in this run).",
        "picks": [
            {
                "ticker": c["ticker"],
                "confidence": 0.5,
                "reason": f"{(c['ret_3m'] or 0):+.0%} over 3 months, {c['ret_1w']:+.1%} this week.",
            }
            for c in scored[:max_picks]
        ],
        "model": "simulated",
    }


def ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
