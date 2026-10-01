# Short-Term Price Markets dashboard

Polymarket vs Kalshi metrics for contracts that settle on a financial price within one day:
Up/Down (5m, 15m, 1h, 4h, daily), above/below strike ladders, price ranges and same-day
touch markets, across crypto, equities, commodities, and FX/rates.

The site is static (`site/`) and served by GitHub Pages. Two GitHub Actions keep it fresh:

| Workflow | Schedule | What it does |
|---|---|---|
| `daily.yml` | triggered from cron (13:40, 16:40, 19:40, 22:40 UTC) and by the order-book chain when data is stale | Runs `pipeline/refresh.py`, which picks **full** (first run of the UTC day: Dune history, fees, Kalshi, Polymarket US, leaderboards), **light** (a source is still missing yesterday: re-pull Kalshi and Polymarket US, ~1 credit, max 4 attempts/day) or **skip**. Then commits and deploys |
| `orderbook.yml` | continuous: each run dispatches the next (cron every 3h restarts the chain if it breaks) | Samples the live 15m BTC/ETH/SOL/XRP order books once a minute for ~54 minutes, commits, deploys, and after 13:40 UTC triggers `daily.yml` if data is stale |
| `deploy.yml` | on pushes to `site/` code | Redeploys the page |

GitHub's cron is best-effort (runs can be hours late or dropped), which is why the daily refresh has several triggers and decides for itself whether to do anything. State lives in `data/refresh_state.json`. To force a run, use **Actions → Daily refresh → Run workflow** and pick a mode.

## What's on the page

- **Volume by market**: stacked by market (asset × duration), asset, duration, asset class or contract type. Day, week or month buckets, over the last 30 days to 12 months.
- **Traders per market**: unique Polymarket wallets, makers and takers, per period. Kalshi doesn't publish account-level trades.
- **Share of platform volume**: in-scope volume as a % of each venue's total (Polymarket Intl, Polymarket US, Kalshi), plus a head-to-head of the venues.
- **Top traders**: Polymarket wallets ranked from on-chain fills (7d daily, 30d weekly), with realized PnL, maker %, and linked X handles from Polymarket profiles. Kalshi uses its public Crypto and Financials leaderboards.
- **Order book liquidity**: dollars at the best bid and offer, depth within 5¢, spread, and how depth evolves through the 15-minute window.

## Data sources

| Data | Source |
|---|---|
| Polymarket Intl (on-chain) trades, markets, resolutions | Dune `polymarket_polygon.market_trades` / `market_details` |
| Polymarket US (CFTC-regulated, off-chain) trades | Public daily time-and-sales files: `polymarketexchange.com/files/time-and-sales/manifest.json` |
| Kalshi per-series daily contracts and cash volume, platform totals | Dune `kalshi.market_report` / `kalshi.trade_report` |
| Kalshi series metadata | `api.elections.kalshi.com/trade-api/v2/series` |
| Kalshi leaderboard | `api.elections.kalshi.com/v1/social/leaderboard` (unofficial) |
| Polymarket profiles (name, X handle) | `gamma-api.polymarket.com/public-profile` |
| Order books | Polymarket CLOB `/book`, Kalshi `/markets/{ticker}/orderbook` |

## Definitions

- **Notional** counts contracts traded, each worth $1 at settlement. For Polymarket it's taker-side shares per fill; for Kalshi it's contracts. This is the default because it's comparable across venues.
- **Cash** counts dollars paid: the Polymarket taker's USDC, and Kalshi yes-price × contracts.
- Polymarket.com shows roughly 2× these numbers, because it counts both legs of every trade.
- **Polymarket US** is a separate, off-chain exchange that Dune can't see. It's ingested from the exchange's daily time-and-sales files (`pipeline/pmus.py`), which have symbol, price and quantity but no account IDs or taker side. So it has volume only: no trader counts or leaderboard, and cash is price × quantity. Sessions run 5pm–5pm ET, so the latest UTC day fills in a day later.
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
python3 -m pipeline.orderbook_snapshot             # one order-book sample
python3 -m pipeline.build_orderbook
python3 -m http.server 8765 --directory site
```

No third-party Python packages are needed.

## Costs

- **Dune:** about 30–35 credits for each day's full refresh (more on the 1st–3rd of a month, which re-finalizes the previous month), so roughly 1,000 a month. Light retries cost about 1 credit.
- **GitHub Actions:** free, because the repo is public.
