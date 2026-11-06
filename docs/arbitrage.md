# Fee-adjusted arbitrage between Kalshi and Polymarket

`src/arb.py` checks each verified pair (`data/processed/pairs.parquet`, see
[matching.md](matching.md)) for an arbitrage: two contracts that together
always pay $1 but cost less than $1, **after both venues' trading fees**. This
page gives the fee formulas and their sources, the assumptions, the columns of
the output, and a worked example to check by hand. Numbers are from
2026-11-04.

## How to run it

```bash
python src/arb.py --download
```

Downloads fresh order books for the three example pairs, with both venues
less than a second apart. It saves them to
`data/processed/arb_examples.parquet` and prints one row per pair.

```bash
python src/arb.py
```

Prints the rows again from the saved order books, without downloading.

```bash
python src/arb.py --explain "KXFEDDECISION-26DEC-H25 / 3215008"
```

Also prints every step of one pair's calculation.

In Python or a notebook, `arb.snapshot_row(pair, kalshi_levels, poly_levels, poly_market)`
turns any Kalshi and Polymarket order-book snapshots of one pair into one row.
The order books are tables as made by `kalshi.parse_orderbook()` and
`polymarket.parse_orderbook()`.

## The idea

A pair is one outcome listed on both venues. Its `poly_yes_equals` column
(called "side" in the review sheet) says how the two YES contracts relate.
The two legs of each direction always pay exactly $1 together, because exactly
one of them wins:

| side (`poly_yes_equals`) | direction 1 | direction 2 |
|---|---|---|
| `Kalshi YES` (Polymarket YES = Kalshi YES) | Kalshi YES + Polymarket NO | Kalshi NO + Polymarket YES |
| `Kalshi NO` (Polymarket YES = Kalshi NO) | Kalshi YES + Polymarket YES | Kalshi NO + Polymarket NO |

We always **buy at the ask**, the cheapest offer already in the book, never
at the mid or the last price. That makes us the *taker*, who pays the taker
fee on both venues.

* **Top of book, per contract.**
  - gross gap = 1 − Kalshi ask − Polymarket ask
  - net profit per contract = gross gap − Kalshi fee − Polymarket fee

  The fees are those of a real order for all contracts on offer at *both* best
  asks (the smaller of the two sizes), divided by that number.
* **Only one direction can be positive.** The two gross gaps add up to minus
  the two venues' bid-ask spreads. If one direction makes money, the other
  loses at least as much. Each row therefore shows the better direction.
* **Depth.** The code buys the cheapest offers first, the same number of
  contracts on both venues, one step at a time. A step ends when an offer on
  either venue is used up. It stops at the first step whose profit per
  contract (before rounding) would be ≤ 0. Prices, and price + fee, only rise
  as the walk goes down the books, so every later step would lose money too.
  `max_size` is therefore the **profit-maximizing** size, and `total_profit`
  the highest total profit. The totals are computed exactly, with all
  rounding, after each step.
* **Polymarket's NO book is the mirror image of its YES book**: an offer to
  sell NO at p is a bid to buy YES at 1 − p, so both books show the same
  orders. The code only uses the asks of the token it buys (YES from the YES
  book, NO from the NO book) and never adds YES and NO depth together.
  Kalshi lists only bids, so its YES asks are its NO bids at 1 − p.
* **Return on capital** = profit / capital locked. Both venues want the full
  price plus fee up front (no borrowing), so the capital is everything paid.
  It stays locked until both markets pay out.
  - **Annualised:** simple, not compounded: return × 365 / days until resolution.
  - **Resolution time:** the later of Kalshi's close time and Polymarket's end
    date.

## Fees

### Kalshi

Source: Kalshi fee schedule, https://kalshi.com/docs/kalshi-fee-schedule.pdf,
version effective 7 July 2026.

> **The PDF was verified manually.** The cloud session could not download it.
> Kalshi's website answered HTTP 429 with a "Vercel Security Checkpoint" page
> (a browser check that needs JavaScript), also after retries with backoff.
> The help centre (`help.kalshi.com`) and the CFTC copies of Kalshi's filings
> (`www.cftc.gov`) are blocked by the environment's network. The facts below
> were read from the PDF by hand on 2026-11-04 and given to the code.

* **Taker fee** = round up(M × 0.07 × C × P × (1 − P)). C = contracts, P = price
  in dollars, M = the series' taker multiplier, default 1.
