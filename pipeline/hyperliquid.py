"""Hyperliquid HIP-4 outcome markets: collect volume for its short-term price markets.

Hyperliquid's public API has no volume history for outcome markets: once a market settles its trades
and candles are gone, and Dune doesn't carry them. But one call (spotMetaAndAssetCtxs) returns the
rolling 24-hour volume of every live outcome side ("#<10 x outcome + side>"), so polling it every few
seconds and adding up the increases gives each market's volume as it trades. The Yes and No sides
print the same trades (No at 1 - the Yes price), so only the Yes side is read: contracts are its
shares and dollars are valued at the Yes price. This runs alongside the order-book sampler in the
hourly Action:

  python -m pipeline.hyperliquid --duration 3240 --interval 10

A market's rolling volume only stops counting trades 24 hours after they happen, so the increases are
exact for the short-term markets kept here (they settle within a day of listing). Gaps between runs
are caught up on the next poll, except for markets that list and settle entirely inside a gap. Trades
in a market's last seconds before it settles (up to one poll interval) are missed.

Platform totals (every outcome market, mostly sports) come from DefiLlama's Hyperliquid indexer.
"""
import argparse
import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

from .markets import ASSETS
from .net import get_json, post_json

INFO = "https://api.hyperliquid.xyz/info"
DATA = os.path.join(os.path.dirname(__file__), "..", "data")
STATE = os.path.join(DATA, "hyperliquid", "state.json")
VOLUME = os.path.join(DATA, "hyperliquid_volume_daily.csv")
TOTALS = os.path.join(DATA, "hyperliquid_totals_daily.csv")
FIELDS = ["d", "asset", "asset_class", "dur", "ctype", "notional", "cash"]
LLAMA = "https://api.llama.fi/summary/dexs/hyperliquid-outcomes"

# a market's life (listing to expiry) is binned up to the dashboard's durations; longer ones are out of scope
DUR_BINS = [(5 * 60, "5m"), (15 * 60, "15m"), (3600, "1h"), (4 * 3600, "4h"), (24 * 3600 + 15 * 60, "1d")]
PERIODS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
PRICE_TEMPLATES = {"template:binaryPrice": "Strike", "template:priceTouch": "Strike"}
# perps that aren't in markets.ASSETS under their own ticker
PERP_ASSETS = {
    "sp500": ASSETS["spx"], "xyz100": ("XYZ100", "Equities"), "skhx": ASSETS["skhy"], "sndk": ("SNDK", "Equities"),
    "ant": ("Anthropic", "Equities"),
}


def info(body):
    return post_json(INFO, body, timeout=30)


def _fields(desc):
    return dict(x.split(":", 1) for x in (desc or "").split("|") if ":" in x)


def _ts(s):
    """'20261110-1826' (UTC) -> epoch seconds."""
    return datetime.strptime(s, "%Y%m%d-%H%M").replace(tzinfo=timezone.utc).timestamp()


def _asset(perp):
    tok = perp.split(":")[-1].lower()
    return PERP_ASSETS.get(tok) or ASSETS.get(tok) or (tok.upper(), "Other")


def _bin(seconds):
    return next((label for limit, label in DUR_BINS if seconds <= limit), None)


def classify(outcome, question, first_seen):
    """-> [asset, asset_class, dur, ctype] for a short-term price market, else None. first_seen is
    None for markets already listed when collection began: their life can't be known."""
    name, f = outcome["name"], _fields(outcome["description"])
    if name == "Recurring" and f.get("class") == "priceBinary":
        dur, ctype, perp = PERIODS.get(f.get("period")), "Strike", f.get("underlying")
    elif name.startswith("Recurring") and question:  # one bucket of a price-range question
        q = _fields(question["description"])
        if q.get("class") != "priceBucket":
            return None
        dur, ctype, perp = PERIODS.get(q.get("period")), "Range", q.get("underlying")
    elif name in PRICE_TEMPLATES and f.get("time") and f.get("perp") and first_seen:
        dur, ctype, perp = _bin(_ts(f["time"]) - first_seen), PRICE_TEMPLATES[name], f["perp"]
    else:
        return None
    if not dur or not perp:
        return None
    return [*_asset(perp), dur, ctype]


def _load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"coins": {}, "outcomes": {}, "started": time.time()}


