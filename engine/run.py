"""Run the city: every building trades for today, then the website's data file is rewritten.

    python -m engine.run                 # real prices (Yahoo), broker from config/bots.json
    python -m engine.run --force         # rebalance now even if not due / market closed
    python -m engine.run --simulate 90   # fake prices, 90 trading days, for previews and tests
"""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .brokers import PaperBroker, make_broker
from .city import BotDay, equity, new_ledger
from .config import ROOT, load_config, load_exclusions
from .data import SyntheticPrices, YahooPrices
from .exclusions import Exclusions
from .strategy import ai_picks

LEDGER = ROOT / "state" / "ledger.json"
SIM_LEDGER = ROOT / "state" / "sim_ledger.json"
STATE_OUT = ROOT / "docs" / "data" / "state.json"
HISTORY_DAYS = 300


def load_ledger(path, cfg):
    data = json.loads(Path(path).read_text()) if Path(path).exists() else {"bots": {}}
    for bot in cfg["bots"]:
        data["bots"].setdefault(bot["id"], new_ledger(bot["starting_cash"]))
    return data


def no_ai_picker(*_):
    raise RuntimeError("AI picks are off: add an ANTHROPIC_API_KEY to the server settings to turn them on")


def all_tickers(cfg, ledger):
    out = set()
    for bot in cfg["bots"]:
        out |= set(bot["universe"])
        out |= {p["ticker"] for p in ledger["bots"][bot["id"]]["positions"].values()}
    return sorted(out)


def build_state(cfg, ledger, closes, excl_raw, source, broker_name, run_note):
    last = closes.ffill().iloc[-1]
    prices = {t: float(v) for t, v in last.items() if pd.notna(v)}
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    bots_out = []
    for bot in cfg["bots"]:
        led = ledger["bots"][bot["id"]]
        eq = equity(led, prices)
        positions = []
        for p in led["positions"].values():
            px = prices.get(p["ticker"], p["avg_cost"])
            positions.append(
                {
                    **p,
                    "price": round(px, 2),
                    "value": round(px * p["shares"], 2),
                    "pnl_pct": round(px / p["avg_cost"] - 1, 4) if p["avg_cost"] else 0,
                    "weight": round(px * p["shares"] / eq, 4) if eq else 0,
                }
            )
        positions.sort(key=lambda x: -x["value"])
        hist = led["history"]
        prev = hist[-2]["equity"] if len(hist) > 1 else led["contributed"]
        bots_out.append(
            {
                "id": bot["id"],
                "name": bot["name"],
                "sector": bot["sector"],
                "color": bot.get("color", "#a78bfa"),
                "enabled": bot["enabled"],
                "settings": bot,
                "cash": round(led["cash"], 2),
                # sale money that can't buy again until it settles (cash-account rule)
                "settling": round(sum(u["amount"] for u in led.get("unsettled", []) if u["settles"] > today), 2),
                "equity": round(eq, 2),
                "contributed": round(led["contributed"], 2),
                "pnl": round(eq - led["contributed"], 2),
                "pnl_pct": round(eq / led["contributed"] - 1, 4) if led["contributed"] else 0,
                "day_change": round(eq - prev, 2),
                "day_change_pct": round(eq / prev - 1, 4) if prev else 0,
                "positions": positions,
                "trades": list(reversed(led["trades"][-80:])),
                "history": hist,
                "ranking": led.get("ranking", []),
                "intraday_signals": led.get("intraday_signals", []),
                "ai": led.get("ai", {}),
                "blocked_in_universe": led.get("blocked_in_universe", []),
                "notes": list(reversed(led.get("notes", [])[-12:])),
                "last_momentum": led.get("last_momentum"),
                "last_ai": led.get("last_ai"),
            }
        )
    total = sum(b["equity"] for b in bots_out)
    contributed = sum(b["contributed"] for b in bots_out)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price_source": source,
        "broker": broker_name,
        "run_note": run_note,
        "totals": {
            "equity": round(total, 2),
            "contributed": round(contributed, 2),
            "pnl": round(total - contributed, 2),
            "pnl_pct": round(total / contributed - 1, 4) if contributed else 0,
            "day_change": round(sum(b["day_change"] for b in bots_out), 2),
        },
        "bots": bots_out,
        "config": {"broker": cfg["broker"], "live_trading_confirmed": cfg["live_trading_confirmed"], "bots": cfg["bots"]},
        "exclusions": excl_raw,
    }


