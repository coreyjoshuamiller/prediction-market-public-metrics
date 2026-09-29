"""Top-trader tables.

Polymarket: ranked from Dune over in-scope short-term price markets (7d daily, 30d weekly),
enriched with the public profile (name, avatar, linked X account).
Kalshi: account-level trades aren't public, so this uses Kalshi's public leaderboard
(Crypto and Financials categories), which is the closest available cut.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from .dune import run_sql
from .net import get_json
from .queries import poly_leaders

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "leaders.json")
PROFILES = os.path.join(ROOT, "data", "poly_profiles.json")
KALSHI_SOCIAL = "https://api.elections.kalshi.com/v1/social"
PROFILE_TTL_DAYS = 7


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


def poly_window(days, today):
    end = today
    start = end - timedelta(days=days)
    rows = run_sql(poly_leaders(start, end, limit=100), label=f"leaders {days}d")
    profs = poly_profiles([r["trader"] for r in rows])
    out = []
    for i, r in enumerate(rows, 1):
        p = profs.get(r["trader"], {})
        name = p.get("name") or ""
        if name.lower().startswith("0x"):  # unnamed accounts show their address as a name
            name = ""
        out.append({
            "rank": i,
            "address": r["trader"],
            "name": name,
            "x": p.get("x", ""),
            "verified": p.get("verified", False),
            "image": p.get("image", ""),
            "url": f"https://polymarket.com/profile/{r['trader']}",
            "volume": round(r["volume"] or 0),
            "notional": round(r["notional"] or 0),
            "pnl": None if r["realized_pnl"] is None else round(r["realized_pnl"]),
            "fills": r["fills"],
            "markets": r["markets"],
            "maker_share": None if r["maker_share"] is None else round(r["maker_share"], 3),
            "top_market": r["top_market"],
        })
    return {"start": str(start), "end": str(end), "rows": out}


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


def update(today, force_all=False):
    data = _load(OUT, {"polymarket": {}, "kalshi": {}})
    data.setdefault("polymarket", {})
    data["polymarket"]["7d"] = poly_window(7, today)
    # the 30-day ranking scans ~4x the data; refresh it weekly (Mondays) unless missing
    if force_all or today.weekday() == 0 or "30d" not in data["polymarket"]:
        data["polymarket"]["30d"] = poly_window(30, today)
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
