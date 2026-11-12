"""Hyperliquid HIP-4 outcome markets: collect volume for its short-term price markets.

Hyperliquid's public API has no volume history for outcome markets: once a market settles its trades
and candles are gone, and Dune doesn't carry them. But one call (spotMetaAndAssetCtxs) returns the
rolling 24-hour volume of every live outcome side ("#<10 x outcome + side>"), so polling it every few
seconds and adding up the increases gives each market's volume as it trades. The Yes and No sides
print the same trades (No at 1 - the Yes price), so only the Yes side is read: contracts are its
shares and dollars are valued at the Yes price. This runs alongside the order-book sampler in the
hourly Action:

  python -m pipeline.hyperliquid --duration 3240 --interval 10

A market's rolling volume only stops counting trades 24 hours after they happen, so the increases are
exact for the short-term markets kept here (they settle within a day of listing). Gaps between runs
are caught up on the next poll, except for markets that list and settle entirely inside a gap. Trades
in a market's last seconds before it settles (up to one poll interval) are missed.

Platform totals (every outcome market, mostly sports) come from DefiLlama's Hyperliquid indexer.
"""
import argparse
import csv
import gzip
import json
import os
import queue
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from .markets import ASSETS
from .net import get_json, post_json
from .ws import WebSocket

INFO = "https://api.hyperliquid.xyz/info"
DATA = os.path.join(os.path.dirname(__file__), "..", "data")
STATE = os.path.join(DATA, "hyperliquid", "state.json")
VOLUME = os.path.join(DATA, "hyperliquid_volume_daily.csv")
TOTALS = os.path.join(DATA, "hyperliquid_totals_daily.csv")
WALLETS = os.path.join(DATA, "hyperliquid", "wallets")
FIELDS = ["d", "asset", "asset_class", "dur", "ctype", "notional", "cash", "fees"]
WS_HOST = "api.hyperliquid.xyz"
# Estimated outcome fees (charged only when closing or settling a position), from observed fills in
# Nov 2026: taker sells mostly paid 0.068% of value, maker sells 0.055-0.06%, settlement 0.134% of
# the $1 payout. Rates fall with a wallet's volume tier and staking, which the public feed doesn't show.
FEE_TAKER, FEE_MAKER, FEE_SETTLE = 0.0007, 0.0006, 0.0014
LLAMA = "https://api.llama.fi/summary/dexs/hyperliquid-outcomes"
LLAMA_FEES = "https://api.llama.fi/summary/fees/hyperliquid-outcomes"

# a market's life (listing to expiry) is binned up to the dashboard's durations; longer ones are out of
# scope. Deployers list a few minutes before the window opens (a 15-minute market can be listed 16-17
# minutes before it settles), so each bin allows some slack.
DUR_BINS = [(8 * 60, "5m"), (20 * 60, "15m"), (70 * 60, "1h"), (4 * 3600 + 20 * 60, "4h"), (24 * 3600 + 30 * 60, "1d")]
PERIODS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
PRICE_TEMPLATES = {"template:binaryPrice": "Strike", "template:priceTouch": "Strike"}
# perps that aren't in markets.ASSETS under their own ticker
PERP_ASSETS = {
    "sp500": ASSETS["spx"], "xyz100": ("XYZ100", "Equities"), "skhx": ASSETS["skhy"], "sndk": ("SNDK", "Equities"),
    "ant": ("Anthropic", "Equities"),
}


def info(body):
    return post_json(INFO, body, timeout=30)


def _fields(desc):
    return dict(x.split(":", 1) for x in (desc or "").split("|") if ":" in x)


def _ts(s):
    """'20261110-1826' (UTC) -> epoch seconds."""
    return datetime.strptime(s, "%Y%m%d-%H%M").replace(tzinfo=timezone.utc).timestamp()


def _asset(perp):
    tok = perp.split(":")[-1].lower()
    return PERP_ASSETS.get(tok) or ASSETS.get(tok) or (tok.upper(), "Other")


def _bin(seconds):
    return next((label for limit, label in DUR_BINS if seconds <= limit), None)


def classify(outcome, question, first_seen):
    """-> [asset, asset_class, dur, ctype] for a short-term price market, else None. first_seen is
    None for markets already listed when collection began: their life can't be known."""
    name, f = outcome["name"], _fields(outcome["description"])
    if name == "Recurring" and f.get("class") == "priceBinary":
        dur, ctype, perp = PERIODS.get(f.get("period")), "Strike", f.get("underlying")
    elif name.startswith("Recurring") and question:  # one bucket of a price-range question
        q = _fields(question["description"])
        if q.get("class") != "priceBucket":
            return None
        dur, ctype, perp = PERIODS.get(q.get("period")), "Range", q.get("underlying")
    elif name in PRICE_TEMPLATES and f.get("time") and f.get("perp") and first_seen:
        dur, ctype, perp = _bin(_ts(f["time"]) - first_seen), PRICE_TEMPLATES[name], f["perp"]
    else:
        return None
    if not dur or not perp:
        return None
    return [*_asset(perp), dur, ctype]


