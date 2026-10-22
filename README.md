# Short-Term Price Markets dashboard

Polymarket vs Kalshi vs Predict.fun metrics for contracts that settle on a financial price within one day:
Up/Down (5m, 15m, 1h, 4h, daily), above/below strike ladders, price ranges and same-day
touch markets, across crypto, equities, commodities, and FX/rates.

The site is static (`site/`) and served by GitHub Pages. Two GitHub Actions keep it fresh:

| Workflow | Schedule | What it does |
|---|---|---|
| `daily.yml` | triggered from cron (13:40, 16:40, 19:40, 22:40 UTC) and by the order-book chain when data is stale | Runs `pipeline/refresh.py`, which picks **full** (first run of the UTC day: Dune history, fees, Kalshi, Polymarket US, Predict.fun, leaderboards), **light** (a source is still missing yesterday: re-pull Kalshi and Polymarket US, ~1 credit, plus Predict.fun if it's the one missing; max 4 attempts/day) or **skip**. Then commits and deploys |
| `orderbook.yml` | continuous: each run dispatches the next (cron every 3h restarts the chain if it breaks) | Samples the live 15m BTC/ETH/SOL/XRP/BNB order books (Polymarket, Kalshi, Predict.fun) once a minute for ~54 minutes, commits, deploys, and after 13:40 UTC triggers `daily.yml` if data is stale |
| `deploy.yml` | on pushes to `site/` code | Redeploys the page |

GitHub's cron is best-effort (runs can be hours late or dropped), which is why the daily refresh has several triggers and decides for itself whether to do anything. State lives in `data/refresh_state.json`. To force a run, use **Actions → Daily refresh → Run workflow** and pick a mode.

## What's on the page

- **Volume by market**: stacked by market (asset × duration), asset, duration, asset class or contract type. Day, week or month buckets, over the last 30 days to 12 months.
- **Traders per market**: unique Polymarket Intl and Predict.fun wallets, makers and takers, per period. Kalshi and Polymarket US don't publish account-level trades.
- **Share of platform volume**: in-scope volume as a % of each venue's total (Polymarket Intl, Polymarket US, Kalshi, Predict.fun), plus a head-to-head of the venues.
- **Volume concentration**: how Polymarket Intl and Predict.fun volume is spread across wallets (7d and 30d windows): share of volume by the top X% of wallets, percentile tiers, wallet-size bands, Gini, and a weekly trend of top 1% / 5% / 10% share. Refreshed weekly by `pipeline/concentration.py`.
- **Top traders**: Polymarket and Predict.fun wallets ranked from on-chain fills (7d daily, 30d weekly), with realized PnL, maker %, and names / linked X handles from each venue's profiles. Kalshi uses its public Crypto and Financials leaderboards. Predict.fun has no outcome table on Dune, so its PnL reads each token's outcome from mint order (the exchange splits collateral as [Up, Down]; see `queries.predict_leaders`).
- **Order book liquidity**: dollars at the best bid and offer, depth within 5¢, spread, and how depth evolves through the 15-minute window.

## Data sources

| Data | Source |
|---|---|
| Polymarket Intl (on-chain) trades, markets, resolutions | Dune `polymarket_polygon.market_trades` / `market_details` |
| Polymarket US (CFTC-regulated, off-chain) trades | Public daily time-and-sales files: `polymarketexchange.com/files/time-and-sales/manifest.json` |
| Kalshi per-series daily contracts and cash volume, platform totals | Dune `kalshi.market_report` / `kalshi.trade_report` |
| Predict.fun (BNB Chain) fills, Up/Down markets, platform totals | Dune `predict_bnb.*` (decoded contracts): `ctfexchange_evt_orderfilled`, the Chainlink Up/Down adapter's `roundcreated`, UMA `questioninitialized` titles, `conditionpreparation`, `tokenregistered` |
| Kalshi series metadata | `api.elections.kalshi.com/trade-api/v2/series` |
| Kalshi leaderboard | `api.elections.kalshi.com/v1/social/leaderboard` (unofficial) |
| Polymarket profiles (name, X handle) | `gamma-api.polymarket.com/public-profile` |
| Order books | Polymarket CLOB `/book`, Kalshi `/markets/{ticker}/orderbook`, Predict.fun GraphQL `category(id: slug) { markets { orderbook } }` |
| Predict.fun account names, X handles | `graphql.predict.fun` `account(address:)` (needs an EIP-55 checksummed address; `pipeline/predict_api.py`) |

## Definitions

- **Notional** counts contracts traded, each worth $1 at settlement. For Polymarket it's taker-side shares per fill; for Kalshi it's contracts. This is the default because it's comparable across venues.
- **Cash** counts dollars paid: the Polymarket taker's USDC, and Kalshi yes-price × contracts.
- Polymarket.com shows roughly 2× these numbers, because it counts both legs of every trade.
- **Polymarket US** is a separate, off-chain exchange that Dune can't see. It's ingested from the exchange's daily time-and-sales files (`pipeline/pmus.py`), which have symbol, price and quantity but no account IDs or taker side. So it has volume only: no trader counts or leaderboard, and cash is price × quantity. Sessions run 5pm–5pm ET, so the latest UTC day fills in a day later.
- **Predict.fun** is a Polymarket-style exchange on BNB Chain (`pipeline/queries.py`, `predict_activity`). Only its Up/Down markets are in scope, found on-chain: 5m/15m rounds from its Chainlink Up/Down adapter (asset from the price feed ID, `markets.PREDICT_FEEDS`) and hourly/daily markets from UMA question titles (`markets.PREDICT_TITLE_WORDS`). Notional is taker-side shares, cash the taker's USDT, fees the on-chain `fee` on every fill (a buyer's fee is in shares, valued at the fill price). A feed or title that isn't mapped yet shows up as an "Other"-class asset and a warning in the refresh log; look up its name via the market's `oracleQuestionId` on `graphql.predict.fun` and add it. History starts with its first Up/Down market in November 2025.
- Trader counts use Dune's `approx_distinct`, accurate to about 2%.
- Days are UTC. The current partial day is excluded.

