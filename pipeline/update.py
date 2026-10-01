"""Pull history from Dune into data/*.csv (upserting), then rebuild the site JSON.

  python -m pipeline.update --backfill 2025-09-01    # one-time, month by month
  python -m pipeline.update --daily                  # what the GitHub Action runs
"""
import argparse
import csv
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from . import leaders, pmus
from .dune import run_sql
from .kalshi_series import build as build_kalshi_series, build_fees as build_kalshi_fees
from .queries import kalshi_series_daily, poly_activity, poly_fees, poly_totals

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")

FILES = {
    "poly_volume": ("poly_volume_daily.csv", ["d", "asset", "asset_class", "dur", "ctype", "notional", "cash", "trades"], 5),
    "poly_traders": ("poly_traders.csv", ["period", "start", "dim", "key", "traders"], 4),
    "poly_totals": ("poly_totals_daily.csv", ["d", "notional", "cash", "traders"], 1),
    "kalshi": ("kalshi_series_daily.csv", ["d", "series", "contracts", "cash", "fees", "trades"], 2),
    "poly_fees": ("poly_fees_daily.csv", ["d", "asset", "asset_class", "dur", "ctype", "fees"], 5),
}

DUR_NORM = {"60m": "1h", "240m": "4h", "24h": "1d"}


def _path(name):
    return os.path.join(DATA, FILES[name][0])


def read(name):
    p = _path(name)
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def upsert(name, new_rows, replace):
    """Drop existing rows for which replace(row) is True, add new_rows, write sorted."""
    fname, fields, nkey = FILES[name]
    rows = [r for r in read(name) if not replace(r)] + [{k: r.get(k, "") for k in fields} for r in new_rows]
    rows.sort(key=lambda r: tuple(str(r[k]) for k in fields[:nkey]))
    os.makedirs(DATA, exist_ok=True)
    with open(_path(name), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"  {fname}: {len(rows)} rows")


def _r(x, nd=2):
    return "" if x is None else round(float(x), nd)


# --- Polymarket ------------------------------------------------------------------------

def monday(d):
    return d - timedelta(days=d.weekday())


def month_start(d):
    return d.replace(day=1)


def next_month(d):
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _split(rows, day_ok, week_ok, month_ok):
    """Turn poly_activity rows into (volume rows, trader-count rows), keeping only the
    days/weeks/months the caller says are complete in this query window."""
    vol, trd = [], []
    for r in rows:
        if r.get("dur"):
            r["dur"] = DUR_NORM.get(r["dur"], r["dur"])
        cols = [c for c in ("asset", "dur", "asset_class", "ctype") if r.get(c)]
        if r.get("d") and len(cols) == 4:
            if day_ok(r["d"]):
                vol.append({"d": r["d"], "asset": r["asset"], "asset_class": r["asset_class"], "dur": r["dur"],
                            "ctype": r["ctype"], "notional": _r(r["notional"]), "cash": _r(r["cash"]), "trades": r["trades"]})
            continue
        if r.get("d"):
            period, s, ok = "day", r["d"], day_ok(r["d"])
        elif r.get("w"):
            period, s, ok = "week", r["w"], week_ok(r["w"])
        else:
            period, s, ok = "month", r["mo"], month_ok(r["mo"])
        if ok:
            trd.append({"period": period, "start": s, "dim": _dim(cols), "key": _key(r, cols), "traders": r["traders"]})
    return vol, trd


def poly_window(start, end, keep_days_from, keep_weeks_from, keep_months):
    """Query Polymarket for [start, end) and upsert the grains that are complete within it.

    keep_days_from: keep day rows >= this date
    keep_weeks_from: keep week rows whose Monday >= this date (weeks fully inside the window,
                     or the still-running current week)
    keep_months: set of month-start dates whose rows to keep
    """
    print(f"polymarket {start} -> {end}")
    rows = run_sql(poly_activity(start, end), label=f"poly {start}")
    months = {str(m) for m in keep_months}
    vol, trd = _split(rows, lambda d: d >= str(keep_days_from), lambda w: w >= str(keep_weeks_from), lambda m: m in months)
    kept = {(t["period"], t["start"]) for t in trd}
    upsert("poly_volume", vol, lambda x: str(keep_days_from) <= x["d"] < str(end))
    upsert("poly_traders", trd, lambda x: (x["period"], x["start"]) in kept)
    poly_totals_window(keep_days_from, end)


def poly_totals_window(start, end):
    tot = run_sql(poly_totals(start, end), label=f"poly totals {start}")
    upsert("poly_totals", [{"d": r["d"], "notional": _r(r["notional"]), "cash": _r(r["cash"]), "traders": r["traders"]} for r in tot],
           lambda x: str(start) <= x["d"] < str(end))


# --- Kalshi -----------------------------------------------------------------------------

def kalshi_window(start, end):
    print(f"kalshi {start} -> {end}")
    series = build_kalshi_series()
    rows = run_sql(kalshi_series_daily(start, end, build_kalshi_fees()), label=f"kalshi {start}")
    keep, totals = [], {}
    for r in rows:
        t = totals.setdefault(r["d"], [0.0, 0.0, 0.0, 0])
        t[0] += r["contracts"] or 0
        t[1] += r["cash"] or 0
        t[2] += r["fees"] or 0
        t[3] += r["trades"] or 0
        if r["series"] in series:
            keep.append({"d": r["d"], "series": r["series"], "contracts": _r(r["contracts"]), "cash": _r(r["cash"]),
                         "fees": _r(r["fees"]), "trades": r["trades"]})
    for d, (c, cash, fees, n) in totals.items():
        keep.append({"d": d, "series": "__TOTAL__", "contracts": _r(c), "cash": _r(cash), "fees": _r(fees), "trades": n})
    upsert("kalshi", keep, lambda x: str(start) <= x["d"] < str(end))


