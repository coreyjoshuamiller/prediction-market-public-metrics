"""Build site/data/metrics.json (+ copy leaders.json) from the CSVs in data/."""
import csv
import json
import os
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .kalshi_series import load as load_kalshi_series

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
SITE = os.path.join(ROOT, "site", "data")
DUR_ORDER = ["5m", "15m", "1h", "4h", "1d"]


def rows(name):
    p = os.path.join(DATA, name)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def f(x):
    return float(x) if x not in ("", None) else 0.0


def build():
    pv, pt, ptot, kv = rows("poly_volume_daily.csv"), rows("poly_traders.csv"), rows("poly_totals_daily.csv"), rows("kalshi_series_daily.csv")
    ks = load_kalshi_series()
    all_days = sorted({r["d"] for r in pv} | {r["d"] for r in kv if r["series"] == "__TOTAL__"} | {r["d"] for r in ptot})
    if not all_days:
        print("no data yet")
        return
    d0, d1 = date.fromisoformat(all_days[0]), date.fromisoformat(all_days[-1])
    days = [(d0 + timedelta(i)).isoformat() for i in range((d1 - d0).days + 1)]
    idx = {d: i for i, d in enumerate(days)}
    n = len(days)

    def series_block(detail_rows, keyfn, metrics):
        """-> list of {asset, dur, ctype, cls, <metric>: [daily]}"""
        out = {}
        for r in detail_rows:
            k = keyfn(r)
            if k is None or r["d"] not in idx:
                continue
            s = out.setdefault(k, {"asset": k[0], "dur": k[1], "ctype": k[2], "cls": k[3], **{m: [0.0] * n for m in metrics}})
            for m, col in metrics.items():
                s[m][idx[r["d"]]] += f(r[col])
        for s in out.values():
            for m in metrics:
                s[m] = [round(v) for v in s[m]]
        return list(out.values())

    poly_series = series_block(pv, lambda r: (r["asset"], r["dur"], r["ctype"], r["asset_class"]),
                               {"notional": "notional", "cash": "cash", "trades": "trades"})

    def kkey(r):
        m = ks.get(r["series"])
        return m and (m["asset"], m["duration"], m["ctype"], m["asset_class"])

    kalshi_series = series_block(kv, kkey, {"notional": "contracts", "cash": "cash", "trades": "trades"})

    def totals(src, filt, ncol, ccol):
        nt, ct, tr = [0.0] * n, [0.0] * n, [None] * n
        for r in src:
            if filt(r) and r["d"] in idx:
                i = idx[r["d"]]
                nt[i] += f(r[ncol])
                ct[i] += f(r[ccol])
                if "traders" in r and r["traders"]:
                    tr[i] = int(float(r["traders"]))
        return {"notional": [round(x) for x in nt], "cash": [round(x) for x in ct], "traders": tr}

    poly_totals = totals(ptot, lambda r: True, "notional", "cash")
    kalshi_totals = totals(kv, lambda r: r["series"] == "__TOTAL__", "contracts", "cash")
    kalshi_totals.pop("traders")

    # trader counts: {period: {dim: {key: {start: n}}}}
    traders = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for r in pt:
        traders[r["period"]][r["dim"]][r["key"]][r["start"]] = int(float(r["traders"]))

    # stable ordering of entities by total notional across both platforms (drives color slots)
    def order(field):
        tot = defaultdict(float)
        for s in poly_series + kalshi_series:
            k = f"{s['asset']} {s['dur']}" if field == "market" else s[field]
            tot[k] += sum(s["notional"])
        keys = sorted(tot, key=lambda k: -tot[k])
        if field == "dur":
            keys = [d for d in DUR_ORDER if d in tot]
        return keys

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": days,
        "order": {"market": order("market"), "asset": order("asset"), "dur": order("dur"),
                  "cls": order("cls"), "ctype": order("ctype")},
        "polymarket": {"series": poly_series, "totals": poly_totals, "traders": traders},
        "kalshi": {"series": kalshi_series, "totals": kalshi_totals},
        "kalshi_series_map": ks,
    }
    os.makedirs(SITE, exist_ok=True)
    path = os.path.join(SITE, "metrics.json")
    with open(path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"metrics.json: {len(days)} days, {len(poly_series)} poly + {len(kalshi_series)} kalshi series, "
          f"{os.path.getsize(path)/1e6:.2f} MB")
    lp = os.path.join(DATA, "leaders.json")
    if os.path.exists(lp):
        shutil.copy(lp, os.path.join(SITE, "leaders.json"))


if __name__ == "__main__":
    build()
