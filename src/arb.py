"""
arb.py - Fee-adjusted arbitrage between the matched Kalshi and Polymarket markets.

Each pair in data/processed/pairs.parquet is ONE outcome that is listed on
both venues (see docs/matching.md). Buy a contract that pays $1 if the
outcome happens on one venue, and a contract that pays $1 if it does NOT
happen on the other venue: together they pay exactly $1, whatever happens.
If both cost less than $1 together, including both venues' trading fees, the
difference is a profit that does not depend on the outcome: an arbitrage.
(It is riskless only if both venues settle the same way. docs/matching.md
lists the cases in which they did not.) Full explanation and a worked
example: docs/arbitrage.md.

How to run it (from the project folder):
    python src/arb.py --download    download fresh order books of the 3 example pairs (both
                                    venues within about a second), save them to
                                    data/processed/arb_examples.parquet and print the rows
    python src/arb.py               print the rows for the saved example order books
    python src/arb.py --explain "KXFEDDECISION-26DEC-H0 / 3215007"
                                    also print every step of one pair's calculation

The functions, from low level to high level:
    kalshi_fee(), kalshi_fee_for_fills(), kalshi_multiplier()   Kalshi's taker fee
    polymarket_fee(), polymarket_fee_per_share()                Polymarket's taker fee
    ask_ladder(), poly_outcome_to_buy()     what we can buy, and which side to buy
    top_of_book()                           profit per contract at the two best asks
    walk_books()                            profit when walking down both order books
    return_on_capital(), annualised_return(), resolution_time(), days_until_resolution()
    snapshot_row()                          two order-book snapshots -> one row
    download_example_books(), example_rows(), print_worked_example()

Money is computed with Python's Decimal type, which counts exactly like a
calculator (0.07 x 0.5 is exactly 0.035). Ordinary floats carry tiny binary
errors: 0.07 * 100 * 0.5 * 0.5 gives 1.7500000000000002, and "round up to the
next cent" would then charge $1.76 instead of $1.75. The finished rows use
ordinary numbers again.

Rules this code follows (CLAUDE.md): it only reads public order books (GET
requests, no account, no keys) and never places orders; every fee formula
cites its official source and is tested in tests/test_arb.py.
"""

import argparse
import math
import sys
import time
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

import pandas as pd

import kalshi
import polymarket

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

PROCESSED_FOLDER = kalshi.PROCESSED_FOLDER                      # data/processed/
PAIRS_FILE = PROCESSED_FOLDER / "pairs.parquet"                 # made by src/build_pairs.py
POLY_MARKETS_FILE = PROCESSED_FOLDER / "poly_markets.parquet"   # made by src/download_polymarket.py
EXAMPLES_NAME = "arb_examples"
EXAMPLES_FILE = PROCESSED_FOLDER / f"{EXAMPLES_NAME}.parquet"   # the example order books

# ---------------------------------------------------------------------------
# Kalshi's taker fee: the facts
# ---------------------------------------------------------------------------
# Kalshi fee schedule, https://kalshi.com/docs/kalshi-fee-schedule.pdf
# (version effective 7 July 2026). The cloud session cannot download this PDF
# (Kalshi's website asks for a browser check), so the facts below were read
# from it by hand on 2026-11-04; see docs/arbitrage.md.
#     taker fee = round up(M x 0.07 x C x P x (1 - P))
#         C = number of contracts, P = price in dollars,
#         M = the series' taker multiplier (default 1)
#     the fee plus the cost of the position is rounded to a centicent ($0.0001)
#     the schedule's example table: 100 contracts at $0.55 -> $1.74 fee, which is
#         the fee rounded up to a whole cent (with centicents it is $1.7325)
# How the rounding works in detail, https://docs.kalshi.com/getting_started/fee_rounding.md :
#   * the fee of each fill is first rounded up to $0.000001;
#   * then cost + fee is rounded against the trader to the account's precision:
#     $0.0001 for direct members of the exchange, $0.01 for accounts at a broker (FCM);
#   * a "fee accumulator" carries the rounding over all fills of one order, so an
#     order that fills at several prices pays what one fill of the same size would.
# We only take offers that are already in the book (we are the "taker"), so
# Kalshi's maker fees (0.0175 x ..., charged on Fed and CPI series to orders
# that wait in the book) never apply to us.

KALSHI_TAKER_RATE = Decimal("0.07")   # the 0.07 in the formula
KALSHI_DEFAULT_MULTIPLIER = 1         # M for a series the schedule lists without a multiplier
CENTICENT = Decimal("0.0001")         # 1/100 of a cent: the schedule's rounding step (our default)
CENT = Decimal("0.01")                # a whole cent: the step of the schedule's example table
MICRODOLLAR = Decimal("0.000001")     # each fill's fee is first rounded up to this

# The taker multiplier M of each series. A series that the fee schedule lists
# with another taker multiplier belongs here. Our six series all use M = 1:
# Kalshi's API reports fee_multiplier = 1 for each of them
# (GET /series/{ticker}, https://docs.kalshi.com/api-reference/market/get-series),
# its fee-change history has no change for any of them (GET /series/fee_changes,
# https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes), and
# none of our 63 events has an event-level override (GET /events/fee_changes,
# https://docs.kalshi.com/api-reference/events/get-event-fee-changes). Checked 2026-11-04.
KALSHI_TAKER_MULTIPLIERS = {
    "KXFEDDECISION": 1, "KXCPI": 1, "KXCPIYOY": 1,   # current Fed decision and CPI series
    "FEDDECISION": 1, "CPI": 1, "CPIYOY": 1,         # their older names (2024 markets)
}