def _load_state():
    if os.path.exists(STATE):
        st = json.load(open(STATE))
    else:
        st = {"coins": {}, "outcomes": {}, "started": time.time()}
    st.setdefault("pos", {})  # outcome -> wallet -> [net Yes shares (negative = No), cash, est. fees]
    st.setdefault("fed", {})  # coin -> [time of the last trade taken from the feed, its trade ids]
    return st


def _day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


class TradeFeed(threading.Thread):
    """Hyperliquid's public trades websocket for every live Yes coin in `want`. Each fill (with
    both wallets) goes on `q`; a subscription first replays the market's recent trades, which
    covers the minutes between runs. Reconnects on any error."""

    def __init__(self):
        super().__init__(daemon=True)
        self.want, self.q, self.stop = set(), queue.Queue(), threading.Event()
        self.errors = 0

    def run(self):
        while not self.stop.is_set():
            ws, subbed = None, set()
            try:
                ws = WebSocket(WS_HOST)
                pinged = time.time()
                while not self.stop.is_set():
                    for coin in list(self.want - subbed):
                        ws.send_json({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}})
                        subbed.add(coin)
                    if time.time() - pinged > 30:  # the server drops connections that are quiet for a minute
                        ws.send_json({"method": "ping"})
                        pinged = time.time()
                    m = ws.recv_json(timeout=2)
                    if m and m.get("channel") == "trades":
                        # oldest first: the replay on subscribe isn't in time order, and booking skips anything older than the last fill
                        for t in sorted(m["data"], key=lambda t: (t["time"], t["tid"])):
                            self.q.put(t)
            except Exception as e:
                self.errors += 1
                print(f"  hyperliquid trade feed: {e}; reconnecting")
                time.sleep(2)
            finally:
                if ws:
                    ws.close()


class Wallets:
    """Per UTC day: each wallet's activity per market key, saved to data/hyperliquid/wallets/<day>.json.gz
    as {wallet: {"asset|class|dur|ctype": [dollars, contracts, maker dollars, fills, est. fees, PnL],
    "_m": [outcome ids traded]}}."""

    def __init__(self):
        self.add = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0.0] * 6)))
        self.markets = defaultdict(lambda: defaultdict(set))
        self.fees = defaultdict(float)  # (day, *key) -> estimated fees, for the volume CSV

    def record(self, day, wallet, key, i, v):
        self.add[day][wallet]["|".join(key)][i] += v

    def save(self):
        os.makedirs(WALLETS, exist_ok=True)
        for day in set(self.add) | set(self.markets):
            path = os.path.join(WALLETS, f"{day}.json.gz")
            data = json.load(gzip.open(path, "rt")) if os.path.exists(path) else {}
            for wallet, keys in self.add.get(day, {}).items():
                w = data.setdefault(wallet, {})
                for k, vals in keys.items():
                    cur = w.setdefault(k, [0] * 6)
                    w[k] = [round(a + b, 4) for a, b in zip(cur, vals)]
            for wallet, oids in self.markets.get(day, {}).items():
                w = data.setdefault(wallet, {})
                w["_m"] = sorted(set(w.get("_m", [])) | oids)
            with gzip.open(path, "wt") as fh:
                json.dump(data, fh, separators=(",", ":"))
        self.add.clear()
        self.markets.clear()


