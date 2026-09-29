"""Sample top-of-book liquidity for the live 15-minute Up/Down markets on Polymarket and Kalshi.

Each sample records, for the "Up"/YES side of the currently-live 15m window:
best bid/ask, $ resting at the best bid/ask, and $ resting within 5c of the best bid/ask.

Usage:
  python -m pipeline.orderbook_snapshot                 # one sample
  python -m pipeline.orderbook_snapshot --duration 3300 --interval 60   # loop (used by the hourly Action)
"""
import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .net import get_json

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
WINDOW = 900  # 15 minutes
DEPTH_BAND = 0.05
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "orderbook")
FIELDS = [
    "ts", "platform", "asset", "market", "window_start", "secs_into_window",
    "best_bid", "best_ask", "bid_usd", "ask_usd", "bid_depth5_usd", "ask_depth5_usd",
]

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

_market_cache = {}


def _summarize(bids, asks):
    """bids/asks: lists of (price, shares). Returns top-of-book metrics in USD (shares * price)."""
    bids = sorted(bids, key=lambda x: -x[0])
    asks = sorted(asks, key=lambda x: x[0])
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    return {
        "best_bid": round(bb, 4),
        "best_ask": round(ba, 4),
        "bid_usd": round(bids[0][0] * bids[0][1], 2),
        "ask_usd": round(asks[0][0] * asks[0][1], 2),
        "bid_depth5_usd": round(sum(p * s for p, s in bids if p >= bb - DEPTH_BAND - 1e-9), 2),
        "ask_depth5_usd": round(sum(p * s for p, s in asks if p <= ba + DEPTH_BAND + 1e-9), 2),
    }


def polymarket_market(asset, window_start):
    key = ("polymarket", asset, window_start)
    if key not in _market_cache:
        slug = f"{asset.lower()}-updown-15m-{window_start}"
        events = get_json(f"{GAMMA}/events", {"slug": slug})
        if not events:
            return None
        m = events[0]["markets"][0]
        outcomes = json.loads(m["outcomes"])
        tokens = json.loads(m["clobTokenIds"])
        _market_cache[key] = (slug, tokens[outcomes.index("Up")])
    return _market_cache[key]


def polymarket_sample(asset, window_start):
    mk = polymarket_market(asset, window_start)
    if not mk:
        return None
    slug, token = mk
    book = get_json(f"{CLOB}/book", {"token_id": token})
    bids = [(float(o["price"]), float(o["size"])) for o in book.get("bids", [])]
    asks = [(float(o["price"]), float(o["size"])) for o in book.get("asks", [])]
    s = _summarize(bids, asks)
    return s and {"market": slug, **s}


def kalshi_market(asset, window_start):
    key = ("kalshi", asset, window_start)
    if key not in _market_cache:
        res = get_json(f"{KALSHI}/markets", {"series_ticker": f"KX{asset}15M", "status": "open", "limit": 5})
        live = None
        for m in res.get("markets", []):
            ot = datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp()
            if int(ot) == window_start:
                live = m["ticker"]
        if not live:
            return None
        _market_cache[key] = live
    return _market_cache[key]


def kalshi_sample(asset, window_start):
    ticker = kalshi_market(asset, window_start)
    if not ticker:
        return None
    ob = get_json(f"{KALSHI}/markets/{ticker}/orderbook").get("orderbook_fp") or {}
    bids = [(float(p), float(q)) for p, q in ob.get("yes_dollars") or []]
    # A NO bid at p is a YES offer at 1 - p
    asks = [(round(1 - float(p), 4), float(q)) for p, q in ob.get("no_dollars") or []]
    s = _summarize(bids, asks)
    return s and {"market": ticker, **s}


def sample_once():
    now = time.time()
    window_start = int(now // WINDOW * WINDOW)
    ts = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs = [(p, a) for p in ("polymarket", "kalshi") for a in ASSETS]

    def run(job):
        platform, asset = job
        fn = polymarket_sample if platform == "polymarket" else kalshi_sample
        try:
            r = fn(asset, window_start)
        except Exception as e:  # one flaky venue shouldn't kill the whole sample
            print(f"  {platform} {asset}: {e}")
            return None
        return r and {
            "ts": ts, "platform": platform, "asset": asset,
            "window_start": window_start, "secs_into_window": int(now - window_start), **r,
        }

    with ThreadPoolExecutor(len(jobs)) as ex:
        rows = [r for r in ex.map(run, jobs) if r]

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, ts[:10] + ".csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"{ts} wrote {len(rows)} rows -> {os.path.relpath(path)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=0, help="seconds to keep sampling (0 = one sample)")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    end = time.time() + args.duration
    while True:
        started = time.time()
        sample_once()
        if time.time() + args.interval > end:
            break
        time.sleep(max(0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()
