"""predict.fun's public GraphQL API (the website's own backend; no key needed) and EVM address
checksums, which its account lookups require.

Used for order-book snapshots of the live 15m Up/Down markets and for leaderboard names. Volume
and trades come from Dune instead (see queries.predict_activity).
"""
import time

from .net import post_json

GRAPHQL = "https://graphql.predict.fun/graphql"


def gql(query, variables=None, retries=3):
    delay = 1.0
    for attempt in range(retries):
        try:
            r = post_json(GRAPHQL, {"query": query, "variables": variables or {}}, timeout=30)
            if r.get("errors"):
                raise RuntimeError(f"predict.fun GraphQL: {r['errors'][0].get('message')}")
            return r["data"]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


# --- Keccak-256 (the pre-NIST padding Ethereum uses; hashlib.sha3_256 differs) -------------
_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(v, n):
    return ((v << n) | (v >> (64 - n))) & _M if n else v


def _keccak_f(a):
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    data = bytearray(data) + b"\x01"
    data += b"\x00" * (-len(data) % rate)
    data[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(data), rate):
        block = data[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        a = _keccak_f(a)
    return b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))


def checksum_address(addr: str) -> str:
    """EIP-55 mixed-case address."""
    a = addr.lower().removeprefix("0x")
    h = keccak256(a.encode()).hex()
    return "0x" + "".join(ch.upper() if ch.isalpha() and int(h[i], 16) >= 8 else ch for i, ch in enumerate(a))
