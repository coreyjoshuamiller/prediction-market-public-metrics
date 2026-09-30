"""Build the Kalshi series -> (asset, class, duration, type) map from the public API."""
import json
import os

from .markets import kalshi_classify
from .net import get_json

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CATEGORIES = ["Crypto", "Financials", "Commodities", "Economics"]
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_series.json")
FEES_OUT = os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_fees.json")

# Kalshi's published fee formula: fee = rate x contracts x P x (1 - P), rounded up to the cent
# per order. Taker rate is 7% x the series' fee_multiplier; series with maker fees also charge
# makers 1.75% x multiplier (every trade has one maker, so both apply to each fill).
TAKER_RATE, MAKER_RATE = 0.07, 0.0175


def build_fees():
    """-> {series: combined fee rate} for series that differ from the default 0.07."""
    out = {}
    for s in get_json(f"{KALSHI}/series").get("series", []):
        mult = float(s.get("fee_multiplier") if s.get("fee_multiplier") is not None else 1)
        rate = TAKER_RATE * mult + (MAKER_RATE * mult if "maker_fees" in (s.get("fee_type") or "") else 0)
        if abs(rate - TAKER_RATE) > 1e-9:
            out[s["ticker"]] = round(rate, 6)
    with open(FEES_OUT, "w") as f:
        json.dump(dict(sorted(out.items())), f, indent=0)
    return out


def build():
    out = {}
    for cat in CATEGORIES:
        for s in get_json(f"{KALSHI}/series", {"category": cat}).get("series", []):
            c = kalshi_classify(s)
            if c:
                out[s["ticker"]] = {**c, "title": s.get("title"), "category": cat}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(dict(sorted(out.items())), f, indent=1)
    return out


def load():
    if not os.path.exists(OUT):
        return build()
    return json.load(open(OUT))


if __name__ == "__main__":
    m = build()
    for t, v in m.items():
        print(f"{t:18} {v['asset']:12} {v['asset_class']:12} {v['duration']:4} {v['ctype']:8} {v['title']}")
    print(len(m), "series")
