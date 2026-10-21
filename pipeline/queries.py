"""SQL builders for the Dune queries."""
from datetime import date, timedelta

from .markets import assets_values_sql, poly_classify_sql, predict_assets_values_sql

SLUG = "regexp_extract(polymarket_link, 'event/([^/?''\"]+)', 1)"


def _poly_markets_cte():
    tok, dur, typ = poly_classify_sql("slug")
    return f"""
mk AS (
  -- market_details keys are hex strings; market_trades keys are varbinary
  SELECT from_hex(substr(condition_id, 3)) AS condition_id, max({SLUG}) AS slug
  FROM polymarket_polygon.market_details
  GROUP BY 1
),
cls AS (
  SELECT condition_id, {tok} AS tok, {dur} AS dur, {typ} AS ctype FROM mk
),
cm AS (
  SELECT c.condition_id, a.asset, a.asset_class, c.dur, c.ctype
  FROM cls c JOIN {assets_values_sql()} ON a.token = c.tok
  WHERE c.tok IS NOT NULL
)"""


def poly_activity(start, end):
    """Volume + unique traders for in-scope markets, plus platform totals, over [start, end).

    Each row of market_trades is one trader's side of a fill: `maker` is that trader.
    Notional = shares on the taker leg (one row per match), cash = USDC on the taker leg.
    """
    dims = ["asset, dur", "asset", "dur", "asset_class", "ctype", ""]
    sets = []
    for p in ("d", "w", "mo"):
        for dset in dims:
            sets.append(f"({p}{', ' + dset if dset else ''})")
    # the full detail grain (volume only is used from it, but it's cheap)
    sets.append("(d, asset, asset_class, dur, ctype)")
    return f"""
WITH {_poly_markets_cte()},
t AS (
  SELECT
    date(tr.block_time) AS d,
    date(date_trunc('week', tr.block_time)) AS w,
    date(date_trunc('month', tr.block_time)) AS mo,
    cm.asset, cm.asset_class, cm.dur, cm.ctype,
    tr.maker AS trader, tr.is_taker_side, tr.shares, tr.amount
  FROM polymarket_polygon.market_trades tr
  JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
)
SELECT
  d, w, mo, asset, asset_class, dur, ctype,
  grouping(d, w, mo, asset, asset_class, dur, ctype) AS gid,
  sum(shares) FILTER (WHERE is_taker_side) AS notional,
  sum(amount) FILTER (WHERE is_taker_side) AS cash,
  count(*) FILTER (WHERE is_taker_side) AS trades,
  approx_distinct(trader) AS traders
FROM t
GROUP BY GROUPING SETS ({', '.join(sets)})
"""


def poly_totals(start, end):
    """Whole-platform daily notional / cash / unique traders (all markets)."""
    return f"""
SELECT date(block_time) AS d,
  sum(shares) FILTER (WHERE is_taker_side) AS notional,
  sum(amount) FILTER (WHERE is_taker_side) AS cash,
  approx_distinct(maker) AS traders
FROM polymarket_polygon.market_trades
WHERE block_time >= timestamp '{start}' AND block_time < timestamp '{end}'
GROUP BY 1
"""


def kalshi_series_daily(start, end, fee_rates=None):
    """Per-series daily contracts (market_report), cash volume and estimated fees (trade_report).

    trade_report has one row per trade: cash = yes price (cents) * contracts / 100.
    fees = rate * contracts * P * (1 - P) with the series' rate (default 0.07); P(1-P) is the
    same for either side, so the taker's side doesn't matter.
    """
    rates = fee_rates or {}
    fee_join = ""
    rate_expr = "0.07"
    if rates:
        vals = ", ".join(f"('{k}', {v})" for k, v in rates.items())
        fee_join = f"LEFT JOIN (VALUES {vals}) AS fr(series, rate) ON fr.series = split_part(tr.ticker_name, '-', 1)"
        rate_expr = "coalesce(fr.rate, 0.07)"
    return f"""
WITH v AS (
  SELECT date, report_ticker AS series, sum(cast(daily_volume AS double)) AS contracts
  FROM kalshi.market_report
  WHERE date >= '{start}' AND date < '{end}'
  GROUP BY 1, 2
),
c AS (
  SELECT tr.date, split_part(tr.ticker_name, '-', 1) AS series,
    sum(cast(tr.price AS double) * cast(tr.contracts_traded AS double)) / 100 AS cash,
    sum({rate_expr} * cast(tr.contracts_traded AS double)
        * (cast(tr.price AS double) / 100) * (1 - cast(tr.price AS double) / 100)) AS fees,
    count(*) AS trades
  FROM kalshi.trade_report tr
  {fee_join}
  WHERE tr.date >= '{start}' AND tr.date < '{end}'
  GROUP BY 1, 2
)
SELECT coalesce(v.date, c.date) AS d, coalesce(v.series, c.series) AS series,
  coalesce(v.contracts, 0) AS contracts, coalesce(c.cash, 0) AS cash, coalesce(c.fees, 0) AS fees,
  coalesce(c.trades, 0) AS trades
FROM v FULL OUTER JOIN c ON v.date = c.date AND v.series = c.series
WHERE coalesce(v.contracts, 0) > 0 OR coalesce(c.cash, 0) > 0
"""