def _save(state, added):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(state, fh, separators=(",", ":"))
    if not added:
        return
    rows = {}
    if os.path.exists(VOLUME):
        for r in csv.DictReader(open(VOLUME)):
            rows[tuple(r[k] for k in FIELDS[:5])] = [float(r["notional"]), float(r["cash"])]
    for k, (n, c) in added.items():
        cur = rows.setdefault(k, [0.0, 0.0])
        cur[0] += n
        cur[1] += c
    with open(VOLUME, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for k in sorted(rows):
            w.writerow([*k, round(rows[k][0], 2), round(rows[k][1], 2)])
    added.clear()


def collect(duration, interval, meta_every=60, save_every=300):
    state = _load_state()
    coins, outcomes = state["coins"], state["outcomes"]
    # with no saved state, the first poll only records where each market's volume stands
    bootstrap = not coins
    added = defaultdict(lambda: [0.0, 0.0])  # (d, asset, class, dur, ctype) -> [contracts, dollars]
    end = time.time() + duration
    meta_at = saved_at = 0
    by_id, question_of = {}, {}
    polls = errors = 0
    while True:
        started = time.time()
        try:
            if started - meta_at >= meta_every:
                m = info({"type": "outcomeMeta"})
                by_id = {o["outcome"]: o for o in m["outcomes"]}
                question_of = {o: q for q in m["questions"] for o in q["namedOutcomes"] + [q["fallbackOutcome"]]}
                meta_at = started
            ctxs = [c for c in info({"type": "spotMetaAndAssetCtxs"})[1] if c["coin"].startswith("#") and c["coin"].endswith("0")]
            now = time.time()
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            for c in ctxs:
                coin, oid = c["coin"], str(int(c["coin"][1:]) // 10)
                o = outcomes.get(oid)
                if o is None or (o["key"] is None and int(oid) in by_id and not o.get("classified")):
                    first_seen = o["first_seen"] if o else (None if bootstrap else now)
                    spec = by_id.get(int(oid))
                    key = classify(spec, question_of.get(int(oid)), first_seen) if spec else None
                    o = outcomes[oid] = {"first_seen": first_seen, "key": key, "classified": bool(spec)}
                o["last_seen"] = now
                v, b = float(c["dayNtlVlm"]), float(c["dayBaseVlm"])
                prev = coins.get(coin)
                dv, db = (v - prev["v"], b - prev["b"]) if prev else (v, b)
                coins[coin] = {"v": v, "b": b, "t": now}
                if o["key"] and not bootstrap and (dv > 0 or db > 0):
                    a = added[(day, *o["key"])]
                    a[0] += max(db, 0)
                    a[1] += max(dv, 0)
            polls += 1
            bootstrap = False
        except Exception as e:  # keep going: a missed poll is caught up by the next one
            errors += 1
            print(f"  hyperliquid poll failed: {e}")
        if time.time() - saved_at >= save_every or time.time() >= end:
            cutoff = time.time() - 2 * 3600
            for k in [k for k, x in coins.items() if x["t"] < cutoff]:
                del coins[k]
            for k in [k for k, x in outcomes.items() if x.get("last_seen", 0) < time.time() - 3 * 86400]:
                del outcomes[k]
            _save(state, added)
            saved_at = time.time()
        if time.time() >= end:
            break
        time.sleep(max(0, interval - (time.time() - started)))
    print(f"hyperliquid: {polls} polls, {errors} errors, {len(coins)} live sides")


def platform_totals():
    """Every HIP-4 outcome market per UTC day, from DefiLlama (dollars; contracts at $1 each)."""
    def series(kind):
        d = get_json(LLAMA, {"excludeTotalDataChartBreakdown": "true", "dataType": kind}, timeout=60)
        return {datetime.fromtimestamp(t, timezone.utc).date().isoformat(): v for t, v in d.get("totalDataChart") or []}
    cash, notional = series("dailyVolume"), series("dailyNotionalVolume")
    with open(TOTALS, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["d", "notional", "cash"])
        for d in sorted(cash):
            w.writerow([d, round(notional.get(d, 0), 2), round(cash[d], 2)])
    print(f"  {os.path.basename(TOTALS)}: {len(cash)} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=0, help="seconds to keep polling (0 = one poll)")
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()
    collect(args.duration, args.interval)