def poly_fees_window(start, end):
    print(f"polymarket fees {start} -> {end}")
    _upsert_poly_fees(run_sql(poly_fees(start, end), label=f"poly fees {start}"), start, end)


def _upsert_poly_fees(rows, start, end):
    out = [{"d": r["d"], "asset": r["asset"], "asset_class": r["asset_class"] or "",
            "dur": DUR_NORM.get(r["dur"], r["dur"]) if r["dur"] else "", "ctype": r["ctype"] or "", "fees": _r(r["fees"])}
           for r in rows]
    upsert("poly_fees", out, lambda x: str(start) <= x["d"] < str(end))


# --- entry points -----------------------------------------------------------------------

def backfill(since):
    since = month_start(date.fromisoformat(since))
    today = date.today()
    months = []
    m = since
    while m <= today:
        months.append(m)
        m = next_month(m)
    # Kalshi is cheap: one query per quarter-ish
    for i in range(0, len(months), 3):
        kalshi_window(months[i], min(next_month(months[min(i + 2, len(months) - 1)]), today))
    # Polymarket: one month per query, widened to whole weeks; run a few in parallel
    def one(m):
        # widen to whole weeks: from the Monday on/before the 1st through the end of the
        # last week that starts inside the month (weeks belong to the month of their Monday)
        m_end = next_month(m)
        return m, monday(m), min(monday(m_end - timedelta(days=1)) + timedelta(days=7), today)
    plans = [one(m) for m in months]
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda p: (p, run_sql(poly_activity(p[1], p[2]), label=f"poly {p[0]}")), plans))
    _apply_backfill(results)
    # platform totals, per quarter
    for i in range(0, len(months), 3):
        poly_totals_window(months[i], min(next_month(months[min(i + 2, len(months) - 1)]), today))
        poly_fees_window(months[i], min(next_month(months[min(i + 2, len(months) - 1)]), today))


def _apply_backfill(results):
    vol, trd = [], []
    for (m, _start, _end), rows in results:
        lo, hi = str(m), str(next_month(m))
        # days and weeks (by their Monday) belong to this month's chunk; the window was widened
        # so every such week is complete
        v, t = _split(rows, lambda d: lo <= d < hi, lambda w: lo <= w < hi, lambda mo: mo == lo)
        vol += v
        trd += t
    upsert("poly_volume", vol, lambda x: True)
    upsert("poly_traders", trd, lambda x: True)


def _dim(cols):
    if cols == ["asset", "dur"]:
        return "market"
    if not cols:
        return "total"
    return {"asset": "asset", "dur": "duration", "asset_class": "class", "ctype": "type"}[cols[0]]


def _key(r, cols):
    if cols == ["asset", "dur"]:
        return f"{r['asset']} {r['dur']}"
    if not cols:
        return "All"
    return r[cols[0]]


def backfill_fees(since):
    """Fee history without re-running the (costlier) activity/trader queries. Kalshi rows are
    rewritten with the new fees column, which also refreshes their volumes."""
    since = month_start(date.fromisoformat(since))
    today = date.today()
    starts = []
    m = since
    while m <= today:
        starts.append(m)
        m = next_month(next_month(next_month(m)))
    spans = [(s, min(next_month(next_month(next_month(s))), today)) for s in starts]
    # queries run in parallel; CSV writes happen one at a time (upsert rewrites the whole file)
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda sp: (sp, run_sql(poly_fees(*sp), label=f"poly fees {sp[0]}")), spans))
    for (s0, e0), rows in results:
        _upsert_poly_fees(rows, s0, e0)
    for sp in spans:
        kalshi_window(*sp)


def daily(run_leaders=True, today=None):
    today = today or datetime.now(timezone.utc).date()
    end = today  # complete UTC days only
    start = min(month_start(end - timedelta(days=1)), monday(end) - timedelta(days=7))
    keep_months = {month_start(end - timedelta(days=1))}
    if end.day <= 3:  # finalize last month during the first days of a new one
        start = month_start(month_start(end) - timedelta(days=1))
        keep_months.add(start)
    first_full_week = start if monday(start) == start else monday(start) + timedelta(days=7)
    poly_window(start, end, keep_days_from=start, keep_weeks_from=first_full_week, keep_months=keep_months)
    poly_fees_window(end - timedelta(days=10), end)
    kalshi_window(end - timedelta(days=10), end)
    pmus.update(last_n=3)
    if run_leaders:
        leaders.update(today)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", help="YYYY-MM-DD start (month-aligned)")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--no-leaders", action="store_true")
    ap.add_argument("--backfill-fees", help="YYYY-MM-DD: backfill only fees (Polymarket, Kalshi) from this date")
    args = ap.parse_args()
    if args.backfill:
        backfill(args.backfill)
        if not args.no_leaders:
            leaders.update(date.today(), force_all=True)
    if args.backfill_fees:
        backfill_fees(args.backfill_fees)
    if args.daily:
        daily(run_leaders=not args.no_leaders)
    from .build_site import build
    build()


if __name__ == "__main__":
    main()
