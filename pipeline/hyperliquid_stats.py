"""Hyperliquid trader counts, concentration and top traders, from the collector's daily wallet files
(data/hyperliquid/wallets/<day>.json.gz, written by pipeline/hyperliquid.py from the public trade feed).

Each file maps wallet -> {"asset|class|dur|ctype": [dollars, contracts, maker dollars, fills,
est. fees, PnL], "_m": [outcome ids]}. Everything here is exact over what the feed saw, so unlike the
Dune venues there are no approximate distinct counts or weekly top-N cut-offs.
"""
import gzip
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .queries import CONC_TIERS

ROOT = os.path.join(os.path.dirname(__file__), "..")
WALLETS = os.path.join(ROOT, "data", "hyperliquid", "wallets")
STATE = os.path.join(ROOT, "data", "hyperliquid", "state.json")
EXPLORER = "https://app.hyperliquid.xyz/explorer/address/"
TOP = 500


def feed_from():
    """The first full UTC day the trade feed covered, or None."""
    try:
        started = json.load(open(STATE)).get("feed_started")
    except (OSError, ValueError):
        return None
    if not started:
        return None
    t = datetime.fromtimestamp(started, timezone.utc)
    return t.date() + timedelta(days=0 if t.time() == datetime.min.time() else 1)


def load(start, end):
    """{day: wallets} for the stored days in [start, end)."""
    out = {}
    d = start
    while d < end:
        path = os.path.join(WALLETS, f"{d}.json.gz")
        if os.path.exists(path):
            with gzip.open(path, "rt") as f:
                out[d] = json.load(f)
        d += timedelta(days=1)
    return out


def _keys(wallet):
    return [(k, v) for k, v in wallet.items() if not k.startswith("_")]


def _monday(d):
    return d - timedelta(days=d.weekday())


def traders(first, last):
    """{period: {dim: {key: {start: unique wallets}}}} for days first..last (inclusive), the same
    shape as the Polymarket and predict.fun trader trees. Weeks and months count partial periods."""
    days = load(first, last + timedelta(days=1))
    sets = defaultdict(set)  # (period, dim, key, start) -> wallets
    for d, data in days.items():
        starts = {"day": d, "week": _monday(d), "month": d.replace(day=1)}
        for wallet, w in data.items():
            for k, v in _keys(w):
                if not v[3]:
                    continue
                asset, cls, dur, ctype = k.split("|")
                for period, s in starts.items():
                    for dim, key in (("market", f"{asset} {dur}"), ("asset", asset), ("duration", dur),
                                     ("class", cls), ("type", ctype), ("total", "All")):
                        sets[(period, dim, key, s.isoformat())].add(wallet)
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for (period, dim, key, s), ws in sets.items():
        tree[period][dim][key][s] = len(ws)
    return tree


def wallet_totals(start, end):
    """wallet -> {volume, notional, maker_vol, fills, fees, pnl, markets, by_market} over [start, end)."""
    acc = defaultdict(lambda: {"volume": 0.0, "notional": 0.0, "maker_vol": 0.0, "fills": 0, "fees": 0.0,
                               "pnl": None, "markets": set(), "by_market": defaultdict(float)})
    for d, data in load(start, end).items():
        for wallet, w in data.items():
            a = acc[wallet]
            a["markets"] |= set(w.get("_m", []))
            for k, v in _keys(w):
                a["volume"] += v[0]
                a["notional"] += v[1]
                a["maker_vol"] += v[2]
                a["fills"] += int(v[3])
                a["fees"] += v[4]
                if v[5]:
                    a["pnl"] = (a["pnl"] or 0) + v[5]
                asset, _, dur, _ = k.split("|")
                a["by_market"][f"{asset} {dur}"] += v[0]
    return {w: a for w, a in acc.items() if a["volume"] > 0}


def _ranked(start, end):
    return sorted(((w, a["volume"], a["maker_vol"]) for w, a in wallet_totals(start, end).items()), key=lambda x: -x[1])


def _gini(vols):
    n, tot = len(vols), sum(vols)
    return 2 * sum((n - i) * v for i, v in enumerate(vols)) / (n * tot) - (n + 1) / n


