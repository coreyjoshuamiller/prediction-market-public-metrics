"""Build site/data/metrics.json (+ copy leaders.json, concentration.json) from the CSVs in data/."""
import csv
import json
import os
import shutil
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import hyperliquid_stats
from .kalshi_series import load as load_kalshi_series
from .rebates import pmus_coef, poly_rate
from .rewards import CATEGORIES as REWARD_CATEGORIES

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
SITE = os.path.join(ROOT, "site", "data")
DUR_ORDER = ["5m", "15m", "1h", "4h", "1d"]


def rows(name):
    p = os.path.join(DATA, name)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


def f(x):
    return float(x) if x not in ("", None) else 0.0


def rewards_block():
    """Daily liquidity rewards per venue and category, over the days both venues have."""
    sources = {"kalshi": rows("kalshi_rewards_daily.csv"), "polymarket": rows("poly_rewards_daily.csv")}
    if not all(sources.values()):
        return None
    start = date.fromisoformat(min(r["d"] for src in sources.values() for r in src))
    end = date.fromisoformat(min(max(r["d"] for r in src) for src in sources.values()))
    days = [str(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    at = {d: i for i, d in enumerate(days)}
    block = {"days": days, "categories": REWARD_CATEGORIES}
    for venue, src in sources.items():
        block[venue] = {c: [0] * len(days) for c in REWARD_CATEGORIES}
        block[f"{venue}_first"] = min(r["d"] for r in src)
        for r in src:
            if r["d"] in at:
                block[venue][r["category"]][at[r["d"]]] = round(f(r["usd"]))
    # from the first saved snapshot, split Polymarket's actual payouts by its posted reward rates;
    # a day without a snapshot (a missed run) uses the latest one before it
    snapshots = defaultdict(dict)
    for r in rows("poly_reward_rates_daily.csv"):
        snapshots[r["d"]][r["category"]] = f(r["rate"])
    block["poly_rates_from"] = min(snapshots) if snapshots else None
    poly, latest = block["polymarket"], None
    for d, i in at.items():
        latest = snapshots.get(d, latest)
        if latest:
            paid = sum(poly[c][i] for c in REWARD_CATEGORIES)
            total_rate = sum(latest.values())
            for c in REWARD_CATEGORIES:
                poly[c][i] = round(paid * latest.get(c, 0) / total_rate)
    return block


def build():
    pv, pt, ptot, kv = rows("poly_volume_daily.csv"), rows("poly_traders.csv"), rows("poly_totals_daily.csv"), rows("kalshi_series_daily.csv")
    us = rows("pmus_daily.csv")
    pf = rows("poly_fees_daily.csv")
    prv, prt, prtot = rows("predict_volume_daily.csv"), rows("predict_traders.csv"), rows("predict_totals_daily.csv")
    flows, poly_paid = rows("predict_fee_flows.csv"), rows("poly_rebates_daily.csv")
    hv, htot = rows("hyperliquid_volume_daily.csv"), rows("hyperliquid_totals_daily.csv")
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
                s[m][idx[r["d"]]] += f(r.get(col))
        for s in out.values():
            for m in metrics:
                s[m] = [round(v) for v in s[m]]
        return list(out.values())

    # volume rows and fee rows share the (asset, dur, type, class) key; each fills its own metrics
    poly_series = series_block(pv + [r for r in pf if r["asset"] != "__TOTAL__"],
                               lambda r: (r["asset"], r["dur"], r["ctype"], r["asset_class"]),
                               {"notional": "notional", "cash": "cash", "trades": "trades", "fees": "fees"})

    # Hyperliquid volume is collected live from the first full UTC day of collection (pipeline/hyperliquid.py)
    hl_state = os.path.join(DATA, "hyperliquid", "state.json")
    hl_from = None
    if hv and os.path.exists(hl_state):
        started = datetime.fromtimestamp(json.load(open(hl_state))["started"], timezone.utc)
        hl_from = (started.date() + timedelta(days=0 if started.time() == datetime.min.time() else 1)).isoformat()
    hl_series = series_block([r for r in hv if hl_from and r["d"] >= hl_from], lambda r: (r["asset"], r["dur"], r["ctype"], r["asset_class"]),
                             {"notional": "notional", "cash": "cash", "fees": "fees"})
    # wallets, and so fees, come from the trade feed: fees need positions seen from a market's
    # listing, so they start the day after the feed's first full day (daily markets list a day ahead)
    hl_feed = hyperliquid_stats.feed_from()
    hl_fees_from = (hl_feed + timedelta(days=1)).isoformat() if hl_feed else None

    def kkey(r):
        m = ks.get(r["series"])
        return m and (m["asset"], m["duration"], m["ctype"], m["asset_class"])

    kalshi_series = series_block(kv, kkey, {"notional": "contracts", "cash": "cash", "trades": "trades", "fees": "fees"})
    us_series = series_block([r for r in us if not r["key"].startswith("__")], lambda r: tuple(r["key"].split("|")),
                             {"notional": "notional", "cash": "cash", "trades": "trades", "fees": "fees"})
    predict_series = series_block(prv, lambda r: (r["asset"], r["dur"], r["ctype"], r["asset_class"]),
                                  {"notional": "notional", "cash": "cash", "trades": "trades", "fees": "fees"})

    def totals(src, filt, ncol, ccol, fcol="fees"):
        nt, ct, ft, tr = [0.0] * n, [0.0] * n, [0.0] * n, [None] * n
        for r in src:
            if filt(r) and r["d"] in idx:
                i = idx[r["d"]]
                nt[i] += f(r.get(ncol))
                ct[i] += f(r.get(ccol))
                ft[i] += f(r.get(fcol))
                if "traders" in r and r["traders"]:
                    tr[i] = int(float(r["traders"]))
        return {"notional": [round(x) for x in nt], "cash": [round(x) for x in ct], "fees": [round(x) for x in ft], "traders": tr}

    poly_totals = totals(ptot, lambda r: True, "notional", "cash")
    poly_totals["fees"] = totals(pf, lambda r: r["asset"] == "__TOTAL__", "", "")["fees"]
    # Dune's Polymarket `fee` field over-reports before 2026-04-29 (18-40x, varying daily). For
    # those days use DefiLlama's daily fee total and rescale the per-market split to match it.
    calp = os.path.join(DATA, "poly_fee_calibration.json")
    if os.path.exists(calp):
        cal = json.load(open(calp))
        poly_totals["fees_scaled_before"] = cal["cutoff"]
        for d, true_total in cal["daily_total_fees"].items():
            i = idx.get(d)
            if i is None:
                continue
            raw = poly_totals["fees"][i]
            k = true_total / raw if raw else 0
            poly_totals["fees"][i] = round(true_total)
            for s_ in poly_series:
                s_["fees"][i] = round(s_["fees"][i] * k)
    kalshi_totals = totals(kv, lambda r: r["series"] == "__TOTAL__", "contracts", "cash")
    kalshi_totals.pop("traders")
    us_totals = totals(us, lambda r: r["key"] == "__TOTAL__", "notional", "cash")
    us_totals.pop("traders")
    us_combo = totals(us, lambda r: r["key"] == "__COMBO__", "notional", "cash")
    us_totals["combo_notional"], us_totals["combo_cash"] = us_combo["notional"], us_combo["cash"]
    predict_totals = totals(prtot, lambda r: True, "notional", "cash")
    hl_totals = totals(htot, lambda r: hl_from and r["d"] >= hl_from, "notional", "cash")
    hl_totals.pop("traders")
    for arrs in [hl_totals] + hl_series:  # no fee figures before the feed could estimate them
        arrs["fees"] = [v if hl_fees_from and days[i] >= hl_fees_from else None for i, v in enumerate(arrs["fees"])]
        arrs["fees_net"] = list(arrs["fees"])  # outcome trading has no maker rebates

    # --- net of maker rebates (fees_net); Kalshi has none, so the page uses its gross for net ---
    for s_ in poly_series:
        s_["fees_net"] = [round(v * (1 - poly_rate(days[i], s_["cls"]))) for i, v in enumerate(s_["fees"])]
    paid = {r["d"]: f(r["rebates"]) for r in poly_paid}
    # rebates are paid a day later, so the newest day may have no payout yet: use the last week's rate
    tot_net, rates = [], []
    for i, v in enumerate(poly_totals["fees"]):
        if days[i] in paid and v:
            rates = (rates + [paid[days[i]] / v])[-7:]
        rate = paid[days[i]] / v if days[i] in paid and v else (sum(rates) / len(rates) if rates else 0)
        tot_net.append(round(max(v - v * rate, 0)))
    poly_totals["fees_net"] = tot_net

    for s_ in us_series:
        s_["fees_net"] = [round(v * (1 - pmus_coef(days[i]))) for i, v in enumerate(s_["fees"])]
    # combos: no maker rebate since 2026-10-07 (and they're out of scope)
    us_totals["fees_net"] = [round(v - (v - (us_combo["fees"][i] if days[i] >= "2026-10-07" else 0)) * pmus_coef(days[i]))
                             for i, v in enumerate(us_totals["fees"])]

    # Predict.fun: a fill records the most an order could be charged and the excess is refunded in
    # the same transaction, so gross = recorded fee x (1 - refund share). Rebates are measured too.
    # Shares per (day, duration), applied to the dollar fees; a day not measured yet uses the last.
    fl = {(r["d"], r["dur"]): (f(r["fee_sh"]), f(r["refund_sh"]), f(r["rebate_sh"])) for r in flows}

    def fee_shares(dur):
        out, last = [], (0.0, 0.0)
        for d in days:
            a = fl.get((d, dur))
            if a and a[0]:
                last = (min(a[1] / a[0], 1.0), min(a[2] / a[0], 1.0))
            out.append(last)
        return out

    def predict_net(item, dur):
        sh, raw = fee_shares(dur), item["fees"]
        item["fees"] = [round(v * (1 - sh[i][0])) for i, v in enumerate(raw)]
        item["fees_net"] = [round(max(v * (1 - sh[i][0] - sh[i][1]), 0)) for i, v in enumerate(raw)]

    if fl:
        for s_ in predict_series:
            predict_net(s_, s_["dur"])
        predict_net(predict_totals, "__ALL__")

    # Polymarket US publishes 5pm-5pm ET sessions (file YYYYMMDD ends 21:00 UTC that day), so a
    # UTC day is only complete once the next session's file exists. Blank the incomplete tail.
    last_file = max((r["file"] for r in us), default=None)
    if last_file:
        lf = date(int(last_file[:4]), int(last_file[4:6]), int(last_file[6:8]))
        complete = (lf - timedelta(days=1)).isoformat()
        for arrs in [us_totals] + us_series:
            for m in ("notional", "cash", "trades", "fees", "fees_net", "combo_notional", "combo_cash"):
                if m in arrs:
                    arrs[m] = [None if days[i] > complete else v for i, v in enumerate(arrs[m])]
        us_totals["complete_through"] = complete

    # trader counts: {period: {dim: {key: {start: n}}}}
    def trader_tree(src):
        tree = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
        for r in src:
            tree[r["period"]][r["dim"]][r["key"]][r["start"]] = int(float(r["traders"]))
        return tree
    traders, predict_traders = trader_tree(pt), trader_tree(prt)

    # stable ordering of entities by total notional across both platforms (drives color slots)
    def order(field):
        tot = defaultdict(float)
        for s in poly_series + kalshi_series + us_series + predict_series + hl_series:
            k = f"{s['asset']} {s['dur']}" if field == "market" else s[field]
            tot[k] += sum(v or 0 for v in s["notional"])
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
        "polymarket_us": {"series": us_series, "totals": us_totals},
        "predict": {"series": predict_series, "totals": predict_totals, "traders": predict_traders},
        "kalshi_series_map": ks,
        **({"hyperliquid": {"series": hl_series, "totals": hl_totals, "collected_from": hl_from,
                            "traders": hyperliquid_stats.traders(hl_feed, date.fromisoformat(days[-1])) if hl_feed and str(hl_feed) <= days[-1] else {},
                            "traders_from": str(hl_feed) if hl_feed else None, "fees_from": hl_fees_from}}
           if hl_from and hl_from <= days[-1] else {}),
        "rewards": rewards_block(),
    }
    os.makedirs(SITE, exist_ok=True)
    path = os.path.join(SITE, "metrics.json")
    with open(path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"metrics.json: {len(days)} days, {len(poly_series)} poly + {len(kalshi_series)} kalshi + {len(us_series)} poly US + "
          f"{len(predict_series)} predict.fun series, "
          f"{os.path.getsize(path)/1e6:.2f} MB")
    for name in ("leaders.json", "concentration.json"):
        src = os.path.join(DATA, name)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(SITE, name))


if __name__ == "__main__":
    build()