# ---------------------------------------------------------------------------
# Polymarket's taker fee: the facts
# ---------------------------------------------------------------------------
# Polymarket fees, https://docs.polymarket.com/trading/fees.md (read 2026-11-04):
#     fee = C x feeRate x p x (1 - p)          C = number of shares, p = price
#   * "Makers are never charged fees. Only takers pay fees."
#   * "Fees are rounded to 5 decimal places." (smallest fee: 0.00001 USDC)
# Each market carries its own fee fields (fees_enabled, fee_rate, fee_exponent,
# fee_taker_only in poly_markets.parquet), and fees must be calculated from
# them: https://docs.polymarket.com/changelog/predictions.md (Mar 31, 2026).
# The fees page no longer shows the exponent. The market-details page calls it
# the "exponent applied to the price component of the fee curve"
# (https://docs.polymarket.com/market-data/market-details.md#trading-fees), so
#     fee = C x feeRate x (p x (1 - p)) ** exponent
# Every market in our data has exponent 1, which gives exactly the formula above.
#
# Dates. "Fee Structure V2" brought fees to Economics markets (the Fed and CPI)
# on 30 March 2026 (https://docs.polymarket.com/changelog/predictions.md,
# Mar 30, 2026), so trades before that day pay no fee. Neither the docs nor the
# market records say when a market's CURRENT fee schedule started. ASSUMPTION:
# a market's current schedule applies from 2026-03-30 00:00 UTC on.

POLY_FEE_STEP = Decimal("0.00001")    # Polymarket rounds fees to 5 decimal places
FEE_STRUCTURE_V2_START = pd.Timestamp("2026-03-30 00:00", tz="UTC")

# ---------------------------------------------------------------------------
# Other settings
# ---------------------------------------------------------------------------

DAYS_PER_YEAR = 365

# The three open pairs of the worked example (all open on 2026-11-04).
EXAMPLE_PAIR_IDS = [
    "KXCPI-26OCT-T0.1 / 5691223",        # October 2026 CPI MoM; the only open "Kalshi NO" pair
    "KXFEDDECISION-26DEC-H0 / 3215007",  # the Fed holds its rate in December 2026
    "KXFEDDECISION-26DEC-H25 / 3215008", # the Fed hikes by 25 bp in December 2026
]
# A short extra pause before each order-book request, on top of each venue's
# own throttle: both venues together get at most 5 requests per second.
SECONDS_BETWEEN_REQUESTS = 0.2

# Columns of arb_examples.parquet: one row per price level, in the format of
# kalshi.parse_orderbook() and polymarket.parse_orderbook().
EXAMPLE_COLUMNS = ["pair_id", "venue", "market", "snapshot_time", "outcome", "side", "level",
                   "price", "quantity"]