def conc_window(start, end):
    """The concentration.json window block (same fields as pipeline/concentration.window)."""
    r = _ranked(start, end)
    if not r:
        return None
    n, vols = len(r), [v for _, v, _ in r]
    tot, maker = sum(vols), sum(m for _, _, m in r)
    pts = sorted({math.ceil(10 ** (x / 40)) for x in range(321)} | {n})
    cum, curve = 0.0, []
    marks = set(pts)
    for i, v in enumerate(vols, 1):
        cum += v
        if i in marks:
            curve.append([i, round(cum)])

    def group(rows, label):
        vs = [v for _, v, _ in rows]
        return {"label": label, "wallets": len(rows), "volume": round(sum(vs)), "maker_volume": round(sum(m for _, _, m in rows)),
                "min": round(min(vs), 2), "max": round(max(vs), 2)}

    tiers, used = [], 0
    for label, p in CONC_TIERS:
        upto = max(1, math.ceil(n * p))
        rows = r[used:upto]
        if rows:
            tiers.append(group(rows, label))
        used = max(used, upto)
    bands = defaultdict(list)
    for x in r:
        bands[int(math.floor(math.log10(max(x[1], 1))))].append(x)
    return {"start": str(start), "end": str(end), "wallets": n, "volume": round(tot), "maker_volume": round(maker),
            "gini": round(_gini(vols), 4), "curve": curve, "tiers": tiers,
            "bands": [{**group(bands[k], None), "k": k} for k in sorted(bands)]}


def conc_weekly(first_week, end):
    """Complete Monday weeks from first_week up to `end` (same fields as concentration weekly rows)."""
    out, wk = [], first_week
    while wk + timedelta(days=7) <= end:
        vols = [v for _, v, _ in _ranked(wk, wk + timedelta(days=7))]
        if vols:
            n, tot = len(vols), sum(vols)
            top = lambda k: sum(vols[:k]) / tot
            out.append({"week": str(wk), "wallets": n, "volume": round(tot),
                        "top1": round(top(max(1, math.ceil(n * 0.01))), 4), "top5": round(top(max(1, math.ceil(n * 0.05))), 4),
                        "top10": round(top(max(1, math.ceil(n * 0.10))), 4), "top10w": round(top(10), 4),
                        "gini": round(_gini(vols), 4)})
        wk += timedelta(days=7)
    return out


def concentration(today):
    """{"windows": {"7d", "30d"}, "weekly": [...]} or None before the feed has a full day."""
    first = feed_from()
    if not first or first >= today:
        return None
    windows = {}
    for name, n in (("7d", 7), ("30d", 30)):
        w = conc_window(max(today - timedelta(days=n), first), today)
        if w:
            windows[name] = w
    first_week = _monday(first) if _monday(first) == first else _monday(first) + timedelta(days=7)
    return {"windows": windows, "weekly": conc_weekly(first_week, _monday(today)), "since": str(first)}


def leaders(today):
    """{window: {"start", "end", "rows"}} for 7d, 30d and all (since the feed started)."""
    first = feed_from()
    if not first or first >= today:
        return None
    out = {}
    for name, n in (("7d", 7), ("30d", 30), ("all", None)):
        start = max(today - timedelta(days=n), first) if n else first
        tot = wallet_totals(start, today)
        ranked = sorted(tot.items(), key=lambda kv: -kv[1]["volume"])[:TOP]
        out[name] = {"start": str(start), "end": str(today), "rows": [{
            "rank": i, "address": w, "name": "", "x": "", "image": "", "url": EXPLORER + w,
            "volume": round(a["volume"]), "notional": round(a["notional"]),
            "pnl": None if a["pnl"] is None else round(a["pnl"]),
            "fills": a["fills"], "markets": len(a["markets"]),
            "maker_share": round(a["maker_vol"] / a["volume"], 3) if a["volume"] else None,
            "top_market": max(a["by_market"], key=a["by_market"].get) if a["by_market"] else None,
        } for i, (w, a) in enumerate(ranked, 1)]}
    return out
