"""SQL builders for the Dune queries."""
from datetime import date, timedelta

from .markets import assets_values_sql, poly_classify_sql, predict_assets_values_sql
from .rewards import poly_category_sql

# Dune's public Polymarket tables (polymarket_polygon.market_details and market_trades went private
# on 2026-11-12): market_trades_raw has the same fills; polymarket.gamma_markets_0001 and
# polymarket.markets_raw are Polymarket's own market listings (slugs, tags, outcome tokens, winners).
TRADES = "polymarket_polygon.market_trades_raw"

# per outcome token, its payout once the market has resolved (1 = winner, 0 = loser, 0.5 = 50/50)
_RES = """res AS (
  SELECT tok, max(payout) AS payout FROM (
    SELECT token_1_id AS tok, CASE WHEN is_50_50_outcome THEN 0.5 WHEN token_1_winner THEN 1.0 ELSE 0.0 END AS payout
    FROM polymarket.markets_raw WHERE token_1_winner OR token_2_winner OR is_50_50_outcome
    UNION ALL
    SELECT token_2_id, CASE WHEN is_50_50_outcome THEN 0.5 WHEN token_2_winner THEN 1.0 ELSE 0.0 END
    FROM polymarket.markets_raw WHERE token_1_winner OR token_2_winner OR is_50_50_outcome
  ) GROUP BY 1
)"""


def _poly_markets_cte():
    tok, dur, typ = poly_classify_sql("slug")
    ev_tok = poly_classify_sql("event_slug")[0]
    return f"""
mk AS (
  -- the listing's keys are hex strings; the trades' keys are varbinary. Classify by the event's
  -- slug, or by the market's own when the event's doesn't fit a pattern (e.g. the daily
  -- "will-bitcoin-reach-X-on-<day>" markets sit in "what-price-will-bitcoin-hit-..." events).
  -- Weekly markets ("nvda-above-170-week-november-13") can sit in daily-looking events: out of scope.
  SELECT from_hex(substr(condition_id, 3)) AS condition_id,
    max(CASE WHEN market_slug LIKE '%-week-%' THEN market_slug
             WHEN {ev_tok} IS NOT NULL THEN event_slug ELSE market_slug END) AS slug
  FROM polymarket.gamma_markets_0001
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


def _recent_days(days_from):
    """HAVING clause that drops day-grain rows before `days_from` (week/month rows have d NULL):
    the daily window re-reads month to date for the trader counts, but only recent days change,
    and Dune bills every row returned."""
    return f"HAVING d IS NULL OR d >= DATE '{days_from}'" if days_from else ""


def poly_activity(start, end, days_from=None):
    """Volume + unique traders for in-scope markets, plus platform totals, over [start, end).

    Each row of the trades table is one trader's side of a fill: `maker` is that trader.
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
  FROM {TRADES} tr
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
{_recent_days(days_from)}
"""


def poly_totals(start, end):
    """Whole-platform daily notional / cash / unique traders (all markets)."""
    return f"""
SELECT date(block_time) AS d,
  sum(shares) FILTER (WHERE is_taker_side) AS notional,
  sum(amount) FILTER (WHERE is_taker_side) AS cash,
  approx_distinct(maker) AS traders
FROM {TRADES}
WHERE block_time >= timestamp '{start}' AND block_time < timestamp '{end}'
GROUP BY 1
"""


def kalshi_series_daily(start, end, fee_rates=None, series=None):
    """Per-series daily contracts (market_report), cash volume and estimated fees (trade_report).

    With `series`, only those series come back, plus one row per day with series '__TOTAL__'
    summed over every series (the platform total): Dune bills result rows, and only the
    in-scope ones are kept.

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
{_kalshi_select(series)}
"""


def _kalshi_select(series):
    rows = """SELECT coalesce(v.date, c.date) AS d, coalesce(v.series, c.series) AS series,
  coalesce(v.contracts, 0) AS contracts, coalesce(c.cash, 0) AS cash, coalesce(c.fees, 0) AS fees,
  coalesce(c.trades, 0) AS trades
