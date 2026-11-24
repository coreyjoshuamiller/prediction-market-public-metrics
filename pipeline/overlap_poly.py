"""Polymarket: who trades both prediction markets and Perps.

Perps match off-chain, but collateral only enters through the Perps exchange contract on Polygon.
Each Deposit event names the Polymarket wallet that funded it (the same proxy wallet that trades
prediction markets) and the Perps account it credits, so deposits give the full list of Perps
accounts and the link back to prediction-market wallets. A user can hold more than one wallet, so
the account's other wallets are found on-chain by owner. What each account actually traded, and
when, comes from the public Perps API (`/v1/info/fills`), read as weekly activity.

Steps (each writes to data/overlap/ and can be re-run):
  python3 -m pipeline.overlap_poly deposits   # Dune: wallet <-> perps account, first deposit
  python3 -m pipeline.overlap_poly wallets    # Dune: every Polymarket wallet each account owns
  python3 -m pipeline.overlap_poly crawl      # Perps API: weekly fills per account (resumable, ~2h)
  python3 -m pipeline.overlap_poly queries    # Dune: overlap aggregates against prediction trades
"""
import csv
import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .dune import run_sql

ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(ROOT, "data", "overlap")
ACCOUNTS = os.path.join(OUT, "poly_perps_accounts.csv.gz")
ACTIVITY = os.path.join(OUT, "poly_perps_activity.json.gz")
WALLET_LINKS = os.path.join(OUT, "poly_wallet_links.json")

PERPS_API = "https://api.perpetuals.polymarket.com"
PERPS_CONTRACT = "0xDCa4af75705dbB50f62437045afF9921947917d2"  # /v1/info/exchange
DEPOSIT_TOPIC = "0x364bb76a44233df8584c690de6da7810626a5e77192f3ebc942c35bcb1add24f"
FIRST_DAY = "2026-05-01"  # the first deposit is 2026-05-05 (early access); public launch 2026-09-03
WEEK = 7 * 86400 * 1000


