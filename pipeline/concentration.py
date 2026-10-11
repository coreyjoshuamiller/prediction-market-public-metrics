"""How Polymarket Intl volume in in-scope markets is spread across wallets.

Writes data/concentration.json:
  windows.7d / windows.30d  snapshot for the trailing window: the cumulative-volume curve by
                            wallet rank, percentile tiers, $-size bands and the Gini coefficient
  weekly                    per complete week (Monday start): wallets, volume, top 1% / 5% / 10%
                            and top-10-wallet shares, Gini

Concentration moves slowly, so the daily run refreshes it on Mondays (or when something is
missing): ~5 credits for the 7d window, ~10 for 30d, ~1 per new week.

  python -m pipeline.concentration                     # refresh both windows + new weeks
  python -m pipeline.concentration --since 2025-09-01  # (re)build weekly history from a date
"""
import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone

from .dune import run_sql
from .queries import CONC_TIERS, poly_concentration, poly_concentration_weekly

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "concentration.json")
HISTORY_START = date(2025, 9, 1)


def _load():
    try:
        return json.load(open(OUT))
    except (OSError, ValueError):
        return {"windows": {}, "weekly": []}


def _monday(d):
    return d - timedelta(days=d.weekday())


def window(days, today):
    end = today
    start = end - timedelta(days=days)
    rows = run_sql(poly_concentration(start, end), label=f"concentration {days}d")
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


def weekly(start, end, existing):
    """Upsert complete weeks in [start, end) into the existing list."""
    if start >= end:
        return existing
    rows = run_sql(poly_concentration_weekly(start, end), label=f"concentration weekly {start}")
    new = {str(r["wk"]): {
        "week": str(r["wk"]), "wallets": r["wallets"], "volume": round(r["vol"]),
        **{k: round(r[k], 4) for k in ("top1", "top5", "top10", "top10w", "gini")},
    } for r in rows}
    keep = {w["week"]: w for w in existing if not (str(start) <= w["week"] < str(end))}
    keep.update(new)
    return [keep[k] for k in sorted(keep)]


def update(today, force=False, since=None):
    data = _load()
    windows = data.setdefault("windows", {})
    if force or today.weekday() == 0 or not all(k in windows for k in ("7d", "30d")):
        windows["7d"] = window(7, today)
        windows["30d"] = window(30, today)
    # complete weeks only: everything before this week's Monday
    end = _monday(today)
    if since:
        start = _monday(since)
    else:
        last = max((w["week"] for w in data.get("weekly", [])), default=None)
        start = date.fromisoformat(last) + timedelta(days=7) if last else HISTORY_START
    data["weekly"] = weekly(start, end, data.get("weekly", []))
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  concentration.json written ({len(data['weekly'])} weeks)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD: rebuild weekly history from this date")
    args = ap.parse_args()
    update(datetime.now(timezone.utc).date(), force=True, since=date.fromisoformat(args.since) if args.since else None)