FROM v FULL OUTER JOIN c ON v.date = c.date AND v.series = c.series
WHERE coalesce(v.contracts, 0) > 0 OR coalesce(c.cash, 0) > 0"""
    if not series:
        return rows
    vals = ", ".join("('" + s.replace("'", "") + "')" for s in sorted(series))
    return f"""SELECT d, IF(grouping(series) = 1, '__TOTAL__', series) AS series,
  sum(contracts) AS contracts, sum(cash) AS cash, sum(fees) AS fees, sum(trades) AS trades
FROM (
  SELECT r.*, k.series IS NOT NULL AS in_scope
  FROM ({rows}) r LEFT JOIN (VALUES {vals}) AS k(series) ON k.series = r.series
)
GROUP BY GROUPING SETS ((d, series, in_scope), (d))
HAVING grouping(series) = 1 OR in_scope"""


def poly_fees(start, end):
    """Daily fees (the on-chain `fee` field, charged to takers) for in-scope markets by
    asset/duration/type, plus the platform total (asset = '__TOTAL__')."""
    return f"""
WITH {_poly_markets_cte()},
t AS (
  SELECT date(tr.block_time) AS d, cm.asset, cm.asset_class, cm.dur, cm.ctype, coalesce(tr.fee, 0) AS fee
  FROM {TRADES} tr
  LEFT JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
)
SELECT d, asset, asset_class, dur, ctype, sum(fee) AS fees FROM t WHERE asset IS NOT NULL GROUP BY 1, 2, 3, 4, 5
UNION ALL
SELECT d, '__TOTAL__', NULL, NULL, NULL, sum(fee) FROM t GROUP BY 1
"""


def poly_rebates_paid(start, end):
    """Maker rebates paid per day over [start, end), by payment date (a day's rebates are paid the
    next day): the distributors' transfers of USDC.e/pUSD, without their sweeps to the protocol fee
    wallet and the pUSD contract. Addresses as in DefiLlama's Polymarket adapter; a maker's total here
    matches clob.polymarket.com/rebates/current to the cent."""
    return f"""
SELECT date(evt_block_time) AS paid, sum(cast(value AS double)) / 1e6 AS rebates
FROM erc20_polygon.evt_Transfer
WHERE evt_block_date >= DATE '{start}' AND evt_block_date < DATE '{end}'
  AND "from" IN (0x3a9418b2651c8164DB5EBc56F12008137865e0f7, 0xfdB1b8dC7f5789a0c9A398026585B8B10FbA5507)
  AND contract_address IN (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174, 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB)
  AND "to" NOT IN (0x2d507657cA4EBCc8F9a38F6764c07310B66DEA54, 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB)
GROUP BY 1
"""


def poly_rewards_paid(start, end):
    """Liquidity rewards paid on dates in [start, end), per day earned (a day's rewards are paid just
    after midnight UTC) and estimated category. Distributors as in DefiLlama's Polymarket adapter, less
    sweeps to the protocol fee wallet and pUSD. Each wallet's payout is split by its maker volume that
    day; short-term Up/Down markets carry almost no rewards, so they don't count. A wallet with no
    fills that day has no category."""
    fills_from, fills_to = start - timedelta(days=1), end - timedelta(days=1)
    return f"""
