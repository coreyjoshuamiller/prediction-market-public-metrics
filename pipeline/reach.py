"""Opt-in reachability for leaderboard wallets.

Only uses channels a trader has chosen to publish or enable:
  - XMTP: whether the account's signing wallet has an XMTP inbox (wallet-to-wallet messaging;
    you can message the address without knowing who owns it)
  - ENS / Basenames / Farcaster / Lens profiles the signing wallet has set up (via web3.bio)

The "signing wallet" is the account's own key: the owner of a Gnosis Safe account (read with
getOwners on Polygon), or the address itself when it trades directly. Polymarket email/Magic
accounts use a custodied key that can't hold these, so they're marked as such and skipped.
No funding-flow tracing, clustering or off-chain identity lookups.
"""
import json
import os
import subprocess
import time
from datetime import date, timedelta

from .net import get_json, post_json

ROOT = os.path.join(os.path.dirname(__file__), "..")
CACHE = os.path.join(ROOT, "data", "poly_reach.json")
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
XMTP_TOOL = os.path.join(ROOT, "tools", "xmtp")
TTL_DAYS = 7
GET_OWNERS = "0xa0e67e2b"
PROFILE_URL = {
    "ens": "https://app.ens.domains/{}",
    "basenames": "https://www.base.org/name/{}",
    "farcaster": "https://farcaster.xyz/{}",
    "lens": "https://hey.xyz/u/{}",
}


def _rpc(method, params):
    r = post_json(POLYGON_RPC, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return r.get("result"), r.get("error")


def account_signer(addr):
    """-> (account_type, signer or None)"""
    code, _ = _rpc("eth_getCode", [addr, "latest"])
    if not code or code == "0x":
        return "wallet", addr.lower()
    owners, err = _rpc("eth_call", [{"to": addr, "data": GET_OWNERS}, "latest"])
    if owners and not err and len(owners) >= 194:
        # ABI: offset, length, then 32-byte words; single-owner Safes are the norm
        n = int(owners[66:130], 16)
        if n >= 1:
            return "safe", "0x" + owners[130 + 24:194]
    return "email", None


def web3bio_profiles(addr):
    try:
        res = get_json(f"https://api.web3.bio/profile/{addr}", retries=2)
    except Exception:
        return []
    out = []
    for p in res if isinstance(res, list) else []:
        plat, ident = p.get("platform"), p.get("identity")
        if plat in PROFILE_URL and ident:
            out.append({"platform": plat, "name": ident, "url": PROFILE_URL[plat].format(ident)})
    return out


def xmtp_reachable(addrs):
    if not addrs:
        return {}
    if not os.path.isdir(os.path.join(XMTP_TOOL, "node_modules")):
        subprocess.run(["npm", "install", "--silent"], cwd=XMTP_TOOL, check=True)
    out = {}
    for i in range(0, len(addrs), 50):
        r = subprocess.run(["node", "can_message.mjs", *addrs[i:i + 50]], cwd=XMTP_TOOL,
                           capture_output=True, text=True, timeout=180)
        line = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if line:
            out.update(json.loads(line[-1]))
        else:
            print(f"  xmtp check failed: {r.stderr[-300:]}")
    return out


def update(addresses):
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    cutoff = (date.today() - timedelta(days=TTL_DAYS)).isoformat()
    todo = [a for a in dict.fromkeys(a.lower() for a in addresses) if cache.get(a, {}).get("checked", "") < cutoff]
    print(f"reach: checking {len(todo)} wallets")
    for a in todo:
        kind, signer = account_signer(a)
        cache[a] = {"type": kind, "signer": signer, "profiles": web3bio_profiles(signer) if signer else [],
                    "xmtp": False, "checked": date.today().isoformat()}
        time.sleep(0.3)  # be gentle with the free profile API
    xm = xmtp_reachable([cache[a]["signer"] for a in todo if cache[a]["signer"]])
    for a in todo:
        s = cache[a]["signer"]
        cache[a]["xmtp"] = bool(s and xm.get(s.lower()))
    with open(CACHE, "w") as f:
        json.dump(cache, f, indent=0, sort_keys=True)
    return cache


if __name__ == "__main__":
    L = json.load(open(os.path.join(ROOT, "data", "leaders.json")))
    addrs = [r["address"] for w in L.get("polymarket", {}).values() for r in w.get("rows", [])]
    c = update(addrs)
    rows = [c[a.lower()] for a in dict.fromkeys(addrs)]
    kinds = {k: sum(1 for r in rows if r["type"] == k) for k in ("safe", "wallet", "email")}
    print(f"{len(rows)} wallets: {kinds}; XMTP {sum(r['xmtp'] for r in rows)}; "
          f"with profiles {sum(1 for r in rows if r['profiles'])}")
