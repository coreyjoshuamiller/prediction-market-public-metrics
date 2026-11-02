"""Maker rebates, for the Net view of the fee charts. Only maker rebates: referral rewards, taker
rebates, liquidity rewards and market-maker programmes are deliberately left out.

  Polymarket Intl  rate schedule below for in-scope markets (docs.polymarket.com/market-makers/
                   maker-rebates gives today's rates, no history; the dates come from rebates actually
                   paid); platform total from on-chain payouts (data/poly_rebates_daily.csv)
  Polymarket US    coefficient schedule from its CFTC filings and docs.polymarket.us/fees
  Predict.fun      measured from its fee contracts (data/predict_fee_flows.csv, see build_site)
  Kalshi           none: in-scope series have fee_type "quadratic" and the fee schedule has no
                   per-fill maker rebate, so net = gross
"""
from .pmus import fee_rate


def poly_rate(d, cls):
    """Share of the taker fee Polymarket Intl paid to makers. From on-chain payouts per day: all of
    it for fills through 2026-01-14, then 20% on crypto (flat through the crypto-only weeks; per-market
    checks on 2026-06-20 and 2026-10-20 gave 19.4% and 19.8%). Non-crypto price markets take the
    published finance rate, 25%; they're well under 1% of in-scope fees and weren't checked directly."""
    if cls == "Crypto":
        return 1.0 if d < "2026-01-15" else 0.20
    return 0.25


def pmus_coef(d):
    """Maker rebate as a share of Polymarket US's estimated taker fee on a date.

    Fee = theta x contracts x p(1-p). The rebate coefficient stayed 0.0125 when theta rose in July
    and September. From 2026-10-19 13:00 ET it is tiered by each maker's prior-month maker volume
    (0, 0.0035, 0.0070, 0.0104), which the public trade files can't show, so this assumes the top
    tier: a low estimate of net. The separate market-maker programme (rates redacted) is excluded.
    """
    theta, kind = fee_rate(d)
    if not theta:
        return 0.0
    if kind == "flat":  # before the p(1-p) curve: basis points of premium
        return 1.0 if d < "2026-03-09" else 20 / 30  # 10bp/10bp, then 20bp/30bp (2026-03-06 filing)
    return (0.0104 if d >= "2026-10-20" else 0.0125) / theta
