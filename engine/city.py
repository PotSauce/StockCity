"""One trading check for one building: capital changes, stops, then any due rebalances.

The server runs a check every few minutes while the market is open. Guardrails keep that from
turning into churn: a minimum hold time, a daily trade cap, a size tolerance, and a rank buffer
so a holding isn't swapped out the moment it slips one place in the leaderboard.
"""
import math
from datetime import datetime, time

import pandas as pd

from .strategy import ai_picks, momentum

MAX_TRADES_KEPT = 500
REBALANCE_TOLERANCE = 0.10  # leave a holding alone if it's within 10% of its target size
SIM_TIME = time(15, 35)  # simulated runs pretend each day's check happens at this time


def new_ledger(starting_cash):
    return {
        "cash": float(starting_cash),
        "contributed": float(starting_cash),
        "positions": {},
        "trades": [],
        "history": [],
        "last_momentum": None,
        "last_ai": None,
        "last_check": None,
        "ai": {"status": "waiting", "picks": [], "market_view": None, "model": None, "date": None},
        "ranking": [],
        "notes": [],
    }


def minutes_since(last, now):
    if not last:
        return 10**9
    return (pd.Timestamp(now) - pd.Timestamp(last)).total_seconds() / 60


def equity(led, prices):
    held = sum(p["shares"] * prices.get(p["ticker"], p["avg_cost"]) for p in led["positions"].values())
    return led["cash"] + held