def poly_fees(start, end):
    """Daily fees (the on-chain `fee` field, charged to takers) for in-scope markets by
    asset/duration/type, plus the platform total (asset = '__TOTAL__')."""
    return f"""
WITH {_poly_markets_cte()},
t AS (
  SELECT date(tr.block_time) AS d, cm.asset, cm.asset_class, cm.dur, cm.ctype, coalesce(tr.fee, 0) AS fee
  FROM polymarket_polygon.market_trades tr
  LEFT JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
)
SELECT d, asset, asset_class, dur, ctype, sum(fee) AS fees FROM t WHERE asset IS NOT NULL GROUP BY 1, 2, 3, 4, 5
UNION ALL
SELECT d, '__TOTAL__', NULL, NULL, NULL, sum(fee) FROM t GROUP BY 1
"""


def _poly_wallet_volume(start, end, by_week=False):
    """CTEs ending in `w`: dollars each wallet traded in in-scope markets over [start, end),
    counting its own side of every fill (maker or taker), so wallets sum to 2x taker cash."""
    wk = "date(date_trunc('week', tr.block_time)) AS wk, " if by_week else ""
    return f"""
WITH {_poly_markets_cte()},
t AS (
  SELECT {wk}tr.maker AS trader, tr.amount, tr.is_taker_side
  FROM polymarket_polygon.market_trades tr
  JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
),
w AS (
  SELECT {"wk, " if by_week else ""}trader, sum(amount) AS vol, coalesce(sum(amount) FILTER (WHERE NOT is_taker_side), 0) AS maker_vol
  FROM t GROUP BY {"1, 2" if by_week else "1"}
  HAVING sum(amount) > 0
)"""


# percentile tiers for the concentration breakdown: (label, upper bound as a fraction of wallets)
CONC_TIERS = [("Top 0.1%", 0.001), ("0.1–1%", 0.01), ("1–5%", 0.05), ("5–10%", 0.10),
              ("10–25%", 0.25), ("25–50%", 0.50), ("Bottom 50%", 1.0)]


def poly_concentration(start, end):
    """How volume is spread across wallets in in-scope markets over [start, end).

    One result set, told apart by `kind`:
      curve  cumulative volume of the top `rn` wallets (by volume) at ~40 log-spaced ranks per decade
      tier   wallets / volume / maker volume per percentile tier (CONC_TIERS)
      band   wallets / volume / maker volume per order of magnitude of dollars traded (k = floor(log10))
      stats  wallet count, total volume and the exact Gini coefficient
    """
    tier = " ".join(f"WHEN rn <= greatest(1, ceil(n * {p})) THEN '{label}'" for label, p in CONC_TIERS)
    return f"""
{_poly_wallet_volume(start, end)},
r AS (
  SELECT vol, maker_vol,
    row_number() OVER (ORDER BY vol DESC) AS rn,
    count(*) OVER () AS n,
    sum(vol) OVER (ORDER BY vol DESC ROWS UNBOUNDED PRECEDING) AS cum
  FROM w
),
pts AS (SELECT DISTINCT cast(ceil(power(10, x / 40.0)) AS bigint) AS rn FROM unnest(sequence(0, 320)) AS s(x))
SELECT 'curve' AS kind, cast(r.rn AS varchar) AS k, r.rn, r.n, r.cum AS vol, NULL AS maker_vol, NULL AS wallets, NULL AS lo, NULL AS hi
FROM r WHERE r.rn IN (SELECT rn FROM pts) OR r.rn = r.n
UNION ALL
SELECT 'tier', CASE {tier} END, NULL, max(n), sum(vol), sum(maker_vol), count(*), min(vol), max(vol)
FROM r GROUP BY 2
UNION ALL
SELECT 'band', cast(cast(floor(log10(greatest(vol, 1))) AS integer) AS varchar), NULL, max(n), sum(vol), sum(maker_vol), count(*), min(vol), max(vol)
FROM r GROUP BY 2
UNION ALL
-- Gini over wallets in ascending order i = n - rn + 1: 2*sum(i*x) / (n*sum(x)) - (n+1)/n
SELECT 'stats', 'all', NULL, max(n), sum(vol), sum(maker_vol), count(*),
  2 * sum((n - rn + 1) * vol) / (max(n) * sum(vol)) - (max(n) + 1.0) / max(n), NULL
FROM r
"""


