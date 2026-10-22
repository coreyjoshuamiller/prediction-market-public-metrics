"""Top-trader tables.

Polymarket: ranked from Dune over in-scope short-term price markets (7d daily, 30d weekly),
enriched with the public profile (name, avatar, linked X account).
Kalshi: account-level trades aren't public, so this uses Kalshi's public leaderboard
(Crypto and Financials categories), which is the closest available cut.
predict.fun: ranked from Dune like Polymarket (Up/Down markets), with the account name and X
handle from predict.fun's public GraphQL API.

Windows: 7d (daily) and 30d (weekly) are exact rolling queries. 8w, 13w and "all" are sums of
stored weekly pieces (data/leaders_weekly/<venue>/<monday>.csv.gz, each week's top PER_WEEK
wallets, queried once when the week completes), so a long window costs nothing extra to rebuild.
A wallet's weeks outside that week's top PER_WEEK aren't counted; the window records the largest
such cut-off so the page can say how much a rank could be off by.
"""
import csv
import gzip
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from .dune import run_sql
from .net import get_json
from . import reach
from .predict_api import checksum_address, gql
from .queries import poly_leaders, poly_leaders_weekly, predict_leaders, predict_leaders_weekly

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "leaders.json")
PROFILES = os.path.join(ROOT, "data", "poly_profiles.json")
PREDICT_PROFILES = os.path.join(ROOT, "data", "predict_profiles.json")
KALSHI_SOCIAL = "https://api.elections.kalshi.com/v1/social"
PROFILE_TTL_DAYS = 7
POLY_TOP = 500  # wallets ranked per window
WEEKLY_DIR = os.path.join(ROOT, "data", "leaders_weekly")
PER_WEEK = 2000
WEEKS_PER_QUERY = 5  # a month-ish per Dune query: the market metadata is a fixed cost per query
WEEKLY_START = {"polymarket": date(2025, 9, 1), "predict": date(2025, 11, 24)}  # first weeks in scope
WEEKLY_QUERY = {"polymarket": poly_leaders_weekly, "predict": predict_leaders_weekly}
LONG_WINDOWS = {"8w": 8, "13w": 13, "all": None}  # complete weeks; None = every stored week
WEEK_FIELDS = ["rk", "trader", "volume", "notional", "fills", "markets", "maker_vol", "pnl", "top_market", "top_market_vol"]


