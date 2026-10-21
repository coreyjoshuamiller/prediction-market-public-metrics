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


# --- predict.fun: classify Up/Down markets on-chain --------------------------------------
# 5m/15m rounds (Apr 2026+) come from the Chainlink Up/Down adapter, keyed by Chainlink Data
# Streams feed ID. Hourly/daily markets (and 15m BTC before the adapter) are UMA questions,
# keyed by the first word of their title ("Bitcoin Up or Down - October 23, 5PM ET").
# Each key maps to an ASSETS token, or straight to (label, class) for names ASSETS lacks.
# A new feed or title shows up as an "Other"-class asset and a warning in the refresh log;
# find its name via the market's oracleQuestionId (graphql.predict.fun) and add it here.
PREDICT_FEEDS = {
    "0x0003ef42b4d20a19409a187c39670a6f5be7ce17fc78c290d25eb45bbf769d37": "btc",
    "0x0003543336278a459ac24c2f239ffa36f279cadc71f4babc9f8126c6ff10011d": "eth",
    "0x00035c9310fe33d312e347dcbf4e5ad90f5c04c4c5906e91fe78b53b82116d1b": "bnb",
    "0x00037ea46df8151220bcc35cc2a49d4e8d12a99b7d63a2c4f4e1d0a70a6482ff": "cl",
    "0x00033266140dfe07bafeccb4aa76b30adb226c7a6058a654797a46002749a926": "xauusd",
    "0x0003dfc3f64f4136938077e7a626042e7e022307abd2e0e9d58327d0b6db2a81": "amzn",
    "0x0003cb21cfb6c1923ab32d75b251a8ce46fe38681d87a1dfaadb6a31aae5446b": "avgo",
    "0x0003a36a9127b2ec01ec8776c4d252ab5cbf7256e202381a65001f7e50cad127": "googl",
    "0x0003c763ede01c669f2ceb3cc50a4833127ce6e167b435ee208c25b0fef1b450": "meta",
    "0x00031350137f77072d2ea4661769b3bc536bf2fee4e6f98b81f44d0198e7f266": "msft",
    "0x0003952f3b20c5eb2c8d0691632a4856e0132867dc66ae157142f084c0d68d9d": "mu",
    "0x0003769678f55bfe60197c679384e3de8db4091adf996e8e564358fcd1d93f38": "nvda",
    "0x0003ec0f419739672f4dc76cc60c9f0518c1dbf1654e930f085e26344dc7b88c": "qqq",
    "0x00035b0e56c158ce4fc0a2a7955462fcbf5c3ba1871a77612e713fcf5ccdd1c0": "spy",
    "0x00036cb94fa3facfcd023e7045fb47a589ed8041fd9a81bf1aae7c8ede91cf58": "skhy",
    "0x0003ccf879d3174c6fc4e26ff3390fef77159114e3557e9980d0eea438c984bb": ("SNDK", "Equities"),
    "0x00032aaedac29896c9fd8a0d2961a97657268a34f6c4a3fe56af8ee54792daca": "spcx",
    "0x00038a1c96483d74c2e8302628959dd51a94051f47529bbc3b1497b2c65ac460": ("TSM", "Equities"),
    "0x0003c1cc66225d491dc5fa4591ae8e730466fd35bfce263eab48094071469b80": ("Unitree", "Equities"),
    "0x00034472315bf23bc34593586127346ca228659ac81771752ba45a9d2e703ddf": ("Zhipu", "Equities"),
}
# first word of a UMA question title, lowercased
PREDICT_TITLE_WORDS = {
    "btc": "btc", "bitcoin": "btc", "eth": "eth", "ethereum": "eth", "bnb": "bnb",
    "aapl": "aapl", "s&p": "spx", "nasdaq": "ndx", "dax": "dax", "hang": "hsi", "sk": "skhy",
    "kospi": ("KOSPI", "Equities"), "sse": ("SSE Composite", "Equities"),
    "unitree": ("Unitree", "Equities"), "cxmt": ("CXMT", "Equities"), "shein": ("SHEIN", "Equities"),
}


def _predict_asset(v):
    return ASSETS[v] if isinstance(v, str) else v


def predict_assets_values_sql():
    """VALUES table pa(k, asset, asset_class), k = 'feed:<feed id>' or 'title:<first word>'."""
    keys = [(f"feed:{k}", _predict_asset(v)) for k, v in PREDICT_FEEDS.items()]
    keys += [(f"title:{k}", _predict_asset(v)) for k, v in PREDICT_TITLE_WORDS.items()]
    rows = ",\n    ".join(f"('{k}', '{a.replace(chr(39), '')}', '{c}')" for k, (a, c) in keys)
    return f"pa(k, asset, asset_class) AS (VALUES\n    {rows}\n)"