def poly_concentration_weekly(start, end):
    """Per week (Monday start) in [start, end): wallets, volume, the share of it traded by the
    top 1% / 5% / 10% of wallets and the top 10 wallets, and the Gini coefficient."""
    return f"""
{_poly_wallet_volume(start, end, by_week=True)},
r AS (
  SELECT wk, vol,
    row_number() OVER (PARTITION BY wk ORDER BY vol DESC) AS rn,
    count(*) OVER (PARTITION BY wk) AS n
  FROM w
)
SELECT wk, max(n) AS wallets, sum(vol) AS vol,
  sum(vol) FILTER (WHERE rn <= greatest(1, ceil(n * 0.01))) / sum(vol) AS top1,
  sum(vol) FILTER (WHERE rn <= greatest(1, ceil(n * 0.05))) / sum(vol) AS top5,
  sum(vol) FILTER (WHERE rn <= greatest(1, ceil(n * 0.10))) / sum(vol) AS top10,
  sum(vol) FILTER (WHERE rn <= 10) / sum(vol) AS top10w,
  2 * sum((n - rn + 1) * vol) / (max(n) * sum(vol)) - (max(n) + 1.0) / max(n) AS gini
FROM r GROUP BY 1
"""


def poly_leaders(start, end, limit=150):
    """Top traders in in-scope markets over [start, end): volume, trades, maker share,
    and realized PnL on markets that have resolved (cashflows + $1 per winning share held)."""
    return f"""
WITH {_poly_markets_cte()},
res AS (  -- settlement_value is the per-token payout (1 = winning outcome, 0 = losing)
  SELECT from_hex(substr(condition_id, 3)) AS condition_id, lower(token_outcome) AS tok_out,
    max(cast(settlement_value AS double)) AS payout
  FROM polymarket_polygon.market_details
  WHERE settlement_value IS NOT NULL
  GROUP BY 1, 2
),
t AS (
  SELECT tr.maker AS trader, tr.condition_id, lower(tr.token_outcome) AS tok_out,
    cm.asset, cm.dur, tr.is_taker_side, tr.maker_side, tr.shares, tr.amount, coalesce(tr.fee, 0) AS fee
  FROM polymarket_polygon.market_trades tr
  JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
),
pos AS (  -- per trader, per market outcome token
  SELECT trader, condition_id, tok_out,
    sum(CASE WHEN maker_side = 'BUY' THEN -amount ELSE amount END) - sum(fee) AS cashflow,
    sum(CASE WHEN maker_side = 'BUY' THEN shares ELSE -shares END) AS net_shares
  FROM t GROUP BY 1, 2, 3
),
pnl AS (
  SELECT p.trader,
    sum(p.cashflow + p.net_shares * r.payout) AS realized_pnl
  FROM pos p JOIN res r ON r.condition_id = p.condition_id AND r.tok_out = p.tok_out
  GROUP BY 1
),
agg AS (
  SELECT trader,
    sum(amount) AS volume,
    sum(shares) AS notional,
    count(*) AS fills,
    count(DISTINCT condition_id) AS markets,
    sum(amount) FILTER (WHERE NOT is_taker_side) / nullif(sum(amount), 0) AS maker_share
  FROM t GROUP BY 1
),
fav AS (
  SELECT trader, asset || ' ' || dur AS top_market,
    row_number() OVER (PARTITION BY trader ORDER BY sum(amount) DESC) AS rn
  FROM t GROUP BY trader, asset, dur
)
SELECT a.trader, a.volume, a.notional, a.fills, a.markets, a.maker_share, p.realized_pnl, f.top_market
FROM agg a
LEFT JOIN pnl p ON p.trader = a.trader
LEFT JOIN fav f ON f.trader = a.trader AND f.rn = 1
ORDER BY a.volume DESC
LIMIT {limit}
"""