def _fill(t, state, outcomes, wallets, now):
    """Book one Yes-coin fill: users = [buyer, seller] of Yes. A seller with no Yes is buying No,
    so positions are net Yes shares (negative = No), and a fill that shrinks a position closes it."""
    coin, oid = t["coin"], str(int(t["coin"][1:]) // 10)
    o = outcomes.get(oid)
    if not o or not o.get("key"):
        return False
    ts = t["time"] / 1000
    fed = state["fed"].setdefault(coin, [0, []])
    if ts < fed[0] or (ts == fed[0] and t["tid"] in fed[1]):
        return False  # already booked (the feed replays recent trades on subscribe)
    fed[:] = [ts, fed[1] + [t["tid"]]] if ts == fed[0] else [ts, [t["tid"]]]
    px, sz = float(t["px"]), float(t["sz"])
    day, key = _day(ts), o["key"]
    tracked = o.get("tracked")  # the feed has seen this market since it listed: positions are complete
    pos = state["pos"].setdefault(oid, {})
    o["fed_sz"] = o.get("fed_sz", 0) + sz
    rate = {True: FEE_TAKER, False: FEE_MAKER}
    for i, (user, d) in enumerate(zip(t["users"], (1, -1))):
        p = pos.setdefault(user, [0.0, 0.0, 0.0])
        taker = (t["side"] == "B") == (d == 1)
        closing = min(sz, abs(p[0])) if p[0] * d < 0 else 0.0
        # dollars on this wallet's own side: closing No / opening Yes at the Yes price's complement or itself
        if d == 1:
            dollars = closing * (1 - px) + (sz - closing) * px
            close_value = closing * (1 - px)
        else:
            dollars = closing * px + (sz - closing) * (1 - px)
            close_value = closing * px
        if not tracked:  # position unknown: value both sides at the Yes price, no fee estimate
            dollars, close_value = sz * px, 0.0
        fee = close_value * rate[taker] * o.get("fs", 1.0)
        p[0] += d * sz
        p[1] -= d * px * sz
        p[2] += fee
        wallets.record(day, user, key, 0, dollars)
        wallets.record(day, user, key, 1, sz)
        if not taker:
            wallets.record(day, user, key, 2, dollars)
        wallets.record(day, user, key, 3, 1)
        wallets.record(day, user, key, 4, fee)
        wallets.markets[day][user].add(int(oid))
        if fee:
            wallets.fees[(day, *key)] += fee
    return True


def _settle(oid, o, state, wallets, now):
    """A settled market: winning shares pay the settlement fee, and each wallet's PnL (cash flows +
    payout - estimated fees) is booked on the settlement day. Returns False to retry later."""
    try:
        r = info({"type": "settledOutcome", "outcome": int(oid)})
        yes = float(r["settleFraction"])
    except Exception:
        o["settle_tries"] = o.get("settle_tries", 0) + 1
        if o["settle_tries"] >= 30:  # no result after ~5 minutes of polls: give up on its PnL
            state["pos"].pop(oid, None)
        return False
    pos = state["pos"].pop(oid, {})
    day, key = _day(now), o["key"]
    # PnL needs every fill: skip markets the feed joined late or covered incompletely
    complete = o.get("tracked") and o.get("fed_sz", 0) >= 0.98 * o.get("ctx_sz", 0)
    for user, (shares, cash, fees) in pos.items():
        payout = shares * yes if shares > 0 else -shares * (1 - yes)  # winning shares are paid $1 each
        fee = payout * FEE_SETTLE * o.get("fs", 1.0) if o.get("tracked") else 0.0
        if fee:
            wallets.record(day, user, key, 4, fee)
            wallets.fees[(day, *key)] += fee
        if complete:
            # net Yes shares x the Yes payout settles both sides (No holders were short Yes)
            wallets.record(day, user, key, 5, cash + shares * yes - fees - fee)
    o["settled"] = yes
    return True


def _save(state, added, fees=None):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(state, fh, separators=(",", ":"))
    if not added and not fees:
        return
    rows = {}
    if os.path.exists(VOLUME):
        for r in csv.DictReader(open(VOLUME)):
            rows[tuple(r[k] for k in FIELDS[:5])] = [float(r["notional"]), float(r["cash"]), float(r.get("fees") or 0)]
    for k, (n, c) in added.items():
        cur = rows.setdefault(k, [0.0, 0.0, 0.0])
        cur[0] += n
        cur[1] += c
    for k, f in (fees or {}).items():
        rows.setdefault(k, [0.0, 0.0, 0.0])[2] += f
    with open(VOLUME, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        for k in sorted(rows):
            w.writerow([*k, *(round(x, 2) for x in rows[k])])
    added.clear()
    if fees:
        fees.clear()


def collect(duration, interval, meta_every=60, save_every=300):
    state = _load_state()
    coins, outcomes = state["coins"], state["outcomes"]
    # with no saved state, the first poll only records where each market's volume stands
    bootstrap = not coins
    added = defaultdict(lambda: [0.0, 0.0])  # (d, asset, class, dur, ctype) -> [contracts, dollars]
    end = time.time() + duration
    meta_at = saved_at = 0
    by_id, question_of = {}, {}
    polls = errors = fills = settled = 0
    feed, wallets = TradeFeed(), Wallets()
    # markets listed after this point are seen by the trade feed from their first fill
    state.setdefault("feed_started", time.time())
    feed.start()
    while True:
        started = time.time()
        try:
            if started - meta_at >= meta_every:
                m = info({"type": "outcomeMeta"})
                by_id = {o["outcome"]: o for o in m["outcomes"]}
                question_of = {o: q for q in m["questions"] for o in q["namedOutcomes"] + [q["fallbackOutcome"]]}
                meta_at = started
            ctxs = [c for c in info({"type": "spotMetaAndAssetCtxs"})[1] if c["coin"].startswith("#") and c["coin"].endswith("0")]
            now = time.time()
            day = _day(now)
            live = set()
            for c in ctxs:
                coin, oid = c["coin"], str(int(c["coin"][1:]) // 10)
                live.add(oid)
                o = outcomes.get(oid)
                if o is None or (o["key"] is None and int(oid) in by_id and not o.get("classified")):
                    first_seen = o["first_seen"] if o else (None if bootstrap else now)
                    spec = by_id.get(int(oid))
                    key = classify(spec, question_of.get(int(oid)), first_seen) if spec else None
                    o = outcomes[oid] = {"first_seen": first_seen, "key": key, "classified": bool(spec),
                                         "tracked": bool(first_seen and first_seen >= state["feed_started"]),
                                         "fs": float((spec or {}).get("deployerFeeScale") or 1.0)}
                o["last_seen"] = now
                if o["key"]:
                    feed.want.add(coin)
                v, b = float(c["dayNtlVlm"]), float(c["dayBaseVlm"])
                prev = coins.get(coin)
                dv, db = (v - prev["v"], b - prev["b"]) if prev else (v, b)
                coins[coin] = {"v": v, "b": b, "t": now}
                if o["key"] and not bootstrap and (dv > 0 or db > 0):
                    a = added[(day, *o["key"])]
                    a[0] += max(db, 0)
                    a[1] += max(dv, 0)
                    o["ctx_sz"] = o.get("ctx_sz", 0) + max(db, 0)
            polls += 1
            bootstrap = False
            while not feed.q.empty():
                fills += _fill(feed.q.get(), state, outcomes, wallets, now)
            # a market that has left the live list for 30s has settled
            for oid in [k for k in state["pos"] if k not in live]:
                o = outcomes.get(oid)
                if not o:
                    state["pos"].pop(oid)
                elif now - o["last_seen"] > 30 and _settle(oid, o, state, wallets, now):
                    settled += 1
        except Exception as e:  # keep going: a missed poll is caught up by the next one
            errors += 1
            print(f"  hyperliquid poll failed: {e}")
        if time.time() - saved_at >= save_every or time.time() >= end:
            cutoff = time.time() - 2 * 3600
            for k in [k for k, x in coins.items() if x["t"] < cutoff]:
                del coins[k]
                state["fed"].pop(k, None)
            for k in [k for k, x in outcomes.items() if x.get("last_seen", 0) < time.time() - 3 * 86400]:
                del outcomes[k]
                state["pos"].pop(k, None)
            wallets.save()
            _save(state, added, wallets.fees)
            saved_at = time.time()
        if time.time() >= end:
            break
        time.sleep(max(0, interval - (time.time() - started)))
    feed.stop.set()
    print(f"hyperliquid: {polls} polls, {errors} errors, {len(coins)} live sides, {fills} fills, "
          f"{settled} settled, {feed.errors} feed reconnects")


def platform_totals():
    """Every HIP-4 outcome market per UTC day, from DefiLlama (dollars; contracts at $1 each)."""
    def series(kind):
        d = get_json(LLAMA, {"excludeTotalDataChartBreakdown": "true", "dataType": kind}, timeout=60)
        return {datetime.fromtimestamp(t, timezone.utc).date().isoformat(): v for t, v in d.get("totalDataChart") or []}
    cash, notional = series("dailyVolume"), series("dailyNotionalVolume")
    try:  # fees users paid on closing or settling, including front-ends' builder fees
        d = get_json(LLAMA_FEES, {"excludeTotalDataChartBreakdown": "true", "dataType": "dailyFees"}, timeout=60)
        fees = {datetime.fromtimestamp(t, timezone.utc).date().isoformat(): v for t, v in d.get("totalDataChart") or []}
    except Exception as e:
        print(f"  hyperliquid platform fees failed: {e}")
        fees = {}
    with open(TOTALS, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["d", "notional", "cash", "fees"])
        for d in sorted(cash):
            w.writerow([d, round(notional.get(d, 0), 2), round(cash[d], 2), round(fees.get(d, 0), 2)])
    print(f"  {os.path.basename(TOTALS)}: {len(cash)} days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=0, help="seconds to keep polling (0 = one poll)")
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()
    collect(args.duration, args.interval)
