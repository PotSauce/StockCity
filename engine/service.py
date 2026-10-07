"""The always-on city: keeps prices fresh, runs each building's trading day, guards stop losses.

The web server (server/app.py) owns one City and calls these methods from a background loop.
Everything that must survive a restart lives in DATA_DIR: settings (bots.json) and the ledger.
"""
import json
import os
import shutil
import threading
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .brokers import PaperBroker, make_broker
from .city import BotDay, minutes_since, new_ledger
from .config import CONFIG_PATH, load_exclusions, normalize_bot, parse_config
from .data import SyntheticPrices, YahooPrices
from .exclusions import Exclusions
from .markets import make_market
from .run import build_state, no_ai_picker
from .strategy import ai_picks

NY = ZoneInfo("America/New_York")
HISTORY_DAYS = 300
# Bots trade inside this New York-time window, skipping the jumpy first 15 minutes and the
# closing auction. Each building checks on its own interval (check_every_minutes).
TRADE_START, TRADE_END = time(9, 45), time(15, 55)
STOP_CHECK_SECONDS = 300


def now_utc():
    return datetime.now(timezone.utc)


class City:
    def __init__(self, data_dir, price_source="yahoo"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data_dir / "bots.json"
        self.ledger_path = self.data_dir / "ledger.json"
        if not self.config_path.exists():
            shutil.copy(CONFIG_PATH, self.config_path)
        self.lock = threading.RLock()
        self.excl_raw = load_exclusions()
        self.excl = Exclusions(self.excl_raw)
        self.price_source = price_source
        self._add_new_repo_tickers()
        self.cfg = parse_config(json.loads(self.config_path.read_text()), source=self.config_path)
        self.ledger = self._load_ledger()
        self.markets = {}
        self.closes = {}  # market id -> daily closes
        self.quotes = {}  # market id -> {symbol: price}
        self.bars = {}  # market id -> today's 1-minute (closes, volumes)
        self.closes_day = None
        self.history_asked = {}  # market id -> symbols already asked for daily history today
        self.last_quote_at = None
        self.last_stop_check = None
        self.last_error = None
        self.run_note = "Starting up"

    # ---- persistence -----------------------------------------------------------
    def _add_new_repo_tickers(self):
        """Settings live in DATA_DIR, so ticker lists added to the repo later wouldn't reach a
        running city. When config/bots.json has a higher universe_version than the saved
        settings, add its new tickers to each building's saved list (keeping the user's own)."""
        repo = json.loads(CONFIG_PATH.read_text())
        saved = json.loads(self.config_path.read_text())
        if saved.get("universe_version", 1) >= repo.get("universe_version", 1):
            return
        repo_lists = {b["id"]: b.get("universe", []) for b in repo["bots"]}
        for b in saved["bots"]:
            have = b.get("universe", [])
            b["universe"] = have + [t for t in repo_lists.get(b["id"], []) if t not in have and not self.excl.is_blocked(t)]
        saved["universe_version"] = repo.get("universe_version", 1)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(saved, indent=2) + "\n")
        tmp.replace(self.config_path)

    def _load_ledger(self):
        data = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {"bots": {}, "meta": {}}
        data.setdefault("meta", {})
        for bot in self.cfg["bots"]:
            data["bots"].setdefault(bot["id"], new_ledger(bot["starting_cash"]))
        return data

    def save(self):
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ledger, indent=1))
        tmp.replace(self.ledger_path)

    # ---- markets & prices --------------------------------------------------------
    def market(self, market_id):
        if market_id not in self.markets:
            prices = (
                SyntheticPrices()
                if self.price_source == "simulated"
                else YahooPrices(self.data_dir / "ticker_info.json", self.data_dir / "tape.pkl")
            )
            self.markets[market_id] = make_market(market_id, prices=prices)
        return self.markets[market_id]

    def symbols(self, market_id):
        out = set()
        for bot in self.cfg["bots"]:
            if bot["market"] == market_id:
                out |= set(bot["universe"])
                out |= {p["ticker"] for p in self.ledger["bots"][bot["id"]]["positions"].values()}
        return sorted(out)

    def market_ids(self):
        return sorted({b["market"] for b in self.cfg["bots"]})

    def refresh_prices(self, force_history=False):
        """Daily history once a day (or when tickers change), live quotes every call."""
        with self.lock:
            self._refresh_prices(force_history)

    def holdings(self, market_id):
        return sorted(
            {p["ticker"] for b in self.cfg["bots"] if b["market"] == market_id for p in self.ledger["bots"][b["id"]]["positions"].values()}
        )

    def _refresh_prices(self, force_history):
        today = now_utc().astimezone(NY).date()
        for mid in self.market_ids():
            m = self.market(mid)
            syms = self.symbols(mid)
            m.prefetch_info(syms)
            # daily history: everything once a day, then only stocks that were added since
            asked = self.history_asked.setdefault(mid, set())
            if force_history or mid not in self.closes or self.closes_day != today:
                self.closes[mid] = m.history(syms, HISTORY_DAYS)
                asked.clear()
                asked.update(syms)
            elif set(syms) - asked:
                new = sorted(set(syms) - asked)
                extra = m.history(new, HISTORY_DAYS)
                asked.update(new)
                if not extra.empty:
                    have = self.closes[mid]
                    self.closes[mid] = have.join(extra[[c for c in extra.columns if c not in have.columns]], how="outer").ffill()
            try:
                if m.is_trading_day(today):
                    bars = m.intraday(syms, now=now_utc(), priority=self.holdings(mid))
                    self.bars[mid] = bars
                    if not bars[0].empty:
                        last = bars[0].ffill().iloc[-1]
                        self.quotes[mid] = {t: float(v) for t, v in last.items() if pd.notna(v) and v > 0}
                        continue
                self.quotes[mid] = m.quotes(syms)
            except Exception as e:  # keep the last quotes; history still works
                self.last_error = f"Quotes failed: {e}"
        self.closes_day = today
        self.last_quote_at = now_utc()

    def frame(self, market_id):
        """Daily closes with today's row filled in from live quotes."""
        closes = self.closes[market_id]
        quotes = self.quotes.get(market_id) or {}
        m = self.market(market_id)
        today = pd.Timestamp(now_utc().astimezone(NY).date())
        if not quotes or not m.is_trading_day(today.date()):
            return closes
        row = closes.iloc[-1].copy()
        for t, p in quotes.items():
            if t in row.index:
                row[t] = p
        out = closes[closes.index < today]
        return pd.concat([out, row.to_frame(today).T])

    # ---- trading -------------------------------------------------------------------
    def _picker(self):
        return ai_picks.claude_picks if ai_picks.ai_available() else no_ai_picker

    def settle(self):
        rule = self.cfg["cash_account_rules"]
        return rule == "always" or (rule == "live" and self.cfg["broker"] == "schwab")

    def bot_due(self, bot, now):
        return minutes_since(self.ledger["bots"][bot["id"]].get("last_check"), now) >= bot["check_every_minutes"]

    def trade_cycle(self, force=False):
        """A trading check for every building that's due (or all of them when forced)."""
        with self.lock:
            now = now_utc()
            now_ny = now.astimezone(NY)
            today = now_ny.date()
            self.refresh_prices()
            notes = []
            broker = None
            for mid in self.market_ids():
                m = self.market(mid)
                if not m.is_open(now):
                    # Run now while the market is closed refreshes the rankings but never trades:
                    # fills at stale prices would be fiction on paper and impossible with real money.
                    if force:
                        frame = self.frame(mid)
                        for bot in self.cfg["bots"]:
                            if bot["market"] == mid:
                                led = self.ledger["bots"][bot["id"]]
                                BotDay(bot, led, frame, today, PaperBroker(), self.excl, no_ai_picker, info_fn=m.info_cached).run(trade=False)
                    notes.append(f"{m.name}: market closed, no trades")
                    continue
                broker = broker or make_broker(self.cfg)
                live_cash = broker.available_cash()
                frame = self.frame(mid)
                checked = 0
                for bot in self.cfg["bots"]:
                    if bot["market"] != mid or not (force or self.bot_due(bot, now)):
                        continue
                    led = self.ledger["bots"][bot["id"]]
                    before = len(led["trades"])
                    BotDay(
                        bot, led, frame, today, broker, self.excl, self._picker(), info_fn=m.info_cached, force=force,
                        now=now_ny.replace(tzinfo=None), minute_bars=self.bars.get(mid), settle=self.settle(),
                    ).run(live_cash=live_cash)
                    led["last_check"] = now.isoformat(timespec="seconds")
                    checked += 1
                    if live_cash is not None:
                        live_cash = min(live_cash, broker.available_cash())
                    if len(led["trades"]) != before:
                        notes.append(f"{bot['name']}: {len(led['trades']) - before} trade(s)")
                if checked:
                    self.ledger["meta"]["last_trade_at"] = now.isoformat(timespec="seconds")
            when = now_ny.strftime("%-I:%M %p")
            self.run_note = f"Last check {when}: " + ("; ".join(notes) if notes else "no trades needed")
            self.save()
            return self.run_note

    def guard(self):
        """Between trading cycles: stop losses and the do-not-buy list, on live prices."""
        with self.lock:
            now = now_utc()
            now_ny = now.astimezone(NY).replace(tzinfo=None)
            today = now_ny.date()
            broker = None
            for mid in self.market_ids():
                m = self.market(mid)
                frame = self.frame(mid)
                kw = dict(now=now_ny, minute_bars=self.bars.get(mid), settle=self.settle())
                for bot in self.cfg["bots"]:
                    if bot["market"] != mid:
                        continue
                    led = self.ledger["bots"][bot["id"]]
                    if bot["enabled"] and m.is_open(now) and led["positions"]:
                        broker = broker or make_broker(self.cfg)
                        day = BotDay(bot, led, frame, today, broker, self.excl, no_ai_picker, info_fn=m.info_cached, **kw)
                        day.sell_blocked()
                        day.stop_losses()
                        day.finish()
                    elif m.is_trading_day(today):
                        BotDay(bot, led, frame, today, PaperBroker(), self.excl, no_ai_picker, **kw).finish()
            self.last_stop_check = now
            self.save()

    def due_for_trade(self):
        now = now_utc()
        if not (TRADE_START <= now.astimezone(NY).time() < TRADE_END):
            return False
        open_markets = {mid for mid in self.market_ids() if self.market(mid).is_open(now)}
        return any(b["market"] in open_markets and self.bot_due(b, now) for b in self.cfg["bots"])

    # ---- settings ------------------------------------------------------------------
    def update_bot(self, bot_id, settings):
        with self.lock:
            idx = next((i for i, b in enumerate(self.cfg["bots"]) if b["id"] == bot_id), None)
            if idx is None:
                raise KeyError(bot_id)
            merged = normalize_bot({**self.cfg["bots"][idx], **settings, "id": bot_id})
            blocked = [t for t in merged["universe"] if self.excl.is_blocked(t)]
            if blocked:
                raise ValueError(f"On the do-not-buy list: {', '.join(blocked)}")
            raw = json.loads(self.config_path.read_text())
            raw["bots"] = [merged if b["id"] == bot_id else b for b in raw["bots"]]
            self.cfg = parse_config(raw, source=self.config_path)
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2) + "\n")
            tmp.replace(self.config_path)
            return merged

    # ---- website data --------------------------------------------------------------
    def state(self):
        with self.lock:
            mid = self.market_ids()[0]
            frames = [self.frame(m) for m in self.market_ids()]
            closes = pd.concat(frames, axis=1) if len(frames) > 1 else frames[0]
            st = build_state(
                self.cfg, self.ledger, closes, self.excl_raw, self.market(mid).source, self.cfg["broker"], self.run_note
            )
            now = now_utc()
            st["server"] = {
                "mode": "live",
                "markets": [
                    {"id": m, "name": self.market(m).name, "open": self.market(m).is_open(now)} for m in self.market_ids()
                ],
                "last_quote_at": self.last_quote_at.isoformat(timespec="seconds") if self.last_quote_at else None,
                "last_trade_at": self.ledger["meta"].get("last_trade_at"),
                "trade_window_ny": f"{TRADE_START.strftime('%-I:%M')}–{TRADE_END.strftime('%-I:%M %p')}",
                "cash_account_rules": self.settle(),
                "ai_enabled": ai_picks.ai_available(),
                "password_set": bool(os.environ.get("APP_PASSWORD")),
                "last_error": self.last_error,
            }
            return st
