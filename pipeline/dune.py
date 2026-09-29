"""Minimal Dune API client: run ad-hoc SQL and return rows.

Reads DUNE_API_KEY from the environment, falling back to the repo's .env file.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.dune.com/api/v1"
ROOT = os.path.join(os.path.dirname(__file__), "..")


def _key():
    k = os.environ.get("DUNE_API_KEY")
    if not k:
        env = os.path.join(ROOT, ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("DUNE_API_KEY="):
                    k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit("DUNE_API_KEY not set (env var or .env)")
    return k


def _req(path, body=None):
    h = {"X-Dune-Api-Key": _key(), "Content-Type": "application/json", "User-Agent": "pm-metrics"}
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{API}{path}", data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dune {path}: HTTP {e.code} {e.read().decode()[:500]}") from None


def run_sql(sql, performance="medium", timeout=1800, label=""):
    t0 = time.time()
    ex = _req("/sql/execute", {"sql": sql, "performance": performance})
    eid = ex["execution_id"]
    delay = 2
    while True:
        st = _req(f"/execution/{eid}/status")
        if st.get("is_execution_finished"):
            break
        if time.time() - t0 > timeout:
            raise TimeoutError(f"Dune query {label or eid} still running after {timeout}s")
        time.sleep(delay)
        delay = min(delay * 1.5, 15)
    if st.get("state") != "QUERY_STATE_COMPLETED":
        raise RuntimeError(f"Dune query {label or eid} failed: {json.dumps(st.get('error') or st)[:800]}")
    rows, offset = [], 0
    while True:
        res = _req(f"/execution/{eid}/results?limit=50000&offset={offset}")
        batch = res.get("result", {}).get("rows", [])
        rows += batch
        if not res.get("next_offset"):
            break
        offset = res["next_offset"]
    cost = st.get("execution_cost_credits")
    print(f"  dune[{label}] {len(rows)} rows in {time.time()-t0:.0f}s" + (f", {cost} credits" if cost else ""))
    return rows


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    for row in run_sql(q, label="cli"):
        print(json.dumps(row, default=str))
