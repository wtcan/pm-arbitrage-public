# Results: historical price gaps between Kalshi and Polymarket

Made by `notebooks/01_historical_gaps.ipynb` from the data in `data/processed/`
(155 verified pairs, November 2024 to 4 November 2026; for part 7 all 177
settled candidate pairs). Re-running the notebook rewrites this file. All
times UTC; gaps in cents per contract (one contract pays $1).

## Key findings

1. **Profitable-looking hours are common but tiny.** After both fees, the
   executable-style gap is positive in 26% of
   the 162,937 main-sample pair-hours (Fed 20%,
   CPI MoM 43%, CPI YoY 43%),
   with a median of 0.87 cents per contract.
   The share depends mostly on Polymarket's unobserved spread: from
   41% (no spread) to 17% (half spread 1 cent).
2. **Gaps close within hours.** The median half-life of a gap is
   5.3 hours
   (2.6 hours with fresh Polymarket prices only).
3. **Gaps are smaller near prices of 0 or 1 and larger in CPI markets**
   (-0.050 cents per cent of distance from 0.5;
   CPI MoM +1.06 and CPI YoY +1.61 cents compared with the Fed).
   More Polymarket volume goes with smaller gaps once outliers are capped.
4. **In Fed markets Kalshi tends to move first:** Kalshi leads in
   28% of the Fed pairs, Polymarket in
   13% (Granger tests, hourly).
5. **Resolution risk eats the profit.** 3 of 177 settled pairs paid
   differently, and 3 of them showed a positive fee-adjusted gap at the last
   moment both venues had prices. Trading every settled pair with a positive gap at that
   moment would have earned $0.25 instead of the
   $1.25 the gaps promised (32 contracts). Covering
   such losses needs about 1.3 cents per normal trade, more
   than the typical positive gap.

## Main choices

* **Grid:** one row per pair and hour (`bar_end`), from the first hour with a
  price on both venues to the last full hour before Kalshi's close (open pairs:
  until the Kalshi download, 2026-11-04 16:00 UTC).
* **Kalshi:** the last hourly candle carried forward without a time limit; an
  empty bid or ask stays empty; mid = (bid + ask) / 2.
