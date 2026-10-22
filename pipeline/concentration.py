"""How Polymarket Intl and predict.fun volume in in-scope markets is spread across wallets.

Writes data/concentration.json (Polymarket at the top level, predict.fun under "predict"):
  windows.7d / windows.30d  snapshot for the trailing window: the cumulative-volume curve by
                            wallet rank, percentile tiers, $-size bands and the Gini coefficient
  weekly                    per complete week (Monday start): wallets, volume, top 1% / 5% / 10%
                            and top-10-wallet shares, Gini

Concentration moves slowly, so the daily run refreshes it on Mondays (or when something is
missing): ~5 credits for the 7d window, ~10 for 30d, ~1 per new week (predict.fun about the same).

  python -m pipeline.concentration                     # refresh both windows + new weeks
  python -m pipeline.concentration --since 2025-09-01  # (re)build weekly history from a date
"""
import argparse
import json
import math
import os
from datetime import date, datetime, timedelta, timezone

from .dune import run_sql
from .queries import CONC_TIERS, poly_concentration, poly_concentration_weekly, predict_concentration_weekly, predict_wallets

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "concentration.json")
HISTORY_START = date(2025, 9, 1)
PREDICT_HISTORY_START = date(2025, 12, 1)  # its first Up/Down volume


def conc_rows(wallets):
    """poly_concentration's result rows (curve / tier / band / stats), computed in Python from
    [{vol, maker_vol}] sorted by vol descending. Used where the fills scan is too costly to
    repeat for every CTE reference."""
    n = len(wallets)
    if not n:
        return []
    pts = {math.ceil(10 ** (x / 40)) for x in range(321)} | {n}
    rows, cum, gsum = [], 0.0, 0.0
    tiers, bands = {}, {}
    for rn, w in enumerate(wallets, 1):
        v, mv = float(w["vol"]), float(w["maker_vol"] or 0)
        cum += v
        gsum += (n - rn + 1) * v
        if rn in pts:
            rows.append({"kind": "curve", "k": str(rn), "rn": rn, "n": n, "vol": cum})
        label = next(lb for lb, p in CONC_TIERS if rn <= max(1, math.ceil(n * p)))
        for grp, k in ((tiers, label), (bands, str(math.floor(math.log10(max(v, 1)))))):
            g = grp.setdefault(k, {"vol": 0.0, "maker_vol": 0.0, "wallets": 0, "lo": v, "hi": v})
            g["vol"] += v
            g["maker_vol"] += mv
            g["wallets"] += 1
            g["lo"], g["hi"] = min(g["lo"], v), max(g["hi"], v)
    rows += [{"kind": "tier", "k": k, "n": n, **g} for k, g in tiers.items()]
    rows += [{"kind": "band", "k": k, "n": n, **g} for k, g in bands.items()]
    total = sum(float(w["vol"]) for w in wallets)
    rows.append({"kind": "stats", "k": "all", "n": n, "vol": total, "wallets": n,
                 "maker_vol": sum(float(w["maker_vol"] or 0) for w in wallets),
                 "lo": 2 * gsum / (n * total) - (n + 1) / n, "hi": None})
    return rows


def predict_concentration(start, end):
    return run_sql(predict_wallets(start, end), label=f"predict wallets {start}")


VENUES = {  # venue -> (window rows, weekly query, first week)
    "polymarket": (lambda a, b: run_sql(poly_concentration(a, b), label=f"polymarket concentration {a}"), poly_concentration_weekly, HISTORY_START),
    "predict": (lambda a, b: conc_rows(predict_concentration(a, b)), predict_concentration_weekly, PREDICT_HISTORY_START),
}


def _load():
    try:
        return json.load(open(OUT))
    except (OSError, ValueError):
        return {"windows": {}, "weekly": []}


def _monday(d):
    return d - timedelta(days=d.weekday())


