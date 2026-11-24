"""Hyperliquid: do outcome-market (HIP-4) traders also trade perps?

Outcome markets and perps share one account on HyperCore, so the overlap is by address.

Outcome traders come from two places:
  * the trade feed this repo already follows (data/hyperliquid/wallets/, price markets, daily
    since 2026-11-12), and
  * a snapshot of the trades Hyperliquid replays on subscribing to every live outcome market,
    which adds sports and other markets the collector doesn't follow (`sample`).
Each wallet's perps activity comes from the public `portfolio` request: perps volume over the
last day, week, month and all time, beside the same windows for the whole account.

  python3 -m pipeline.overlap_hl sample       # recent trades on every live outcome market (~2 min)
  python3 -m pipeline.overlap_hl portfolios   # one request per wallet, 60 a minute
  python3 -m pipeline.overlap_hl leaderboard  # Hyperliquid's public perps leaderboard (the perps side)
  python3 -m pipeline.overlap_hl holders      # which month-active leaderboard accounts hold outcome positions
  python3 -m pipeline.overlap_hl firsts       # each wallet's own fill history: first trades, weekly activity (~60-90 min)
"""
import glob
import gzip
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict

from .ws import WebSocket, WSClosed

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "overlap")
WALLETS = os.path.join(ROOT, "data", "hyperliquid", "wallets")
SAMPLE = os.path.join(OUT, "hl_outcome_sample.json.gz")
PORTFOLIOS = os.path.join(OUT, "hl_portfolios.json.gz")
LEADERBOARD = os.path.join(OUT, "hl_leaderboard.json.gz")
HOLDERS = os.path.join(OUT, "hl_outcome_holders.json.gz")
FIRSTS = os.path.join(OUT, "hl_first_fills.json.gz")
INFO = "https://api.hyperliquid.xyz/info"
WINDOWS = ("day", "week", "month", "allTime")


def info(body, retries=6):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(INFO, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))  # 429s clear within the minute


def market_group(name, desc):
    """Price markets (the ones the main dashboard follows) vs sports vs everything else."""
    text = f"{name}|{desc}"
    if "perp:" in desc or name.startswith("Recurring") or "price" in text.lower():
        return "Price"
    if "sports" in name.lower() or "competition:" in desc or "participant:" in desc:
        return "Sports"
    return "Other"


