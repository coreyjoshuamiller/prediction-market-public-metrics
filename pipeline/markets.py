"""Which markets count as "short-term price markets", and how they're labeled.

Scope: contracts that resolve on a financial price over a window of one day or less —
Up/Down, above/below strike ladders, price ranges and same-day touch markets —
across crypto, equities/indices, commodities, and FX/rates.

Edit ASSETS / KALSHI_SERIES_EXCLUDE here to change what's included.
"""
import re

# token (as it appears in slugs / Kalshi tickers) -> (label, asset class)
ASSETS = {
    # crypto
    "btc": ("BTC", "Crypto"), "bitcoin": ("BTC", "Crypto"),
    "eth": ("ETH", "Crypto"), "ethereum": ("ETH", "Crypto"),
    "sol": ("SOL", "Crypto"), "solana": ("SOL", "Crypto"),
    "xrp": ("XRP", "Crypto"), "ripple": ("XRP", "Crypto"),
    "doge": ("DOGE", "Crypto"), "dogecoin": ("DOGE", "Crypto"),
    "bnb": ("BNB", "Crypto"),
    "hype": ("HYPE", "Crypto"), "hyperliquid": ("HYPE", "Crypto"),
    "zec": ("ZEC", "Crypto"), "zcash": ("ZEC", "Crypto"),
    "ada": ("ADA", "Crypto"), "cardano": ("ADA", "Crypto"),
    "bch": ("BCH", "Crypto"), "ltc": ("LTC", "Crypto"), "litecoin": ("LTC", "Crypto"),
    "near": ("NEAR", "Crypto"), "ton": ("TON", "Crypto"), "avax": ("AVAX", "Crypto"),
    "link": ("LINK", "Crypto"), "chainlink": ("LINK", "Crypto"), "shiba": ("SHIB", "Crypto"),
    "dot": ("DOT", "Crypto"), "xlm": ("XLM", "Crypto"), "sui": ("SUI", "Crypto"),
    # equity indices
    "spx": ("S&P 500", "Equities"), "spy": ("S&P 500", "Equities"), "inx": ("S&P 500", "Equities"),
    "ndx": ("Nasdaq 100", "Equities"), "qqq": ("Nasdaq 100", "Equities"), "nasdaq100": ("Nasdaq 100", "Equities"),
    "ndq": ("Nasdaq 100", "Equities"), "nasdaq": ("Nasdaq 100", "Equities"),
    "djia": ("Dow", "Equities"), "dji": ("Dow", "Equities"), "dow-jones": ("Dow", "Equities"),
    "rut": ("Russell 2000", "Equities"), "nya": ("NYSE Comp", "Equities"),
    "dax": ("DAX", "Equities"), "hsi": ("Hang Seng", "Equities"), "nik": ("Nikkei", "Equities"),
    "nky": ("Nikkei", "Equities"), "ukx": ("FTSE 100", "Equities"), "kr200": ("KOSPI", "Equities"),
    "ewy": ("EWY", "Equities"),
    # single stocks (kept as their own label; small ones fold into "Other" in charts)
    **{t: (t.upper(), "Equities") for t in [
        "aapl", "amzn", "googl", "meta", "msft", "nvda", "tsla", "pltr", "abnb", "nflx", "mu", "open",
        "coin", "hood", "rklb", "mstr", "spcx", "skhy", "amd", "avgo", "orcl", "crcl", "gme", "intc"]},
    # commodities
    "wti": ("WTI Oil", "Commodities"), "cl": ("WTI Oil", "Commodities"), "oil": ("WTI Oil", "Commodities"),
    "brent": ("Brent Oil", "Commodities"),
    "xauusd": ("Gold", "Commodities"), "gold": ("Gold", "Commodities"), "gc": ("Gold", "Commodities"),
    "xagusd": ("Silver", "Commodities"), "silver": ("Silver", "Commodities"), "si": ("Silver", "Commodities"),
    "ng": ("Nat Gas", "Commodities"), "natgas": ("Nat Gas", "Commodities"), "natural-gas": ("Nat Gas", "Commodities"),
    "copper": ("Copper", "Commodities"), "platinum": ("Platinum", "Commodities"), "palladium": ("Palladium", "Commodities"),
    # FX & rates
    "eurusd": ("EUR/USD", "FX & Rates"), "gbpusd": ("GBP/USD", "FX & Rates"), "usdjpy": ("USD/JPY", "FX & Rates"),
    "audusd": ("AUD/USD", "FX & Rates"), "nzdusd": ("NZD/USD", "FX & Rates"), "usdcad": ("USD/CAD", "FX & Rates"),
    "usdchf": ("USD/CHF", "FX & Rates"), "usdnok": ("USD/NOK", "FX & Rates"), "usdbrl": ("USD/BRL", "FX & Rates"),
    "usdinr": ("USD/INR", "FX & Rates"), "gbp": ("GBP/USD", "FX & Rates"), "jpy": ("USD/JPY", "FX & Rates"),
    "euro": ("EUR/USD", "FX & Rates"), "ust": ("UST Yields", "FX & Rates"), "tnote": ("UST Yields", "FX & Rates"),
    "rate": ("UST Yields", "FX & Rates"), "dxy": ("DXY", "FX & Rates"),
    "usdsek": ("USD/SEK", "FX & Rates"), "usdtry": ("USD/TRY", "FX & Rates"), "usdmxn": ("USD/MXN", "FX & Rates"),
    "usdkrw": ("USD/KRW", "FX & Rates"), "usdzar": ("USD/ZAR", "FX & Rates"),
    "gbp-usd": ("GBP/USD", "FX & Rates"), "eur-usd": ("EUR/USD", "FX & Rates"), "usd-jpy": ("USD/JPY", "FX & Rates"),
    "usd-cad": ("USD/CAD", "FX & Rates"), "usd-krw": ("USD/KRW", "FX & Rates"),
    "cl-f": ("WTI Oil", "Commodities"), "gc-f": ("Gold", "Commodities"), "si-f": ("Silver", "Commodities"),
    "ftse": ("FTSE 100", "Equities"), "sp-500-spx": ("S&P 500", "Equities"),
    "sp-500-spx-opening-price": ("S&P 500", "Equities"), "pengu": ("PENGU", "Crypto"), "wlfi": ("WLFI", "Crypto"),
}