def _ms(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def week_of(ms):
    """Monday 00:00 UTC of the week holding `ms`, as a date string (matches Dune's date_trunc('week'))."""
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    monday = ms - (d.weekday() * 86400 + d.hour * 3600 + d.minute * 60 + d.second) * 1000 - ms % 1000
    return datetime.fromtimestamp(monday / 1000, timezone.utc).strftime("%Y-%m-%d")


# ---------- deposits (Dune) ----------

def deposits():
    """One row per (wallet, perps account). Dune bills result cells, so rows come back packed
    500 to a string (about 90 rows instead of 42,000)."""
    sql = f"""
WITH d AS (
  SELECT bytearray_substring(topic1, 13, 20) AS w, bytearray_substring(data, 45, 20) AS a,
    min(block_time) AS t0, count(*) AS n, sum(bytearray_to_uint256(bytearray_substring(data, 1, 32))) / 1e6 AS usd
  FROM polygon.logs
  WHERE block_date >= date '{FIRST_DAY}' AND contract_address = {PERPS_CONTRACT} AND topic0 = {DEPOSIT_TOPIC}
  GROUP BY 1, 2
), r AS (SELECT *, row_number() OVER (ORDER BY t0, a) AS rn FROM d)
SELECT (rn - 1) / 500 AS chunk,
  array_join(array_agg(concat(lower(to_hex(w)), ':', lower(to_hex(a)), ':', cast(cast(to_unixtime(t0) AS bigint) AS varchar),
    ':', cast(n AS varchar), ':', cast(cast(round(usd) AS bigint) AS varchar)) ORDER BY rn), ',') AS s
FROM r GROUP BY 1 ORDER BY 1"""
    rows = run_sql(sql, label="perps-deposits")
    os.makedirs(OUT, exist_ok=True)
    n = 0
    with gzip.open(ACCOUNTS, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wallet", "account", "first_deposit", "deposits", "usd"])
        for r in sorted(rows, key=lambda r: r["chunk"]):
            for item in r["s"].split(","):
                wal, acc, t0, cnt, usd = item.split(":")
                w.writerow(["0x" + wal, "0x" + acc, t0, cnt, usd])
                n += 1
    print(f"deposits: {n} wallet-account pairs -> {ACCOUNTS}")


def load_accounts():
    with gzip.open(ACCOUNTS, "rt") as f:
        return list(csv.DictReader(f))


# ---------- weekly activity (Perps API) ----------

class _Limiter:
    """Spaces requests evenly: the API allows 1,000 weighted tokens a minute per IP."""

    def __init__(self, per_sec):
        self.gap, self.next, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            at = max(now, self.next)
            self.next = at + self.gap
        if at > now:
            time.sleep(at - now)

    def backoff(self, secs):
        with self.lock:
            self.next = max(self.next, time.time() + secs)


LIMIT = _Limiter(15)


def _get(path):
    for attempt in range(8):
        LIMIT.wait()
        try:
            req = urllib.request.Request(PERPS_API + path, headers={"User-Agent": "Mozilla/5.0 (pm-metrics)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            if e.code == 429:
                LIMIT.backoff(float(e.headers.get("Retry-After") or 1) + 0.5)
                continue
            if e.code < 500:
                return {"error": body, "code": e.code}
        except Exception:
            pass
        time.sleep(1 + attempt)
    return {"error": "gave up", "code": 0}


def account_weeks(account, start_ms, now_ms):
    """Weekly fills for one account: {week: [trades, notional, complete, forced]}.

    `trades` and `notional` leave out liquidations, auto-deleveraging and settlement fills (`forced`),
    which close a position without the trader placing an order; Polymarket's leaderboard notional
    leaves them out too. Fills come 100 to a page, oldest first. A page that ends mid-week marks that
    week as not fully counted, and the next request starts at the following Monday, so a busy account
    costs at most one request per active week instead of thousands of pages."""
    weeks, start, first, last, err = {}, start_ms, None, None, None
    while start < now_ms:
        j = _get(f"/v1/info/fills?address={account}&sort=asc&start_timestamp={start}")
        if "data" not in j:
            err = j.get("error")
            break
        data = j["data"]
        for f in data:
            w = weeks.setdefault(week_of(f["timestamp"]), [0, 0.0, 1, 0])
            if f.get("liquidation") or f.get("adl") or f.get("settlement"):
                w[3] += 1
                continue
            w[0] += 1
            w[1] += float(f["price"]) * float(f["quantity"])
            first = first or f["timestamp"]
            last = f["timestamp"]
        if not j.get("more") or not data:
            break
        wk = week_of(data[-1]["timestamp"])
        weeks[wk][2] = 0
        start = _ms(wk) + WEEK
    out = {"w": {k: [v[0], round(v[1], 2), v[2], v[3]] for k, v in weeks.items()}, "first": first, "last": last}
    if err:
        out["err"] = err
    if any(not v[2] for v in weeks.values()):
        # busy account: take its exact all-time notional and PnL from the leaderboard row
        j = _get(f"/v1/info/leaderboard?window=all&sort_by=notional&limit=1&address={account}")
        a = j.get("account") or {}
        if a:
            out["lb"] = [round(float(a.get("notional") or 0), 2), round(float(a.get("pnl") or 0), 2)]
    return out


def traded_weeks(v):
    """Weeks in which the account placed at least one trade of its own."""
    return [wk for wk, w in v.get("w", {}).items() if w[0] > 0]


def _load_activity():
    if os.path.exists(ACTIVITY):
        with gzip.open(ACTIVITY, "rt") as f:
            return json.load(f)
    return {"asof": None, "accounts": {}}


def _save_activity(state):
    tmp = ACTIVITY + ".tmp"
    with gzip.open(tmp, "wt") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, ACTIVITY)


def crawl(limit=None, threads=16):
    state = _load_activity()
    done = state["accounts"]
    accounts = sorted({r["account"] for r in load_accounts()})
    todo = [a for a in accounts if a not in done or "err" in done[a]]
    if limit:
        todo = todo[:limit]
    now_ms = int(time.time() * 1000)
    start_ms = _ms(FIRST_DAY)
    print(f"crawl: {len(todo)} of {len(accounts)} accounts to read")
    t0, n, lock = time.time(), 0, threading.Lock()

    def work(a):
        nonlocal n
        res = account_weeks(a, start_ms, now_ms)
        with lock:
            done[a] = res
            n += 1
            if n % 2000 == 0:
                state["asof"] = now_ms
                _save_activity(state)
                rate = n / (time.time() - t0)
                print(f"  {n}/{len(todo)}  {rate:.1f} accounts/s  eta {(len(todo) - n) / rate / 60:.0f} min", flush=True)

    with ThreadPoolExecutor(threads) as ex:
        list(ex.map(work, todo))
    state["asof"] = now_ms
    _save_activity(state)
    traded = sum(1 for v in done.values() if traded_weeks(v))
    print(f"crawl: done in {(time.time() - t0) / 60:.0f} min; {traded} of {len(done)} accounts have traded")


# ---------- every wallet of each account (Dune) ----------

DEPOSIT_WALLET_FACTORY = "0x00000000000fb5c9adea0298d729a0cb3823cc07"
WALLET_CREATED_TOPIC = "0x7441de0ad639fe5d2bf1c22447715a0528b682385736bb40ae8dd92555eb8276"  # (wallet, owner, owner)
PROXY_FACTORY = ("0xaB45c5A4B0c941a2F231C04C3f49182e1A254052", "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b")
EPOCH = "2020-01-01"
KEY_BYTES = 6  # wallets are matched on their first 6 bytes, see _cte


def _key(addr):
    import base64
    return base64.b64encode(bytes.fromhex(addr[2:])[:KEY_BYTES]).decode()


def magic_proxy(owner):
    """The email-login proxy wallet for an owner: CREATE2 from Polymarket's proxy factory, salt = keccak(owner)."""
    from .predict_api import keccak256
    factory, init_hash = PROXY_FACTORY
    salt = keccak256(bytes.fromhex(owner[2:]))
    return "0x" + keccak256(b"\xff" + bytes.fromhex(factory[2:]) + salt + bytes.fromhex(init_hash[2:]))[12:].hex()


def wallets():
    """A Perps account is the user's signer address, and one user can hold several Polymarket wallets:
    a deposit wallet (the kind created since May 2026), a Safe (browser-wallet users) or a proxy
    (email users). The wallet that funded Perps isn't always the one that trades prediction markets,
    so this finds them all on-chain: deposit wallets and Safes by owner, proxies by deriving the
    address and checking it exists. Saves {account id: [[wallet key, kind, day created], ...]}."""
    ids = account_ids()
    order = sorted(ids, key=ids.get)
    owners = ",".join(_key(a) for a in order)
    rows = run_sql(f"""
WITH o AS (SELECT ord - 1 AS aid, from_base64(x) AS k FROM UNNEST(split('{owners}', ',')) WITH ORDINALITY AS t(x, ord)),
w AS (
  SELECT bytearray_substring(topic2, 13, {KEY_BYTES}) AS ok, bytearray_substring(topic1, 13, {KEY_BYTES}) AS wk, block_time, 'd' AS kind
  FROM polygon.logs
  WHERE block_date >= date '{FIRST_DAY}' AND contract_address = {DEPOSIT_WALLET_FACTORY} AND topic0 = {WALLET_CREATED_TOPIC}
  UNION ALL
  SELECT bytearray_substring(owner, 1, {KEY_BYTES}), bytearray_substring(proxy, 1, {KEY_BYTES}), block_time, 's'
  FROM polymarket_polygon.users_safe_proxies
),
j AS (SELECT o.aid, w.wk, w.kind, min(w.block_time) AS t FROM o JOIN w ON w.ok = o.k GROUP BY 1, 2, 3)
SELECT aid / 1000 AS chunk, array_join(array_agg(concat(cast(aid AS varchar), ':', to_base64(wk), ':', kind, ':',
  cast(date_diff('day', date '{EPOCH}', date(t)) AS varchar)) ORDER BY aid), ',') AS s
FROM j GROUP BY 1 ORDER BY 1""", label="perps-owner-wallets", timeout=3600)
    out = {}
    for r in rows:
        for item in r["s"].split(","):
            aid, key, kind, day = item.split(":")
            out.setdefault(aid, []).append([key, kind, int(day)])
    derived = [magic_proxy(a) for a in order]
    rows = run_sql(f"""
WITH o AS (SELECT ord - 1 AS aid, from_base64(x) AS k FROM UNNEST(split('{",".join(_key(w) for w in derived)}', ',')) WITH ORDINALITY AS t(x, ord)),
m AS (SELECT bytearray_substring(proxy, 1, {KEY_BYTES}) AS k, min(block_time) AS t FROM polymarket_polygon.users_magic_wallet_proxies GROUP BY 1)
SELECT o.aid / 1000 AS chunk, array_join(array_agg(concat(cast(o.aid AS varchar), ':',
  cast(date_diff('day', date '{EPOCH}', date(m.t)) AS varchar)) ORDER BY o.aid), ',') AS s
FROM o JOIN m ON m.k = o.k GROUP BY 1 ORDER BY 1""", label="perps-proxy-wallets", timeout=3600)
    for r in rows:
        for item in r["s"].split(","):
            aid, day = item.split(":")
            out.setdefault(aid, []).append([_key(derived[int(aid)]), "p", int(day)])
    with open(WALLET_LINKS, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    kinds = {"d": 0, "s": 0, "p": 0}
    for v in out.values():
        for _, kind, _ in v:
            kinds[kind] += 1
    print(f"wallets: {len(out)} of {len(order)} accounts own a wallet on-chain "
          f"({kinds['d']} deposit wallets, {kinds['s']} Safes, {kinds['p']} proxies)")


# ---------- overlap queries (Dune) ----------

ROUTER_MIN_ACCOUNTS = 10  # a "wallet" that funded more accounts than this is a router or desk, not one user's wallet
PRED_FROM = "2026-04-27"   # Monday before the first Perps deposit; weekly series start here
LAUNCH = "2026-09-03"      # public launch of Polymarket Perps
TRADES = "polymarket_polygon.market_trades_raw"  # one row per trader's side of a fill; `maker` is that trader


def account_ids():
    """Stable small ids, so per-account results can come back from Dune without the 42-character address."""
    return {a: i for i, a in enumerate(sorted({r["account"] for r in load_accounts()}))}


def links():
    """{account: set(wallet keys)}: the wallets that funded it (minus routers) and the wallets it owns."""
    rows = load_accounts()
    funded = {}
    for r in rows:
        funded.setdefault(r["wallet"], set()).add(r["account"])
    routers = {w for w, accts in funded.items() if len(accts) > ROUTER_MIN_ACCOUNTS}
    out = {}
    for r in rows:
        out.setdefault(r["account"], set())
        if r["wallet"] not in routers:
            out[r["account"]].add(_key(r["wallet"]))
    if os.path.exists(WALLET_LINKS):
        owned = json.load(open(WALLET_LINKS))
        for a, i in account_ids().items():
            out[a].update(k for k, _, _ in owned.get(str(i), []))
    return out, routers


def joined_days():
    """{account: day (since EPOCH) its first Polymarket wallet was created}, where a wallet was found."""
    owned = json.load(open(WALLET_LINKS)) if os.path.exists(WALLET_LINKS) else {}
    return {a: min(d for _, _, d in owned[str(i)]) for a, i in account_ids().items() if str(i) in owned}


def exact_notional(v):
    """All-time notional: the leaderboard figure for accounts too busy to read in full, else the sum of fills."""
    return v["lb"][0] if "lb" in v else sum(w[1] for w in v.get("w", {}).values())


def week_index(wk):
    return (_ms(wk) - _ms(PRED_FROM)) // WEEK


def perps_mask(v):
    """Bit i is set when the account traded Perps in week PRED_FROM + i."""
    m = 0
    for wk in traded_weeks(v):
        if week_index(wk) >= 0:
            m |= 1 << week_index(wk)
    return m


def _b36(n):
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + out
    return out


def _cte(masks=True):
    """The account list, written into the query itself.

    This Dune plan can't upload private tables and a query can't exceed about 1 MB, so each account
    is one list entry by position (its id): the first 6 bytes of each linked wallet (base64), and its
    Perps weeks as a base-36 bitmask. Six bytes are plenty to tell wallets apart: about one chance in
    a thousand of a single false match across every wallet that has ever traded on Polymarket.
    -> links (aid, k), and with masks: lw (k, pmask), one row per wallet key."""
    ids = account_ids()
    lk, _ = links()
    acts = _load_activity()["accounts"]
    order = sorted(ids, key=ids.get)
    wal = ",".join("|".join(sorted(lk.get(a, ()))) for a in order)
    sql = f"""
l0 AS (SELECT ord - 1 AS aid, ws FROM UNNEST(split('{wal}', ',')) WITH ORDINALITY AS t(ws, ord) WHERE ws <> ''),
links AS (SELECT DISTINCT l0.aid, from_base64(x) AS k FROM l0 CROSS JOIN UNNEST(split(l0.ws, '|')) AS u(x))"""
    if masks:
        msk = ",".join(_b36(perps_mask(acts.get(a, {}))) for a in order)
        sql += f""",
a AS (SELECT ord - 1 AS aid, CASE WHEN m = '' THEN BIGINT '0' ELSE from_base(m, 36) END AS pmask
      FROM UNNEST(split('{msk}', ',')) WITH ORDINALITY AS t(m, ord)),
lw AS (SELECT l.k, bitwise_or_agg(a.pmask) AS pmask FROM links l JOIN a ON a.aid = l.aid GROUP BY 1)"""
    return sql


KEY = f"bytearray_substring(maker, 1, {KEY_BYTES})"


def q_weekly(start=PRED_FROM):
    """Per week: every prediction-market wallet and its dollars traded, and the part that also trades
    Perps: that same week (`perps_same`), or at any time up to and including that week (`perps_ever`)."""
    return f"""
WITH {_cte()},
t AS (
  SELECT date_trunc('week', block_time) AS wk, maker, sum(amount) AS vol
  FROM {TRADES} WHERE block_time >= timestamp '{start}'
  GROUP BY 1, 2
),
t2 AS (SELECT date(wk) AS wk, date_diff('week', timestamp '{PRED_FROM}', wk) AS wi, {KEY} AS k, vol FROM t)
SELECT t2.wk, count(*) AS traders, sum(t2.vol) AS vol,
  count(lw.k) AS linked, coalesce(sum(t2.vol) FILTER (WHERE lw.k IS NOT NULL), 0) AS linked_vol,
  count_if(bitwise_and(lw.pmask, bitwise_left_shift(BIGINT '2', wi) - 1) <> 0) AS perps_ever,
  coalesce(sum(t2.vol) FILTER (WHERE bitwise_and(lw.pmask, bitwise_left_shift(BIGINT '2', wi) - 1) <> 0), 0) AS perps_ever_vol,
  count_if(bitwise_and(lw.pmask, bitwise_left_shift(BIGINT '1', wi)) <> 0) AS perps_same,
  coalesce(sum(t2.vol) FILTER (WHERE bitwise_and(lw.pmask, bitwise_left_shift(BIGINT '1', wi)) <> 0), 0) AS perps_same_vol
FROM t2 LEFT JOIN lw ON lw.k = t2.k
GROUP BY 1 ORDER BY 1"""


def q_accounts(start=None):
    """Per Perps account, its prediction-market history across all linked wallets: first trade day,
    lifetime dollars, dollars since the Perps launch, and a bitmask of active weeks since PRED_FROM.
    Packed 1,000 accounts to a row to keep the billed result small."""
    where = f"WHERE tr.block_time >= timestamp '{start}'" if start else ""
    return f"""
WITH {_cte(masks=False)},
s AS (
  SELECT l.aid,
    date_diff('day', date '{EPOCH}', min(date(tr.block_time))) AS d0,
    cast(round(sum(tr.amount)) AS bigint) AS vol,
    cast(round(coalesce(sum(tr.amount) FILTER (WHERE tr.block_time >= timestamp '{LAUNCH}'), 0)) AS bigint) AS vol_launch,
    coalesce(sum(DISTINCT CASE WHEN tr.block_time >= timestamp '{PRED_FROM}'
      THEN cast(power(2, date_diff('week', timestamp '{PRED_FROM}', tr.block_time)) AS bigint) END), 0) AS mask
  FROM {TRADES} tr JOIN links l ON bytearray_substring(tr.maker, 1, {KEY_BYTES}) = l.k
  {where}
  GROUP BY 1
)
SELECT aid / 1000 AS chunk,
  array_join(array_agg(concat(cast(aid AS varchar), '.', cast(d0 AS varchar), '.', cast(vol AS varchar), '.',
    cast(vol_launch AS varchar), '.', cast(mask AS varchar)) ORDER BY aid), ',') AS s
FROM s GROUP BY 1 ORDER BY 1"""


def q_bands(start=LAUNCH):
    """Prediction-market wallets active since the Perps launch, by dollars traded (order of magnitude),
    and how many of each size have traded Perps."""
    return f"""
WITH {_cte()},
t AS (SELECT maker, sum(amount) AS vol FROM {TRADES} WHERE block_time >= timestamp '{start}' GROUP BY 1)
SELECT cast(floor(log10(greatest(t.vol, 1))) AS integer) AS band, count(*) AS wallets, sum(t.vol) AS vol,
  count(lw.k) AS linked, count_if(lw.pmask <> 0) AS perps, coalesce(sum(t.vol) FILTER (WHERE lw.pmask <> 0), 0) AS perps_vol
FROM t LEFT JOIN lw ON lw.k = bytearray_substring(t.maker, 1, {KEY_BYTES})
GROUP BY 1 ORDER BY 1"""


def q_categories(start=LAUNCH):
    """What Perps traders trade on the prediction side, against everyone, since the Perps launch."""
    from .rewards import poly_category_sql
    return f"""
WITH {_cte()},
md AS (SELECT from_hex(substr(condition_id, 3)) AS condition_id, arbitrary(tags) AS t FROM polymarket.markets_raw GROUP BY 1),
cat AS (SELECT condition_id, CASE WHEN contains(t, 'Up or Down') THEN 'Up/Down (short-term)' ELSE {poly_category_sql()} END AS category FROM md),
f AS (
  SELECT coalesce(c.category, 'Other') AS category, tr.maker, sum(tr.amount) AS vol
  FROM {TRADES} tr LEFT JOIN cat c ON c.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}'
  GROUP BY 1, 2
)
SELECT f.category, sum(f.vol) AS vol, count(*) AS traders,
  coalesce(sum(f.vol) FILTER (WHERE lw.pmask <> 0), 0) AS perps_vol, count_if(lw.pmask <> 0) AS perps_traders
FROM f LEFT JOIN lw ON lw.k = bytearray_substring(f.maker, 1, {KEY_BYTES})
GROUP BY 1 ORDER BY 2 DESC"""


def queries(test_from=None, only=None):
    """Runs the four aggregates and saves them under data/overlap/. `test_from` narrows every scan
    (for checking the SQL cheaply) and writes to *_test files."""
    sfx = "_test" if test_from else ""
    jobs = [("weekly", lambda: q_weekly(test_from or PRED_FROM)), ("bands", lambda: q_bands(test_from or LAUNCH)),
            ("categories", lambda: q_categories(test_from or LAUNCH)), ("accounts", lambda: q_accounts(test_from))]
    for name, make in jobs:
        if only and name not in only:
            continue
        sql = make()
        print(f"  {name}: query is {len(sql) / 1e6:.2f} MB")
        rows = run_sql(sql, label=f"overlap-{name}", timeout=3600)
        if name == "accounts":
            out = {}
            for r in rows:
                for item in r["s"].split(","):
                    aid, d0, vol, vl, mask = item.split(".")
                    out[aid] = [int(d0), int(vol), int(vl), int(mask)]
            rows = out
        with open(os.path.join(OUT, f"poly_{name}{sfx}.json"), "w") as f:
            json.dump(rows, f, separators=(",", ":"), default=str)
        print(f"  saved poly_{name}{sfx}.json ({len(rows)} rows)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "deposits":
        deposits()
    elif cmd == "wallets":
        wallets()
    elif cmd == "queries":  # queries [test-from-date] [name,name]
        queries(test_from=(sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None),
                only=sys.argv[3].split(",") if len(sys.argv) > 3 else None)
    elif cmd == "crawl":
        crawl(limit=int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        sys.exit(__doc__)