def sample(listen=75):
    """Subscribe to trades on every live outcome market and keep what the feed replays.

    Each trade carries both wallets. Only the Yes coin is read: the No coin prints the same trades."""
    meta = info({"type": "outcomeMeta"})
    questions = {}
    for q in meta.get("questions", []):
        for oid in q.get("namedOutcomes", []) + ([q["fallbackOutcome"]] if q.get("fallbackOutcome") is not None else []):
            questions[oid] = q
    coins = {}
    for o in meta["outcomes"]:
        q = questions.get(o["outcome"], {})
        coins[f"#{o['outcome'] * 10}"] = market_group(o.get("name", "") + "|" + q.get("name", ""), o.get("description", "") + "|" + q.get("description", ""))
    trades, seen = [], set()
    names = sorted(coins)
    for i in range(0, len(names), 90):  # at most 100 subscriptions per connection
        ws = WebSocket("api.hyperliquid.xyz")
        for c in names[i:i + 90]:
            ws.send_json({"method": "subscribe", "subscription": {"type": "trades", "coin": c}})
        end = time.time() + listen
        try:
            while time.time() < end:
                msg = ws.recv_json(timeout=5)
                if not msg or msg.get("channel") != "trades":
                    continue
                for t in msg["data"]:
                    if t["tid"] in seen:
                        continue
                    seen.add(t["tid"])
                    trades.append([t["coin"], t["time"], float(t["px"]), float(t["sz"]), t["users"][0].lower(), t["users"][1].lower()])
        except WSClosed:
            pass
        ws.close()
        print(f"  {min(i + 90, len(names))}/{len(names)} markets, {len(trades)} trades", flush=True)
    wallets = defaultdict(lambda: defaultdict(lambda: [0, 0.0, None, None]))  # wallet -> group -> [fills, dollars, first, last]
    for coin, ts, px, sz, buyer, seller in trades:
        for w, dollars in ((buyer, px * sz), (seller, (1 - px) * sz)):  # the seller of Yes holds the No side
            g = wallets[w][coins[coin]]
            g[0] += 1
            g[1] += dollars
            g[2] = ts if g[2] is None else min(g[2], ts)
            g[3] = ts if g[3] is None else max(g[3], ts)
    os.makedirs(OUT, exist_ok=True)
    per_coin = defaultdict(lambda: [0, None])
    for coin, ts, *_ in trades:
        per_coin[coin][0] += 1
        per_coin[coin][1] = ts if per_coin[coin][1] is None else min(per_coin[coin][1], ts)
    out = {"asof": int(time.time() * 1000), "markets": len(names), "trades": len(trades),
           "coins": {c: [coins[c], per_coin[c][0], per_coin[c][1]] for c in names},
           "wallets": {w: {g: [v[0], round(v[1], 2), v[2], v[3]] for g, v in gs.items()} for w, gs in wallets.items()}}
    with gzip.open(SAMPLE, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    by_group = defaultdict(set)
    for w, gs in wallets.items():
        for g in gs:
            by_group[g].add(w)
    print(f"sample: {len(trades)} trades on {len(names)} markets, {len(wallets)} wallets "
          + ", ".join(f"{g} {len(ws_)}" for g, ws_ in sorted(by_group.items())))


def feed_wallets():
    """{day: {wallet: [dollars, fills]}} from the collector's daily wallet files (price markets)."""
    days = {}
    for path in sorted(glob.glob(os.path.join(WALLETS, "*.json.gz"))):
        day = os.path.basename(path)[:10]
        with gzip.open(path, "rt") as f:
            data = json.load(f)
        days[day] = {w.lower(): [round(sum(v[0] for k, v in keys.items() if k != "_m"), 2),
                                 int(sum(v[3] for k, v in keys.items() if k != "_m"))] for w, keys in data.items()}
    return days


def _load(path, default):
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            return json.load(f)
    return default


def portfolios(max_age_hours=20):
    """Per wallet: perps volume and whole-account volume for each window, and when the account began.

    `portfolio` costs 20 of the 1,200 weight a minute Hyperliquid allows per IP, so 1 a second."""
    want = set()
    for day in feed_wallets().values():
        want |= set(day)
    want |= set(_load(SAMPLE, {"wallets": {}})["wallets"])
    state = _load(PORTFOLIOS, {})
    now = time.time()
    todo = sorted(w for w in want if w not in state or now - state[w]["t"] > max_age_hours * 3600)
    print(f"portfolios: {len(todo)} of {len(want)} wallets to read (~{len(todo) / 55:.0f} min)")
    for i, w in enumerate(todo):
        t0 = time.time()
        try:
            p = dict(info({"type": "portfolio", "user": w}))
        except Exception as e:
            print(f"  {w}: {e}")
            continue
        hist = p.get("allTime", {}).get("accountValueHistory") or []
        state[w] = {"t": int(time.time()),
                    "perp": [round(float(p.get("perp" + k[0].upper() + k[1:], {}).get("vlm") or 0), 2) for k in WINDOWS],
                    "all": [round(float(p.get(k, {}).get("vlm") or 0), 2) for k in WINDOWS],
                    "since": hist[0][0] if hist else None,
                    "value": round(float(hist[-1][1]), 2) if hist else 0}
        if (i + 1) % 100 == 0 or i + 1 == len(todo):
            with gzip.open(PORTFOLIOS + ".tmp", "wt") as f:
                json.dump(state, f, separators=(",", ":"))
            os.replace(PORTFOLIOS + ".tmp", PORTFOLIOS)
            print(f"  {i + 1}/{len(todo)}", flush=True)
        time.sleep(max(0, 1.05 - (time.time() - t0)))


def fill_kind(coin):
    """'o' outcome token, 's' spot pair, 'p' perp (including builder-deployed perps like xyz:XYZ100)."""
    return "o" if coin.startswith("#") else "s" if coin.startswith("@") or "/" in coin else "p"


WEEK0 = "2026-04-27"  # Monday before outcome markets launched; week numbers count from here
WEEK_MS = 7 * 86400 * 1000


def _week0_ms():
    from datetime import datetime, timezone
    return int(datetime.strptime(WEEK0, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _read_fills(w, start, max_pages, week0):
    """Page forward through one wallet's fills from `start`. Returns None on a failed request, else
    (first fill time by kind, weeks traded by kind, fills read, oldest time, newest time, whether
    the last page was full, i.e. more fills remain)."""
    first, weeks = {"o": None, "p": None, "s": None}, {"o": set(), "p": set(), "s": set()}
    n, pages, earliest, latest, fills = 0, 0, None, None, []
    while pages < max_pages:
        t0 = time.time()
        try:
            fills = info({"type": "userFillsByTime", "user": w, "startTime": start, "aggregateByTime": True})
        except Exception as e:
            print(f"  {w}: {e}")
            return None
        pages += 1
        for f in fills:
            k = fill_kind(f["coin"])
            first[k] = first[k] or f["time"]
            if f["time"] >= week0:
                weeks[k].add((f["time"] - week0) // WEEK_MS)
        n += len(fills)
        if fills:
            earliest = earliest if earliest is not None else fills[0]["time"]
            latest = fills[-1]["time"]
        time.sleep(max(0, (20 + len(fills) / 20) / 19 - (time.time() - t0)))
        if len(fills) < 2000:
            return first, weeks, n, earliest, latest, False
        start = fills[-1]["time"] + 1
    return first, weeks, n, earliest, latest, True


def _save_firsts(state):
    with gzip.open(FIRSTS + ".tmp", "wt") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(FIRSTS + ".tmp", FIRSTS)


def firsts(max_pages=5, more_pages=25):
    """Per wallet, from its own fill history (`userFillsByTime`): when it first traded an outcome
    market, a perp and spot, and which weeks it traded outcomes (`wo`) and perps (`wp`).

    The request returns 2,000 fills a page, oldest first. Hyperliquid's docs say only a wallet's
    10,000 most recent fills are served, but paging forward by time went well past that in
    November 2026 (36,000 for one wallet), so five pages do not reach the present for a busy
    wallet. A wallet that fills the first five pages has `capped` set: its record may start after
    its real first trade, so it only counts from `from` (its oldest fill on record). A second pass
    then reads on until it reaches the present; `to` is the newest fill read and `open` is set if
    `more_pages` ran out first. It costs 20 weight plus 1 per 20 fills returned; the pause after each request
    keeps to 1,200 weight a minute."""
    want = set()
    for day in feed_wallets().values():
        want |= set(day)
    want |= set(_load(SAMPLE, {"wallets": {}})["wallets"])
    state = _load(FIRSTS, {})
    todo = sorted(w for w in want if w not in state)
    week0 = _week0_ms()
    print(f"firsts: {len(todo)} of {len(want)} wallets to read")
    for i, w in enumerate(todo):
        got = _read_fills(w, 0, max_pages, week0)
        if got is not None:
            first, weeks, n, earliest, latest, more = got
            # five full pages: older fills may exist that the API no longer serves
            state[w] = {"o": first["o"], "p": first["p"], "s": first["s"], "n": n, "from": earliest,
                        "capped": more, "wo": sorted(weeks["o"]), "wp": sorted(weeks["p"]), "to": latest, "open": more}
        if (i + 1) % 50 == 0 or i + 1 == len(todo):
            _save_firsts(state)
            print(f"  {i + 1}/{len(todo)}", flush=True)

    # second pass: busy wallets, from where the first pass stopped up to the present
    todo = sorted(w for w, v in state.items() if v["capped"] and v.get("open", True))
    print(f"firsts: {len(todo)} busy wallets to read through to the present")
    for i, w in enumerate(todo):
        v = state[w]
        # an older record has no `to`: start again from the last week it saw (or its oldest fill)
        resume = v["to"] + 1 if v.get("to") else max([v["from"] or 0, week0] + [week0 + k * WEEK_MS for k in v["wo"] + v["wp"]])
        got = _read_fills(w, resume, more_pages, week0)
        if got is not None:
            first, weeks, n, earliest, latest, more = got
            for k in "ops":
                v[k] = v[k] or first[k]
            v["wo"], v["wp"] = sorted(set(v["wo"]) | weeks["o"]), sorted(set(v["wp"]) | weeks["p"])
            v["n2"] = v.get("n2", 0) + n
            v["to"], v["open"] = latest or v.get("to") or resume, more
        if (i + 1) % 20 == 0 or i + 1 == len(todo):
            _save_firsts(state)
            print(f"  {i + 1}/{len(todo)}", flush=True)
    left = sum(1 for v in state.values() if v["capped"] and v.get("open", True))
    print(f"firsts: {len(state)} wallets, {sum(1 for v in state.values() if v['capped'])} busy, {left} still not read to the present")


def leaderboard():
    """Hyperliquid's public leaderboard file: about 48,000 perps accounts with volume by window.
    It isn't every perps trader (small accounts are left out), so it's a floor for the perps side."""
    req = urllib.request.Request("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        rows = json.load(r)["leaderboardRows"]
    out = {"asof": int(time.time() * 1000), "rows": {}}
    for r in rows:
        wp = dict(r["windowPerformances"])
        out["rows"][r["ethAddress"].lower()] = [round(float(wp[k]["vlm"])) for k in WINDOWS] + [round(float(r["accountValue"]))]
    os.makedirs(OUT, exist_ok=True)
    with gzip.open(LEADERBOARD, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"leaderboard: {len(rows)} accounts; with volume " + ", ".join(
        f"{k} {sum(1 for v in out['rows'].values() if v[i] > 0)}" for i, k in enumerate(WINDOWS)))


def holders(window="month", per_sec=8):
    """Which perps accounts hold an outcome position right now.

    Outcome tokens show up in `spotClearinghouseState` balances as `+<id>` coins, and that request
    costs 2 weight, so every leaderboard account with perps volume in the window can be checked
    (about 19,000 accounts in 40 minutes). Holding is a floor for trading: an account that traded
    outcomes and is flat again isn't seen here."""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    rows = _load(LEADERBOARD, {"rows": {}})["rows"]
    idx = WINDOWS.index(window)
    state = _load(HOLDERS, {"asof": None, "window": window, "checked": {}})
    todo = sorted(w for w, v in rows.items() if v[idx] > 0 and w not in state["checked"])
    print(f"holders: {len(todo)} accounts to check (~{len(todo) / per_sec / 60:.0f} min)")
    lock, nxt, n = threading.Lock(), [time.time()], [0]

    def save():
        state["asof"] = int(time.time() * 1000)
        with gzip.open(HOLDERS + ".tmp", "wt") as f:
            json.dump(state, f, separators=(",", ":"))
        os.replace(HOLDERS + ".tmp", HOLDERS)

    def work(w):
        with lock:
            at = max(time.time(), nxt[0])
            nxt[0] = at + 1.0 / per_sec
        time.sleep(max(0, at - time.time()))
        try:
            bal = info({"type": "spotClearinghouseState", "user": w}).get("balances", [])
        except Exception:
            return
        held = [b for b in bal if b["coin"].startswith("+") and float(b["total"]) > 0]
        with lock:
            # [outcome tokens held, contracts held]; each contract pays $1 at most
            state["checked"][w] = [len(held), round(sum(float(b["total"]) for b in held))]
            n[0] += 1
            if n[0] % 2000 == 0:
                save()
                print(f"  {n[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, todo))
    save()
    hold = sum(1 for v in state["checked"].values() if v[0])
    print(f"holders: {hold} of {len(state['checked'])} accounts hold an outcome position")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sample":
        sample()
    elif cmd == "portfolios":
        portfolios()
    elif cmd == "leaderboard":
        leaderboard()
    elif cmd == "holders":
        holders()
    elif cmd == "firsts":
        firsts()
    else:
        sys.exit(__doc__)
