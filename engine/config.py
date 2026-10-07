"""Loads and sanity-checks the city's settings (config/bots.json)."""
import copy
import re
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
    "momentum.fast_lookback_days": (2, 63),
    "momentum.stop_loss_pct": (0.01, 0.9),
    "momentum.trailing_stop_pct": (0.01, 0.9),
    "momentum.rank_buffer": (0, 10),
    "ai.max_picks": (1, 5),
    "ai.review_every_minutes": (30, 10080),
    "check_every_minutes": (2, 390),
    "min_hold_minutes": (0, 10080),
    "max_buys_per_day": (1, 500),
    "intraday.lookback_minutes": (2, 120),
    "intraday.entry_pct": (0.0005, 0.05),
    "intraday.take_profit_pct": (0.001, 0.2),
    "intraday.stop_pct": (0.001, 0.2),
    "intraday.max_positions": (1, 10),
    "intraday.min_hold_minutes": (0, 390),
    "intraday.cooldown_minutes": (0, 390),
}

DEFAULT_BOT = {
    "market": "us_stocks",
    "enabled": True,
    "starting_cash": 2500,
    "ai_share": 0.2,
    "check_every_minutes": 3,
    "min_hold_minutes": 30,
    "max_buys_per_day": 30,
    "style": "intraday",
    "intraday": {
        "lookback_minutes": 15,
        "entry_pct": 0.002,
        "take_profit_pct": 0.008,
        "stop_pct": 0.005,
        "max_positions": 2,
        "min_hold_minutes": 3,
        "cooldown_minutes": 20,
        "no_entries_after": "15:40",
        "close_out_at": "15:50",
    },
    "momentum": {
        "top_n": 3,
        "lookback_days": 126,
        "short_lookback_days": 63,
        "trend_sma_days": 50,
        "fast_lookback_days": 5,
        "stop_loss_pct": 0.1,
        "trailing_stop_pct": 0.07,
        "rank_buffer": 2,
    },
    "ai": {"max_picks": 2, "review_every_minutes": 120},
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


# The risk slider on each building's settings tab. Each level sets the numbers below; level 0
# means "custom" (the user typed their own numbers under Fine-tune). Higher risk = bigger, more
# concentrated bets with wider stops and quicker entries.
RISK_LEVELS = {
    1: {
        "name": "Careful",
        "intraday": {"entry_pct": 0.003, "take_profit_pct": 0.006, "stop_pct": 0.0035, "max_positions": 3, "cooldown_minutes": 30},
        "momentum": {"stop_loss_pct": 0.06, "trailing_stop_pct": 0.04, "top_n": 5},
    },
    2: {
        "name": "Steady",
        "intraday": {"entry_pct": 0.0025, "take_profit_pct": 0.007, "stop_pct": 0.0045, "max_positions": 2, "cooldown_minutes": 25},
        "momentum": {"stop_loss_pct": 0.08, "trailing_stop_pct": 0.05, "top_n": 4},
    },
    3: {
        "name": "Balanced",
        "intraday": {"entry_pct": 0.002, "take_profit_pct": 0.008, "stop_pct": 0.005, "max_positions": 2, "cooldown_minutes": 20},
        "momentum": {"stop_loss_pct": 0.1, "trailing_stop_pct": 0.07, "top_n": 3},
    },
    4: {
        "name": "Bold",
        "intraday": {"entry_pct": 0.0015, "take_profit_pct": 0.012, "stop_pct": 0.008, "max_positions": 1, "cooldown_minutes": 15},
        "momentum": {"stop_loss_pct": 0.12, "trailing_stop_pct": 0.09, "top_n": 2},
    },
    5: {
        "name": "Aggressive",
        "intraday": {"entry_pct": 0.001, "take_profit_pct": 0.02, "stop_pct": 0.012, "max_positions": 1, "cooldown_minutes": 10},
        "momentum": {"stop_loss_pct": 0.15, "trailing_stop_pct": 0.12, "top_n": 1},
    },
}


def detect_risk(bot):
    """The level whose numbers a building already uses, or 0 (custom)."""
    for level, preset in RISK_LEVELS.items():
        if all(abs(bot[sec][k] - v) < 1e-9 for sec in ("intraday", "momentum") for k, v in preset[sec].items()):
            return level
    return 0


def apply_risk(bot, level):
    for sec in ("intraday", "momentum"):
        bot[sec].update(RISK_LEVELS[level][sec])


RETIRED_KEYS = {"momentum": ["rebalance_days"], "ai": ["rebalance_days"], "": ["max_trades_per_day"]}


def normalize_bot(raw):
    bot = _merge(DEFAULT_BOT, raw)
    # risk level: the slider's numbers win; a building saved before the slider existed gets the
    # level matching its numbers, or "custom"
    risk = raw.get("risk")
    risk = detect_risk(bot) if risk is None else int(min(max(int(risk), 0), 5))
    if risk:
        apply_risk(bot, risk)
    bot["risk"] = risk
    for section, keys in RETIRED_KEYS.items():
        for k in keys:
            (bot[section] if section else bot).pop(k, None)
    if bot["style"] not in ("intraday", "swing"):
        raise ValueError(f"style must be 'intraday' or 'swing', not {bot['style']!r}")
    for k in ("no_entries_after", "close_out_at"):
        if not re.fullmatch(r"\d\d:\d\d", str(bot["intraday"][k])):
            raise ValueError(f"intraday.{k} must look like 15:40")
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
        # Cash-account settlement rule: "live" (only with real money), "always" (paper too), or "off"
        "cash_account_rules": raw.get("cash_account_rules", "live"),
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