DURATIONS = ["5m", "15m", "1h", "4h", "1d"]
TYPES = ["Up/Down", "Strike", "Range"]

# --- Polymarket: classify by event slug -------------------------------------------------
MON = "(?:january|february|march|april|may|june|july|august|september|october|november|december)"
DAY = rf"{MON}-[0-9]+(?:-[0-9]{{4}})?"
HOUR = r"[0-9]+(?:am|pm)-et"
STRIKE = r"[0-9pkt.]+"
# (regex, asset group index, duration (literal or group index), type). First match wins.
POLY_PATTERNS = [
    (r"^(?:arch)*(?:polyv[0-9]+)?([a-z]+)-(?:updown|up-or-down)-([0-9]+[mh])-[0-9]+", 1, 2, "Up/Down"),
    (r"^(?:arch)*([a-z0-9]+)-multistrike-([0-9]+h)-", 1, 2, "Strike"),
    (rf"^(?:arch)*([a-z0-9-]+?)-up-or-down-{DAY}-{HOUR}(?:-[0-9]+)?$", 1, "1h", "Up/Down"),
    (rf"^(?:arch)*([a-z0-9-]+?)-(?:opens-)?up-or-down-on-{DAY}(?:-[0-9]+)?$", 1, "1d", "Up/Down"),
    (rf"^(?:arch)*([a-z0-9-]+?)-above-(?:{STRIKE}-)?on-{DAY}-{HOUR}(?:-[0-9]+)?$", 1, "1h", "Strike"),
    (rf"^(?:arch)*([a-z0-9-]+?)-(?:closes?-)?above-(?:{STRIKE}-)?on-{DAY}(?:-[0-9]+)?$", 1, "1d", "Strike"),
    (rf"^(?:arch)*([a-z0-9-]+?)-price---{MON}-[0-9]+,-{HOUR}(?:-[0-9]+)?$", 1, "1h", "Range"),
    (rf"^(?:arch)*([a-z0-9-]+?)-price-on-{DAY}(?:-[0-9]+)?$", 1, "1d", "Range"),
    (rf"^(?:arch)*will-([a-z0-9-]+?)-(?:reach|dip-to)-{STRIKE}(?:-{STRIKE})?-on-{DAY}(?:-[0-9]+)?$", 1, "1d", "Strike"),
]


