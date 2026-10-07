"""The do-not-buy list: healthcare and anything tied to private prisons."""


class Exclusions:
    def __init__(self, raw):
        self.tickers = {k.upper(): v for k, v in raw.get("blocked_tickers", {}).items()}
        self.sectors = {s.lower() for s in raw.get("blocked_sectors", [])}
        self.keywords = [k.lower() for k in raw.get("blocked_name_keywords", [])]

    def reason(self, ticker, name=None, sector=None, industry=None):
        """Return why a ticker is blocked, or None if it may be bought."""
        t = ticker.upper()
        if t in self.tickers:
            return self.tickers[t]
        if sector and sector.lower() in self.sectors:
            return f"Blocked sector: {sector}"
        text = " ".join(x for x in (name, industry) if x).lower()
        for kw in self.keywords:
            if kw in text:
                return f"Blocked keyword '{kw}' in {name or industry}"
        return None

    def is_blocked(self, ticker, **info):
        return self.reason(ticker, **info) is not None
