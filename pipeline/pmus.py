"""Polymarket US (the CFTC-regulated exchange, polymarketexchange.com) daily volume.

Polymarket US trades off-chain, so it isn't in Dune. The exchange publishes a public
time-and-sales CSV per session (Transaction Time, Symbol, Last Price, Last Quantity),
listed in a manifest. Sessions run 5pm-5pm ET, so each file spans two UTC dates; we
aggregate each file by the UTC date of every trade and keep one row per (file, date, key).
Summing a date's rows across files gives the full UTC day.

There are no account IDs or taker sides in the feed, so trader counts and leaderboards
aren't possible, and cash is computed as price x quantity of the listed instrument.
Fees are estimated from the exchange's published taker-fee schedule (maker rebates ignored).
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .markets import ASSETS

BASE = "https://www.polymarketexchange.com/files/time-and-sales"
ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "pmus_daily.csv")
FIELDS = ["file", "d", "key", "notional", "cash", "fees", "trades"]
UA = {"User-Agent": "Mozilla/5.0 (pm-metrics dashboard)"}

# Short-term price instruments. Only BTC Up/Down (15m, 1h) trade today; the patterns cover
# other assets/durations so new listings are picked up automatically.
UPDOWN = re.compile(r"^cpc-([a-z]+)-updown-(\d+[mh])-\d{4}-\d{2}-\d{2}")
COMBO_PREFIX = "caoc-"
DUR_NORM = {"60m": "1h", "240m": "4h"}


def fee_rate(d):
    """Polymarket US taker fee schedule (as published; same as DefiLlama's adapter).
    Returns (theta, kind): 'quad' -> theta*C*P*(1-P), 'flat' -> theta*C*P."""
    if d < "2026-01-09":
        return 0.0, "flat"
    if d <= "2026-04-03":
        return 0.01, "flat"
    if d < "2026-07-01":
        return 0.05, "quad"
    if d < "2026-09-25":
        return 0.06, "quad"
    return 0.0695, "quad"


def classify(symbol):
    """-> 'asset|dur|ctype|class' for in-scope symbols, else None."""
    m = UPDOWN.match(symbol)
    if not m or m.group(1) not in ASSETS:
        return None
    label, cls = ASSETS[m.group(1)]
    return f"{label}|{DUR_NORM.get(m.group(2), m.group(2))}|Up/Down|{cls}"


def manifest():
    with urllib.request.urlopen(urllib.request.Request(f"{BASE}/manifest.json", headers=UA), timeout=60) as r:
        return [f for f in json.load(r)["files"] if f["size"] > 100]


def _utc_date(ts):
    # e.g. 2026-09-27T17:00:00.05394624-04:00 (nanoseconds; trim to micro for fromisoformat)
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    return datetime.fromisoformat(ts).astimezone(timezone.utc).date().isoformat()


def process_file(fname):
    """Stream one session file -> list of aggregate rows."""
    agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    rates = {}
    date_cache = {}
    req = urllib.request.Request(f"{BASE}/{fname}", headers=UA)
    with urllib.request.urlopen(req, timeout=600) as r:
        reader = csv.reader(io.TextIOWrapper(r, encoding="utf-8", newline=""))
        next(reader, None)
        for row in reader:
            if len(row) < 4:
                continue
            ts, sym, px, qty = row[0], row[1], float(row[2]), float(row[3])
            hour = ts[:13] + ts[-6:]  # UTC date only depends on the local hour + offset
            d = date_cache.get(hour)
            if d is None:
                d = date_cache[hour] = _utc_date(ts)
            keys = ["__TOTAL__"]
            if sym.startswith(COMBO_PREFIX):
                keys.append("__COMBO__")
            else:
                k = classify(sym)
                if k:
                    keys.append(k)
            theta, kind = rates.get(d) or rates.setdefault(d, fee_rate(d))
            fee = theta * qty * px * ((1 - px) if kind == "quad" else 1)
            for k in keys:
                a = agg[(d, k)]
                a[0] += qty
                a[1] += qty * px
                a[2] += fee
                a[3] += 1
    return [{"file": fname, "d": d, "key": k, "notional": round(v[0], 2), "cash": round(v[1], 2), "fees": round(v[2], 2), "trades": v[3]}
            for (d, k), v in agg.items()]


def read():
    if not os.path.exists(OUT):
        return []
    with open(OUT) as f:
        return list(csv.DictReader(f))


def write(rows):
    rows.sort(key=lambda r: (r["d"], r["file"], r["key"]))
    with open(OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def update(last_n=None, workers=4):
    """Process the newest `last_n` session files (all files if None), replacing their rows."""
    files = [f["filename"] for f in manifest()]
    todo = files if last_n is None else files[-last_n:]
    print(f"polymarket US: processing {len(todo)} session file(s)")
    new = []
    with ThreadPoolExecutor(workers) as ex:
        for fname, rows in zip(todo, ex.map(process_file, todo)):
            print(f"  {fname}: {len(rows)} rows")
            new += rows
    done = set(todo)
    write([r for r in read() if r["file"] not in done] + new)
    print(f"  pmus_daily.csv: {len(read())} rows")


if __name__ == "__main__":
    update(None if "--all" in sys.argv else 3)
