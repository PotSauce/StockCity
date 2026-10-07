"""One trading day for one building: capital changes, stop losses, then any due rebalances."""
import math
import pandas as pd

from .strategy import ai_picks, momentum

MAX_TRADES_KEPT = 300
REBALANCE_TOLERANCE = 0.10  # leave a holding alone if it's within 10% of its target size


def new_ledger(starting_cash):
    return {
        "cash": float(starting_cash),
        "contributed": float(starting_cash),
        "positions": {},
        "trades": [],
        "history": [],
        "last_momentum": None,
        "last_ai": None,
        "ai": {"status": "waiting", "picks": [], "market_view": None, "model": None, "date": None},
        "ranking": [],
        "notes": [],
    }


def _days_since(last, today):
    if not last:
        return 10_000
    return (pd.Timestamp(today) - pd.Timestamp(last)).days


def equity(led, prices):
    held = sum(p["shares"] * prices.get(p["ticker"], p["avg_cost"]) for p in led["positions"].values())
    return led["cash"] + held


class BotDay:
    def __init__(self, bot, led, closes, today, broker, excl, picker, info_fn=None, force=False):
        self.bot = bot
        self.led = led
        self.closes = closes
        self.today = str(pd.Timestamp(today).date())
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

    def _record(self, fill, sleeve, reason):
        self.led["trades"].append(
            {
                "date": self.today,
                "ticker": fill.ticker,
                "side": fill.side,
                "shares": fill.shares,
                "price": round(fill.price, 2),
                "value": round(fill.shares * fill.price, 2),
                "sleeve": sleeve,
                "reason": reason,
            }
        )
        self.led["trades"] = self.led["trades"][-MAX_TRADES_KEPT:]

    def _sell(self, key, shares, reason):
        pos = self.led["positions"][key]
        price = self.prices.get(pos["ticker"])
        if price is None or shares <= 0:
            return
        fill = self.broker.sell(pos["ticker"], int(shares), price)
        self.led["cash"] += fill.shares * fill.price
        pos["shares"] -= fill.shares
        if pos["shares"] <= 0:
            del self.led["positions"][key]
        self._record(fill, pos["sleeve"], reason)

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
            key, {"ticker": ticker, "sleeve": sleeve, "shares": 0, "avg_cost": 0.0, "opened": self.today}
        )
        total_cost = pos["avg_cost"] * pos["shares"] + fill.price * fill.shares
        pos["shares"] += fill.shares
        pos["avg_cost"] = round(total_cost / pos["shares"], 4)
        self._record(fill, sleeve, reason)

    # ---- the day ---------------------------------------------------------------
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
        for key, pos in list(self.led["positions"].items()):
            price = self.prices.get(pos["ticker"])
            if price is not None and price <= pos["avg_cost"] * (1 - stop):
                self._sell(key, pos["shares"], f"Stop loss: down {1 - price / pos['avg_cost']:.1%}")

    def sell_blocked(self):
        for key, pos in list(self.led["positions"].items()):
            why = self._blocked(pos["ticker"])
            if why:
                self._sell(key, pos["shares"], f"Do-not-buy list: {why}")

    def candidates(self):
        ok, blocked = [], []
        for t in self.bot["universe"]:
            why = self._blocked(t)
            (blocked if why else ok).append(t if not why else {"ticker": t, "reason": why})
        return [t for t in ok if t in self.closes.columns], blocked

    def plan_sleeve(self, sleeve, targets, slot_value):
        """Return (sells, buys) to move a sleeve to `targets` (list of tickers)."""
        sells, buys = [], []
        held = {p["ticker"]: (k, p) for k, p in self.led["positions"].items() if p["sleeve"] == sleeve}
        for ticker, (key, pos) in held.items():
            if ticker not in targets:
                sells.append((key, pos["shares"], "Rotated out: no longer a top pick"))
        for ticker in targets:
            price = self.prices.get(ticker)
            if not price:
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
                sells.append((held[ticker][0], have - want, "Trimmed back to target size"))
            elif want > have:
                buys.append((ticker, sleeve, want - have, "New pick" if not have else "Topped up to target size"))
        return sells, buys

    def run(self, live_cash=None, trade=True):
        self.adjust_capital()
        cands, blocked = self.candidates()
        ranking = momentum.rank(self.closes[cands], self.bot["momentum"]) if cands else []
        self.led["ranking"] = ranking[:12]
        self.led["blocked_in_universe"] = blocked

        if not trade:
            return self.finish()
        if not self.bot["enabled"]:
            self.notes.append("Paused: no trades")
            return self.finish()

        self.sell_blocked()
        self.stop_losses()

        eq = equity(self.led, self.prices)
        ai_share = self.bot["ai_share"]
        sells, buys = [], []

        m = self.bot["momentum"]
        if self.force or _days_since(self.led["last_momentum"], self.today) >= m["rebalance_days"]:
            targets = momentum.picks(ranking, m["top_n"])
            s, b = self.plan_sleeve("momentum", targets, eq * (1 - ai_share) / m["top_n"])
            sells += s
            buys += b
            self.led["last_momentum"] = self.today
            if not targets:
                self.notes.append("Momentum: nothing in an uptrend, sleeve stays in cash")

        a = self.bot["ai"]
        if ai_share > 0 and (self.force or _days_since(self.led["last_ai"], self.today) >= a["rebalance_days"]):
            ai_targets = self.ai_targets(cands, ranking)
            if ai_targets is not None:
                s, b = self.plan_sleeve("ai", ai_targets, eq * ai_share / a["max_picks"])
                sells += s
                buys += b
                self.led["last_ai"] = self.today

        for key, shares, reason in sells:
            if key in self.led["positions"]:
                self._sell(key, shares, reason)
        for ticker, sleeve, shares, reason in buys:
            cap = self.led["cash"] if live_cash is None else min(self.led["cash"], live_cash)
            before = self.led["cash"]
            self._buy(ticker, sleeve, shares, reason, cap)
            if live_cash is not None:
                live_cash -= before - self.led["cash"]
        return self.finish()

    def ai_targets(self, cands, ranking):
        holding_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
        avoid = sorted(set(holding_m) | set(momentum.picks(ranking, self.bot["momentum"]["top_n"])))
        cand_metrics = ai_picks.metrics(self.closes[cands]) if cands else []
        try:
            result = self.picker(self.bot["sector"], cand_metrics, self.bot["ai"]["max_picks"], avoid)
        except Exception as e:  # keep current AI holdings and retry next run
            self.led["ai"] = {**self.led["ai"], "status": "error", "error": str(e)[:300]}
            self.notes.append(f"AI picks failed, will retry next run: {str(e)[:120]}")
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
            "date": self.today,
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