# --- predict.fun (BNB Chain) ------------------------------------------------------------
# A Polymarket CTF-exchange fork; Dune decodes it as predict_bnb.*. Up/Down markets come from
# two kinds of oracle (see markets.PREDICT_FEEDS): the Chainlink Up/Down adapter's rounds and
# UMA questions whose title says "Up or Down". Question -> condition (ConditionPreparation on
# either conditional-tokens contract) -> outcome tokens (TokenRegistered) -> fills.
# Amounts are 18-decimal (BSC USDT). Regexes are kept out of f-strings to spare the braces.
# Markets are created at most ~a week before they trade, so metadata is read from
# PREDICT_META_LOOKBACK days before the window instead of all history (cuts the fixed cost).
PREDICT_META_LOOKBACK = 45

_PREDICT_UMA_DUR = r"""CASE
      WHEN regexp_like(title, '\d(?::\d{2})?\s*(?:AM|PM)?\s*-\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM) ET') THEN
        CASE ((CAST(regexp_extract(title, '(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM) ET', 3) AS integer) * 60
               + CAST(coalesce(regexp_extract(title, '(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM) ET', 4), '0') AS integer))
              - (CAST(regexp_extract(title, '(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM) ET', 1) AS integer) * 60
               + CAST(coalesce(regexp_extract(title, '(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM)?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(?:AM|PM) ET', 2), '0') AS integer))
              + 720) % 720
          WHEN 5 THEN '5m' WHEN 15 THEN '15m' WHEN 60 THEN '1h' WHEN 240 THEN '4h' END
      WHEN regexp_like(title, ',\s*\d{1,2}\s*(?:AM|PM) ET$') OR regexp_like(title, 'Up or Down Hour \d+') THEN '1h'
      WHEN regexp_like(title, 'Up or Down on [A-Z][a-z]+ \d') THEN '1d'
    END"""

_PREDICT_MARKETS = r"""
__ASSETS__,
cl AS (  -- Chainlink adapter rounds
  SELECT contract_address AS oracle, questionID AS qid, 'feed:' || lower(CAST(feedId AS varchar)) AS k,
    CASE interval WHEN 300 THEN '5m' WHEN 900 THEN '15m' WHEN 3600 THEN '1h' WHEN 14400 THEN '4h' WHEN 86400 THEN '1d' END AS dur
  FROM predict_bnb.chainlinkupdownadapter_evt_chainlinkupdownadapter__roundcreated
  WHERE evt_block_date >= DATE '__SINCE__'
),
uq AS (  -- UMA questions titled "... Up or Down ..."
  SELECT contract_address AS oracle, questionID AS qid,
    trim(regexp_extract(from_utf8(ancillaryData), 'q: title: (.*?), description:', 1)) AS title
  FROM predict_bnb.umacompatiblectfadapter_evt_questioninitialized
  WHERE evt_block_date >= DATE '__SINCE__' AND strpos(from_utf8(ancillaryData), 'Up or Down') > 0
),
uma AS (
  SELECT oracle, qid, 'title:' || lower(regexp_extract(title, '^([A-Za-z0-9&]+)', 1)) AS k, __UMA_DUR__ AS dur
  FROM uq WHERE regexp_like(title, '^\S.*?\sUp or Down\s')
),
q AS (SELECT * FROM cl UNION ALL SELECT * FROM uma),
cond AS (
  SELECT p.conditionId, q.k, q.dur
  FROM (
    SELECT conditionId, oracle, questionId FROM predict_bnb.conditionaltokens_evt_conditionpreparation WHERE evt_block_date >= DATE '__SINCE__'
    UNION ALL
    SELECT conditionId, oracle, questionId FROM predict_bnb.yieldbearingconditionaltokens_evt_conditionpreparation WHERE evt_block_date >= DATE '__SINCE__'
  ) p JOIN q ON p.questionId = q.qid AND p.oracle = q.oracle
  WHERE q.dur IS NOT NULL
),
tok AS (
  SELECT DISTINCT token, conditionId FROM (
    SELECT token0 AS token, conditionId FROM predict_bnb.ctfexchange_evt_tokenregistered WHERE evt_block_date >= DATE '__SINCE__'
    UNION ALL
    SELECT token1, conditionId FROM predict_bnb.ctfexchange_evt_tokenregistered WHERE evt_block_date >= DATE '__SINCE__'
  )
),
mk AS (  -- one row per outcome token of an in-scope market
  SELECT tok.token, coalesce(pa.asset, cond.k) AS asset, coalesce(pa.asset_class, 'Other') AS asset_class,
    cond.dur, 'Up/Down' AS ctype
  FROM tok JOIN cond ON cond.conditionId = tok.conditionId
  LEFT JOIN pa ON pa.k = cond.k
)"""

