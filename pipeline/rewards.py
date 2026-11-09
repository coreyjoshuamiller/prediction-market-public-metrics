"""Liquidity rewards by category, across each venue's whole platform.

  Kalshi      /incentive_programs lists every Liquidity Incentive pool, per market. A pool counts
              if it was marked paid or is still in progress or just ended, spread evenly over its
              period. Only the pool size is public, so this is an upper limit: a market without
              two-sided depth pays less than its pool. History starts 2025-09-24.
  Polymarket  Actual on-chain payouts (queries.poly_rewards_paid). Transfers don't name a market, so
              the split is estimated. Polymarket's posted reward rates are the better guide but are
              only available live (Gamma drops them from most markets once they close), so they're
              saved once a day and used from the first saved day; before that, each paid wallet's
              payout is split by where it made markets that day.
"""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .net import get_json

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_REWARDS = "https://clob.polymarket.com/rewards/markets/current"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
CATEGORIES = ["Sports", "Politics & world", "Finance & economy", "Crypto", "Tech & science",
              "Culture & entertainment", "Weather", "Other", "Not attributed"]
KALSHI_CATEGORY = {
    "Sports": "Sports",
    "Elections": "Politics & world", "Politics": "Politics & world", "World": "Politics & world",
    "Financials": "Finance & economy", "Economics": "Finance & economy", "Commodities": "Finance & economy",
    "Companies": "Finance & economy", "Business": "Finance & economy",
    "Crypto": "Crypto",
    "Science and Technology": "Tech & science",
    "Entertainment": "Culture & entertainment", "Culture": "Culture & entertainment",
    "Social": "Culture & entertainment", "Mentions": "Culture & entertainment",
    "Climate and Weather": "Weather",
}
# Polymarket tags, first match wins; a market can carry several of these
POLY_CATEGORY_TAGS = [
    ("Sports", ["Sports", "Esports", "Games"]),
    ("Crypto", ["Crypto"]),
    ("Weather", ["Weather", "Daily Temperature"]),
    ("Finance & economy", ["Finance", "Economy", "Business"]),
    ("Tech & science", ["Tech", "AI", "Science"]),
    ("Politics & world", ["Politics", "Elections", "Geopolitics"]),
    ("Culture & entertainment", ["Culture", "Pop Culture", "Mentions"]),
]
PAYOUT_GRACE_DAYS = 2  # Kalshi marks almost all pools paid within a day or two of ending


def poly_category_sql(tags="t"):
    whens = []
    for cat, names in POLY_CATEGORY_TAGS:
        match = " OR ".join(f"contains({tags}, '{n}')" for n in names)
        whens.append(f"WHEN {match} THEN '{cat}'")
    return f"CASE {' '.join(whens)} ELSE 'Other' END"


def _market_tags(condition_ids):
    query = "&".join(f"condition_ids={c}" for c in condition_ids)
    return get_json(f"{GAMMA_MARKETS}?limit=100&include_tag=true&{query}", timeout=60)


def poly_posted_rates():
    """-> {category: dollars per day} that Polymarket is offering right now."""
    rates, cursor = {}, None
    while True:
        page = get_json(CLOB_REWARDS, {"next_cursor": cursor} if cursor else None)
        rates.update({m["condition_id"]: m.get("total_daily_rate") or 0 for m in page["data"]})
        cursor = page.get("next_cursor")
        if not page["data"] or not cursor or cursor == "LTE=":  # LTE= marks the last page
            break
    ids = list(rates)
    with ThreadPoolExecutor(8) as pool:
        pages = pool.map(_market_tags, [ids[i:i + 50] for i in range(0, len(ids), 50)])
    tags = {m["conditionId"]: m.get("tags") for page in pages for m in page}
    totals = defaultdict(float)
    for condition_id, rate in rates.items():
        labels = {t.get("label") for t in tags.get(condition_id) or []}
        totals[next((cat for cat, names in POLY_CATEGORY_TAGS if labels.intersection(names)), "Other")] += rate
    return totals


def _programs():
    out, cursor = [], None
    while True:
        params = {"limit": 1000, "status": "all"}
        if cursor:
            params["cursor"] = cursor
        page = get_json(f"{KALSHI}/incentive_programs", params, timeout=60)
        out += page.get("incentive_programs") or []
        cursor = page.get("next_cursor")
        if not cursor:
            return out


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def kalshi_daily(today):
    """-> [{d, category, usd}] for days before `today`. period_reward is in centi-cents."""
    series = {s["ticker"]: s.get("category") for s in get_json(f"{KALSHI}/series", timeout=90).get("series", [])}
    end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    paid_by = end - timedelta(days=PAYOUT_GRACE_DAYS)
    totals = defaultdict(float)
    for p in _programs():
        a, b = _ts(p["start_date"]), _ts(p["end_date"])
        if p["incentive_type"] != "liquidity" or b <= a or a >= end or (not p["paid_out"] and b < paid_by):
            continue
        cat = KALSHI_CATEGORY.get(series.get(p["market_ticker"].split("-")[0]), "Other")
        usd_per_sec = p["period_reward"] / 1e4 / (b - a).total_seconds()
        t, stop = a, min(b, end)
        while t < stop:  # a program can start and end at any time of day
            next_day = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            totals[t.date().isoformat(), cat] += usd_per_sec * (min(next_day, stop) - t).total_seconds()
            t = next_day
    return [{"d": d, "category": c, "usd": round(v, 2)} for (d, c), v in totals.items()]
