"""One trading check for one building: capital changes, stops, then any due rebalances.

The server runs a check every few minutes while the market is open. Guardrails keep that from
turning into churn: a minimum hold time, a daily trade cap, a size tolerance, and a rank buffer
so a holding isn't swapped out the moment it slips one place in the leaderboard.
"""
import math
from datetime import datetime, time

import pandas as pd

from .markets.us_stocks import next_trading_day
from .strategy import ai_picks, intraday, momentum

MAX_TRADES_KEPT = 500
REBALANCE_TOLERANCE = 0.10  # leave a holding alone if it's within 10% of its target size
AI_CANDIDATES = 40  # stocks Claude reviews per building
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
    def __init__(self, bot, led, closes, today, broker, excl, picker, info_fn=None, force=False, now=None, minute_bars=None, settle=False):
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
        # today's 1-minute bars (close, volume), used by the intraday style; newest prices win
        self.bars_close, self.bars_vol = minute_bars if minute_bars is not None else (None, None)
        if self.bars_close is not None and not self.bars_close.empty:
            for t, v in self.bars_close.ffill().iloc[-1].items():
                if pd.notna(v) and v > 0:
                    self.prices[t] = float(v)
        # cash-account rule: money from a sale can't buy again until it settles (next trading day)
        self.settle = settle
        self.led.setdefault("unsettled", [])
        self.led["unsettled"] = [u for u in self.led["unsettled"] if u["settles"] > self.today]
        self.notes = []

    # ---- helpers -------------------------------------------------------------
    def _blocked(self, ticker):
        return self.excl.reason(ticker, **(self.info_fn(ticker) or {}))

    def _known(self, ticker):
        """False while the stock's sector is still being looked up; it isn't bought until then."""
        return self.info_fn(ticker) is not None

    def spendable(self):
        return self.led["cash"] - sum(u["amount"] for u in self.led["unsettled"])

    def buys_today(self):
        return sum(1 for t in self.led["trades"] if t["date"] == self.today and t["side"] == "buy")

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
        if self.settle:
            settles = str(next_trading_day(pd.Timestamp(self.today).date()))
            self.led["unsettled"].append({"amount": round(fill.shares * fill.price, 2), "settles": settles})
        pos["shares"] -= fill.shares
        if pos["shares"] <= 0:
            del self.led["positions"][key]
        self._record(fill, pos["sleeve"], reason, protective)

    def _buy(self, ticker, sleeve, shares, reason, cash_cap):
        price = self.prices.get(ticker)
        if price is None or shares <= 0:
            return
        shares = min(int(shares), int(min(cash_cap, self.spendable()) // (price * 1.002)))
        if shares <= 0:
            if self.settle and self.spendable() < self.led["cash"]:
                self._note_once(f"Waiting for cash to settle before buying more (cash account rule)")
            else:
                self._note_once(f"Not enough cash to buy {ticker}")
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
        day = self.bot["intraday"]
        for key, pos in list(self.led["positions"].items()):
            price = self.prices.get(pos["ticker"])
            if price is None:
                continue
            pos["high"] = max(pos.get("high", pos["avg_cost"]), price)
            if pos["sleeve"] == "intraday":
                gain = price / pos["avg_cost"] - 1
                if self.now.strftime("%H:%M") >= day["close_out_at"] or pos["opened"] != self.today:
                    self._sell(key, pos["shares"], f"End-of-day close-out ({gain:+.2%})", True)
                elif gain >= day["take_profit_pct"]:
                    self._sell(key, pos["shares"], f"Take profit: up {gain:.2%}", True)
                elif gain <= -day["stop_pct"]:
                    self._sell(key, pos["shares"], f"Stop: down {-gain:.2%}", True)
                continue
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
        ok, blocked, pending = [], [], 0
        for t in self.bot["universe"]:
            why = self._blocked(t)
            if why:
                blocked.append({"ticker": t, "reason": why})
            elif not self._known(t):
                pending += 1
            else:
                ok.append(t)
        if pending:
            self._note_once("Still checking some stocks against the do-not-buy list; they can't be bought until that's done")
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
                self._note_once(f"Skipped {ticker} ({sleeve}): one share costs more than its ${slot_value:,.0f} slot")
            have = held.get(ticker, (None, {"shares": 0}))[1]["shares"]
            if have and abs(want - have) * price <= REBALANCE_TOLERANCE * slot_value:
                continue
            if want < have:
                if self._held_minutes(held[ticker][1]) >= min_hold or self.force:
                    sells.append((held[ticker][0], have - want, "Trimmed back to target size"))
            elif want > have:
                buys.append((ticker, sleeve, want - have, "New pick" if not have else "Topped up to target size"))
        return sells, buys

    def _minutes_since_exit(self, ticker):
        exits = [t for t in self.led["trades"] if t["ticker"] == ticker and t["side"] == "sell" and t["sleeve"] == "intraday" and t["date"] == self.today]
        if not exits:
            return 10**9
        return minutes_since(f"{exits[-1]['date']}T{exits[-1].get('time', '00:00')}", self.now)

    def plan_intraday(self, cands, sleeve_value):
        """Intraday sleeve: exit fading runs, enter stocks that are running up right now."""
        cfg = self.bot["intraday"]
        if self.bars_close is None or self.bars_close.empty:
            self._note_once("Intraday: waiting for today's minute-by-minute prices")
            return [], []
        cols = [t for t in cands if t in self.bars_close.columns]
        sig = intraday.signals(self.bars_close[cols], self.bars_vol, cfg)
        self.led["intraday_signals"] = sig[:12]
        by_ticker = {r["ticker"]: r for r in sig}
        held = {p["ticker"]: (k, p) for k, p in self.led["positions"].items() if p["sleeve"] == "intraday"}
        sells, buys = [], []
        for ticker, (key, pos) in held.items():
            r = by_ticker.get(ticker)
            if r and r["fading"] and self._held_minutes(pos) >= cfg["min_hold_minutes"]:
                why = "fell below VWAP" if not r["above_vwap"] else f"{cfg['lookback_minutes']}-min move turned {r['move']:+.2%}"
                sells.append((key, pos["shares"], f"Run faded: {why}"))
        if self.now.strftime("%H:%M") >= cfg["no_entries_after"]:
            return sells, []
        open_slots = cfg["max_positions"] - (len(held) - len(sells))
        slot_value = sleeve_value / cfg["max_positions"]
        for r in sig:
            if open_slots <= 0:
                break
            t = r["ticker"]
            if not r["qualifies"] or t in held or self._minutes_since_exit(t) < cfg["cooldown_minutes"]:
                continue
            shares = math.floor(slot_value / r["price"])
            if shares == 0:
                continue  # too pricey for this slot; try the next mover
            buys.append((t, "intraday", shares, f"Running up {r['move']:+.2%} in {cfg['lookback_minutes']} min, above VWAP"))
            open_slots -= 1
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

        cap = self.bot["max_buys_per_day"]
        eq = equity(self.led, self.prices)
        ai_share = self.bot["ai_share"]
        sells, buys = [], []

        if self.bot["style"] == "intraday":
            s, b = self.plan_intraday(cands, eq * (1 - ai_share))
            sells += s
            buys += b
            # holdings left over from the swing style are rotated out
            s, _ = self.plan_sleeve("momentum", [], 0)
            sells += s
        else:
            held_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
            targets = momentum.picks_with_buffer(ranking, m["top_n"], held_m, m["rank_buffer"])
            s, b = self.plan_sleeve("momentum", targets, eq * (1 - ai_share) / m["top_n"])
            sells += s
            buys += b
            if not targets:
                self._note_once("Momentum: nothing in an uptrend, sleeve stays in cash")
        self.led["last_momentum"] = self.now_iso

        a = self.bot["ai"]
        if ai_share > 0 and (self.force or minutes_since(self.led["last_ai"], self.now) >= a["review_every_minutes"]):
            ai_targets = self.ai_targets(cands, ranking, eq * ai_share / a["max_picks"])
            if ai_targets is not None:
                self.led["ai_targets"] = ai_targets
                self.led["last_ai"] = self.now_iso
        if ai_share > 0 and self.led.get("ai_targets") is not None:
            s, b = self.plan_sleeve("ai", self.led["ai_targets"], eq * ai_share / a["max_picks"])
            sells += s
            buys += b

        # Selling is always allowed; the daily cap limits new buys.
        for key, shares, reason in sells:
            if key in self.led["positions"]:
                self._sell(key, shares, reason)
        room = cap - self.buys_today() if not self.force else 10**6
        for ticker, sleeve, shares, reason in buys:
            if room <= 0:
                self._note_once(f"Reached today's limit of {cap} buys; selling still works")
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

    def ai_targets(self, cands, ranking, slot_value):
        holding_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
        avoid = sorted(set(holding_m) | set(momentum.picks(ranking, self.bot["momentum"]["top_n"])))
        # only offer stocks where at least one share fits the AI slot
        affordable = [t for t in cands if self.prices.get(t, 1e12) <= slot_value]
        if not affordable:
            self._note_once(f"AI picks: no stock here costs under its ${slot_value:,.0f} slot")
            return []
        # Claude sees the strongest 40 by momentum, which keeps each request small
        ranked = [r["ticker"] for r in ranking if r["ticker"] in affordable]
        affordable = (ranked + [t for t in affordable if t not in ranked])[:AI_CANDIDATES]
        cand_metrics = ai_picks.metrics(self.closes[affordable])
        try:
            result = self.picker(self.bot["sector"], cand_metrics, self.bot["ai"]["max_picks"], avoid)
        except Exception as e:  # keep current AI holdings and retry next time
            self.led["ai"] = {**self.led["ai"], "status": "error", "error": str(e)[:300]}
            self._note_once(f"AI picks unavailable: {str(e)[:140]}")
            return None
        allowed = set(affordable)
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