WITH pay AS (
  SELECT date(evt_block_time) - interval '1' day AS d, "to" AS wallet, sum(cast(value AS double)) / 1e6 AS usd
  FROM erc20_polygon.evt_Transfer
  WHERE evt_block_date >= DATE '{start}' AND evt_block_date < DATE '{end}'
    AND "from" IN (0xc288480574783BD7615170660d71753378159c47, 0x2c2795EA295d5Eb51F9121B728eD2eA4e936a709)
    AND contract_address IN (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174, 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB)
    AND "to" NOT IN (0x2d507657cA4EBCc8F9a38F6764c07310B66DEA54, 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB)
  GROUP BY 1, 2
),
md AS (
  SELECT from_hex(substr(condition_id, 3)) AS condition_id, arbitrary(tags) AS t
  FROM polymarket.markets_raw
  GROUP BY 1
),
cat AS (
  SELECT condition_id, {poly_category_sql()} AS category FROM md WHERE NOT contains(t, 'Up or Down')
),
f AS (
  SELECT date(tr.block_time) AS d, tr.maker AS wallet, c.category, sum(tr.amount) AS vol
  FROM {TRADES} tr
  JOIN cat c ON c.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{fills_from}' AND tr.block_time < timestamp '{fills_to}' AND NOT tr.is_taker_side
  GROUP BY 1, 2, 3
),
s AS (
  SELECT d, wallet, category, vol / sum(vol) OVER (PARTITION BY d, wallet) AS sh FROM f
)
SELECT p.d, coalesce(s.category, 'Not attributed') AS category, sum(p.usd * coalesce(s.sh, 1)) AS usd
FROM pay p
LEFT JOIN s ON s.d = p.d AND s.wallet = p.wallet
GROUP BY 1, 2
"""


def _poly_wallet_volume(start, end, by_week=False):
    """CTEs ending in `w`: dollars each wallet traded in in-scope markets over [start, end),
    counting its own side of every fill (maker or taker), so wallets sum to 2x taker cash."""
    wk = "date(date_trunc('week', tr.block_time)) AS wk, " if by_week else ""
    return f"""