# Columns of the rows made by snapshot_row(). Money in dollars, sizes in
# contracts (one contract pays $1), times in UTC, returns as fractions (0.02 = 2%).
ROW_COLUMNS = [
    "timestamp",                 # the later of the two order-book snapshots
    "pair_id",
    "direction",                 # what to buy, e.g. "Kalshi YES + Polymarket NO"
    "gross_gap",                 # 1 - Kalshi ask - Polymarket ask (best asks, per contract)
    "fees",                      # both taker fees per contract, at the best asks
    "net_profit_per_contract",   # gross_gap - fees
    "max_size",                  # contracts on EACH venue that maximize total profit (0 = no trade)
    "total_profit",              # profit of buying max_size contracts, walking down both books
    "annualised_return",         # total_profit / capital_locked, x 365 / days_to_resolution
    "kalshi_ask", "poly_ask",    # the two best asks
    "top_size",                  # contracts on offer at both best asks (the smaller size)
    "kalshi_fee_per_contract", "poly_fee_per_contract",
    "capital_locked",            # cash paid for max_size contracts on both venues, fees included
    "return_on_capital",         # total_profit / capital_locked
    "top_return_on_capital",     # the same for one contract at the best asks
    "top_annualised_return",
    "days_to_resolution",        # from timestamp to resolution_time()
    "kalshi_snapshot_time", "poly_snapshot_time",
    "seconds_apart",             # time between the two snapshots
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def to_decimal(value):
    """A number as an exact Decimal: 0.55 -> Decimal("0.55"), "13.00" -> Decimal("13.00")."""
    return Decimal(str(value))


def round_up(amount, step):
    """Round a dollar amount UP to a whole number of `step`s: (1.7325, CENT) -> 1.74."""
    return amount.quantize(step, rounding=ROUND_CEILING)


def to_utc(timestamp):
    """Any time (text, datetime or Timestamp) as a pandas Timestamp in UTC (no zone = UTC)."""
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


# ---------------------------------------------------------------------------
# Kalshi's taker fee
# ---------------------------------------------------------------------------

def kalshi_multiplier(ticker):
    """The taker multiplier M of the series a Kalshi market belongs to.

    The series is the part of the ticker before the first "-": market
    "KXCPI-26OCT-T0.1" belongs to series "KXCPI". Series that are not in
    KALSHI_TAKER_MULTIPLIERS get the schedule's default, 1.
    """
    series = ticker.split("-")[0]
    return KALSHI_TAKER_MULTIPLIERS.get(series, KALSHI_DEFAULT_MULTIPLIER)


def kalshi_fee_per_contract(price, multiplier=KALSHI_DEFAULT_MULTIPLIER):
    """Kalshi's fee formula for ONE contract, before rounding: M x 0.07 x P x (1 - P).

    Source: https://kalshi.com/docs/kalshi-fee-schedule.pdf (taker fee).
    Example: at P = $0.55, 0.07 x 0.55 x 0.45 = $0.017325 per contract.
    """
    price = to_decimal(price)
    return to_decimal(multiplier) * KALSHI_TAKER_RATE * price * (1 - price)


def kalshi_fee_for_fills(fills, multiplier=KALSHI_DEFAULT_MULTIPLIER, precision=CENTICENT):
    """Kalshi's taker fee, in dollars, for ONE order that fills at one or more prices.

    fills:       [(price, contracts), ...], e.g. [(0.19, 500), (0.20, 300)] when an
                 order takes all offers at $0.19 and 300 contracts at $0.20
    multiplier:  the series' taker multiplier M (see kalshi_multiplier())
    precision:   the step to which cost + fee is rounded up: CENTICENT ($0.0001, the
                 fee schedule's rule) or CENT ($0.01, as in the schedule's table)

    Steps, from https://kalshi.com/docs/kalshi-fee-schedule.pdf and
    https://docs.kalshi.com/getting_started/fee_rounding.md :
      1. the fee of each fill = M x 0.07 x C x P x (1 - P), rounded up to $0.000001
      2. what we pay = the cost of all contracts (C x P, added up) + all fees,
         rounded UP to `precision`
      3. the fee = what we pay - the cost of the contracts
    Kalshi carries the rounding over all fills of one order (its "fee
    accumulator"), so the total is rounded once, not once per fill.
    Returns a Decimal.
    """
    cost = Decimal(0)
    fees = Decimal(0)
    for price, contracts in fills:
        contracts = to_decimal(contracts)
        cost += to_decimal(price) * contracts
        fees += round_up(kalshi_fee_per_contract(price, multiplier) * contracts, MICRODOLLAR)
    paid = round_up(cost + fees, precision)
    return paid - cost


def kalshi_fee(price, contracts, multiplier=KALSHI_DEFAULT_MULTIPLIER, precision=CENTICENT):
    """Kalshi's taker fee, in dollars, for buying `contracts` contracts at `price`.

        fee = round up(M x 0.07 x C x P x (1 - P))
              (https://kalshi.com/docs/kalshi-fee-schedule.pdf),
        with cost + fee rounded up to a centicent ($0.0001) by default.

    Example, 100 contracts at $0.55: 0.07 x 100 x 0.55 x 0.45 = $1.7325, so
        kalshi_fee(0.55, 100)                  -> Decimal("1.7325")
        kalshi_fee(0.55, 100, precision=CENT)  -> Decimal("1.74"), the value in Kalshi's table
    Returns a Decimal. For an order that fills at several prices, use
    kalshi_fee_for_fills().
    """
    return kalshi_fee_for_fills([(price, contracts)], multiplier, precision)


# ---------------------------------------------------------------------------
# Polymarket's taker fee
# ---------------------------------------------------------------------------

def has_fee_schedule(market):
    """True if a Polymarket market charges fees: fees_enabled is True and it has a fee rate."""
    enabled = market["fees_enabled"]
    if pd.isna(enabled) or not bool(enabled):
        return False
    return not pd.isna(market["fee_rate"])


def polymarket_fee_per_share(market, price, timestamp):
    """Polymarket's fee formula for ONE share bought by a taker, before rounding.

        fee per share = feeRate x (p x (1 - p)) ** exponent

    market:     a row of poly_markets.parquet (or a dict) with fees_enabled,
                fee_rate and fee_exponent
    price:      the price p of the share, in dollars
    timestamp:  when the trade happens (UTC)
    Zero when the market has no fee schedule, and before Fee Structure V2
    (2026-03-30), when Fed and CPI markets charged no fees.
    Sources: https://docs.polymarket.com/trading/fees.md (formula),
    https://docs.polymarket.com/market-data/market-details.md#trading-fees (exponent),
    https://docs.polymarket.com/changelog/predictions.md (Mar 30 and Mar 31, 2026).
    """
    if to_utc(timestamp) < FEE_STRUCTURE_V2_START:
        return Decimal(0)
    if not has_fee_schedule(market):
        return Decimal(0)
    price = to_decimal(price)
    rate = to_decimal(market["fee_rate"])
    exponent = market["fee_exponent"]
    # A schedule without an exponent: use 1, the formula on the fees page.
    exponent = Decimal(1) if pd.isna(exponent) else to_decimal(exponent)
    return rate * (price * (1 - price)) ** exponent


def polymarket_fee(market, price, contracts, timestamp, taker=True):
    """Polymarket's fee, in dollars, for trading `contracts` shares at `price` at `timestamp`.

        fee = C x feeRate x (p x (1 - p)) ** exponent, rounded to 5 decimal places

    Computed from the market's OWN fee fields (fees_enabled, fee_rate,
    fee_exponent, fee_taker_only), as Polymarket asks
    (https://docs.polymarket.com/trading/fees.md). Zero for a market without a
    fee schedule, and for any trade before 2026-03-30 (see polymarket_fee_per_share()).
    taker: True when we take an offer that is already in the book, which is what
           this project always does. A maker (an order waiting in the book) pays
           nothing: "Makers are never charged fees", which a market states with
           fee_taker_only = True.
    Examples (rate 0.05, exponent 1), 100 shares: at $0.50 -> $1.25, at $0.10 ->
    $0.45, the values of the docs' Economics table. Returns a Decimal.
    """
    if not taker:
        # No market in our data lets makers pay; stop if one ever does, because
        # the docs do not say how much they would pay. (Missing = taker only.)
        taker_only = market["fee_taker_only"]
        if has_fee_schedule(market) and not pd.isna(taker_only) and not bool(taker_only):
            raise ValueError(f"Polymarket market {market.get('market_id')} charges makers too; "
                             "the docs do not describe maker fees")
        return Decimal(0)
    fee = polymarket_fee_per_share(market, price, timestamp) * to_decimal(contracts)
    return fee.quantize(POLY_FEE_STEP, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Order books: what we can buy, and which side to buy
# ---------------------------------------------------------------------------

def ask_ladder(levels, outcome):
    """The offers to SELL us `outcome` ("yes" or "no"), cheapest first: [(price, quantity), ...].

    levels: one venue's order book, as kalshi.parse_orderbook() or
            polymarket.parse_orderbook() returns it (columns outcome, side,
            price, quantity), or the rows of one venue in arb_examples.parquet
    Kalshi only lists bids; kalshi.parse_orderbook() has already turned each NO
    bid at p into a YES ask at 1 - p (and each YES bid into a NO ask).
    Polymarket: the asks of that outcome's OWN book. Its NO book is the mirror
    image of its YES book (an offer to sell NO at p is a bid to buy YES at
    1 - p): they show the SAME orders, so their depth is never added together.
    """
    asks = levels[(levels["outcome"] == outcome) & (levels["side"] == "ask")]
    asks = asks.sort_values("price")
    return [(to_decimal(price), to_decimal(quantity))
            for price, quantity in zip(asks["price"], asks["quantity"])]


def poly_outcome_to_buy(kalshi_outcome, side):
    """The Polymarket token that pays $1 exactly when the Kalshi contract we bought pays $0.

    kalshi_outcome:  "yes" or "no": the Kalshi contract we buy
    side:            the pair's poly_yes_equals column ("side" in the review sheet)
        "Kalshi YES": Polymarket YES = Kalshi YES, so buy the OTHER label on Polymarket
        "Kalshi NO":  Polymarket YES = Kalshi NO,  so buy the SAME label on Polymarket
    Example: side "Kalshi NO", buying Kalshi YES -> buy Polymarket YES.
    """
    if side == "Kalshi YES":
        return "no" if kalshi_outcome == "yes" else "yes"
    if side == "Kalshi NO":
        return kalshi_outcome
    raise ValueError(f"unknown side {side!r}: expected 'Kalshi YES' or 'Kalshi NO'")


def direction_name(kalshi_outcome, poly_outcome):
    """A readable name for a direction, e.g. "Kalshi YES + Polymarket NO"."""
    return f"Kalshi {kalshi_outcome.upper()} + Polymarket {poly_outcome.upper()}"


# ---------------------------------------------------------------------------
# Arbitrage at the best prices, and with depth
# ---------------------------------------------------------------------------

def top_of_book(kalshi_asks, poly_asks, multiplier, poly_market, timestamp, precision=CENTICENT):
    """Profit per contract when buying one contract on each venue at the best (lowest) ask.

        gross gap  = 1 - Kalshi ask - Polymarket ask
        net profit = gross gap - Kalshi fee - Polymarket fee       (all per contract)

    The two contracts together pay exactly $1 whatever happens: one Kalshi YES
    and one NO pay $1 together (https://docs.kalshi.com/getting_started/orderbook_responses),
    so do one Polymarket YES and one NO token
    (https://docs.polymarket.com/concepts/markets-events.md), and the pair's
    side tells which Polymarket token is the opposite of our Kalshi contract.
    The fees are those of a real order for all contracts on offer at BOTH best
    asks (the smaller of the two sizes), divided by that number, so the rounding
    is spread the way a real order would spread it.
    Returns a dict of Decimals, or None if one of the books has no offers.
    """
    if not kalshi_asks or not poly_asks:
        return None
    kalshi_price, kalshi_size = kalshi_asks[0]
    poly_price, poly_size = poly_asks[0]
    size = min(kalshi_size, poly_size)
    if size <= 0:
        return None
    kalshi_fee_each = kalshi_fee(kalshi_price, size, multiplier, precision) / size
    poly_fee_each = polymarket_fee(poly_market, poly_price, size, timestamp) / size
    gross_gap = 1 - kalshi_price - poly_price
    fees = kalshi_fee_each + poly_fee_each
    return {
        "kalshi_ask": kalshi_price, "kalshi_size": kalshi_size,
        "poly_ask": poly_price, "poly_size": poly_size,
        "size": size,
        "gross_gap": gross_gap,
        "kalshi_fee": kalshi_fee_each,
        "poly_fee": poly_fee_each,
        "fees": fees,
        "net_profit": gross_gap - fees,
        "capital": kalshi_price + poly_price + fees,   # cash paid per contract
    }


def add_up_by_price(steps, price_key):
    """[(price, contracts), ...] with the contracts of all steps at the same price added up."""
    totals = {}
    for step in steps:
        price = step[price_key]
        totals[price] = totals.get(price, Decimal(0)) + step["contracts"]
    return list(totals.items())


def depth_totals(steps, multiplier, poly_market, timestamp, precision=CENTICENT):
    """Exact totals, with all the rounding, for buying the contracts of `steps` on both venues.

    The Kalshi part is ONE order that fills at several prices (rounded once, see
    kalshi_fee_for_fills()). The Polymarket part pays one rounded fee per price
    level (Polymarket rounds each fill's fee; one level can hold several orders,
    which changes the total by at most $0.00001 per order).
    """
    kalshi_fills = add_up_by_price(steps, "kalshi_price")
    poly_fills = add_up_by_price(steps, "poly_price")
    size = sum((step["contracts"] for step in steps), Decimal(0))
    kalshi_cost = sum((price * contracts for price, contracts in kalshi_fills), Decimal(0))
    poly_cost = sum((price * contracts for price, contracts in poly_fills), Decimal(0))
    kalshi_fee_total = kalshi_fee_for_fills(kalshi_fills, multiplier, precision)
    poly_fee_total = sum((polymarket_fee(poly_market, price, contracts, timestamp)
                          for price, contracts in poly_fills), Decimal(0))
    capital = kalshi_cost + poly_cost + kalshi_fee_total + poly_fee_total
    return {
        "size": size,
        "kalshi_cost": kalshi_cost, "kalshi_fee": kalshi_fee_total,
        "poly_cost": poly_cost, "poly_fee": poly_fee_total,
        "capital": capital,                 # all cash paid, locked until resolution
        "total_profit": size - capital,     # the pairs pay $1 each at resolution
        "steps": list(steps),
    }


def walk_books(kalshi_asks, poly_asks, multiplier, poly_market, timestamp, precision=CENTICENT):
    """Walk down both order books and find the number of contracts that maximizes total profit.

    We buy the cheapest offers first, the same number of contracts on each
    venue, one step at a time; a step ends when an offer on either venue is
    used up. The profit per contract of a step is
        1 - Kalshi price - Polymarket price - both fees per contract (before rounding)
    Walking down a book, the price rises, and so does price + fee per contract
    (the fee changes more slowly than the price), so this profit can only fall.
    We stop at the first step that would not make money: buying more would
    lower the total. The totals are computed exactly after every step (see
    depth_totals()) and the best one is kept, because rounding can make the
    last few contracts not worth buying.

    Returns the depth_totals() dict of the best size (size = contracts bought on
    EACH venue, 0 if no step makes money), plus "stop": why the walk ended
    (a dict with the prices and profit per contract of the first step that would
    lose money, or None if a book ran out of offers).
    """
    best = depth_totals([], multiplier, poly_market, timestamp, precision)   # buy nothing
    steps = []
    stop = None
    k = p = 0                         # the current price level on each venue
    kalshi_left = kalshi_asks[0][1] if kalshi_asks else Decimal(0)
    poly_left = poly_asks[0][1] if poly_asks else Decimal(0)
    while k < len(kalshi_asks) and p < len(poly_asks):
        kalshi_price, poly_price = kalshi_asks[k][0], poly_asks[p][0]
        profit_per_contract = (1 - kalshi_price - poly_price
                               - kalshi_fee_per_contract(kalshi_price, multiplier)
                               - polymarket_fee_per_share(poly_market, poly_price, timestamp))
        if profit_per_contract <= 0:
            stop = {"kalshi_price": kalshi_price, "poly_price": poly_price,
                    "profit_per_contract": profit_per_contract}
            break
        contracts = min(kalshi_left, poly_left)
        steps.append({"kalshi_price": kalshi_price, "poly_price": poly_price,
                      "contracts": contracts, "profit_per_contract": profit_per_contract})
        totals = depth_totals(steps, multiplier, poly_market, timestamp, precision)
        if totals["total_profit"] > best["total_profit"]:
            best = totals
        # Move on: an offer that is used up is replaced by the next price level.
        kalshi_left -= contracts
        poly_left -= contracts
        if kalshi_left == 0:
            k += 1
            if k < len(kalshi_asks):
                kalshi_left = kalshi_asks[k][1]
        if poly_left == 0:
            p += 1
            if p < len(poly_asks):
                poly_left = poly_asks[p][1]
    return {**best, "stop": stop}


# ---------------------------------------------------------------------------
# Return on capital
# ---------------------------------------------------------------------------

def return_on_capital(profit, capital):
    """Profit as a share of the cash paid up front, which stays locked until both markets pay out.

    Both venues want the full price plus fee when you buy (no borrowing), so
    the capital is everything paid. Example: $0.02 profit on $0.98 paid -> 0.0204
    (2.04%). NaN if nothing was paid.
    """
    if pd.isna(capital) or capital <= 0:
        return math.nan
    return float(profit) / float(capital)


def annualised_return(roc, days):
    """A return earned in `days` days as a simple yearly rate: roc x 365 / days (no compounding).

    Example: 2% in 36.5 days -> 20% a year. NaN if days is unknown or not positive.
    """
    if pd.isna(roc) or pd.isna(days) or days <= 0:
        return math.nan
    return roc * DAYS_PER_YEAR / days


def resolution_time(pair):
    """When both legs have paid out, roughly: the later of Kalshi's close time and Polymarket's end date.

    pair: a row of pairs.parquet. Kalshi closes just before the announcement and
    settles soon after. Polymarket's end date is usually some hours after the
    announcement (it is sometimes wrong, see docs/matching.md, risk 4).
    """
    times = [one for one in (pair["kalshi_close_time"], pair["poly_end_date"]) if not pd.isna(one)]
    return max(times) if times else pd.NaT


def days_until_resolution(timestamp, resolution):
    """Days (with fractions) from `timestamp` to `resolution`; NaN if either is unknown."""
    if pd.isna(timestamp) or pd.isna(resolution):
        return math.nan
    return (to_utc(resolution) - to_utc(timestamp)).total_seconds() / 86400


# ---------------------------------------------------------------------------
# Two order-book snapshots -> one row
# ---------------------------------------------------------------------------

def check_direction(pair, kalshi_outcome, kalshi_levels, poly_levels, poly_market, timestamp,
                    precision=CENTICENT):
    """Top of book and depth for one direction: buy Kalshi `kalshi_outcome` + the opposite Polymarket token."""
    poly_outcome = poly_outcome_to_buy(kalshi_outcome, pair["poly_yes_equals"])
    kalshi_asks = ask_ladder(kalshi_levels, kalshi_outcome)
    poly_asks = ask_ladder(poly_levels, poly_outcome)
    multiplier = kalshi_multiplier(pair["kalshi_ticker"])
    return {
        "direction": direction_name(kalshi_outcome, poly_outcome),
        "top": top_of_book(kalshi_asks, poly_asks, multiplier, poly_market, timestamp, precision),
        "depth": walk_books(kalshi_asks, poly_asks, multiplier, poly_market, timestamp, precision),
    }


def snapshot_row(pair, kalshi_levels, poly_levels, poly_market, precision=CENTICENT):
    """Turn one Kalshi and one Polymarket order-book snapshot of a pair into ONE row.

    pair:           a row of pairs.parquet
    kalshi_levels:  the Kalshi market's order book (kalshi.parse_orderbook() format)
    poly_levels:    the Polymarket market's order book (polymarket.parse_orderbook() format)
    poly_market:    the Polymarket market's row of poly_markets.parquet (for its fees)
    precision:      Kalshi's rounding step (see kalshi_fee())

    Both directions are checked and the row describes the better one (the
    higher net profit per contract). Only one direction can have a positive
    gross gap: the two gaps add up to minus the two venues' bid-ask spreads.
    The two snapshots should be taken at (almost) the same time; the row
    reports how far apart they were in seconds_apart. Columns: ROW_COLUMNS.
    """
    kalshi_time = kalshi_levels["snapshot_time"].max()
    poly_time = poly_levels["snapshot_time"].max()
    timestamp = max(kalshi_time, poly_time)
    days = days_until_resolution(timestamp, resolution_time(pair))
    row = {"timestamp": timestamp, "pair_id": pair["pair_id"], "days_to_resolution": days,
           "kalshi_snapshot_time": kalshi_time, "poly_snapshot_time": poly_time,
           "seconds_apart": abs((poly_time - kalshi_time).total_seconds())}

    results = [check_direction(pair, outcome, kalshi_levels, poly_levels, poly_market, timestamp, precision)
               for outcome in ("yes", "no")]
    possible = [result for result in results if result["top"] is not None]
    if not possible:  # a book without offers: nothing can be bought
        return {column: row.get(column, math.nan) for column in ROW_COLUMNS}
    best = max(possible, key=lambda result: result["top"]["net_profit"])
    top, depth = best["top"], best["depth"]

    top_roc = return_on_capital(top["net_profit"], top["capital"])
    roc = return_on_capital(depth["total_profit"], depth["capital"]) if depth["size"] > 0 else math.nan
    row.update({
        "direction": best["direction"],
        "gross_gap": float(top["gross_gap"]),
        "fees": float(top["fees"]),
        "net_profit_per_contract": float(top["net_profit"]),
        "max_size": float(depth["size"]),
        "total_profit": float(depth["total_profit"]),
        "annualised_return": annualised_return(roc, days),
        "kalshi_ask": float(top["kalshi_ask"]),
        "poly_ask": float(top["poly_ask"]),
        "top_size": float(top["size"]),
        "kalshi_fee_per_contract": float(top["kalshi_fee"]),
        "poly_fee_per_contract": float(top["poly_fee"]),
        "capital_locked": float(depth["capital"]),
        "return_on_capital": roc,
        "top_return_on_capital": top_roc,
        "top_annualised_return": annualised_return(top_roc, days),
    })
    return {column: row[column] for column in ROW_COLUMNS}


# ---------------------------------------------------------------------------
# The worked example: fresh order books of three open pairs
# ---------------------------------------------------------------------------

def find_pair(pairs, pair_id):
    """The row of pairs.parquet with this pair_id."""
    found = pairs[pairs["pair_id"] == pair_id]
    if found.empty:
        raise ValueError(f"pair {pair_id!r} is not in {PAIRS_FILE.name}")
    return found.iloc[0]


def find_poly_market(poly_markets, market_id):
    """The row of poly_markets.parquet with this market_id (for its fee fields)."""
    found = poly_markets[poly_markets["market_id"] == market_id]
    if found.empty:
        raise ValueError(f"Polymarket market {market_id!r} is not in {POLY_MARKETS_FILE.name}")
    return found.iloc[0]


def download_example_books(pairs, pair_ids=EXAMPLE_PAIR_IDS):
    """Download fresh order books of both venues for each pair; return one table (EXAMPLE_COLUMNS).

    For each pair the three books (Kalshi; Polymarket YES and NO) are asked for
    right after each other, so the two venues' snapshots are about a second
    apart. snapshot_time is when the answer arrived (Polymarket: when both of
    its books had arrived). Public GET requests only: kalshi.get_orderbook()
    and polymarket.get_orderbook() throttle and retry, and order books are never
    taken from the cache.
    """
    tables = []
    for pair_id in pair_ids:
        pair = find_pair(pairs, pair_id)
        time.sleep(SECONDS_BETWEEN_REQUESTS)
        kalshi_raw = kalshi.get_orderbook(pair["kalshi_ticker"])
        kalshi_time = pd.Timestamp.now(tz="UTC")
        time.sleep(SECONDS_BETWEEN_REQUESTS)
        yes_book = polymarket.get_orderbook(pair["poly_yes_token_id"])
        time.sleep(SECONDS_BETWEEN_REQUESTS)
        no_book = polymarket.get_orderbook(pair["poly_no_token_id"])
        poly_time = pd.Timestamp.now(tz="UTC")

        _, kalshi_levels = kalshi.parse_orderbook(kalshi_raw, pair["kalshi_ticker"], kalshi_time)
        _, poly_levels = polymarket.parse_orderbook(yes_book, no_book, pair["poly_market_id"], poly_time)
        print(f"   {pair_id}: Kalshi {kalshi_time:%H:%M:%S.%f}, Polymarket {poly_time:%H:%M:%S.%f} UTC "
              f"({(poly_time - kalshi_time).total_seconds():.2f} s apart); Polymarket's NO book mirrors "
              f"its YES book: {polymarket.books_are_mirrored(yes_book, no_book)}")
        tables.append(kalshi_levels.rename(columns={"ticker": "market"})
                      .assign(pair_id=pair_id, venue="kalshi"))
        tables.append(poly_levels.rename(columns={"market_id": "market"})
                      .assign(pair_id=pair_id, venue="polymarket"))
    return pd.concat(tables, ignore_index=True)[EXAMPLE_COLUMNS]


def split_books(books, pair_id):
    """The Kalshi and the Polymarket order book of one pair in arb_examples.parquet."""
    pair_books = books[books["pair_id"] == pair_id]
    return pair_books[pair_books["venue"] == "kalshi"], pair_books[pair_books["venue"] == "polymarket"]


def example_rows(books, pairs, poly_markets, precision=CENTICENT):
    """One row per pair (see snapshot_row()) for the order books in `books` (arb_examples.parquet)."""
    rows = []
    for pair_id in books["pair_id"].unique():
        pair = find_pair(pairs, pair_id)
        poly_market = find_poly_market(poly_markets, pair["poly_market_id"])
        kalshi_levels, poly_levels = split_books(books, pair_id)
        rows.append(snapshot_row(pair, kalshi_levels, poly_levels, poly_market, precision))
    return pd.DataFrame(rows, columns=ROW_COLUMNS)


def show(value):
    """A number as exact text without trailing zeros: Decimal("16.800") -> "16.8", 1.0 -> "1"."""
    return f"{to_decimal(value).normalize():f}"


def print_worked_example(pair, kalshi_levels, poly_levels, poly_market, precision=CENTICENT):
    """Print every number of one pair's calculation, both directions, to check it by hand.

    Totals are exact. A number per contract is a total divided by the number
    of contracts, shown with 8 decimals.
    """
    kalshi_time = kalshi_levels["snapshot_time"].max()
    poly_time = poly_levels["snapshot_time"].max()
    timestamp = max(kalshi_time, poly_time)
    resolution = resolution_time(pair)
    days = days_until_resolution(timestamp, resolution)
    multiplier = kalshi_multiplier(pair["kalshi_ticker"])
    rate, exponent = poly_market["fee_rate"], poly_market["fee_exponent"]

    print(f"Pair {pair['pair_id']}   side: Polymarket YES = {pair['poly_yes_equals']}")
    print(f"   Kalshi:     {pair['kalshi_title']}")
    print(f"   Polymarket: {pair['poly_question']}")
    print(f"   order books: Kalshi {kalshi_time:%Y-%m-%d %H:%M:%S.%f}, Polymarket {poly_time:%H:%M:%S.%f} UTC "
          f"({abs((poly_time - kalshi_time).total_seconds()):.2f} s apart)")
    print(f"   resolution (later of Kalshi close and Polymarket end date): {resolution:%Y-%m-%d %H:%M} UTC, "
          f"{days:.4f} days later")
    if has_fee_schedule(poly_market) and to_utc(timestamp) >= FEE_STRUCTURE_V2_START:
        poly_rule = f"rate {show(rate)}, exponent {show(exponent)}"
    else:
        poly_rule = "no fee"
    print(f"   fees: Kalshi M = {show(multiplier)}, cost + fee rounded up to ${show(precision)}; "
          f"Polymarket {poly_rule}")

    for outcome in ("yes", "no"):
        result = check_direction(pair, outcome, kalshi_levels, poly_levels, poly_market, timestamp, precision)
        top, depth = result["top"], result["depth"]
        print(f"\n   DIRECTION: buy {result['direction']}")
        if top is None:
            print("      one of the books has no offers: nothing to buy")
            continue
        k_price, p_price, size = top["kalshi_ask"], top["poly_ask"], top["size"]
        print(f"   1. Best asks: Kalshi {show(k_price)} ({show(top['kalshi_size'])} contracts), "
              f"Polymarket {show(p_price)} ({show(top['poly_size'])} shares)")
        print(f"      gross gap = 1 - {show(k_price)} - {show(p_price)} = {show(top['gross_gap'])} per contract")

        print(f"   2. Fees, for the {show(size)} contracts on offer at both best asks:")
        k_raw = kalshi_fee_per_contract(k_price, multiplier) * size
        k_fee6 = round_up(k_raw, MICRODOLLAR)
        k_cost = k_price * size
        k_paid = round_up(k_cost + k_fee6, precision)
        print(f"      Kalshi:     {show(multiplier)} x 0.07 x {show(size)} x {show(k_price)} x {show(1 - k_price)}"
              f" = {show(k_raw)} -> up to $0.000001: {show(k_fee6)}")
        print(f"                  cost {show(size)} x {show(k_price)} = {show(k_cost)}; {show(k_cost)} + "
              f"{show(k_fee6)} = {show(k_cost + k_fee6)} -> up to ${show(precision)}: {show(k_paid)}"
              f" -> fee {show(k_paid - k_cost)}")
        p_fee = polymarket_fee(poly_market, p_price, size, timestamp)
        if poly_rule == "no fee":
            print("      Polymarket: no fee")
        else:
            p_raw = polymarket_fee_per_share(poly_market, p_price, timestamp) * size
            print(f"      Polymarket: {show(size)} x {show(rate)} x ({show(p_price)} x {show(1 - p_price)})"
                  f"^{show(exponent)} = {show(p_raw)} -> 5 decimals: {show(p_fee)}")
        print(f"      fees per contract = ({show(k_paid - k_cost)} + {show(p_fee)}) / {show(size)}"
              f" = {top['fees']:.8f}")
        print(f"      net profit per contract = {show(top['gross_gap'])} - {top['fees']:.8f}"
              f" = {top['net_profit']:.8f}")
        top_roc = return_on_capital(top["net_profit"], top["capital"])
        print(f"      capital per contract = {show(k_price)} + {show(p_price)} + {top['fees']:.8f}"
              f" = {top['capital']:.8f}")
        print(f"      return on capital = {top['net_profit']:.8f} / {top['capital']:.8f} = {top_roc:.8f}"
              f" ({top_roc:.4%}); annualised x 365 / {days:.4f} = {annualised_return(top_roc, days):.2%}")

        print("   3. Walking down both books (profit per contract of each step, before rounding):")
        for number, step in enumerate(depth["steps"], start=1):
            print(f"      step {number}: Kalshi {show(step['kalshi_price'])} + Polymarket "
                  f"{show(step['poly_price'])}: {show(step['contracts'])} contracts, "
                  f"{show(step['profit_per_contract'])} each")
        if depth["stop"] is not None:
            stop = depth["stop"]
            print(f"      next:   Kalshi {show(stop['kalshi_price'])} + Polymarket {show(stop['poly_price'])}:"
                  f" {show(stop['profit_per_contract'])} each -> stop")
        else:
            print("      a book ran out of offers -> stop")
        if depth["size"] == 0:
            print("      max size 0: no contract makes money in this direction")
            continue
        roc = return_on_capital(depth["total_profit"], depth["capital"])
        print(f"      max size {show(depth['size'])} contracts on each venue")
        print(f"      Kalshi:     cost {show(depth['kalshi_cost'])}, fee {show(depth['kalshi_fee'])}"
              f" (one order, rounded once)")
        print(f"      Polymarket: cost {show(depth['poly_cost'])}, fee {show(depth['poly_fee'])}")
        print(f"      capital = {show(depth['kalshi_cost'])} + {show(depth['kalshi_fee'])} + "
              f"{show(depth['poly_cost'])} + {show(depth['poly_fee'])} = {show(depth['capital'])}")
        print(f"      total profit = {show(depth['size'])} x $1 - {show(depth['capital'])}"
              f" = {show(depth['total_profit'])}")
        print(f"   4. return on capital = {show(depth['total_profit'])} / {show(depth['capital'])} = {roc:.8f}"
              f" ({roc:.4%}); annualised = {roc:.8f} x 365 / {days:.4f} = {annualised_return(roc, days):.2%}")


def main():
    parser = argparse.ArgumentParser(description="Fee-adjusted arbitrage for the example pairs.")
    parser.add_argument("--download", action="store_true",
                        help="download fresh order books of the example pairs first")
    parser.add_argument("--explain", metavar="PAIR_ID",
                        help='print every step of one pair, e.g. "KXFEDDECISION-26DEC-H0 / 3215007"')
    args = parser.parse_args()

    for path, script in [(PAIRS_FILE, "src/build_pairs.py"), (POLY_MARKETS_FILE, "src/download_polymarket.py")]:
        if not path.exists():
            sys.exit(f"Missing {path.name}: run python {script} first.")
    pairs = pd.read_parquet(PAIRS_FILE)
    poly_markets = pd.read_parquet(POLY_MARKETS_FILE)

    if args.download:
        print(f"Downloading fresh order books for {len(EXAMPLE_PAIR_IDS)} pairs ...")
        try:
            books = download_example_books(pairs)
        except (kalshi.NetworkBlockedError, polymarket.NetworkBlockedError) as error:
            sys.exit(f"Stopped: {error}")
        path = kalshi.save_table(books, EXAMPLES_NAME)   # the same 50 MB check as the download scripts
        print(f"Saved {len(books)} price levels to {path.relative_to(kalshi.PROJECT_ROOT)}\n")
    elif not EXAMPLES_FILE.exists():
        sys.exit("No saved example order books: run  python src/arb.py --download  first.")

    books = pd.read_parquet(EXAMPLES_FILE)
    rows = example_rows(books, pairs, poly_markets)
    with pd.option_context("display.max_colwidth", 40, "display.width", 200):
        print(rows.set_index("pair_id").T.to_string())

    if args.explain:
        print()
        kalshi_levels, poly_levels = split_books(books, args.explain)
        if kalshi_levels.empty or poly_levels.empty:
            sys.exit(f"No saved order books for {args.explain!r} in {EXAMPLES_FILE.name}; "
                     f"the pairs there are: {', '.join(books['pair_id'].unique())}")
        pair = find_pair(pairs, args.explain)
        print_worked_example(pair, kalshi_levels, poly_levels,
                             find_poly_market(poly_markets, pair["poly_market_id"]))


if __name__ == "__main__":
    main()
