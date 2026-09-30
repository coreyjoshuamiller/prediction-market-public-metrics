"""SQL builders for the Dune queries."""
from .markets import assets_values_sql, poly_classify_sql

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