# columns shared by both fill queries; fee is charged in the asset the order receives, so a
# buyer's fee is in shares and is valued at the fill price
_PREDICT_FILL_COLS = """
    f.maker AS trader, f.taker = f.contract_address AS is_taker,
    CAST(IF(f.makerAssetId = 0, f.takerAmountFilled, f.makerAmountFilled) AS double) / 1e18 AS shares,
    CAST(IF(f.makerAssetId = 0, f.makerAmountFilled, f.takerAmountFilled) AS double) / 1e18 AS amount,
    CAST(f.fee AS double) / 1e18 * IF(f.makerAssetId = 0,
      CAST(f.makerAmountFilled AS double) / nullif(CAST(f.takerAmountFilled AS double), 0), 1) AS fee"""


def predict_activity(start, end):
    """Like poly_activity for predict.fun Up/Down markets, plus fees, over [start, end).

    Every OrderFilled is one order's side of a fill and `maker` is that order's owner; the
    taker order's event has taker = the exchange. Notional/cash/trades count taker events
    only; traders count every side; fees sum every side (makers pay none today).
    """
    dims = ["asset, dur", "asset", "dur", "asset_class", "ctype", ""]
    sets = [f"({p}{', ' + dset if dset else ''})" for p in ("d", "w", "mo") for dset in dims]
    sets.append("(d, asset, asset_class, dur, ctype)")
    since = date.fromisoformat(str(start)) - timedelta(days=PREDICT_META_LOOKBACK)
    markets = (_PREDICT_MARKETS.replace("__ASSETS__", predict_assets_values_sql())
               .replace("__UMA_DUR__", _PREDICT_UMA_DUR).replace("__SINCE__", str(since)))
    return f"""
WITH {markets},
t AS (
  SELECT
    date(f.evt_block_time) AS d,
    date(date_trunc('week', f.evt_block_time)) AS w,
    date(date_trunc('month', f.evt_block_time)) AS mo,
    mk.asset, mk.asset_class, mk.dur, mk.ctype,{_PREDICT_FILL_COLS}
  FROM predict_bnb.ctfexchange_evt_orderfilled f
  JOIN mk ON mk.token = IF(f.makerAssetId = 0, f.takerAssetId, f.makerAssetId)
  WHERE f.evt_block_date >= DATE '{start}' AND f.evt_block_date < DATE '{end}'
)
SELECT
  d, w, mo, asset, asset_class, dur, ctype,
  grouping(d, w, mo, asset, asset_class, dur, ctype) AS gid,
  sum(shares) FILTER (WHERE is_taker) AS notional,
  sum(amount) FILTER (WHERE is_taker) AS cash,
  sum(fee) AS fees,
  count(*) FILTER (WHERE is_taker) AS trades,
  approx_distinct(trader) AS traders
FROM t
GROUP BY GROUPING SETS ({', '.join(sets)})
"""


def predict_totals(start, end):
    """Whole-platform daily notional / cash / fees / unique traders (all four exchanges)."""
    return f"""
WITH f AS (
  SELECT evt_block_time, contract_address, maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee
  FROM predict_bnb.ctfexchange_evt_orderfilled
  WHERE evt_block_date >= DATE '{start}' AND evt_block_date < DATE '{end}'
  UNION ALL
  SELECT evt_block_time, contract_address, maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee
  FROM predict_bnb.negriskctfexchange_evt_orderfilled
  WHERE evt_block_date >= DATE '{start}' AND evt_block_date < DATE '{end}'
),
t AS (SELECT date(f.evt_block_time) AS d,{_PREDICT_FILL_COLS} FROM f)
SELECT d,
  sum(shares) FILTER (WHERE is_taker) AS notional,
  sum(amount) FILTER (WHERE is_taker) AS cash,
  sum(fee) AS fees,
  approx_distinct(trader) AS traders
FROM t GROUP BY 1
"""
