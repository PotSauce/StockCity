"""One trading check for one building: capital changes, stops, then any due rebalances.

Each building splits its money three ways: day trades (bought and sold the same day), held stocks
(its strongest stocks by momentum, held for days; sleeve "momentum") and AI picks (sleeve "ai").

The server runs a check every few minutes while the market is open. Guardrails keep that from
turning into churn: a minimum hold time, a daily trade cap, size tolerances, and a rank buffer
so a holding isn't swapped out the moment it slips one place in the leaderboard.
"""
import math
from datetime import date, datetime, time

import pandas as pd

from .config import held_share
from .markets.us_stocks import is_settlement_day, next_settlement_day
from .strategy import ai_picks, intraday, momentum

MAX_TRADES_KEPT = 500
REBALANCE_TOLERANCE = 0.10  # don't top up a holding that's within 10% of its slot
# Whole shares make target sizes jumpy, so a holding is only trimmed once it's worth more than 25%
# over its slot, and never sold to zero just for size.
TRIM_TOLERANCE = 0.25
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


def settlement_date(day):
    """`day` (YYYY-MM-DD) if trades settle that day, else the next day they do."""
    d = date.fromisoformat(day)
    return day if is_settlement_day(d) else str(next_settlement_day(d))


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
        # today's 1-minute bars (close, volume), used by day trades; newest prices win
        self.bars_close, self.bars_vol = minute_bars if minute_bars is not None else (None, None)
        if self.bars_close is not None and not self.bars_close.empty:
            for t, v in self.bars_close.ffill().iloc[-1].items():
                if pd.notna(v) and v > 0:
                    self.prices[t] = float(v)
        # cash-account rule: money from a sale can't buy again until it settles (next settlement day)
        self.settle = settle
        self.led.setdefault("unsettled", [])
        # dates saved before bank holidays were counted (a Friday sale "settling" on Columbus Day)
        # move to the day the money really settles
        for u in self.led["unsettled"]:
            u["settles"] = settlement_date(u["settles"])
        for pos in self.led["positions"].values():
            if pos.get("locked_until"):
                pos["locked_until"] = settlement_date(pos["locked_until"])
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

    def _locked_until(self, ticker):
        """When the building's shares of `ticker` bought with unsettled cash can be sold, or None."""
        until = max((p.get("locked_until") or "" for p in self.led["positions"].values() if p["ticker"] == ticker), default="")
        return until if until > self.today else None

    def _locked(self, pos):
        """Bought with unsettled cash: selling before that cash settles would be a good faith
        violation in a cash account, so the position is held until then (stops included). The
        broker sees one holding per stock, so the same stock in another sleeve waits too."""
        until = self._locked_until(pos["ticker"])
        if until:
            self._note_once(f"{pos['ticker']}: bought with unsettled cash, so it can't be sold until {until}")
            return True
        return False

    def _use_unsettled(self, amount):
        """Take `amount` out of unsettled sale money; returns when the money used settles (or None)."""
        settles = None
        for u in self.led["unsettled"]:
            if amount <= 0:
                break
            take = min(amount, u["amount"])
            if take > 0:
                u["amount"] = round(u["amount"] - take, 2)
                amount -= take
                settles = max(settles or u["settles"], u["settles"])
        self.led["unsettled"] = [u for u in self.led["unsettled"] if u["amount"] > 0]
        return settles

    def buys_today(self):
        return sum(1 for t in self.led["trades"] if t["date"] == self.today and t["side"] == "buy")

    def _record(self, fill, sleeve, reason, protective=False, cost=None):
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
                # what the shares cost, so a sale shows whether it made or lost money
                **({"cost": round(cost, 4)} if cost is not None else {}),
            }
        )
        self.led["trades"] = self.led["trades"][-MAX_TRADES_KEPT:]

    def _sell(self, key, shares, reason, protective=False):
        pos = self.led["positions"][key]
        price = self.prices.get(pos["ticker"])
        if price is None or shares <= 0:
            return
        fill = self.broker.sell(pos["ticker"], int(shares), price)
        cost = pos["avg_cost"]
        self.led["cash"] += fill.shares * fill.price
        if self.settle:
            settles = str(next_settlement_day(pd.Timestamp(self.today).date()))
            self.led["unsettled"].append({"amount": round(fill.shares * fill.price, 2), "settles": settles})
        pos["shares"] -= fill.shares
        if pos["shares"] <= 0:
            del self.led["positions"][key]
        self._record(fill, pos["sleeve"], reason, protective, cost)

    def _buy(self, ticker, sleeve, shares, reason, cash_cap):
        price = self.prices.get(ticker)
        if price is None or shares <= 0:
            return
        # Held stocks and the AI pick stay overnight, so they may buy with unsettled sale money and
        # are then held until that money settles. Day trades sell the same day, so they need settled
        # cash, and so does a held buy of a stock a day trade holds right now (it's sold today, and
        # the broker can't tell which of the shares were sold).
        if sleeve == "intraday" and self._locked_until(ticker):
            return
        unsettled_ok = self.settle and sleeve in ("ai", "momentum") and f"intraday:{ticker}" not in self.led["positions"]
        settled = max(0.0, self.spendable())
        usable = self.led["cash"] if unsettled_ok else settled
        shares = min(int(shares), int(min(cash_cap, usable) // (price * 1.002)))
        if shares <= 0:
            if self.settle and self.spendable() < self.led["cash"]:
                self._note_once(f"Waiting for cash to settle before buying more (cash account rule)")
            else:
                self._note_once(f"Not enough cash to buy {ticker}")
            return
        fill = self.broker.buy(ticker, shares, price)
        cost = fill.shares * fill.price
        self.led["cash"] -= cost
        # Like the broker, a buy is paid from settled cash first; only the rest is unsettled sale
        # money, and only then is the position locked. (Paying with sale money first would leave
        # settled cash on the books that the broker has already used, and a day trade bought with
        # it and sold the same day would be a good faith violation.)
        from_unsettled = round(cost - settled, 2)
        settles = self._use_unsettled(from_unsettled) if unsettled_ok and from_unsettled > 0 else None
        key = f"{sleeve}:{ticker}"
        pos = self.led["positions"].setdefault(
            key,
            {"ticker": ticker, "sleeve": sleeve, "shares": 0, "avg_cost": 0.0, "opened": self.today, "opened_at": self.now_iso, "high": fill.price},
        )
        if settles:
            pos["locked_until"] = max(pos.get("locked_until", ""), settles)
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
            hit = price <= pos["avg_cost"] * (1 - stop) or (price <= pos["high"] * (1 - trail) and pos["high"] > pos["avg_cost"])
            if hit and self._locked(pos):
                continue
            if price <= pos["avg_cost"] * (1 - stop):
                self._sell(key, pos["shares"], f"Stop loss: down {1 - price / pos['avg_cost']:.1%} from cost", True)
            elif price <= pos["high"] * (1 - trail) and pos["high"] > pos["avg_cost"]:
                self._sell(key, pos["shares"], f"Trailing stop: down {1 - price / pos['high']:.1%} from its high of ${pos['high']:,.2f}", True)

    def sell_blocked(self):
        for key, pos in list(self.led["positions"].items()):
            why = self._blocked(pos["ticker"])
            if why and not self._locked(pos):
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
                if (self._held_minutes(pos) < min_hold and not self.force) or self._locked(pos):
                    continue
                sells.append((key, pos["shares"], "Rotated out: no longer a top pick"))
        # a day trade's take profit, stop or close-out says nothing about holding the stock for days
        stopped_today = {t["ticker"] for t in self.led["trades"] if t["date"] == self.today and t.get("protective") and t["sleeve"] != "intraday"}
        # nor is a stock rotated out of held stocks or AI picks today: a score near 0 flips on small
        # price moves, and each round trip pays the spread and turns settled cash into sale money
        rotated_today = {
            t["ticker"] for t in self.led["trades"]
            if t["date"] == self.today and t["side"] == "sell" and t["sleeve"] != "intraday" and t["reason"].startswith("Rotated out")
        }
        for ticker in targets:
            price = self.prices.get(ticker)
            if not price:
                continue
            if ticker in stopped_today and ticker not in held:
                self._note_once(f"{ticker}: not buying back today after a stop")
                continue
            if ticker in rotated_today and ticker not in held:
                self._note_once(f"{ticker}: not buying back today after rotating it out")
                continue
            want = math.floor(slot_value / price)
            have = held.get(ticker, (None, {"shares": 0}))[1]["shares"]
            if want == 0 and not have:
                self._note_once(f"Skipped {ticker} ({sleeve}): one share costs more than its ${slot_value:,.0f} slot")
            if want < have:
                # still a pick: keep at least one share, and leave it alone unless it's well over its slot
                keep = max(want, 1)
                if keep < have and have * price > (1 + TRIM_TOLERANCE) * slot_value:
                    if (self._held_minutes(held[ticker][1]) >= min_hold or self.force) and not self._locked(held[ticker][1]):
                        sells.append((held[ticker][0], have - keep, "Trimmed back to target size"))
            elif want > have:
                if have and (want - have) * price <= REBALANCE_TOLERANCE * slot_value:
                    continue
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
            if self._locked_until(t):
                continue  # shares of it bought with unsettled cash can't be sold today; try the next mover
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
        day_share, ai_share, hold_share = self.bot["day_share"], self.bot["ai_share"], held_share(self.bot)
        sells, buys = [], []

        # Day trades (their stops and the close-out run in stop_losses even when this share is 0)
        if day_share > 0:
            s, b = self.plan_intraday(cands, eq * day_share)
            sells += s
            buys += b
        else:
            self.led["intraday_signals"] = []

        # Held stocks: the building's strongest stocks by momentum, held for days
        targets = []
        if hold_share > 0:
            held_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
            # a stock the AI picks hold (or are set to buy) is left to them, so the two don't double up
            ai_side = {p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "ai"}
            if ai_share > 0:
                ai_side |= set(self.led.get("ai_targets") or [])
            slot = eq * hold_share / m["top_n"]
            # whole shares only: a stock where one share costs more than a slot is passed over for
            # the next strongest, or that slot would sit in cash (stocks already held stay eligible)
            affordable = [
                r for r in ranking
                if r["ticker"] in held_m or (r["ticker"] not in ai_side and self.prices.get(r["ticker"], math.inf) <= slot)
            ]
            targets = momentum.picks_with_buffer(affordable, m["top_n"], held_m, m["rank_buffer"])
            s, b = self.plan_sleeve("momentum", targets, slot)
            sells += s
            buys += b
            rising = momentum.picks(ranking, len(ranking))
            if not targets and [t for t in rising if t not in ai_side]:
                self._note_once(f"Held stocks: one share of every stock in an uptrend costs more than the ${slot:,.0f} slot, so that money stays in cash")
            elif not targets and rising:
                self._note_once("Held stocks: every stock in an uptrend is already an AI pick, so that money stays in cash")
            elif not targets:
                self._note_once("Held stocks: nothing in an uptrend, that money stays in cash")
        else:
            # held stocks left over from before the share went to 0 are rotated out
            s, _ = self.plan_sleeve("momentum", [], 0)
            sells += s
        self.led["last_momentum"] = self.now_iso

        a = self.bot["ai"]
        if ai_share > 0 and (self.force or minutes_since(self.led["last_ai"], self.now) >= a["review_every_minutes"]):
            ai_targets = self.ai_targets(cands, ranking, eq * ai_share / a["max_picks"], targets)
            if ai_targets is not None:
                self.led["ai_targets"] = ai_targets
                self.led["last_ai"] = self.now_iso
        if ai_share > 0 and self.led.get("ai_targets") is not None:
            s, b = self.plan_sleeve("ai", self.led["ai_targets"], eq * ai_share / a["max_picks"])
            sells += s
            buys += b
        elif ai_share <= 0:
            # AI picks left over from before the share went to 0 are rotated out
            s, _ = self.plan_sleeve("ai", [], 0)
            sells += s

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

    def ai_targets(self, cands, ranking, slot_value, held_targets=()):
        """`held_targets`: the stocks the held sleeve aims to own this check (what it holds or is buying)."""
        holding_m = [p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "momentum"]
        avoid = sorted(set(holding_m) | set(held_targets))
        # only offer stocks where at least one share fits the AI slot; a current pick stays eligible
        # after its price rises past the slot (it keeps its share rather than being sold for size)
        holding_ai = {p["ticker"] for p in self.led["positions"].values() if p["sleeve"] == "ai"}
        affordable = [t for t in cands if t in holding_ai or self.prices.get(t, 1e12) <= slot_value]
        if not affordable:
            self._note_once(f"AI picks: no stock here costs under its ${slot_value:,.0f} slot")
            return []
        # Claude sees the strongest 40 by momentum, which keeps each request small (current picks always)
        ranked = [r["ticker"] for r in ranking if r["ticker"] in affordable]
        affordable = (ranked + [t for t in affordable if t not in ranked])[:AI_CANDIDATES]
        affordable += [t for t in cands if t in holding_ai and t not in affordable]
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
