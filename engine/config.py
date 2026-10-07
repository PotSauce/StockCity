"""Loads and sanity-checks the city's settings (config/bots.json)."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "bots.json"
EXCLUSIONS_PATH = ROOT / "config" / "exclusions.json"

# (min, max) limits so a typo in the UI can't produce a reckless bot.
LIMITS = {
    "starting_cash": (0, 10_000_000),
    "ai_share": (0.0, 1.0),
    "momentum.top_n": (1, 10),
    "momentum.lookback_days": (20, 252),
    "momentum.short_lookback_days": (5, 252),
    "momentum.trend_sma_days": (5, 200),
    "momentum.rebalance_days": (1, 90),
    "momentum.stop_loss_pct": (0.01, 0.9),
    "ai.max_picks": (1, 5),
    "ai.rebalance_days": (1, 90),
}

DEFAULT_BOT = {
    "market": "us_stocks",
    "enabled": True,
    "starting_cash": 2500,
    "ai_share": 0.2,
    "momentum": {
        "top_n": 3,
        "lookback_days": 126,
        "short_lookback_days": 63,
        "trend_sma_days": 50,
        "rebalance_days": 7,
        "stop_loss_pct": 0.1,
    },
    "ai": {"max_picks": 2, "rebalance_days": 7},
    "universe": [],
}


def _get(d, dotted):
    for part in dotted.split("."):
        d = d[part]
    return d


def _set(d, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        d = d[part]
    d[parts[-1]] = value


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def normalize_bot(raw):
    bot = _merge(DEFAULT_BOT, raw)
    for key, (lo, hi) in LIMITS.items():
        val = _get(bot, key)
        cast = int if isinstance(lo, int) and key != "starting_cash" else float
        _set(bot, key, cast(min(max(float(val), lo), hi)))
    seen = []
    for t in bot["universe"]:
        t = str(t).strip().upper()
        if t and t not in seen:
            seen.append(t)
    bot["universe"] = seen
    return bot


def load_config(path=CONFIG_PATH):
    return parse_config(json.loads(Path(path).read_text()), source=path)


def parse_config(raw, source="config"):
    cfg = {
        "broker": raw.get("broker", "paper"),
        "live_trading_confirmed": bool(raw.get("live_trading_confirmed", False)),
        "bots": [normalize_bot(b) for b in raw["bots"]],
    }
    ids = [b["id"] for b in cfg["bots"]]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate bot ids in {source}: {ids}")
    if cfg["broker"] not in ("paper", "schwab"):
        raise ValueError(f"Unknown broker {cfg['broker']!r}; use 'paper' or 'schwab'")
    return cfg


def load_exclusions(path=EXCLUSIONS_PATH):
    return json.loads(Path(path).read_text())