def _load(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def poly_profiles(addresses):
    cache = _load(PROFILES, {})
    cutoff = (date.today() - timedelta(days=PROFILE_TTL_DAYS)).isoformat()
    todo = [a for a in addresses if cache.get(a, {}).get("fetched", "") < cutoff]

    def fetch(addr):
        try:
            p = get_json("https://gamma-api.polymarket.com/public-profile", {"address": addr}, retries=2)
        except Exception:
            p = {}
        return addr, {
            "name": p.get("name") or p.get("pseudonym") or "",
            "pseudonym": p.get("pseudonym") or "",
            "x": p.get("xUsername") or "",
            "verified": bool(p.get("verifiedBadge")),
            "image": p.get("profileImage") or "",
            "fetched": date.today().isoformat(),
        }

    with ThreadPoolExecutor(8) as ex:
        for addr, prof in ex.map(fetch, todo):
            cache[addr] = prof
    with open(PROFILES, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    return cache


def _format(venue, rows):
    """Leaderboard rows (trader, volume, notional, fills, markets, maker_share, realized_pnl,
    top_market) -> site rows with profile name / X / link."""
    profs = (poly_profiles if venue == "polymarket" else predict_profiles)([r["trader"] for r in rows])
    out = []
    for i, r in enumerate(rows, 1):
        p = profs.get(r["trader"], {})
        name = p.get("name") or ""
        if name.lower().startswith("0x"):  # unnamed accounts show their address as a name
            name = ""
        url = (f"https://polymarket.com/profile/{r['trader']}" if venue == "polymarket"
               else f"https://bscscan.com/address/{checksum_address(r['trader'])}")
        out.append({
            "rank": i,
            "address": r["trader"],
            "name": name,
            "x": p.get("x", ""),
            **({"verified": p.get("verified", False)} if venue == "polymarket" else {}),
            "image": p.get("image", ""),
            "url": url,
            "volume": round(r["volume"] or 0),
            "notional": round(r["notional"] or 0),
            "pnl": None if r["realized_pnl"] is None else round(r["realized_pnl"]),
            "fills": r["fills"],
            "markets": r["markets"],
            "maker_share": None if r["maker_share"] is None else round(r["maker_share"], 3),
            "top_market": r["top_market"],
        })
    return out


def poly_window(days, today):
    end = today
    start = end - timedelta(days=days)
    rows = run_sql(poly_leaders(start, end, limit=POLY_TOP), label=f"leaders {days}d")
    return {"start": str(start), "end": str(end), "rows": _format("polymarket", rows)}


PREDICT_ACCOUNT = "query($a: Address!) { account(address: $a) { name twitterUsername imageUrl } }"


def predict_profiles(addresses):
    """Account name / X handle per wallet. The API only matches checksummed addresses."""
    cache = _load(PREDICT_PROFILES, {})
    cutoff = (date.today() - timedelta(days=PROFILE_TTL_DAYS)).isoformat()
    todo = [a for a in addresses if cache.get(a, {}).get("fetched", "") < cutoff]

    def fetch(addr):
        try:
            p = gql(PREDICT_ACCOUNT, {"a": checksum_address(addr)}, retries=2).get("account") or {}
        except Exception:
            p = {}
        return addr, {"name": p.get("name") or "", "x": p.get("twitterUsername") or "",
                      "image": p.get("imageUrl") or "", "fetched": date.today().isoformat()}

    with ThreadPoolExecutor(4) as ex:  # their API is shared with the website; stay gentle
        for addr, prof in ex.map(fetch, todo):
            cache[addr] = prof
    with open(PREDICT_PROFILES, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    return cache


def predict_window(days, today):
    end = today
    start = end - timedelta(days=days)
    rows = run_sql(predict_leaders(start, end, limit=POLY_TOP), label=f"predict leaders {days}d")
    return {"start": str(start), "end": str(end), "rows": _format("predict", rows)}


# --- long windows from weekly pieces --------------------------------------------------------

def _week_path(venue, wk):
    return os.path.join(WEEKLY_DIR, venue, f"{wk}.csv.gz")


def _complete_weeks(venue, today):
    """Mondays of every week in scope that has fully ended before `today`."""
    wk, last = WEEKLY_START[venue], today - timedelta(days=today.weekday() + 7)
    out = []
    while wk <= last:
        out.append(wk)
        wk += timedelta(days=7)
    return out


def _week_row(r):
    cents = lambda x: "" if x is None else round(float(x), 2)  # keeps the stored files small
    return {**r, "pnl": cents(r.get("realized_pnl", r.get("pnl"))),
            **{k: cents(r[k]) for k in ("volume", "notional", "maker_vol", "top_market_vol")}}


def fetch_weeks(venue, today):
    """Query and store every complete week that isn't stored yet, a few weeks per query."""
    missing = [w for w in _complete_weeks(venue, today) if not os.path.exists(_week_path(venue, w))]
    runs, run = [], []
    for w in missing:  # consecutive weeks, at most WEEKS_PER_QUERY per query
        if run and (w - run[-1] != timedelta(days=7) or len(run) == WEEKS_PER_QUERY):
            runs.append(run)
            run = []
        run.append(w)
    if run:
        runs.append(run)
    os.makedirs(os.path.join(WEEKLY_DIR, venue), exist_ok=True)

    def one(run):
        start, end = run[0], run[-1] + timedelta(days=7)
        rows = run_sql(WEEKLY_QUERY[venue](start, end, PER_WEEK), label=f"{venue} leaders weeks {start}")
        by = defaultdict(list)
        for r in rows:
            by[str(r["wk"])[:10]].append(r)
        for w in run:  # an empty week still gets a file, so it isn't queried again
            with gzip.open(_week_path(venue, w), "wt", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=WEEK_FIELDS, extrasaction="ignore")
                wr.writeheader()
                wr.writerows(sorted((_week_row(r) for r in by.get(str(w), [])), key=lambda r: r["rk"]))
        return len(rows)

    with ThreadPoolExecutor(3) as ex:
        n = sum(ex.map(one, runs))
    if runs:
        print(f"  {venue} leaders: stored {len(missing)} weeks ({n} rows, {len(runs)} queries)")


def _load_weeks(venue, today):
    weeks = {}
    for w in _complete_weeks(venue, today):
        path = _week_path(venue, w)
        if os.path.exists(path):
            with gzip.open(path, "rt") as f:
                weeks[w] = list(csv.DictReader(f))
    return weeks


def long_window(venue, weeks, n_weeks):
    """Sum the last `n_weeks` stored weeks (all of them if None) into a ranked window."""
    keys = sorted(weeks)[-n_weeks:] if n_weeks else sorted(weeks)
    if not keys:
        return None
    acc = defaultdict(lambda: {"volume": 0.0, "notional": 0.0, "fills": 0, "markets": 0, "maker_vol": 0.0,
                               "pnl": None, "mkts": defaultdict(float)})
    cutoff = 0.0  # the most volume a wallet could have in a week without making that week's list
    for w in keys:
        rows = weeks[w]
        if len(rows) >= PER_WEEK:
            cutoff = max(cutoff, float(rows[-1]["volume"]))
        for r in rows:
            a = acc[r["trader"]]
            a["volume"] += float(r["volume"])
            a["notional"] += float(r["notional"] or 0)
            a["fills"] += int(r["fills"])
            a["markets"] += int(r["markets"])  # markets resolve within a day, so weeks barely overlap
            a["maker_vol"] += float(r["maker_vol"] or 0)
            if r["pnl"] not in ("", None):
                a["pnl"] = (a["pnl"] or 0) + float(r["pnl"])
            if r["top_market"]:
                a["mkts"][r["top_market"]] += float(r["top_market_vol"] or 0)
    ranked = sorted(acc.items(), key=lambda kv: -kv[1]["volume"])[:POLY_TOP]
    rows = [{"trader": t, "volume": a["volume"], "notional": a["notional"], "fills": a["fills"], "markets": a["markets"],
             "maker_share": a["maker_vol"] / a["volume"] if a["volume"] else None, "realized_pnl": a["pnl"],
             "top_market": max(a["mkts"], key=a["mkts"].get) if a["mkts"] else None} for t, a in ranked]
    return {"start": str(keys[0]), "end": str(keys[-1] + timedelta(days=7)), "rows": _format(venue, rows),
            "weekly": {"weeks": len(keys), "per_week": PER_WEEK, "max_unlisted_week_volume": round(cutoff)}}


def update_long_windows(data, venue, today):
    fetch_weeks(venue, today)
    weeks = _load_weeks(venue, today)
    for name, n in LONG_WINDOWS.items():
        w = long_window(venue, weeks, n)
        if w:
            data[venue][name] = w


def kalshi_board(category, period):
    def lb(metric):
        params = {"metric_name": metric, "time_period": period, "limit": 100}
        if category != "All":
            params["category"] = category
        try:
            return get_json(f"{KALSHI_SOCIAL}/leaderboard", params).get("rank_list") or []
        except Exception as e:
            print(f"  kalshi leaderboard {category}/{period}/{metric}: {e}")
            return []

    vol, pnl = lb("volume"), lb("projected_pnl")
    pnl_by = {r["nickname"]: r["value"] for r in pnl}
    rows = []
    for r in vol:
        nick = r["nickname"]
        rows.append({
            "rank": r["rank"],
            "name": nick,
            "anonymous": r.get("is_anonymous", False),
            "url": f"https://kalshi.com/ideas/profiles/{nick}",
            "volume_contracts": round(r["value"]),
            "pnl": None if nick not in pnl_by else round(pnl_by[nick]),
        })
    # PnL leaders who aren't in the volume top 100 are still interesting
    in_vol = {r["name"] for r in rows}
    pnl_only = [{"rank": None, "name": r["nickname"], "anonymous": r.get("is_anonymous", False),
                 "url": f"https://kalshi.com/ideas/profiles/{r['nickname']}", "volume_contracts": None,
                 "pnl": round(r["value"]), "pnl_rank": r["rank"]}
                for r in pnl if r["nickname"] not in in_vol]
    return {"rows": rows, "pnl_only": pnl_only}


def attach_reach(windows):
    """Add opt-in contact channels (XMTP inbox, ENS/Farcaster/etc.) to each ranked wallet.
    A lookup failure shouldn't block the leaderboard, so errors just leave rows without it."""
    addrs = [r["address"] for w in windows.values() for r in w.get("rows", [])]
    try:
        cache = reach.update(addrs)
    except Exception as e:
        print(f"  reach lookup failed: {e}")
        return
    for w in windows.values():
        for r in w.get("rows", []):
            c = cache.get(r["address"].lower())
            if c:
                r["account"] = c["type"]
                r["xmtp"] = c["signer"] if c["xmtp"] else None
                r["profiles"] = c["profiles"]


def update(today, force_all=False):
    data = _load(OUT, {"polymarket": {}, "kalshi": {}})
    data.setdefault("polymarket", {})
    data["polymarket"]["7d"] = poly_window(7, today)
    # the 30-day ranking scans ~4x the data; refresh it weekly (Mondays) unless missing
    if force_all or today.weekday() == 0 or "30d" not in data["polymarket"]:
        data["polymarket"]["30d"] = poly_window(30, today)
    try:  # long windows are additive; a failure here shouldn't cost the 7d/30d boards
        update_long_windows(data, "polymarket", today)
    except Exception as e:
        print(f"  polymarket long-window leaders failed: {e}")
    attach_reach(data["polymarket"])
    try:  # a newer source: don't let it fail the rest of the leaderboard
        data.setdefault("predict", {})
        data["predict"]["7d"] = predict_window(7, today)
        if force_all or today.weekday() == 0 or "30d" not in data["predict"]:
            data["predict"]["30d"] = predict_window(30, today)
        update_long_windows(data, "predict", today)
    except Exception as e:
        print(f"  predict.fun leaders failed: {e}")
    data["kalshi"] = {
        f"{cat}|{per}": kalshi_board(cat, per)
        for cat in ("Crypto", "Financials")
        for per in ("monthly", "all_time")
    }
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(OUT, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    print(f"  leaders.json written")


if __name__ == "__main__":
    update(datetime.now(timezone.utc).date(), force_all=True)
