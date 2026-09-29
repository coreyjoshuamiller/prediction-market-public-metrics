"""Aggregate raw order book samples (data/orderbook/*.csv) into site/data/orderbook.json.

Output:
  recent:  5-minute medians for the last 7 days
  history: hourly medians for the last 180 days
  profile: median by minute-into-window (0-14) over the last 14 days — how books fill and drain

Depth metrics are $ resting on each side within 1c / 2c / 5c of the midpoint. Samples taken
before those columns existed simply have no depth values.
"""
import csv
import glob
import json
import os
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
BANDS = (1, 2, 5)
DEPTH = [f"{side}_{c}c_usd" for c in BANDS for side in ("bid", "ask", "both")]
METRICS = ["bid_usd", "ask_usd", *DEPTH, "spread_c"]


def load_rows(since_ts):
    rows = []
    since_day = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%d")
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "orderbook", "*.csv"))):
        if os.path.basename(path)[:10] < since_day:
            continue
        with open(path) as f:
            for r in csv.DictReader(f):
                t = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                if t < since_ts:
                    continue
                r["t"] = t
                r["spread_c"] = (float(r["best_ask"]) - float(r["best_bid"])) * 100
                for m in METRICS[:-1]:
                    r[m] = float(r[m]) if r.get(m) not in (None, "") else None
                for c in BANDS:  # combined depth, per sample (a median of sums, not a sum of medians)
                    b, a = r[f"bid_{c}c_usd"], r[f"ask_{c}c_usd"]
                    r[f"both_{c}c_usd"] = None if b is None or a is None else b + a
                r["secs_into_window"] = int(r["secs_into_window"])
                rows.append(r)
    return rows


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def bucketed(rows, bucket):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        key = f"{r['platform']}|{r['asset']}"
        groups[key][int(r["t"] // bucket * bucket)].append(r)
    out = {}
    for key, by_t in groups.items():
        ts = sorted(by_t)
        out[key] = {"t": ts}
        for m in METRICS:
            out[key][m] = [_med(x[m] for x in by_t[t]) for t in ts]
    return out


def profile(rows):
    groups = defaultdict(lambda: defaultdict(list))
    for r in rows:
        groups[f"{r['platform']}|{r['asset']}"][min(r["secs_into_window"] // 60, 14)].append(r)
    out = {}
    for key, by_min in groups.items():
        mins = sorted(by_min)
        out[key] = {"minute": mins, "n": [len(by_min[m]) for m in mins]}
        for m in METRICS:
            out[key][m] = [_med(x[m] for x in by_min[mm]) for mm in mins]
    return out


def main():
    now = time.time()
    hist_rows = load_rows(now - 180 * 86400)
    recent_rows = [r for r in hist_rows if r["t"] >= now - 7 * 86400]
    prof_rows = [r for r in hist_rows if r["t"] >= now - 14 * 86400]
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_sample": min((r["ts"] for r in hist_rows), default=None),
        "first_depth_sample": min((r["ts"] for r in hist_rows if r["both_1c_usd"] is not None), default=None),
        "recent": bucketed(recent_rows, 300),
        "history": bucketed(hist_rows, 3600),
        "profile": profile(prof_rows),
    }
    path = os.path.join(ROOT, "site", "data", "orderbook.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"orderbook.json: {len(hist_rows)} samples, {os.path.getsize(path)/1e3:.0f} KB")


if __name__ == "__main__":
    main()
