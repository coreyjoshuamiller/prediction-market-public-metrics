"""Builds site/overlap/data/overlap.json from the files overlap_poly.py and overlap_hl.py leave in
data/overlap/. Everything here is local arithmetic; nothing is fetched.

  python3 -m pipeline.overlap_build
"""
import gzip
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import overlap_hl as hl
from . import overlap_poly as poly

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "overlap")
SITE = os.path.join(ROOT, "site", "overlap", "data", "overlap.json")
HIP4_LAUNCH = "2026-05-02"  # Hyperliquid outcome markets went live


def _json(name, default=None):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        return default
    opener = gzip.open if name.endswith(".gz") else open
    with opener(path, "rt") as f:
        return json.load(f)


def _day(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _band(v, edges):
    for i, e in enumerate(edges):
        if v < e:
            return i
    return len(edges)


# ---------- Polymarket ----------

PERP_EDGES = [1e3, 1e4, 1e5, 1e6, 1e7]
PERP_LABELS = ["Under $1k", "$1k–10k", "$10k–100k", "$100k–1M", "$1M–10M", "$10M+"]
PRED_LABELS = {0: "Under $10", 1: "$10–100", 2: "$100–1k", 3: "$1k–10k", 4: "$10k–100k", 5: "$100k–1M", 6: "$1M+"}
JOINED = [("Before 2024", "0000", "2024-01-01"), ("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
          ("Jan–Apr ’26", "2026-01-01", "2026-05-01"), ("May–Aug ’26", "2026-05-01", poly.LAUNCH),
          ("Since 3 Sep", poly.LAUNCH, "9999")]
GAPS = ["Perps first", "Same week", "1–12 weeks before", "3–12 months before", "1–2 years before", "2+ years before"]


def build_poly():
    acts = poly._load_activity()
    accounts, asof = acts["accounts"], acts["asof"]
    ids = poly.account_ids()
    pred = _json("poly_accounts.json", {})
    weekly = _json("poly_weekly.json", [])
    joined_day = poly.joined_days()
    lk, routers = poly.links()
    if not accounts or not pred or not weekly:
        return None
    start = _day(poly.PRED_FROM)
    epoch = _day(poly.EPOCH)
    n_weeks = (datetime.fromtimestamp(asof / 1000, timezone.utc) - start).days // 7 + 1
    week_day0 = [(start + timedelta(weeks=i) - epoch).days for i in range(n_weeks)]  # first day of week i, in d0 units
    traders = {a: v for a, v in accounts.items() if poly.traded_weeks(v)}

    z = lambda: [0] * n_weeks
    P = {k: z() for k in ("active", "pred_same", "pred_4w", "pred_ever", "new", "new_before", "new_later", "new_never")}
    tot = defaultdict(float)
    size = [[0, 0, 0, 0.0] for _ in PERP_LABELS]        # accounts, traded predictions ever, in the last 4 weeks, notional
    joined = [[0, 0] for _ in JOINED] + [[0, 0]]        # [traders, of which traded predictions]; last = no wallet found
    gaps = [0] * len(GAPS)
    last_full = n_weeks - 2
    for a, v in traders.items():
        p = pred.get(str(ids[a]))
        d0, pvol, pvol_launch, pmask = p if p else (None, 0, 0, 0)
        mask = poly.perps_mask(v)
        first = poly.week_index(min(poly.traded_weeks(v)))
        notional = poly.exact_notional(v)
        for i in range(n_weeks):
            if not mask >> i & 1:
                continue
            P["active"][i] += 1
            if pmask >> i & 1:
                P["pred_same"][i] += 1
            if pmask >> max(0, i - 3) & ((1 << min(4, i + 1)) - 1):
                P["pred_4w"][i] += 1
            if d0 is not None and d0 < week_day0[i] + 7:
                P["pred_ever"][i] += 1
        if 0 <= first < n_weeks:
            P["new"][first] += 1
            key = "new_never" if d0 is None else ("new_before" if d0 < week_day0[first] else "new_later")
            P[key][first] += 1
        recent = bool(pmask >> max(0, last_full - 3) & 15)
        tot["perps_traders"] += 1
        tot["notional"] += notional
        if d0 is not None:
            tot["pred_ever"] += 1
            tot["notional_pred_ever"] += notional
            tot["pred_dollars"] += pvol
            first_perps_day = (datetime.fromtimestamp(v["first"] / 1000, timezone.utc) - epoch).days
            gap = first_perps_day - d0
            gaps[0 if gap < -7 else 1 if gap <= 7 else 2 if gap <= 84 else 3 if gap <= 365 else 4 if gap <= 730 else 5] += 1
        if pvol_launch:
            tot["pred_since_launch"] += 1
        if recent:
            tot["pred_4w"] += 1
            tot["notional_pred_4w"] += notional
        b = _band(notional, PERP_EDGES)
        size[b][0] += 1
        size[b][1] += d0 is not None
        size[b][2] += recent
        size[b][3] += notional
        created = (epoch + timedelta(days=joined_day[a])).strftime("%Y-%m-%d") if a in joined_day else ""
        j = next((i for i, (_, lo, hi) in enumerate(JOINED) if created and lo <= created < hi), len(JOINED))
        joined[j][0] += 1
        joined[j][1] += d0 is not None
    tot["depositors"] = len(accounts)
    tot["deposited_never_traded"] = len(accounts) - len(traders)

    wk_rows = {r["wk"][:10]: r for r in weekly}
    weeks = [(start + timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(n_weeks)]
    D = {k: [round(wk_rows.get(w, {}).get(k, 0)) for w in weeks]
         for k in ("traders", "vol", "linked", "perps_ever", "perps_ever_vol", "perps_same", "perps_same_vol")}
    first_week = next((i for i, n in enumerate(P["active"]) if n >= 200), 0)
    cut = lambda arr: arr[first_week:]
    bands = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for r in _json("poly_bands.json", []):
        b = bands[min(max(r["band"], 0), 6)]
        b[0] += r["wallets"]; b[1] += r["perps"]; b[2] += r["vol"]; b[3] += r["perps_vol"]
    return {
        "asof": asof, "launch": poly.LAUNCH, "weeks": cut(weeks), "partial_week": weeks[-1],
        "perps": {k: cut(v) for k, v in P.items()},
        "pred": {k: cut(v) for k, v in D.items()},
        "totals": {k: round(v) for k, v in tot.items()},
        "size": [{"label": PERP_LABELS[i], "accounts": s[0], "pred_ever": s[1], "pred_4w": s[2], "notional": round(s[3])}
                 for i, s in enumerate(size)],
        "joined": [{"label": (JOINED[i][0] if i < len(JOINED) else "Unknown"), "accounts": j[0], "pred_ever": j[1]}
                   for i, j in enumerate(joined) if j[0]],
        "gaps": [{"label": GAPS[i], "accounts": g} for i, g in enumerate(gaps)],
        "bands": [{"label": PRED_LABELS[k], "wallets": v[0], "perps": v[1], "vol": round(v[2]), "perps_vol": round(v[3])}
                  for k, v in sorted(bands.items())],
        "categories": [{"category": r["category"], "vol": round(r["vol"]), "perps_vol": round(r["perps_vol"]),
                        "traders": r["traders"], "perps_traders": r["perps_traders"]} for r in _json("poly_categories.json", [])],
        "linked_accounts": sum(1 for v in lk.values() if v), "wallets_found": len(joined_day), "routers": len(routers),
    }


# ---------- Hyperliquid ----------

OUT_EDGES = [100, 1e3, 1e4, 1e5]
OUT_LABELS = ["Under $100", "$100–1k", "$1k–10k", "$10k–100k", "$100k+"]
AGE = [("Before 2025", "0000-00", "2025-01"), ("2025", "2025-01", "2026-01"), ("Jan–Apr ’26", "2026-01", "2026-05"),
       ("May–Aug ’26", "2026-05", "2026-09"), ("Sep ’26", "2026-09", "2026-10"), ("Oct ’26", "2026-10", "2026-11"),
       ("Nov ’26", "2026-11", "9999-99")]


RATIO_EDGES = [0.1, 1, 10, 100, 1000]
RATIO_LABELS = ["Under 0.1×", "0.1–1×", "1–10×", "10–100×", "100–1,000×", "Over 1,000×"]
FIRST_LABELS = ["Perps first", "Both within a day", "Outcomes first, perps later", "Outcomes only, no perps", "Not known"]


def _status(p):
    """0 traded perps in the last 7 days, 1 in the last 30, 2 earlier only, 3 never."""
    day, week, month, ever = p["perp"]
    return 0 if week > 0 else 1 if month > 0 else 2 if ever > 0 else 3


def build_hl():
    ports = _json("hl_portfolios.json.gz", {})
    if not ports:
        return None
    feed = hl.feed_wallets()
    sample = _json("hl_outcome_sample.json.gz", {"wallets": {}, "asof": None})
    lb = _json("hl_leaderboard.json.gz", {"rows": {}})
    holders = _json("hl_outcome_holders.json.gz", {"checked": {}, "window": "month"})
    asof = max(p["t"] for p in ports.values()) * 1000
    today = datetime.fromtimestamp(asof / 1000, timezone.utc).strftime("%Y-%m-%d")

    days = sorted(feed)
    daily = {k: [] for k in ("traders", "s0", "s1", "s2", "s3", "missing")}
    for d in days:
        counts = [0, 0, 0, 0]
        missing = 0
        for w in feed[d]:
            if w in ports:
                counts[_status(ports[w])] += 1
            else:
                missing += 1
        daily["traders"].append(len(feed[d]))
        for i in range(4):
            daily[f"s{i}"].append(counts[i])
        daily["missing"].append(missing)

    def summary(wallets):
        known = [ports[w] for w in wallets if w in ports]
        c = [0, 0, 0, 0]
        for p in known:
            c[_status(p)] += 1
        return {"wallets": len(known), "day": sum(1 for p in known if p["perp"][0] > 0), "s": c}

    feed_all = set().union(*feed.values()) if feed else set()
    groups = {"Price": set(feed_all), "Sports": set(), "Other": set()}
    for w, gs in sample["wallets"].items():
        for g in gs:
            groups[g].add(w)
    universe = set().union(*groups.values())

    out_dollars = defaultdict(float)
    for d in days:
        for w, (dollars, fills) in feed[d].items():
            out_dollars[w] += dollars
    bands = [[0, 0, 0, 0, 0] for _ in OUT_LABELS]
    for w, v in out_dollars.items():
        if w in ports:
            b = bands[_band(v, OUT_EDGES)]
            b[0] += 1
            b[1 + _status(ports[w])] += 1
    age = [[0, 0, 0, 0, 0] for _ in AGE]
    before_launch = [0, 0]
    for w in universe:
        p = ports.get(w)
        if not p or not p["since"]:
            continue
        month = datetime.fromtimestamp(p["since"] / 1000, timezone.utc).strftime("%Y-%m")
        i = next(i for i, (_, lo, hi) in enumerate(AGE) if lo <= month < hi)
        age[i][0] += 1
        age[i][1 + _status(p)] += 1
        pre = datetime.fromtimestamp(p["since"] / 1000, timezone.utc).strftime("%Y-%m-%d") < HIP4_LAUNCH
        before_launch[0] += pre
        before_launch[1] += 1

    # dollars: what the price-market traders did in outcomes over the feed's days vs in perps over 7 days
    week_days = [d for d in days if d != today][-7:]
    out7 = defaultdict(float)
    for d in week_days:
        for w, (dollars, fills) in feed[d].items():
            out7[w] += dollars
    both = [(out7[w], ports[w]["perp"][1]) for w in out7 if w in ports and ports[w]["perp"][1] > 0]
    volumes = {"days": len(week_days), "outcome": round(sum(out7.values())), "wallets": len(out7),
               "overlap_wallets": len(both), "overlap_outcome": round(sum(o for o, _ in both)),
               "overlap_perp": round(sum(p for _, p in both)), "perp_bigger": sum(1 for o, p in both if p > o)}

    ratio = [0] * len(RATIO_LABELS)
    for o, p in both:
        ratio[_band(p / o if o else 1e9, RATIO_EDGES)] += 1

    # what each outcome trader traded first on Hyperliquid
    fills = _json("hl_first_fills.json.gz", {})
    order = [0] * len(FIRST_LABELS)
    for w in universe:
        p, f = ports.get(w), fills.get(w)
        if not p or not f:
            continue
        pre = p["since"] and datetime.fromtimestamp(p["since"] / 1000, timezone.utc).strftime("%Y-%m-%d") < HIP4_LAUNCH
        if p["perp"][3] <= 0:
            order[3] += 1                                   # never traded a perp
        elif f["capped"] and pre:
            order[0] += 1                                   # busy account from before outcome markets existed
        elif f["p"] and f["o"]:
            order[0 if f["p"] < f["o"] else 2 if f["p"] - f["o"] > 86400e3 else 1] += 1
        elif f["p"] and pre:
            order[0] += 1
        else:
            order[4] += 1

    # back-history: for the wallets known today, which weeks each traded outcomes and perps
    week0 = _day(hl.WEEK0)
    week0_ms = int(week0.timestamp() * 1000)
    n_weeks = (datetime.fromtimestamp(asof / 1000, timezone.utc) - week0).days // 7 + 1
    hist = {k: [0] * n_weeks for k in ("traders", "same", "prior3", "earlier", "never")}
    hist_wallets = hist_open = 0
    for w in universe:
        f = fills.get(w)
        if not f or "wo" not in f:
            continue
        hist_wallets += 1
        wp = set(f["wp"])
        # A busy wallet's record may start after its real first trade, and until the second pass
        # has read it through it stops before the present: count it only in weeks read in full.
        first_ok, last_ok = 0, n_weeks - 1
        if f["capped"]:
            first_ok = (f["from"] - week0_ms) // hl.WEEK_MS + 1
            if f.get("open", True):
                hist_open += 1
                last_ok = ((f["to"] - week0_ms) // hl.WEEK_MS if f.get("to") else max(f["wo"] + f["wp"] + [-1])) - 1
        # first perps fill on record, as a week; negative when it was before outcome markets opened
        p_week = (f["p"] - week0_ms) // hl.WEEK_MS if f["p"] else None
        for i in f["wo"]:
            if not first_ok <= i <= min(last_ok, n_weeks - 1):
                continue
            hist["traders"][i] += 1
            if i in wp:
                hist["same"][i] += 1
            elif any(j in wp for j in (i - 1, i - 2, i - 3)):
                hist["prior3"][i] += 1
            elif (p_week is not None and p_week < i) or (f["capped"] and ports.get(w, {"perp": [0] * 4})["perp"][3] > 0):
                hist["earlier"][i] += 1
            else:
                hist["never"][i] += 1
    first_week = next((i for i, n in enumerate(hist["traders"]) if n >= 20), 0)
    history = {"weeks": [(week0 + timedelta(weeks=i)).strftime("%Y-%m-%d") for i in range(first_week, n_weeks)],
               "wallets": hist_wallets, "busy": sum(1 for w in universe if fills.get(w, {}).get("capped")),
               "unread": hist_open, **{k: v[first_week:] for k, v in hist.items()}} if hist_wallets else None

    # the other direction: perps accounts on the public leaderboard that touch outcomes
    rows = lb["rows"]
    checked = holders["checked"]
    reverse = {}
    for i, win in ((1, "week"), (2, "month")):
        active = {w for w, v in rows.items() if v[i] > 0}
        have = {w for w in active if w in checked}
        held = {w for w in have if checked[w][0] > 0}
        seen = active & universe
        reverse[win] = {"active": len(active), "checked": len(have), "holding": len(held), "seen_trading": len(seen),
                        "either": len(held | seen)}
    reverse["leaderboard"] = len(rows)
    reverse["universe_on_leaderboard"] = len(universe & set(rows))
    return {
        "asof": asof, "launch": HIP4_LAUNCH, "feed_from": days[0] if days else None, "sample_asof": sample.get("asof"),
        "sample_trades": sample.get("trades"), "sample_markets": sample.get("markets"),
        "days": days, "partial_day": today if today in days else None, "daily": daily,
        "universe": summary(universe), "feed": summary(feed_all),
        "groups": [{"group": g, **summary(ws)} for g, ws in groups.items()],
        "bands": [{"label": OUT_LABELS[i], "wallets": b[0], "s": b[1:]} for i, b in enumerate(bands)],
        "age": [{"label": AGE[i][0], "wallets": a[0], "s": a[1:]} for i, a in enumerate(age)],
        "before_launch": before_launch, "volumes": volumes, "reverse": reverse,
        "ratio": [{"label": RATIO_LABELS[i], "wallets": n} for i, n in enumerate(ratio)],
        "firsts": [{"label": FIRST_LABELS[i], "wallets": n} for i, n in enumerate(order)] if fills else [],
        "history": history, "partial_week": (week0 + timedelta(weeks=n_weeks - 1)).strftime("%Y-%m-%d"),
    }


def main():
    out = {"built": int(time.time() * 1000), "poly": build_poly(), "hl": build_hl()}
    os.makedirs(os.path.dirname(SITE), exist_ok=True)
    with open(SITE, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"wrote {SITE} ({os.path.getsize(SITE) / 1e3:.0f} kB); polymarket {'ok' if out['poly'] else 'missing'}, "
          f"hyperliquid {'ok' if out['hl'] else 'missing'}")


if __name__ == "__main__":
    main()