* **Polymarket:** the latest trade of either token in Kalshi YES terms (using
  the pair's side), carried forward with its age; prices older than 24 hours are
  left out (main sample: Kalshi mid and a Polymarket price at most 24 h old,
  162,937 pair-hours, 153 pairs).
* **Raw gap** = Polymarket last trade − Kalshi mid.
* **Executable-style gap** = the better of the two directions: 1 − Kalshi ask (or
  1 − bid for NO) − Polymarket last trade ± half spread, minus both taker fees
  (`src/arb.py`, in force at each hour; Polymarket charges nothing before
  2026-03-30). **Half spread: 0.5 cents**, half the median
  spread (1 cent) of the 22 open
  paired Polymarket order books of 2026-11-04.
* Cross-check against Polymarket's own price series: correlation
  0.999, median difference
  0.45 cents.


## 1. Coverage

Shares of pair-hours.

| topic | pairs | pair-hours | Kalshi bid and ask | Polymarket price ≤ 24 h old | both (main sample) | Polymarket price < 1 h old | Polymarket price > 24 h old |
|---|---|---|---|---|---|---|---|
| Fed | 76 | 164,028 | 79.6% | 94.7% | 75.6% | 59.4% | 5.3% |
| CPI MoM | 37 | 24,416 | 73.4% | 89.3% | 68.9% | 26.6% | 10.7% |
| CPI YoY | 42 | 32,165 | 71.1% | 92.5% | 68.6% | 34.9% | 7.5% |

## 2-3. How often and how large

Main sample. *net* = fee-adjusted executable gap; (c) = cents.

| topic | pairs | pair-hours | share net > 0 | share net > 1 c | median pair's share net > 0 | median \|raw gap\| (c) | 95th pct \|raw gap\| (c) | median net when > 0 (c) | median annualised when > 0 |
|---|---|---|---|---|---|---|---|---|---|
| Fed | 76 | 124,043 | 20.2% | 7.6% | 16.9% | 1.30 | 5.50 | 0.76 | 5.0% |
| CPI MoM | 36 | 16,834 | 42.5% | 23.1% | 45.4% | 2.02 | 7.30 | 1.14 | 30.6% |
| CPI YoY | 41 | 22,060 | 43.0% | 24.6% | 38.7% | 2.10 | 9.55 | 1.15 | 27.4% |
| all | 153 | 162,937 | 25.6% | 11.5% | 29.4% | 1.50 | 6.10 | 0.87 | 9.1% |

### Share of pair-hours with a positive fee-adjusted gap, by time to resolution

| time to resolution | Fed | CPI MoM | CPI YoY | all | pair-hours |
|---|---|---|---|---|---|
| under 1 day | 9.0% | 37.8% | 44.8% | 30.6% | 1,742 |
| 1-7 days | 17.4% | 41.6% | 36.9% | 31.0% | 12,127 |
| 1-4 weeks | 21.8% | 42.1% | 42.9% | 33.2% | 50,691 |
| 1-3 months | 17.7% | 59.1% | 58.2% | 19.3% | 57,752 |
| over 3 months | 23.2% | – | – | 23.2% | 40,625 |

### Before / after Polymarket's fees, and by price level

|  | pairs | pair-hours | share net > 0 | share net > 1 c | median pair's share net > 0 | median \|raw gap\| (c) | 95th pct \|raw gap\| (c) | median net when > 0 (c) | median annualised when > 0 |
|---|---|---|---|---|---|---|---|---|---|
| fee period / before 30 Mar 2026 (no Polymarket fee) | 103 | 99,800 | 29.0% | 14.1% | 34.3% | 1.50 | 7.27 | 0.98 | 13.5% |
| fee period / from 30 Mar 2026 (Polymarket fees) | 66 | 63,137 | 20.1% | 7.5% | 21.1% | 1.20 | 4.50 | 0.68 | 4.2% |
| price level / Kalshi mid under 5 c | 74 | 39,906 | 20.1% | 4.5% | 20.5% | 0.80 | 2.70 | 0.40 | 4.3% |
| price level / 5 to 95 c | 135 | 112,582 | 27.0% | 14.3% | 31.3% | 1.86 | 7.11 | 1.07 | 10.4% |
| price level / over 95 c | 47 | 10,449 | 30.9% | 8.1% | 29.2% | 1.00 | 3.20 | 0.49 | 15.2% |

### Distribution of gaps (cents, percentiles over pair-hours)

|  | p1 | p5 | p25 | p50 | p75 | p95 | p99 |
|---|---|---|---|---|---|---|---|
| Fed / raw gap | -9.00 | -4.50 | -1.50 | -0.50 | 0.90 | 3.50 | 7.00 |
| Fed / fee-adjusted gap | -6.93 | -4.34 | -2.01 | -0.96 | -0.18 | 1.73 | 4.77 |
| CPI MoM / raw gap | -12.00 | -5.00 | -2.00 | 0.00 | 2.20 | 5.50 | 14.50 |
| CPI MoM / fee-adjusted gap | -5.70 | -3.37 | -1.34 | -0.27 | 0.87 | 3.61 | 11.72 |
| CPI YoY / raw gap | -13.00 | -7.00 | -2.50 | -0.67 | 1.50 | 5.08 | 26.52 |
| CPI YoY / fee-adjusted gap | -19.18 | -4.15 | -1.50 | -0.30 | 0.98 | 3.75 | 9.44 |
| all / raw gap | -10.00 | -4.80 | -1.50 | -0.50 | 1.00 | 4.00 | 8.50 |
| all / fee-adjusted gap | -7.83 | -4.24 | -1.89 | -0.84 | 0.03 | 2.26 | 6.04 |

### Sensitivity: share of hours with a positive fee-adjusted gap

Rows: maximum age of the Polymarket price. Columns: the assumed half spread.

|  | half spread 0.00 c | half spread 0.25 c | half spread 0.50 c | half spread 1.00 c |
|---|---|---|---|---|
| price at most 1 h old | 42.8% | 33.7% | 24.5% | 15.3% |
| price at most 6 h old | 41.4% | 33.3% | 25.3% | 16.6% |
| price at most 24 h old | 40.8% | 33.2% | 25.6% | 17.2% |

## 4. Persistence (AR(1) per pair)

Pairs with at least 100 pairs of neighbouring hours. Pooled AR(1) with pair fixed effects: rho = 0.892 (s.e. 0.015, clustered by pair), half-life 6.1 hours.

| topic | pairs | median rho | rho p25 | rho p75 | median half-life (h) | half-life p25 (h) | half-life p75 (h) | share under 1 h | share over 24 h |
|---|---|---|---|---|---|---|---|---|---|
| Fed | 72 | 0.90 | 0.80 | 0.93 | 6.90 | 3.20 | 9.68 | 4.2% | 1.4% |
| CPI MoM | 30 | 0.85 | 0.78 | 0.92 | 4.33 | 2.75 | 8.75 | 6.7% | 6.7% |
| CPI YoY | 34 | 0.83 | 0.76 | 0.90 | 3.79 | 2.48 | 6.85 | 5.9% | 2.9% |
| all | 136 | 0.88 | 0.77 | 0.93 | 5.34 | 2.70 | 9.40 | 5.1% | 2.9% |

Robustness (median across pairs):

|  | pairs Fed | pairs CPI MoM | pairs CPI YoY | pairs all | median rho Fed | median rho CPI MoM | median rho CPI YoY | median rho all | median half-life (h) Fed | median half-life (h) CPI MoM | median half-life (h) CPI YoY | median half-life (h) all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| main: raw gap | 72 | 30 | 34 | 136 | 0.90 | 0.85 | 0.83 | 0.88 | 6.90 | 4.33 | 3.79 | 5.34 |
| fresh Polymarket prices only (< 1 h) | 58 | 11 | 24 | 93 | 0.81 | 0.65 | 0.68 | 0.77 | 3.25 | 1.61 | 1.80 | 2.60 |
| fee-adjusted gap | 72 | 30 | 34 | 136 | 0.87 | 0.80 | 0.78 | 0.84 | 4.94 | 3.10 | 2.83 | 3.85 |

## 5. Determinants of the absolute gap

OLS; dependent variable |raw gap| in cents; standard errors clustered by pair in brackets; \* p < 0.1, \*\* p < 0.05, \*\*\* p < 0.01.

|  | (1) main | (2) + controls | (3) capped at p99 |
|---|---|---|---|
| const | 3.953\*\*\* | 1.176\*\* | 3.265\*\*\* |
|  | (0.917) | (0.472) | (0.600) |
| log Kalshi volume (24 h) | -0.042 | 0.088\*\*\* | -0.010 |
|  | (0.053) | (0.013) | (0.029) |
| log Polymarket volume (24 h) | -0.081\* | -0.101\*\*\* | -0.094\*\* |
|  | (0.049) | (0.024) | (0.037) |
| log(1 + days to resolution) | 0.156 | 0.290\*\*\* | 0.230\*\*\* |
|  | (0.145) | (0.058) | (0.081) |
| distance from 0.5 (cents) | -0.050\*\*\* | -0.026\*\*\* | -0.042\*\*\* |
|  | (0.008) | (0.003) | (0.005) |
| CPI MoM | 1.060\*\*\* | 1.171\*\*\* | 1.053\*\*\* |
|  | (0.288) | (0.241) | (0.253) |
| CPI YoY | 1.612\*\*\* | 1.123\*\*\* | 1.331\*\*\* |
|  | (0.440) | (0.231) | (0.322) |
| Polymarket price age (h) |  | -0.004 |  |
|  |  | (0.008) |  |
| Kalshi bid-ask spread (cents) |  | 0.264\*\*\* |  |
|  |  | (0.014) |  |
| pair-hours | 162,937 | 162,937 | 162,937 |
| pairs (clusters) | 153 | 153 | 153 |
| R-squared | 0.146 | 0.404 | 0.186 |

## 6. Price discovery (Granger causality, per pair)

Share of tested pairs in each class at the 5% level, 3 hourly lags, HC1-robust F-tests.

| topic | pairs tested | Kalshi leads | Polymarket leads | both | neither | no result |
|---|---|---|---|---|---|---|
| Fed | 61 | 27.9% | 13.1% | 21.3% | 37.7% | 10 |
| CPI MoM | 28 | 10.7% | 14.3% | 3.6% | 71.4% | 2 |
| CPI YoY | 33 | 15.2% | 6.1% | 6.1% | 72.7% | 1 |
| all | 122 | 20.5% | 11.5% | 13.1% | 54.9% | 13 |

Robustness (all tested pairs):

|  | pairs tested | Kalshi leads | Polymarket leads | both | neither |
|---|---|---|---|---|---|
| main: 3 lags | 122 | 20.5% | 11.5% | 13.1% | 54.9% |
| 1 lag | 122 | 23.8% | 11.5% | 3.3% | 61.5% |
| 6 lags | 122 | 22.1% | 11.5% | 15.6% | 50.8% |
| 3 lags, fresh prices only | 52 | 30.8% | 9.6% | 25.0% | 34.6% |

## 7. Resolution risk (177 settled candidate pairs)

3 of 177 pairs paid differently (1.7%; 95% confidence interval 0.4% to 4.9%).

| verdict | paid differently | paid the same | all |
|---|---|---|---|
| same | 0 | 133 | 133 |
| unsure | 0 | 41 | 41 |
| different | 3 | 0 | 3 |
| all | 3 | 174 | 177 |

The position with the better fee-adjusted gap at the last common price point (Polymarket price history, settlement rows dropped):

| pair_id | time (UTC) | Kalshi bid / ask | Polymarket price | chosen direction | fee-adjusted gap (c) | cost of chosen ($) | paid ($) | profit of chosen ($) | profit of the other direction ($) |
|---|---|---|---|---|---|---|---|---|---|
| KXCPIYOY-25OCT-T3.1 / 654401 | 2025-11-13 12:00 | 0.53 / 0.58 | 0.0700 | Kalshi NO + Polymarket YES | 43.7600 | 0.5624 | 0 | -0.5624 | 0.4679 |
| KXFEDDECISION-26SEP-H0 / 2118744 | 2026-05-11 12:00 | 0.72 / 0.73 | 0.7900 | Kalshi YES + Polymarket NO | 3.2800 | 0.9672 | 0 | -0.9672 | 0.9027 |
| KXFEDDECISION-26SEP-H25 / 2118745 | 2026-05-11 12:00 | 0.07 / 0.08 | 0.0950 | Kalshi YES + Polymarket NO | 0.0800 | 0.9992 | 2 | 1.0008 | -1.0391 |

A trader who enters every settled pair with a positive fee-adjusted gap at the last common point (one contract each):

|  | pairs | expected profit, sum of gaps ($) | realised profit ($) | capital ($) |
|---|---|---|---|---|
| paid differently | 3 | 0.4711 | -0.5289 | 2.5289 |
| paid the same | 29 | 0.7744 | 0.7744 | 28.2256 |
| all traded | 32 | 1.2455 | 0.2455 | 30.7545 |

Break-even: if every divergence lost the stake (on average $0.76), each normal trade would have to earn 1.32 cents; the median positive fee-adjusted gap is 0.87 cents.

## Figures

* `figures/gap_over_time_examples.png` (and `.pdf`): raw and fee-adjusted gap in the last 30 days of one
  pair per topic.
* `figures/gap_histogram.png`: distribution of raw and fee-adjusted gaps.
* `figures/half_life_distribution.png`: half-lives of the gaps, per topic.


## Limitations

1. **Polymarket's executable price is estimated.** There is no historical
   Polymarket order book, only trades. The ask is the last trade + half a
   spread of 0.5 cents, taken from one order-book snapshot (4 November 2026).
   Thin CPI markets probably had wider spreads. Choosing the better of two
   directions also picks up whichever error looks profitable. The
   fee-adjusted gap is therefore an **upper bound**, and the sensitivity table
   shows how much it depends on the spread.
2. **No size.** Kalshi's candles have the best quotes but not the number of
   contracts at them, so we cannot say how much could have been traded.
3. **Hourly timing.** Kalshi's quote is the one at the end of the hour;
   Polymarket's last trade can be from any time in the previous 24 hours.
   Moves within the hour, and leads of a few minutes, are invisible.
4. **Odd single trades.** A few tiny Polymarket trades far from the market
   ("fat fingers", the first trades of a new market) create gaps of 50 cents or
   more. Medians, shares and a capped regression limit their influence, but
   they remain in the data.
5. **Selection on the outcome.** The 155 verified pairs were checked using
   their realised settlements, so pairs that paid differently were removed
   *after the fact*. Parts 1-6 therefore understate the resolution risk a
   trader faced at the time; part 7 adds those pairs back.
6. **Fees.** Kalshi's formula (0.07 × P × (1 − P), multiplier 1) is assumed to
   hold over the whole sample: Kalshi's fee-change history, which only starts
   in October 2025, has no change for these series. Polymarket's current fee
   schedule is assumed to apply from 30 March 2026. Fees are before rounding.
   Not included: maker rebates, deposit and withdrawal costs, and the interest
   the locked-up capital could have earned elsewhere.
7. **Persistence and price discovery.** The AR(1) assumes one constant speed
   of adjustment per pair. Bid-ask bounce makes gaps look less persistent;
   stale prices make them look more persistent. Granger tests compare a
   *quote* (Kalshi's mid) with a *trade* (Polymarket), find leads only at the
   hourly frequency, and give about 5% false positives per direction.
8. **Associations, not causes.** The regression shows which conditions go
   together with large gaps; busy hours are also hours with news.
9. **Few divergences.** Part 7 rests on 3 divergent pairs (two from the same
   faulty Polymarket event), so the divergence rate is imprecise; it uses
   Polymarket's own price series, whose exact definition the API does not give.

