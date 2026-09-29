"""Build the Kalshi series -> (asset, class, duration, type) map from the public API."""
import json
import os

from .markets import kalshi_classify
from .net import get_json

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CATEGORIES = ["Crypto", "Financials", "Commodities", "Economics"]
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "kalshi_series.json")


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
