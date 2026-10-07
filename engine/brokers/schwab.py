"""Real-money trading through Schwab's Trader API (via the schwab-py library).

Needs these GitHub Actions secrets:
  SCHWAB_APP_KEY, SCHWAB_APP_SECRET  - from your app at developer.schwab.com
  SCHWAB_TOKEN_JSON                  - contents of the token file made by `python -m engine.schwab_login`
  SCHWAB_ACCOUNT_NUMBER (optional)   - which account to trade if you have several

Schwab refresh tokens expire after 7 days, so the login step has to be repeated weekly.
"""
import json
import os
import tempfile
import time

from .base import Broker, Fill


class SchwabBroker(Broker):
    name = "schwab"
    live = True

    def __init__(self, fill_timeout_s=60):
        from schwab.auth import client_from_token_file

        key = os.environ["SCHWAB_APP_KEY"]
        secret = os.environ["SCHWAB_APP_SECRET"]
        token_json = os.environ["SCHWAB_TOKEN_JSON"]
        fd, self._token_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(token_json)
        self.client = client_from_token_file(self._token_path, key, secret)
        self.fill_timeout_s = fill_timeout_s

        accounts = self.client.get_account_numbers()
        accounts.raise_for_status()
        accounts = accounts.json()
        wanted = os.environ.get("SCHWAB_ACCOUNT_NUMBER")
        match = [a for a in accounts if not wanted or a["accountNumber"] == wanted]
        if not match:
            raise RuntimeError("SCHWAB_ACCOUNT_NUMBER not found in this Schwab login")
        self.account_hash = match[0]["hashValue"]

    def available_cash(self):
        r = self.client.get_account(self.account_hash)
        r.raise_for_status()
        bal = r.json()["securitiesAccount"]["currentBalances"]
        return float(bal.get("cashAvailableForTrading", bal.get("availableFunds", 0.0)))

    def _place(self, spec, ticker, side, shares, ref_price):
        from schwab.utils import Utils

        resp = self.client.place_order(self.account_hash, spec)
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Schwab rejected {side} {shares} {ticker}: {resp.status_code} {resp.text}")
        order_id = Utils(self.client, self.account_hash).extract_order_id(resp)
        deadline = time.time() + self.fill_timeout_s
        while time.time() < deadline:
            o = self.client.get_order(order_id, self.account_hash).json()
            if o.get("status") == "FILLED":
                legs = [
                    leg
                    for act in o.get("orderActivityCollection", [])
                    for leg in act.get("executionLegs", [])
                ]
                qty = sum(leg["quantity"] for leg in legs) or shares
                px = sum(leg["price"] * leg["quantity"] for leg in legs) / qty if legs else ref_price
                return Fill(ticker, side, int(qty), round(px, 4))
            if o.get("status") in ("REJECTED", "CANCELED", "EXPIRED"):
                raise RuntimeError(f"Schwab order {order_id} {o.get('status')}: {json.dumps(o)[:500]}")
            time.sleep(3)
        raise RuntimeError(f"Schwab order {order_id} for {ticker} not filled within {self.fill_timeout_s}s")

    def buy(self, ticker, shares, ref_price):
        from schwab.orders.equities import equity_buy_market

        return self._place(equity_buy_market(ticker, shares), ticker, "buy", shares, ref_price)

    def sell(self, ticker, shares, ref_price):
        from schwab.orders.equities import equity_sell_market

        return self._place(equity_sell_market(ticker, shares), ticker, "sell", shares, ref_price)