def window(days, today, venue="polymarket"):
    end = today
    start = end - timedelta(days=days)
    rows = VENUES[venue][0](start, end)
    by = {k: [r for r in rows if r["kind"] == k] for k in ("curve", "tier", "band", "stats")}
    stats = by["stats"][0]
    order = {label: i for i, (label, _) in enumerate(CONC_TIERS)}

    def group(r, label):
        return {"label": label, "wallets": r["wallets"], "volume": round(r["vol"]), "maker_volume": round(r["maker_vol"] or 0),
                "min": round(r["lo"], 2), "max": round(r["hi"], 2)}

    return {
        "start": str(start), "end": str(end),
        "wallets": stats["n"],
        "volume": round(stats["vol"]),
        "maker_volume": round(stats["maker_vol"]),
        "gini": round(stats["lo"], 4),
        # [rank, cumulative volume of the top `rank` wallets]
        "curve": sorted([r["rn"], round(r["vol"])] for r in by["curve"]),
        "tiers": [group(r, r["k"]) for r in sorted(by["tier"], key=lambda r: order[r["k"]])],
        # k = order of magnitude of dollars traded: wallets with 10^k <= volume < 10^(k+1)
        "bands": [{**group(r, None), "k": int(r["k"])} for r in sorted(by["band"], key=lambda r: int(r["k"]))],
    }


def weekly(start, end, existing, venue="polymarket"):
    """Upsert complete weeks in [start, end) into the existing list."""
    if start >= end:
        return existing
    rows = []
    for s0 in range(0, (end - start).days, 91):  # a quarter per query keeps long rebuilds under Dune's limits
        a, b = start + timedelta(days=s0), min(start + timedelta(days=s0 + 91), end)
        rows += run_sql(VENUES[venue][1](a, b), label=f"{venue} concentration weekly {a}")
    new = {str(r["wk"]): {
        "week": str(r["wk"]), "wallets": r["wallets"], "volume": round(r["vol"]),
        **{k: round(r[k], 4) for k in ("top1", "top5", "top10", "top10w", "gini")},
    } for r in rows}
    keep = {w["week"]: w for w in existing if not (str(start) <= w["week"] < str(end))}
    keep.update(new)
    return [keep[k] for k in sorted(keep)]


def _update_venue(block, venue, today, force, since):
    windows = block.setdefault("windows", {})
    if force or today.weekday() == 0 or not all(k in windows for k in ("7d", "30d")):
        windows["7d"] = window(7, today, venue)
        windows["30d"] = window(30, today, venue)
    # complete weeks only: everything before this week's Monday
    end = _monday(today)
    if since:
        start = _monday(max(since, VENUES[venue][2]))
    else:
        last = max((w["week"] for w in block.get("weekly", [])), default=None)
        start = date.fromisoformat(last) + timedelta(days=7) if last else VENUES[venue][2]
    block["weekly"] = weekly(start, end, block.get("weekly", []), venue)


def _save(data):
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))


def update(today, force=False, since=None, venues=("polymarket", "predict")):
    data = _load()
    if "polymarket" in venues:
        _update_venue(data, "polymarket", today, force, since)
        _save(data)
    if "predict" in venues:
        try:  # a newer source: don't let it fail the Polymarket refresh
            _update_venue(data.setdefault("predict", {}), "predict", today, force, since)
        except Exception as e:
            print(f"  predict.fun concentration failed: {e}")
        _save(data)
    print(f"  concentration.json written ({len(data['weekly'])} Polymarket weeks, "
          f"{len(data.get('predict', {}).get('weekly', []))} predict.fun weeks)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD: rebuild weekly history from this date")
    ap.add_argument("--venue", choices=list(VENUES), help="only this venue")
    args = ap.parse_args()
    update(datetime.now(timezone.utc).date(), force=True, since=date.fromisoformat(args.since) if args.since else None,
           venues=[args.venue] if args.venue else list(VENUES))