class BotDay:
    def __init__(self, bot, led, closes, today, broker, excl, picker, info_fn=None, force=False, now=None):
        self.bot = bot
        self.led = led
        self.closes = closes
        self.today = str(pd.Timestamp(today).date())
        self.now = pd.Timestamp(now) if now is not None else pd.Timestamp(datetime.combine(pd.Timestamp(today).date(), SIM_TIME))
        self.now_iso = self.now.isoformat()
        self.broker = broker
        self.excl = excl
        self.picker = picker
        self.info_fn = info_fn or (lambda t: {})
        self.force = force
        last = closes.ffill().iloc[-1]
        self.prices = {t: float(v) for t, v in last.items() if pd.notna(v) and v > 0}
        self.notes = []

    # ---- helpers -------------------------------------------------------------
    def _blocked(self, ticker):
        return self.excl.reason(ticker, **self.info_fn(ticker))

    def trades_today(self):
        return sum(1 for t in self.led["trades"] if t["date"] == self.today and not t.get("protective"))

    def _record(self, fill, sleeve, reason, protective=False):
        self.led["trades"].append(
            {
                "date": self.today,
                "time": self.now.strftime("%H:%M"),
                "ticker": fill.ticker,
                "side": fill.side,
                "shares": fill.shares,
                "price": round(fill.price, 2),
                "value": round(fill.shares * fill.price, 2),
                "sleeve": sleeve,
                "reason": reason,
                **({"protective": True} if protective else {}),
            }
        )
        self.led["trades"] = self.led["trades"][-MAX_TRADES_KEPT:]

    def _sell(self, key, shares, reason, protective=False):
        pos = self.led["positions"][key]
        price = self.prices.get(pos["ticker"])
        if price is None or shares <= 0:
            return
        fill = self.broker.sell(pos["ticker"], int(shares), price)
        self.led["cash"] += fill.shares * fill.price
        pos["shares"] -= fill.shares
        if pos["shares"] <= 0:
            del self.led["positions"][key]
        self._record(fill, pos["sleeve"], reason, protective)

    def _buy(self, ticker, sleeve, shares, reason, cash_cap):
        price = self.prices.get(ticker)
        if price is None or shares <= 0:
            return
        shares = min(int(shares), int(cash_cap // (price * 1.002)))
        if shares <= 0:
            self.notes.append(f"Not enough cash to buy {ticker}")
            return
        fill = self.broker.buy(ticker, shares, price)
        self.led["cash"] -= fill.shares * fill.price
        key = f"{sleeve}:{ticker}"
        pos = self.led["positions"].setdefault(
            key,
            {"ticker": ticker, "sleeve": sleeve, "shares": 0, "avg_cost": 0.0, "opened": self.today, "opened_at": self.now_iso, "high": fill.price},
        )
        total_cost = pos["avg_cost"] * pos["shares"] + fill.price * fill.shares
        pos["shares"] += fill.shares
        pos["avg_cost"] = round(total_cost / pos["shares"], 4)
        self._record(fill, sleeve, reason)

    def _held_minutes(self, pos):
        return minutes_since(pos.get("opened_at") or pos.get("opened"), self.now)

    # ---- protective sells (always allowed, never count toward the daily cap) ---------
    def adjust_capital(self):
        delta = self.bot["starting_cash"] - self.led["contributed"]
        if abs(delta) < 0.01:
            return
        if delta < 0:
            delta = -min(-delta, self.led["cash"])
        self.led["cash"] += delta
        self.led["contributed"] += delta
        self.notes.append(f"Capital {'added' if delta > 0 else 'withdrawn'}: ${abs(delta):,.2f}")

    def stop_losses(self):
        stop = self.bot["momentum"]["stop_loss_pct"]
        trail = self.bot["momentum"]["trailing_stop_pct"]
        for key, pos in list(self.led["positions"].items()):
            price = self.prices.get(pos["ticker"])
            if price is None:
                continue
            pos["high"] = max(pos.get("high", pos["avg_cost"]), price)
            if price <= pos["avg_cost"] * (1 - stop):
                self._sell(key, pos["shares"], f"Stop loss: down {1 - price / pos['avg_cost']:.1%} from cost", True)
            elif price <= pos["high"] * (1 - trail) and pos["high"] > pos["avg_cost"]:
                self._sell(key, pos["shares"], f"Trailing stop: down {1 - price / pos['high']:.1%} from its high of ${pos['high']:,.2f}", True)

    def sell_blocked(self):
        for key, pos in list(self.led["positions"].items()):
            why = self._blocked(pos["ticker"])
            if why:
                self._sell(key, pos["shares"], f"Do-not-buy list: {why}", True)

    # ---- planning --------------------------------------------------------------------
    def candidates(self):
        ok, blocked = [], []
        for t in self.bot["universe"]:
            why = self._blocked(t)
            (blocked if why else ok).append(t if not why else {"ticker": t, "reason": why})
        return [t for t in ok if t in self.closes.columns], blocked

    def plan_sleeve(self, sleeve, targets, slot_value):
        """Return (sells, buys) to move a sleeve to `targets` (list of tickers)."""
        sells, buys = [], []
        min_hold = self.bot["min_hold_minutes"]
        held = {p["ticker"]: (k, p) for k, p in self.led["positions"].items() if p["sleeve"] == sleeve}
        for ticker, (key, pos) in held.items():
            if ticker not in targets:
                if self._held_minutes(pos) < min_hold and not self.force:
                    continue
                sells.append((key, pos["shares"], "Rotated out: no longer a top pick"))
        stopped_today = {t["ticker"] for t in self.led["trades"] if t["date"] == self.today and t.get("protective")}
        for ticker in targets:
            price = self.prices.get(ticker)
            if not price:
                continue
            if ticker in stopped_today and ticker not in held:
                self._note_once(f"{ticker}: not buying back today after a stop")
                continue
            want = math.floor(slot_value / price)
            if want == 0:
                self.notes.append(
                    f"Skipped {ticker} ({sleeve}): one share (${price:,.0f}) costs more than its "
                    f"${slot_value:,.0f} slot. Add capital to this building to fix."
                )
            have = held.get(ticker, (None, {"shares": 0}))[1]["shares"]
            if have and abs(want - have) * price <= REBALANCE_TOLERANCE * slot_value:
                continue
            if want < have:
                if self._held_minutes(held[ticker][1]) >= min_hold or self.force:
                    sells.append((held[ticker][0], have - want, "Trimmed back to target size"))
            elif want > have:
                buys.append((ticker, sleeve, want - have, "New pick" if not have else "Topped up to target size"))
        return sells, buys

    # ---- the check -------------------------------------------------------------------
    def run(self, live_cash=None, trade=True):
        self.adjust_capital()
        cands, blocked = self.candidates()
        m = self.bot["momentum"]
        ranking = momentum.rank(self.closes[cands], m) if cands else []
        self.led["ranking"] = ranking[:12]
        self.led["blocked_in_universe"] = blocked

        if not trade:
            return self.finish()
        if not self.bot["enabled"]:
            self.notes.append("Paused: no trades")
            return self.finish()

        self.sell_blocked()
        self.stop_losses()

        cap = self.bot["max_trades_per_day"]
        if self.trades_today() >= cap and not self.force:
            self._note_once(f"Reached today's limit of {cap} trades; only stops will trade until tomorrow")
            return self.finish()

        eq = equity(self.led, self.prices)
        ai_share = self.bot["ai_share"]
        sells, buys = [], []

        held_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
        targets = momentum.picks_with_buffer(ranking, m["top_n"], held_m, m["rank_buffer"])
        s, b = self.plan_sleeve("momentum", targets, eq * (1 - ai_share) / m["top_n"])
        sells += s
        buys += b
        self.led["last_momentum"] = self.now_iso
        if not targets:
            self._note_once("Momentum: nothing in an uptrend, sleeve stays in cash")

        a = self.bot["ai"]
        if ai_share > 0 and (self.force or minutes_since(self.led["last_ai"], self.now) >= a["review_every_minutes"]):
            ai_targets = self.ai_targets(cands, ranking)
            if ai_targets is not None:
                self.led["ai_targets"] = ai_targets
                self.led["last_ai"] = self.now_iso
        if ai_share > 0 and self.led.get("ai_targets") is not None:
            s, b = self.plan_sleeve("ai", self.led["ai_targets"], eq * ai_share / a["max_picks"])
            sells += s
            buys += b

        room = cap - self.trades_today() if not self.force else 10**6
        for key, shares, reason in sells:
            if room <= 0:
                break
            if key in self.led["positions"]:
                self._sell(key, shares, reason)
                room -= 1
        for ticker, sleeve, shares, reason in buys:
            if room <= 0:
                self._note_once(f"Reached today's limit of {cap} trades")
                break
            cash_cap = self.led["cash"] if live_cash is None else min(self.led["cash"], live_cash)
            before = self.led["cash"]
            self._buy(ticker, sleeve, shares, reason, cash_cap)
            if self.led["cash"] != before:
                room -= 1
            if live_cash is not None:
                live_cash -= before - self.led["cash"]
        return self.finish()

    def _note_once(self, text):
        if not any(n["date"] == self.today and n["text"] == text for n in self.led.get("notes", [])) and text not in self.notes:
            self.notes.append(text)

    def ai_targets(self, cands, ranking):
        holding_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
        avoid = sorted(set(holding_m) | set(momentum.picks(ranking, self.bot["momentum"]["top_n"])))
        cand_metrics = ai_picks.metrics(self.closes[cands]) if cands else []
        try:
            result = self.picker(self.bot["sector"], cand_metrics, self.bot["ai"]["max_picks"], avoid)
        except Exception as e:  # keep current AI holdings and retry next time
            self.led["ai"] = {**self.led["ai"], "status": "error", "error": str(e)[:300]}
            self._note_once(f"AI picks unavailable: {str(e)[:140]}")
            return None
        allowed = set(cands)
        picks, rejected = [], []
        for p in result["picks"]:
            t = p["ticker"].upper().strip()
            why = self._blocked(t) or (None if t in allowed else "not in this building's ticker list")
            if why:
                rejected.append({"ticker": t, "reason": why})
            elif t not in [x["ticker"] for x in picks]:
                picks.append({**p, "ticker": t})
        picks = picks[: self.bot["ai"]["max_picks"]]
        self.led["ai"] = {
            "status": "ok",
            "date": f"{self.today} {self.now.strftime('%H:%M')}",
            "model": result.get("model"),
            "market_view": result.get("market_view"),
            "picks": picks,
            "rejected": rejected,
        }
        return [p["ticker"] for p in picks]

    def finish(self):
        eq = round(equity(self.led, self.prices), 2)
        hist = [h for h in self.led["history"] if h["date"] != self.today]
        hist.append({"date": self.today, "equity": eq})
        self.led["history"] = hist[-400:]
        self.led["notes"] = (self.led.get("notes", []) + [{"date": self.today, "text": n} for n in self.notes])[-40:]
        return self.led