* **Maker fee** = round up(M × 0.0175 × C × P × (1 − P)). The default maker
  multiplier is 0, but the CPI, Fed funds, GDP and jobs series are listed with
  1 or more. **It never applies here:** we only take offers that are already
  in the book.
* **Rounding:** fee + cost of the position is rounded to a **centicent**
  ($0.0001). Kalshi's API docs explain how
  (https://docs.kalshi.com/getting_started/fee_rounding.md):
  - each fill's fee is first rounded up to $0.000001;
  - cost + fee is then rounded against the trader to the account's precision:
    $0.0001 for direct members, $0.01 for accounts held through a broker (FCM);
  - a "fee accumulator" carries the rounding over all fills of one order. An
    order that sweeps several price levels therefore pays what one fill of the
    same size would cost, rounded once.
* **Table check:** 100 contracts at $0.55 → $1.74 fee.

**The two rounding facts disagree.**
- With centicent rounding, 100 contracts at $0.55 give 0.07 × 100 × 0.55 × 0.45
  = $1.7325, and 55.00 + 1.7325 = 56.7325 is already a whole number of
  centicents, so the fee is **$1.7325**.
- The table's **$1.74** is the fee rounded up to a whole cent.

Decision (2026-11-04): `kalshi_fee()` uses the **centicent** rule by default.
`precision=arb.CENT` gives whole cents and reproduces the table; both values
are unit tests. The difference is at most $0.01 per order.

**Multiplier.** M = 1 for all six of our series (KXFEDDECISION, KXCPI,
KXCPIYOY and the older FEDDECISION, CPI, CPIYOY). Checked on 2026-11-04 with
Kalshi's public API:
- `GET /series/{ticker}` reports `fee_multiplier` = 1 for each. Its `fee_type`
  is `quadratic_with_maker_fees` for the three KX series and `quadratic` for
  the old ones
  ([docs](https://docs.kalshi.com/api-reference/market/get-series)).
- The fee-change history (`GET /series/fee_changes?show_historical=true`,
  107 changes from 2025-10-04 to 2026-09-03) has no change for any of them
  ([docs](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes)).
- None of our 63 events has an event-level override (`GET /events/fee_changes`,
  [docs](https://docs.kalshi.com/api-reference/events/get-event-fee-changes)).

The multipliers are in `KALSHI_TAKER_MULTIPLIERS` in `src/arb.py`. A series
that the schedule lists with another taker multiplier belongs there.

### Polymarket

Source: https://docs.polymarket.com/trading/fees.md (read 2026-11-04).

* **Fee** = C × feeRate × p × (1 − p), C = shares, p = price. "Makers are
  never charged fees. Only takers pay fees."
* **Rounding:** "Fees are rounded to 5 decimal places" (the smallest fee is
  0.00001 USDC). We round each price level's fee half-up. A level can hold
  several orders, which changes the total by at most $0.00001 per order.
* **Each market's own fee fields:** fees must be calculated from the market's
  `feeSchedule` (changelog, Mar 31, 2026). In `poly_markets.parquet` these are
  `fees_enabled`, `fee_rate`, `fee_exponent` and `fee_taker_only`.
* **The exponent** no longer appears in the fees page's formula. The
  market-details page calls it the "exponent applied to the price component of
  the fee curve"
  ([docs](https://docs.polymarket.com/market-data/market-details.md#trading-fees)).
  The code therefore uses **C × feeRate × (p × (1 − p))^exponent**. Every
  market in our data has exponent 1, so this is exactly the published formula.
* **Dates:** "Fee Structure V2" brought fees to Economics markets (the Fed and
  CPI) on **30 March 2026** (changelog, Mar 30, 2026).
  - A market without a fee schedule pays **no fee**.
  - Any trade before 2026-03-30 00:00 UTC pays **no fee**.
* **ASSUMPTION:** a market's *current* fee schedule applies from 2026-03-30
  00:00 UTC on. Neither the docs nor Gamma's market records say when it
  started. Gamma's record has the schedule and a `feeType` label, but no date
  for the schedule. For the fresh order books of the worked example the
  current schedule is exactly right.
* **ASSUMPTION:** the fee is paid in USDC on top of the price ("Taker fees are
  calculated in USDC"), so buying C shares gives C shares.

The fee schedules of the 155 paired Polymarket markets (as downloaded on
2026-11-04):

| schedule | markets | Gamma `feeType` | which |
|---|---|---|---|
| none (`fees_enabled` False) | 86 | none | all closed by 10 April 2026; two of them (the March 2026 CPI YoY markets 1374255 and 1374263) were still trading after 30 March, and their own fields say they charged no fee |
| rate 0.05, exponent 1 | 60 | `economics_fees` | from the April 2026 Fed meeting (closed 29 April 2026) on, open pairs included |
| rate 0.03, exponent 1 | 4 | `sports_fees_v2` | four March 2026 CPI markets (created 11 March 2026), labelled with a *sports* fee type |
| rate 0, exponent 1 | 5 | `zero_fees` | the October 2026 Fed meeting |

## Columns of a row

`snapshot_row()` returns these columns. Money is in dollars, sizes are in
contracts (one contract pays $1), times are UTC, and returns are fractions
(0.02 = 2%).

| column | meaning |
|---|---|
| `timestamp` | the later of the two order-book snapshots |
| `pair_id` | "Kalshi ticker / Polymarket market id" |
| `direction` | the better direction, e.g. "Kalshi NO + Polymarket YES" |
| `gross_gap` | 1 − Kalshi ask − Polymarket ask (best asks) |
| `fees` | both taker fees per contract at the best asks |
| `net_profit_per_contract` | `gross_gap` − `fees` |
| `max_size` | the profit-maximizing number of contracts on each venue (0 = no trade) |
| `total_profit` | profit of buying `max_size` contracts while walking down both books |
| `annualised_return` | `return_on_capital` × 365 / `days_to_resolution` (empty if `max_size` is 0) |
| `kalshi_ask`, `poly_ask`, `top_size` | the two best asks and the contracts on offer at both |
| `kalshi_fee_per_contract`, `poly_fee_per_contract` | the two parts of `fees` |
| `capital_locked` | all cash paid for `max_size` contracts, fees included |
| `return_on_capital` | `total_profit` / `capital_locked` |
| `top_return_on_capital`, `top_annualised_return` | the same for one contract at the best asks (also when it loses money) |
| `days_to_resolution` | from `timestamp` to the later of Kalshi's close time and Polymarket's end date |
| `kalshi_snapshot_time`, `poly_snapshot_time`, `seconds_apart` | when each book was seen, and how far apart |

The order books in `data/processed/kalshi_orderbooks.parquet` (16:08 to 16:10
UTC) and `poly_orderbooks.parquet` (19:42 to 19:43 UTC on 2026-11-04) were
taken about 3.5 hours apart. **Do not combine them**: prices move in between.
`arb_examples.parquet` has both venues within a second.

## Worked example (order books of 2026-11-04, 23:37 UTC)

`data/processed/arb_examples.parquet` holds the fresh order books of three open
pairs. For each pair the two venues' books were taken 0.67 to 0.94 s apart:

| pair | better direction | gross gap | fees per contract | net per contract | max size | total profit | annualised |
|---|---|---|---|---|---|---|---|
| `KXCPI-26OCT-T0.1 / 5691223` (side `Kalshi NO`) | Kalshi NO + Polymarket NO | −0.01 | 0.011354 | −0.021354 | 0 | 0 | – |
| `KXFEDDECISION-26DEC-H0 / 3215007` | Kalshi YES + Polymarket NO | 0.00 | 0.019908 | −0.019908 | 0 | 0 | – |
| `KXFEDDECISION-26DEC-H25 / 3215008` | Kalshi NO + Polymarket YES | **+0.02** | 0.019780 | **+0.000220** | **8,265.49** | **$1.8184** | **0.23%** |

### Step by step: `KXFEDDECISION-26DEC-H25 / 3215008`

Kalshi "Will the Federal Reserve Hike rates by 25bps at their December 2026
meeting?" and Polymarket "Will the Fed increase interest rates by 25 bps after
the December 2026 meeting?". The side is `Kalshi YES`: the two YES contracts
are the same outcome. Order books were taken at 23:37:35.215 UTC (Kalshi) and
23:37:35.932 UTC (Polymarket), 0.72 s apart.

**0. Which direction.** The other direction (Kalshi YES 0.81 + Polymarket NO
0.23 = 1.04) loses 4 cents before fees. We buy **Kalshi NO + Polymarket YES**:
if the Fed hikes by 25 bp, Polymarket YES pays $1; if not, Kalshi NO pays $1.

**1. Best asks.**
- Kalshi NO: $0.20, 8,265.49 contracts. Kalshi's book only has bids, so this
  is its best YES bid of $0.80 seen from the other side.
- Polymarket YES: $0.78, 151,465.13 shares.
- gross gap = 1 − 0.20 − 0.78 = **0.02** per contract.

**2. Fees, for the 8,265.49 contracts on offer at both best asks.**

*Kalshi:*
- fee = 1 × 0.07 × 8,265.49 × 0.20 × 0.80 = 92.573488
- cost = 8,265.49 × 0.20 = 1,653.098
- 1,653.098 + 92.573488 = 1,745.671488, rounded up to $0.0001 = 1,745.6715
- **fee = $92.5735**

*Polymarket:*
- 8,265.49 × 0.05 × (0.78 × 0.22) = 70.9179042, rounded to 5 decimals
- **fee = $70.91790**

*Per contract:*
- fees = (92.5735 + 70.9179) / 8,265.49 = **0.01978**
  (that is 0.07 × 0.2 × 0.8 = 0.0112 plus 0.05 × 0.78 × 0.22 = 0.00858)
- net profit per contract = 0.02 − 0.01978 = **0.00022**

**3. Depth.**
- Step 1 buys all 8,265.49 Kalshi contracts at $0.20 (and as many Polymarket
  shares at $0.78). Profit per contract before rounding: 0.00022.
- The next step would pay Kalshi's next level, $0.21: 1 − 0.21 − 0.78 −
  0.011613 − 0.00858 = −0.010193 per contract, so the walk stops.
- **max size = 8,265.49** contracts on each venue.

| | cost | fee |
|---|---|---|
| Kalshi: 8,265.49 × 0.20 | 1,653.098 | 92.5735 |
| Polymarket: 8,265.49 × 0.78 | 6,447.0822 | 70.9179 |

- capital = 1,653.098 + 92.5735 + 6,447.0822 + 70.9179 = **$8,263.6716**
- payout at resolution = 8,265.49 × $1 = $8,265.49
- **total profit = 8,265.49 − 8,263.6716 = $1.8184**

**4. Return.**
- return on capital = 1.8184 / 8,263.6716 = 0.00022005 = **0.022%**
- days until resolution: from 2026-11-04 23:37:35.9 UTC to Polymarket's end
  date 2026-12-09 23:59 UTC (later than Kalshi's close at 18:59) = 35.0149 days
- annualised = 0.00022005 × 365 / 35.0149 = **0.23% a year**

**What it means.** The prices differ by 2 cents, but the two taker fees take
1.978 cents of it. What is left is about $1.82 on $8,264 tied up for five
weeks: far less than the same money would earn in a safe interest-bearing
account, and not enough to cover the risk that the two venues settle
differently (see [matching.md](matching.md)). Run
`python src/arb.py --explain "<pair_id>"` to print these steps for any of the
three pairs.

## Limitations

* **Resolution risk:** the arbitrage is riskless only if both venues settle
  the same way. [matching.md](matching.md) lists the cases in which they did
  not (e.g. the October 2025 CPI).
* **Timing:** each pair's two snapshots are 0.67 to 0.94 s apart, so a book can
  still change in between. A real trade also needs time to send two orders.
* **Not included:**
  - the interest the capital could earn elsewhere;
  - deposit and withdrawal costs ("no Polymarket fees to deposit or withdraw
    USDC", but intermediaries may charge);
  - minimum order sizes;
  - Polymarket's taker and maker rebate programmes.
* **Kalshi accounts held through a broker (FCM)** round to whole cents, so they
  pay up to $0.01 more per order (`precision=arb.CENT`).

## Sources

* Kalshi fee schedule (PDF, version effective 7 July 2026, verified by hand):
  https://kalshi.com/docs/kalshi-fee-schedule.pdf
* Kalshi fee rounding: https://docs.kalshi.com/getting_started/fee_rounding.md
* Kalshi series fee fields and fee changes:
  https://docs.kalshi.com/api-reference/market/get-series,
  https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes,
  https://docs.kalshi.com/api-reference/events/get-event-fee-changes
* Kalshi order books (YES and NO pay $1 together; asks from bids):
  https://docs.kalshi.com/getting_started/orderbook_responses
* Polymarket fees: https://docs.polymarket.com/trading/fees.md
* Polymarket market fee fields:
  https://docs.polymarket.com/market-data/market-details.md#trading-fees
* Polymarket changelog (Fee Structure V2, Mar 30, 2026; fees from
  `feeSchedule`, Mar 31, 2026): https://docs.polymarket.com/changelog/predictions.md
* Polymarket order books (the NO book mirrors the YES book):
  https://docs.polymarket.com/market-data/prices-order-books.md