## Changing what's in scope

Everything is in `pipeline/markets.py`:
- `ASSETS` maps slug and ticker tokens to an asset label and asset class.
- `POLY_PATTERNS` holds the Polymarket slug regexes.
- `kalshi_classify()` and `KALSHI_SERIES_EXCLUDE` control which Kalshi series count.

After changing scope, re-run the backfill so history is consistent.

## Running locally

```bash
echo "DUNE_API_KEY=..." > .env           # never committed
python3 -m pipeline.update --backfill 2025-09-01   # one-time, ~160 Dune credits
python3 -m pipeline.update --daily                 # incremental, ~10–25 credits
python3 -m pipeline.pmus --all                     # one-time Polymarket US backfill (~15GB streamed, free)
python3 -m pipeline.update --backfill-predict 2025-11-01   # one-time Predict.fun backfill, ~75 Dune credits
python3 -m pipeline.orderbook_snapshot             # one order-book sample
python3 -m pipeline.build_orderbook
python3 -m http.server 8765 --directory site
```

No third-party Python packages are needed.

## Costs

- **Dune:** about 30–35 credits for each day's full refresh (more on the 1st–3rd of a month, which re-finalizes the previous month), plus 4–13 for Predict.fun (it re-reads month to date for unique-trader counts, so it grows through the month), so roughly 1,250 a month. Light retries cost about 1 credit.
- **Volume concentration:** about 16 credits on Mondays (7d and 30d windows) plus about 1 per new week, so roughly 70 a month. The one-time weekly backfill from 2025-09-01 cost about 25. Predict.fun adds a similar amount; its snapshot pulls the wallet list once and computes the stats in Python, because Dune re-runs a CTE for every reference.
- **GitHub Actions:** free, because the repo is public.