def run_live(args):
    cfg = load_config()
    excl_raw = load_exclusions()
    excl = Exclusions(excl_raw)
    ledger = load_ledger(LEDGER, cfg)
    source = YahooPrices(cache_path=ROOT / "state" / "ticker_info.json")
    closes = source.closes(all_tickers(cfg, ledger), HISTORY_DAYS)

    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    last_bar = closes.index[-1].date()
    trading_day = last_bar == today_ny
    # Paper can be forced to trade on old prices; real money only trades while the market is open.
    trade = trading_day or (args.force and cfg["broker"] == "paper")
    note = "Traded" if trade else f"Market closed today (last prices from {last_bar}); values updated, no trades"

    broker = make_broker(cfg) if trade else PaperBroker()
    picker = ai_picks.claude_picks if (ai_picks.ai_available() and not args.no_ai) else no_ai_picker
    live_cash = broker.available_cash() if trade else None

    for bot in cfg["bots"]:
        led = ledger["bots"][bot["id"]]
        day = BotDay(bot, led, closes, today_ny, broker, excl, picker, info_fn=source.info, force=args.force)
        day.run(live_cash=live_cash, trade=trade)
        if live_cash is not None:
            live_cash = min(live_cash, broker.available_cash())

    LEDGER.parent.mkdir(exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, indent=1))
    state = build_state(cfg, ledger, closes, excl_raw, source.name, broker.name if trade else cfg["broker"], note)
    STATE_OUT.write_text(json.dumps(state, indent=1))
    print(note)
    for b in state["bots"]:
        print(f"  {b['name']:<16} ${b['equity']:>10,.2f}  {b['pnl_pct']:+.2%}  holdings: {', '.join(p['ticker'] for p in b['positions']) or '-'}")


def run_simulation(args):
    cfg = load_config()
    excl_raw = load_exclusions()
    excl = Exclusions(excl_raw)
    if SIM_LEDGER.exists():
        SIM_LEDGER.unlink()
    ledger = load_ledger(SIM_LEDGER, cfg)
    source = SyntheticPrices()
    tickers = all_tickers(cfg, ledger)
    broker = PaperBroker()
    days = source.trading_days(args.simulate)
    step = args.check_minutes
    for d in days:
        daily = source.closes(tickers, HISTORY_DAYS, as_of=d - pd.Timedelta(days=1))
        bars_c, bars_v = source.intraday(tickers, day=d)
        t = d + pd.Timedelta(hours=9, minutes=45)
        while t < d + pd.Timedelta(hours=15, minutes=56):
            c, v = bars_c[bars_c.index <= t], bars_v[bars_v.index <= t]
            frame = pd.concat([daily, c.iloc[[-1]].set_axis([d])])
            for bot in cfg["bots"]:
                BotDay(
                    bot, ledger["bots"][bot["id"]], frame, d, broker, excl, ai_picks.simulated_picks,
                    now=t, minute_bars=(c, v), settle=cfg["cash_account_rules"] == "always",
                ).run()
            t += pd.Timedelta(minutes=step)
    SIM_LEDGER.parent.mkdir(exist_ok=True)
    SIM_LEDGER.write_text(json.dumps(ledger, indent=1))
    closes = source.closes(tickers, HISTORY_DAYS)
    state = build_state(cfg, ledger, closes, excl_raw, source.name, "paper", f"Simulated {args.simulate} trading days with fake prices, checking every {args.check_minutes} minutes")
    out = Path(args.out) if args.out else STATE_OUT
    out.write_text(json.dumps(state, indent=1))
    print(f"Simulated {len(days)} days -> {out}")
    for b in state["bots"]:
        print(f"  {b['name']:<16} ${b['equity']:>10,.2f}  {b['pnl_pct']:+.2%}  trades: {len(b['trades'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebalance now even if not due or market closed")
    ap.add_argument("--no-ai", action="store_true", help="skip the AI sleeve this run")
    ap.add_argument("--simulate", type=int, metavar="DAYS", help="run on fake prices for DAYS trading days")
    ap.add_argument("--out", help="where to write the website data (simulation only)")
    ap.add_argument("--check-minutes", type=int, default=5, help="minutes between checks in a simulation")
    args = ap.parse_args()
    if args.simulate:
        run_simulation(args)
    else:
        run_live(args)


if __name__ == "__main__":
    main()