WITH {_poly_markets_cte()},
t AS (
  SELECT {wk}tr.maker AS trader, tr.amount, tr.is_taker_side
  FROM {TRADES} tr
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


def _predict_wallet_volume(start, end, by_week=False):
    """predict.fun counterpart of _poly_wallet_volume: USDT each wallet paid or received on its
    own side of every in-scope fill."""
    return f"""{_predict_fills(start, end)},
w AS (
  SELECT {"w AS wk, " if by_week else ""}trader, sum(amount) AS vol, coalesce(sum(amount) FILTER (WHERE NOT is_taker), 0) AS maker_vol
  FROM t GROUP BY {"1, 2" if by_week else "1"}
  HAVING sum(amount) > 0
)"""


def poly_concentration(start, end, wallets=None):
    """How volume is spread across wallets in in-scope markets over [start, end).

    One result set, told apart by `kind`:
      curve  cumulative volume of the top `rn` wallets (by volume) at ~40 log-spaced ranks per decade
      tier   wallets / volume / maker volume per percentile tier (CONC_TIERS)
      band   wallets / volume / maker volume per order of magnitude of dollars traded (k = floor(log10))
      stats  wallet count, total volume and the exact Gini coefficient
    """
    tier = " ".join(f"WHEN rn <= greatest(1, ceil(n * {p})) THEN '{label}'" for label, p in CONC_TIERS)
    return f"""
{wallets or _poly_wallet_volume(start, end)},
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


def poly_concentration_weekly(start, end, wallets=None):
    """Per week (Monday start) in [start, end): wallets, volume, the share of it traded by the
    top 1% / 5% / 10% of wallets and the top 10 wallets, and the Gini coefficient."""
    return f"""
{wallets or _poly_wallet_volume(start, end, by_week=True)},
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
{_RES},
t AS (
  SELECT tr.maker AS trader, tr.condition_id, tr.asset_id AS tok,
    cm.asset, cm.dur, tr.is_taker_side, tr.maker_side, tr.shares, tr.amount, coalesce(tr.fee, 0) AS fee
  FROM {TRADES} tr
  JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
),
pos AS (  -- per trader, per market outcome token
  SELECT trader, condition_id, tok,
    sum(CASE WHEN maker_side = 'BUY' THEN -amount ELSE amount END) - sum(fee) AS cashflow,
    sum(CASE WHEN maker_side = 'BUY' THEN shares ELSE -shares END) AS net_shares
  FROM t GROUP BY 1, 2, 3
),
pnl AS (
  SELECT p.trader,
    sum(p.cashflow + p.net_shares * r.payout) AS realized_pnl
  FROM pos p JOIN res r ON r.tok = p.tok
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


def poly_leaders_weekly(start, end, per_week=2000):
    """poly_leaders per Monday-start week in [start, end): each week's top `per_week` wallets,
    with maker and top-market volume (not shares) so weeks can be added up into longer windows.
    PnL is linear in fills and these markets resolve within a day, so weekly PnL sums too."""
    return f"""
WITH {_poly_markets_cte()},
{_RES},
t AS (
  SELECT date(date_trunc('week', tr.block_time)) AS wk, tr.maker AS trader, tr.condition_id, tr.asset_id AS tok,
    cm.asset, cm.dur, tr.is_taker_side, tr.maker_side, tr.shares, tr.amount, coalesce(tr.fee, 0) AS fee
  FROM {TRADES} tr
  JOIN cm ON cm.condition_id = tr.condition_id
  WHERE tr.block_time >= timestamp '{start}' AND tr.block_time < timestamp '{end}'
),
pos AS (
  SELECT wk, trader, condition_id, tok,
    sum(CASE WHEN maker_side = 'BUY' THEN -amount ELSE amount END) - sum(fee) AS cashflow,
    sum(CASE WHEN maker_side = 'BUY' THEN shares ELSE -shares END) AS net_shares
  FROM t GROUP BY 1, 2, 3, 4
),
pnl AS (
  SELECT p.wk, p.trader, sum(p.cashflow + p.net_shares * r.payout) AS realized_pnl
  FROM pos p JOIN res r ON r.tok = p.tok
  GROUP BY 1, 2
),
agg AS (
  SELECT wk, trader, sum(amount) AS volume, sum(shares) AS notional, count(*) AS fills,
    count(DISTINCT condition_id) AS markets, coalesce(sum(amount) FILTER (WHERE NOT is_taker_side), 0) AS maker_vol,
    row_number() OVER (PARTITION BY wk ORDER BY sum(amount) DESC) AS rk
  FROM t GROUP BY 1, 2
),
fav AS (
  SELECT wk, trader, asset || ' ' || dur AS top_market, sum(amount) AS top_market_vol,
    row_number() OVER (PARTITION BY wk, trader ORDER BY sum(amount) DESC) AS rn
  FROM t GROUP BY wk, trader, asset, dur
)
SELECT a.wk, a.rk, a.trader, a.volume, a.notional, a.fills, a.markets, a.maker_vol, p.realized_pnl, f.top_market, f.top_market_vol
FROM agg a
LEFT JOIN pnl p ON p.wk = a.wk AND p.trader = a.trader
LEFT JOIN fav f ON f.wk = a.wk AND f.trader = a.trader AND f.rn = 1
WHERE a.rk <= {per_week}
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
  SELECT tok.token, tok.conditionId, coalesce(pa.asset, cond.k) AS asset, coalesce(pa.asset_class, 'Other') AS asset_class,
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


def _predict_markets_cte(start):
    since = date.fromisoformat(str(start)) - timedelta(days=PREDICT_META_LOOKBACK)
    return (_PREDICT_MARKETS.replace("__ASSETS__", predict_assets_values_sql())
            .replace("__UMA_DUR__", _PREDICT_UMA_DUR).replace("__SINCE__", str(since)))


def _predict_fills(start, end, extra=""):
    """CTE `t`: every in-scope fill side in [start, end), one row per OrderFilled."""
    return f"""
WITH {_predict_markets_cte(start)},
t AS (
  SELECT
    date(f.evt_block_time) AS d,
    date(date_trunc('week', f.evt_block_time)) AS w,
    date(date_trunc('month', f.evt_block_time)) AS mo,
    mk.token, mk.conditionId, mk.asset, mk.asset_class, mk.dur, mk.ctype,{_PREDICT_FILL_COLS}{extra}
  FROM predict_bnb.ctfexchange_evt_orderfilled f
  JOIN mk ON mk.token = IF(f.makerAssetId = 0, f.takerAssetId, f.makerAssetId)
  WHERE f.evt_block_date >= DATE '{start}' AND f.evt_block_date < DATE '{end}'
)"""


def predict_activity(start, end, days_from=None):
    """Like poly_activity for predict.fun Up/Down markets, plus fees, over [start, end).

    Every OrderFilled is one order's side of a fill and `maker` is that order's owner; the
    taker order's event has taker = the exchange. Notional/cash/trades count taker events
    only; traders count every side; fees sum every side (makers pay none today).
    """
    dims = ["asset, dur", "asset", "dur", "asset_class", "ctype", ""]
    sets = [f"({p}{', ' + dset if dset else ''})" for p in ("d", "w", "mo") for dset in dims]
    sets.append("(d, asset, asset_class, dur, ctype)")
    return f"""{_predict_fills(start, end)}
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
{_recent_days(days_from)}
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


def predict_concentration_weekly(start, end):
    return poly_concentration_weekly(start, end, wallets=_predict_wallet_volume(start, end, by_week=True))


def predict_concentration(start, end):
    """poly_concentration's figures for predict.fun in one pass: Dune re-runs a CTE for every
    reference (the fills scan is the costly part) and bills every result row, so instead of
    UNIONing four views of the ranked wallets, one GROUPING SETS query returns ~200 rows:
      g = 3  per percentile tier          g = 5  per order of magnitude of dollars (band)
      g = 6  per curve bucket: wallets up to the next ~40-per-decade rank; (max rn, max cum)
             is a point on the cumulative-volume curve
      g = 7  totals, with gsum for the Gini coefficient
    """
    tier = " ".join(f"WHEN rn <= greatest(1, ceil(n * {p})) THEN '{label}'" for label, p in CONC_TIERS)
    return f"""{_predict_wallet_volume(start, end)},
r AS (
  SELECT vol, maker_vol,
    row_number() OVER (ORDER BY vol DESC) AS rn,
    count(*) OVER () AS n,
    sum(vol) OVER (ORDER BY vol DESC ROWS UNBOUNDED PRECEDING) AS cum
  FROM w
),
b AS (
  SELECT r.*, CASE {tier} END AS tier,
    CAST(floor(log10(greatest(vol, 1))) AS integer) AS band,
    least(n, greatest(rn, CAST(ceil(power(10, ceil(40 * log10(rn)) / 40.0)) AS bigint))) AS pt
  FROM r
)
SELECT grouping(tier, band, pt) AS g, tier, band, pt,
  count(*) AS wallets, sum(vol) AS vol, sum(maker_vol) AS maker_vol, min(vol) AS lo, max(vol) AS hi,
  max(n) AS n, max(rn) AS rn, max(cum) AS cum, sum((n - rn + 1) * vol) AS gsum
FROM b
GROUP BY GROUPING SETS ((tier), (band), (pt), ())
"""


def _predict_pnl_ctes(start, end, by_week=False):
    """CTEs through `j`: one row per wallet and outcome token (and week if by_week) with its
    payout, in one pass over the fills (Dune re-runs a CTE for every reference).

    Realized PnL needs each token's outcome index, which no table records. The exchange mints
    a market's two tokens by splitting collateral with partition [1, 2], and the CTF mints them
    in that order (TransferBatch from 0x0), so ids[1] is outcome 1 ("Up") and ids[2] outcome 2.
    ConditionResolution's payoutNumerators then give each token's payout.

    Cashflows are net of the fee actually paid: a buyer's fee comes out of the shares received, a
    seller's out of the USDT, and FeeRefunded gives back the excess in the same asset and the same
    transaction (all of it for makers, 10% for takers). Maker rebates are added too, in the asset
    of the taker's fee. Checked on four days from 2025-12 to 2026-10: refunds match fills 1:1 on
    (tx, orderHash) and go to the order's owner; rebates are one per maker fill in the same tx.
    Referral payouts go to the referrer and don't change what the trader paid, so they're left out.

    Refunds and rebates in shares carry their token, and a maker's rebate can be in the outcome
    it didn't trade, so they're rows of their own. Those in USDT (token id 0) name no market, so
    they're looked up from the fill they belong to, which also keeps other markets' out of scope.
    """
    zero = "0x0000000000000000000000000000000000000000"
    mint_since = date.fromisoformat(str(start)) - timedelta(days=3)  # tokens are minted as they trade
    wk = "wk, " if by_week else ""
    keys = ", ".join(str(i) for i in range(1, 5 + by_week))
    return f"""
WITH {_predict_markets_cte(start)},
lg AS (
  SELECT block_time AS ts, tx_hash AS tx, topic0 = {_PREDICT_REFUNDED} AS is_refund, topic1,
    substr(IF(topic0 = {_PREDICT_REFUNDED}, topic2, topic1), 13, 20) AS wallet,
    IF(topic0 = {_PREDICT_REFUNDED}, varbinary_to_uint256(substr(data, 1, 32)), varbinary_to_uint256(topic2)) AS tok,
    CAST(varbinary_to_uint256(IF(topic0 = {_PREDICT_REFUNDED}, substr(data, 33, 32), substr(data, 1, 32))) AS double) / 1e18 AS amt
  FROM bnb.logs
  WHERE block_date >= DATE '{start}' AND block_date < DATE '{end}' AND contract_address IN ({_PREDICT_FEE_MODULES})
    AND topic0 IN ({_PREDICT_REFUNDED}, {_PREDICT_REBATED})
),
usdt AS (  -- per tx: USDT refund by orderHash, USDT rebate per maker fill by maker address
  SELECT tx, map_agg(k, amt) AS m FROM (
    SELECT tx, IF(is_refund, topic1, wallet) AS k, avg(amt) AS amt FROM lg WHERE tok = 0 GROUP BY 1, 2
  ) GROUP BY 1
),
s AS (
  SELECT f.evt_block_time AS ts, f.evt_tx_hash AS tx, f.orderHash AS oh, IF(f.makerAssetId = 0, f.takerAssetId, f.makerAssetId) AS token,
    f.maker AS trader, f.taker = f.contract_address AS is_taker,
    CAST(IF(f.makerAssetId = 0, f.takerAmountFilled, f.makerAmountFilled) AS double) / 1e18 AS shares,
    CAST(IF(f.makerAssetId = 0, f.makerAmountFilled, f.takerAmountFilled) AS double) / 1e18 AS amount,
    IF(f.makerAssetId = 0, -CAST(f.makerAmountFilled AS double), CAST(f.takerAmountFilled AS double) - CAST(f.fee AS double)) / 1e18 AS cash,
    IF(f.makerAssetId = 0, CAST(f.takerAmountFilled AS double) - CAST(f.fee AS double), -CAST(f.makerAmountFilled AS double)) / 1e18 AS sh
  FROM predict_bnb.ctfexchange_evt_orderfilled f
  WHERE f.evt_block_date >= DATE '{start}' AND f.evt_block_date < DATE '{end}'
  UNION ALL
  SELECT ts, NULL, NULL, tok, wallet, NULL, 0e0, 0e0, 0e0, amt FROM lg WHERE tok <> 0
),
t AS (
  SELECT date(date_trunc('week', s.ts)) AS wk, s.trader, s.token, mk.conditionId, mk.asset || ' ' || mk.dur AS market,
    s.is_taker, s.shares, s.amount, s.sh,
    s.cash + coalesce(element_at(u.m, s.oh), 0e0) + IF(s.is_taker, 0e0, coalesce(element_at(u.m, s.trader), 0e0)) AS cash
  FROM s
  JOIN mk ON mk.token = s.token
  LEFT JOIN usdt u ON u.tx = s.tx
),
tt AS (  -- per wallet and outcome token
  SELECT {wk}trader, token, conditionId, market,
    sum(amount) AS vol, sum(shares) AS notional, count(is_taker) AS fills,
    coalesce(sum(amount) FILTER (WHERE NOT is_taker), 0) AS maker_vol,
    sum(cash) AS cashflow,
    sum(sh) AS net_shares
  FROM t GROUP BY {keys}
),
mints AS (
  SELECT ids FROM predict_bnb.conditionaltokens_evt_transferbatch
  WHERE evt_block_date >= DATE '{mint_since}' AND evt_block_date < DATE '{end}' AND "from" = {zero} AND cardinality(ids) = 2
  UNION ALL
  SELECT ids FROM predict_bnb.yieldbearingconditionaltokens_evt_transferbatch
  WHERE evt_block_date >= DATE '{mint_since}' AND evt_block_date < DATE '{end}' AND "from" = {zero} AND cardinality(ids) = 2
),
idx AS (  -- outcome index (1-based) of each token
  SELECT token, min(i) AS i FROM (
    SELECT ids[1] AS token, 1 AS i FROM mints UNION ALL SELECT ids[2], 2 FROM mints
  ) GROUP BY 1
),
res AS (
  SELECT conditionId, max_by(p, ts) AS p FROM (
    SELECT conditionId, evt_block_time AS ts, transform(payoutNumerators, x -> CAST(x AS double)) AS p
    FROM predict_bnb.conditionaltokens_evt_conditionresolution
    UNION ALL
    SELECT conditionId, evt_block_time, transform(payoutNumerators, x -> CAST(x AS double))
    FROM predict_bnb.yieldbearingconditionaltokens_evt_conditionresolution
  ) GROUP BY 1
),
j AS (
  SELECT tt.*, element_at(r.p, idx.i) / nullif(reduce(r.p, 0e0, (a, x) -> a + x, a -> a), 0) AS payout
  FROM tt
  LEFT JOIN idx ON idx.token = tt.token
  LEFT JOIN res r ON r.conditionId = tt.conditionId
)"""


def predict_leaders(start, end, limit=150):
    """predict.fun counterpart of poly_leaders; see _predict_pnl_ctes for the PnL. "Most traded"
    is the market of the wallet's largest single-outcome position. A rebate with no fill behind
    it in the window (fills = 0) adds to PnL but isn't a market traded."""
    return f"""{_predict_pnl_ctes(start, end)}
SELECT trader,
  sum(vol) AS volume,
  sum(notional) AS notional,
  sum(fills) AS fills,
  count(DISTINCT conditionId) FILTER (WHERE fills > 0) AS markets,
  sum(maker_vol) / nullif(sum(vol), 0) AS maker_share,
  sum(cashflow + net_shares * payout) FILTER (WHERE payout IS NOT NULL) AS realized_pnl,
  max_by(market, vol) AS top_market
FROM j
GROUP BY 1
HAVING sum(vol) > 0
ORDER BY volume DESC
LIMIT {limit}
"""


def predict_leaders_weekly(start, end, per_week=2000):
    """predict_leaders per Monday-start week in [start, end), each week's top `per_week`
    wallets, with maker and top-market volume so weeks add up (see poly_leaders_weekly)."""
    return f"""{_predict_pnl_ctes(start, end, by_week=True)},
agg AS (
  SELECT wk, trader,
    sum(vol) AS volume, sum(notional) AS notional, sum(fills) AS fills,
    count(DISTINCT conditionId) FILTER (WHERE fills > 0) AS markets, sum(maker_vol) AS maker_vol,
    sum(cashflow + net_shares * payout) FILTER (WHERE payout IS NOT NULL) AS realized_pnl,
    max_by(market, vol) AS top_market, max(vol) AS top_market_vol,
    row_number() OVER (PARTITION BY wk ORDER BY sum(vol) DESC) AS rk
  FROM j GROUP BY 1, 2
  HAVING sum(vol) > 0
)
SELECT wk, rk, trader, volume, notional, fills, markets, maker_vol, realized_pnl, top_market, top_market_vol
FROM agg WHERE rk <= {per_week}
"""


_PREDICT_FEE_MODULES = ", ".join([
    "0xF1f8F5C641F20C48526269EF7DFF19172Efa9783", "0xFBC2259Abb3f01c019ecE1d0200eE673Bb7BA34f",
    "0xF2311C668aAA8dEc48D5da577d3018eb94b3132F", "0xd172f3fbabe763ee8e52d8b32421574236da6057",
    "0x0989942f8E5b778E804858A0cC791b4469A5fD63", "0xf291a67165d751a2e7b4da4f0a012449eef0a279",
    "0xdcffeb0c30263888a48485a664ec9563a54891a1"])
_PREDICT_REFUNDED = "keccak(to_utf8('FeeRefunded(bytes32,address,uint256,uint256,uint256)'))"
_PREDICT_REBATED = "keccak(to_utf8('MakerRebateDistributed(address,uint256,uint256)'))"


def predict_fee_flows(start, end):
    """Per day and duration: the fee recorded on fills, what the fee modules refunded to the
    orders' owners (all of a maker's fee, 10% of a taker's), and what they paid makers as rebates. dur = '__ALL__' is every market on the platform.

    OrderFilled's `fee` is the most the order could be charged; FeeRefunded returns the excess in
    the same transaction, so the fee actually charged is fee - refund. Rebates come from
    MakerRebateDistributed. All in shares of the outcome token (token id != 0): a seller's fee is
    in USDT, which can't be traced to a market, so these are applied as ratios to the dollar fees.
    """
    window = f"block_date >= DATE '{start}' AND block_date < DATE '{end}' AND contract_address IN ({_PREDICT_FEE_MODULES})"
    fills = f"evt_block_date >= DATE '{start}' AND evt_block_date < DATE '{end}' AND fee > 0 AND takerAssetId <> 0"
    return f"""
WITH {_predict_markets_cte(start)},
scope AS (SELECT token, max(dur) AS dur FROM mk GROUP BY 1),
flows AS (
  SELECT block_date AS d, varbinary_to_uint256(substr(data, 1, 32)) AS tok, 0e0 AS fee,
    CAST(varbinary_to_uint256(substr(data, 33, 32)) AS double) / 1e18 AS refund, 0e0 AS rebate
  FROM bnb.logs WHERE {window} AND topic0 = {_PREDICT_REFUNDED}
  UNION ALL
  SELECT block_date, varbinary_to_uint256(topic2), 0e0, 0e0, CAST(varbinary_to_uint256(substr(data, 1, 32)) AS double) / 1e18
  FROM bnb.logs WHERE {window} AND topic0 = {_PREDICT_REBATED}
  UNION ALL
  SELECT evt_block_date, takerAssetId, CAST(fee AS double) / 1e18, 0e0, 0e0 FROM predict_bnb.ctfexchange_evt_orderfilled WHERE {fills}
  UNION ALL
  SELECT evt_block_date, takerAssetId, CAST(fee AS double) / 1e18, 0e0, 0e0 FROM predict_bnb.negriskctfexchange_evt_orderfilled WHERE {fills}
)
SELECT f.d, IF(grouping(s.dur) = 1, '__ALL__', s.dur) AS dur, sum(f.fee) AS fee_sh, sum(f.refund) AS refund_sh, sum(f.rebate) AS rebate_sh
FROM flows f LEFT JOIN scope s ON s.token = f.tok
WHERE f.tok <> 0
GROUP BY GROUPING SETS ((f.d, s.dur), (f.d))
HAVING grouping(s.dur) = 1 OR s.dur IS NOT NULL
"""