def _norm_dur(d):
    return {"60m": "1h", "1h": "1h", "4h": "4h", "240m": "4h", "24h": "1d"}.get(d, d)


def poly_classify_sql(slug_expr="slug"):
    """SQL CASE expressions (asset_token, duration, ctype) for a Polymarket event slug."""
    tok, dur, typ = [], [], []
    for rx, ag, dg, ct in POLY_PATTERNS:
        rx_sql = rx.replace("'", "''")
        cond = f"regexp_like({slug_expr}, '{rx_sql}')"
        tok.append(f"WHEN {cond} THEN regexp_extract({slug_expr}, '{rx_sql}', {ag})")
        if isinstance(dg, int):
            dur.append(f"WHEN {cond} THEN regexp_extract({slug_expr}, '{rx_sql}', {dg})")
        else:
            dur.append(f"WHEN {cond} THEN '{dg}'")
        typ.append(f"WHEN {cond} THEN '{ct}'")
    j = "\n      ".join
    return (f"CASE {j(tok)} END", f"CASE {j(dur)} END", f"CASE {j(typ)} END")


def assets_values_sql():
    rows = ",\n    ".join(f"('{k}', '{v[0].replace(chr(39), '')}', '{v[1]}')" for k, v in ASSETS.items())
    return f"(VALUES\n    {rows}\n  ) AS a(token, asset, asset_class)"


# --- Kalshi: classify by series ticker ----------------------------------------------------
KALSHI_SERIES_EXCLUDE = re.compile(r"(HOLDINGS|AAAGAS|TRUF|SOFR|TEST|TOKENUSED|ERCOT|CRYPTOLEAD|CRYPTOCOMP|MAXD$|DUD$)")
KALSHI_FREQ = {"fifteen_min": "15m", "hourly": "1h", "daily": "1d"}


def kalshi_classify(series):
    """series: dict from /series. Returns dict(asset, asset_class, duration, ctype) or None."""
    t, title = series["ticker"], (series.get("title") or "").lower()
    dur = KALSHI_FREQ.get(series.get("frequency"))
    if not dur or KALSHI_SERIES_EXCLUDE.search(t):
        return None
    if t.endswith("15M"):
        dur = "15m"
    full = re.sub(r"^KX", "", t)
    stripped = re.sub(r"(15M|AH|AD|HA|H|D|U|Z|I|AB|A|E|W)$", "", full) or full
    asset = ("UST Yields", "FX & Rates") if "YRRATE" in full else None
    for cand in (full, stripped):  # try the untouched ticker first so e.g. WTI/DJI aren't mangled
        if asset:
            break
        for key in sorted(ASSETS, key=len, reverse=True):
            if cand.startswith(key.upper().replace("-", "")):
                asset = ASSETS[key]
                break
    if asset is None:
        # fall back to title keywords
        for key in sorted(ASSETS, key=len, reverse=True):
            if len(key) > 3 and key.replace("-", " ") in title:
                asset = ASSETS[key]
                break
    if asset is None:
        return None
    if "range" in title:
        ctype = "Range"
    elif t.endswith("15M") or re.search(r"\bup\b|up down|up/down", title) and "above" not in title:
        ctype = "Up/Down"
    else:
        ctype = "Strike"
    return {"asset": asset[0], "asset_class": asset[1], "duration": dur, "ctype": ctype}
