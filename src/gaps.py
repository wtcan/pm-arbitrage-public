"""
gaps.py - Historical price gaps between Kalshi and Polymarket, hour by hour.

This file holds the steps of notebooks/01_historical_gaps.ipynb that are worth
testing on their own (tests/test_gaps.py). The notebook explains every choice
in plain language; this file only does the arithmetic.

The steps, in the order the notebook uses them:
    1. Align both venues on one hourly UTC grid
           hourly_grid(), kalshi_hourly(), kalshi_mid(), poly_in_kalshi_yes_terms(),
           poly_hourly(), align_pair(), build_panel(), poly_prices_cross_check()
    2. Gaps
           raw_gap(), typical_spread(), executable_gaps()

Words used everywhere below:
  * bar_end: the end of a 1-hour bar, in UTC. Kalshi labels its hourly candles
    by their end (period_end), and so does poly_trade_bars (bar_end), so the
    two are joined on it (docs/poly_trades.md).
  * "Kalshi YES terms": every Polymarket price is turned into the price of the
    Polymarket token that pays $1 exactly when Kalshi's YES pays. Then the two
    venues' numbers mean the same thing and can be subtracted.

Research only (CLAUDE.md): this file reads saved tables and never calls an API.
"""

import numpy as np
import pandas as pd

import arb

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# A Polymarket last-trade price older than this is left out of the main results
# (it is kept in the panel, with its age, so the cut-off can be changed).
MAX_POLY_AGE_HOURS = 24

# Readable names of the three topics (the measure column of pairs.parquet).
TOPIC_NAMES = {"fed_change": "Fed", "cpi_mom": "CPI MoM", "cpi_yoy": "CPI YoY"}

# Polymarket's lowest and highest possible price: one tick above 0 and below 1
# (695 of the 736 markets in poly_markets.parquet have a tick of $0.001).
POLY_MIN_PRICE = 0.001
POLY_MAX_PRICE = 0.999

HOUR = pd.Timedelta(hours=1)
TIME_DTYPE = "datetime64[ns, UTC]"   # one time resolution for all joins


def to_utc(timestamp):
    """Any time as a pandas Timestamp in UTC (a time without a zone counts as UTC)."""
    return arb.to_utc(timestamp)


# ---------------------------------------------------------------------------
# 1. One hourly grid for both venues
# ---------------------------------------------------------------------------

def hourly_grid(start, end):
    """All bar ends from `start` to `end`, one hour apart, both included (UTC).

    Example: hourly_grid("2026-01-01 10:00", "2026-01-01 12:00") gives
    10:00, 11:00 and 12:00. Empty if end is before start.
    """
    start, end = to_utc(start).floor("h"), to_utc(end).floor("h")
    return pd.date_range(start, end, freq="h").astype(TIME_DTYPE)


def kalshi_hourly(candles, grid):
    """One Kalshi market's quotes and last trade at every bar end of `grid`.

    candles: the rows of kalshi_candles.parquet for ONE market (any order)
    grid:    the bar ends (see hourly_grid())

    Kalshi sends a candle only for an hour in which something changed (a trade,
    or a new best bid or ask): in a quiet hour the previous candle still holds
    (https://docs.kalshi.com/api-reference/market/get-market-candlesticks).
    So every bar end takes the LAST candle at or before it, with no time limit:
      * kalshi_bid, kalshi_ask:  that candle's closing best YES bid and ask.
        If that candle has no bid (NaN = nobody bid), the bid stays EMPTY: an
        older bid is never filled in, because it was no longer there.
      * kalshi_last: the last traded YES price so far (a candle without a
        trade has no price, so the trade before it still counts).
    kalshi_volume is the number of contracts traded IN that hour (0 without a candle).
    kalshi_hours_since_candle says how old the candle is (0 = a candle for this hour).
    Returns a table with one row per bar end.
    """
    table = pd.DataFrame({"bar_end": grid})
    candles = candles.sort_values("period_end")
    candles = pd.DataFrame({
        "period_end": candles["period_end"].astype(TIME_DTYPE),
        "kalshi_bid": candles["yes_bid_close"].to_numpy(),
        "kalshi_ask": candles["yes_ask_close"].to_numpy(),
        "kalshi_last": candles["price_close"].ffill().to_numpy(),   # the last trade so far
        "kalshi_volume": candles["volume"].to_numpy(),
    })
    # merge_asof takes, for every bar end, the last candle with period_end <= bar_end,
    # exactly as it is (an empty bid in that candle stays empty).
    table = pd.merge_asof(table, candles.drop(columns="kalshi_volume"),
                          left_on="bar_end", right_on="period_end", direction="backward")
    table["kalshi_hours_since_candle"] = (table["bar_end"] - table["period_end"]) / HOUR
    # Volume belongs to its own hour only: an hour without a candle traded nothing.
    volume = candles.set_index("period_end")["kalshi_volume"]
    table["kalshi_volume"] = table["bar_end"].map(volume).fillna(0.0)
    return table.drop(columns="period_end")


def kalshi_mid(bid, ask):
    """Kalshi's mid price: halfway between the best YES bid and the best YES ask.

        mid = (bid + ask) / 2       e.g. bid 0.40, ask 0.44 -> 0.42
    Empty (NaN) when the bid or the ask is missing: with only one side of the
    book there is no "middle". Bid and ask are the closing quotes of Kalshi's
    candle (yes_bid.close, yes_ask.close,
    https://docs.kalshi.com/api-reference/market/get-market-candlesticks).
    Works on single values and on whole columns.
    """
    return (bid + ask) / 2   # NaN + anything = NaN, so a missing side gives NaN


def poly_in_kalshi_yes_terms(price, outcome, side):
    """A Polymarket trade price turned into the price of "Kalshi YES happens".

    price:    the trade price of a Polymarket token, in dollars (0 to 1)
    outcome:  which token traded, "yes" or "no"
    side:     the pair's poly_yes_equals column: "Kalshi YES" (Polymarket YES is
              the same outcome as Kalshi YES) or "Kalshi NO" (Polymarket YES is
              the same outcome as Kalshi NO)

    One YES and one NO token of a Polymarket market pay exactly $1 together
    (https://docs.polymarket.com/concepts/markets-events.md), and the NO order
    book is the mirror image of the YES book: an offer to sell NO at p is a bid
    for YES at 1 - p (https://docs.polymarket.com/market-data/prices-order-books.md).
    So a trade at p in the token that pays when Kalshi YES pays is worth p in
    Kalshi YES terms, and a trade at p in the other token is worth 1 - p.
        side "Kalshi YES": YES token at p -> p,     NO token at p -> 1 - p
        side "Kalshi NO":  YES token at p -> 1 - p, NO token at p -> p
    Works on single values and on whole columns.
    """
    if side not in ("Kalshi YES", "Kalshi NO"):
        raise ValueError(f"unknown side {side!r}: expected 'Kalshi YES' or 'Kalshi NO'")
    same_token = "yes" if side == "Kalshi YES" else "no"   # the token that pays when Kalshi YES pays
    price = np.asarray(price, dtype=float)
    is_same = np.asarray(outcome) == same_token
    result = np.where(is_same, price, 1.0 - price)
    return result if result.ndim else float(result)


def poly_hourly(bars, side, grid):
    """One Polymarket market's last trade price, in Kalshi YES terms, at every bar end of `grid`.

    bars:  the rows of poly_trade_bars.parquet for ONE market (YES and NO bars)
    side:  the pair's poly_yes_equals column (see poly_in_kalshi_yes_terms())
    grid:  the bar ends (see hourly_grid())

    Polymarket has no historical bid and ask, only trades. In each hour with
    trades, the latest trade of EITHER token is used (a YES and a NO trade
    carry the same information, see poly_in_kalshi_yes_terms()); if both
    tokens traded in the same second, the token that pays with Kalshi YES wins.
    In hours without a trade the last price is carried forward, and
    poly_age_hours says how old it is at the bar end (last trade at 13:20,
    bar end 14:00 -> 0.67 hours). poly_volume = shares traded in that hour,
    YES and NO added up (each fill is counted once, docs/poly_trades.md).
    poly_token = which token the price comes from.
    """
    table = pd.DataFrame({"bar_end": grid})
    bars = bars.copy()
    bars["bar_end"] = bars["bar_end"].astype(TIME_DTYPE)
    bars["poly_price"] = poly_in_kalshi_yes_terms(bars["last_price"], bars["outcome"], side)
    same_token = "yes" if side == "Kalshi YES" else "no"
    bars["is_same_token"] = bars["outcome"] == same_token
    # The latest trade of each hour; in a tie the same token sorts last and wins.
    latest = (bars.sort_values(["bar_end", "last_trade_time", "is_same_token"])
                  .groupby("bar_end").tail(1))
    latest = latest[["bar_end", "poly_price", "last_trade_time", "outcome"]].rename(
        columns={"bar_end": "poly_bar_end", "last_trade_time": "poly_last_trade_time",
                 "outcome": "poly_token"})
    table = pd.merge_asof(table, latest, left_on="bar_end", right_on="poly_bar_end",
                          direction="backward")
    table["poly_age_hours"] = (table["bar_end"] - table["poly_last_trade_time"]) / HOUR
    volume = bars.groupby("bar_end")["volume"].sum()
    table["poly_volume"] = table["bar_end"].map(volume).fillna(0.0)
    return table.drop(columns="poly_bar_end")


def pair_window(pair, candles, bars, data_end):
    """The first and last bar end at which both venues of a pair can be compared.

    First: the later of Kalshi's first candle and Polymarket's first trade bar
    (before that, one venue has no price yet).
    Last: the earliest of
      * Kalshi's close, rounded DOWN to the hour. Kalshi closes just before the
        news (1 minute before the CPI release, a few minutes before the Fed's
        2 PM ET statement). The bar that contains the close would mix
        Polymarket trades made after the news with Kalshi's last quote from
        before it, so it is left out (docs/matching.md, risk 4).
      * Polymarket's close, rounded down to the hour;
      * `data_end`: the last hour the Kalshi candles were downloaded for
        (markets that are still open).
    Returns (first, last); last < first means the venues never overlap.
    """
    first = max(candles["period_end"].min(), bars["bar_end"].min())
    ends = [pair["kalshi_close_time"], pair["poly_closed_time"], data_end]
    last = min(to_utc(end).floor("h") for end in ends if not pd.isna(end))
    return to_utc(first), last


def align_pair(pair, candles, bars, data_end):
    """One pair's hourly table: both venues' prices at every common bar end.

    pair:     a row of pairs.parquet
    candles:  the Kalshi candles of this pair's market
    bars:     the Polymarket trade bars of this pair's market
    data_end: see pair_window()
    Columns: pair_id, topic, side, bar_end, the Kalshi columns of
    kalshi_hourly(), the Polymarket columns of poly_hourly(), and
    hours_to_resolution (hours from the bar end to Kalshi's close, the moment
    the news arrives). Empty if the venues never overlap.
    """
    if candles.empty or bars.empty:
        return pd.DataFrame()
    first, last = pair_window(pair, candles, bars, data_end)
    grid = hourly_grid(first, last)
    if len(grid) == 0:
        return pd.DataFrame()
    kalshi_part = kalshi_hourly(candles, grid)
    poly_part = poly_hourly(bars, pair["poly_yes_equals"], grid)
    table = kalshi_part.merge(poly_part, on="bar_end")
    close = to_utc(pair["kalshi_close_time"])
    table.insert(0, "pair_id", pair["pair_id"])
    table.insert(1, "topic", TOPIC_NAMES[pair["measure"]])
    table.insert(2, "side", pair["poly_yes_equals"])
    table["hours_to_resolution"] = (close - table["bar_end"]) / HOUR
    return table


def build_panel(pairs, candles, bars, data_end=None):
    """The hourly table of every pair (align_pair()), one below the other.

    data_end: the last hour of data for markets that are still open; by default
    the latest candle in `candles` (the hour of the Kalshi download).
    Adds poly_fresh: True when the Polymarket price is at most
    MAX_POLY_AGE_HOURS old (the main results use only these hours).
    """
    if data_end is None:
        data_end = candles["period_end"].max()
    candles_by_ticker = dict(tuple(candles.groupby("ticker")))
    bars_by_market = dict(tuple(bars.groupby("market_id")))
    tables = []
    for pair in pairs.itertuples(index=False):
        pair = pair._asdict()
        table = align_pair(pair,
                           candles_by_ticker.get(pair["kalshi_ticker"], pd.DataFrame()),
                           bars_by_market.get(pair["poly_market_id"], pd.DataFrame()),
                           data_end)
        if not table.empty:
            tables.append(table)
    panel = pd.concat(tables, ignore_index=True)
    panel["poly_fresh"] = panel["poly_age_hours"] <= MAX_POLY_AGE_HOURS
    return panel


def poly_prices_cross_check(panel, poly_prices, pairs, bucket_seconds=10800):
    """Put Polymarket's own price history next to our trade-bar prices, for a cross-check.

    poly_prices: poly_prices.parquet. Its "settlement" rows are dropped: they
                 are the final payout (1 or 0), not a traded price.
    Only the series of `bucket_seconds` (10800 = the 3-hour series that covers
    every market's whole life) and the token that pays when Kalshi YES pays
    are used, so no conversion is needed. A bucket's timestamp is its START
    (polymarket.parse_price_history()). Each point is matched twice: with our
    price at the bucket's start and at its end (start + 3 hours), because the
    API does not say which moment inside the bucket its price describes.
    Returns one row per matched point: pair_id, topic, timestamp, prices_price,
    trade_price_at_start, trade_price_at_end, and the trade price's ages.
    """
    prices = poly_prices[(poly_prices["point_type"] != "settlement")
                         & (poly_prices["bucket_seconds"] == bucket_seconds)].copy()
    prices["timestamp"] = prices["timestamp"].astype(TIME_DTYPE)
    sides = pairs[["pair_id", "poly_market_id", "poly_yes_equals"]].rename(
        columns={"poly_market_id": "market_id"})
    prices = prices.merge(sides, on="market_id")
    same_token = np.where(prices["poly_yes_equals"] == "Kalshi YES", "yes", "no")
    prices = prices[prices["outcome"] == same_token]
    prices = prices[["pair_id", "timestamp", "price"]].rename(columns={"price": "prices_price"})

    ours = panel[["pair_id", "topic", "bar_end", "poly_price", "poly_age_hours"]]
    at_start = ours.rename(columns={"bar_end": "timestamp", "poly_price": "trade_price_at_start",
                                    "poly_age_hours": "age_at_start"})
    at_end = ours.drop(columns="topic").rename(columns={"poly_price": "trade_price_at_end",
                                                        "poly_age_hours": "age_at_end"})
    at_end["timestamp"] = at_end["bar_end"] - pd.Timedelta(seconds=bucket_seconds)
    matched = prices.merge(at_start, on=["pair_id", "timestamp"])
    matched = matched.merge(at_end.drop(columns="bar_end"), on=["pair_id", "timestamp"])
    return matched


# ---------------------------------------------------------------------------
# 2. Gaps
# ---------------------------------------------------------------------------

def raw_gap(poly_price, kalshi_mid):
    """The raw gap: Polymarket's last trade minus Kalshi's mid, both in Kalshi YES terms.

        raw gap = Polymarket last trade - Kalshi mid
    Example: Polymarket 0.47, Kalshi bid 0.40 / ask 0.42 (mid 0.41) -> +0.06:
    Polymarket thinks Kalshi's YES outcome is 6 points more likely than Kalshi
    does. Not a trading profit: it ignores the bid-ask spread and the fees.
    (Kalshi's mid: kalshi_mid(); Polymarket's price: poly_in_kalshi_yes_terms().)
    """
    return poly_price - kalshi_mid


def typical_spread(orderbooks, market_ids):
    """The median bid-ask spread of some Polymarket markets' order books, in dollars.

    orderbooks: poly_orderbooks.parquet (one row per market: best YES bid and ask)
    market_ids: the markets to use (e.g. the paired markets)
        spread = best YES ask - best YES bid
    (the NO book is the mirror image of the YES book, so its spread is the
    same: https://docs.polymarket.com/market-data/prices-order-books.md).
    Books without a bid or an ask are skipped. Returns (median spread, the spreads used).
    """
    books = orderbooks[orderbooks["market_id"].isin(market_ids)]
    spreads = (books["best_yes_ask"] - books["best_yes_bid"]).dropna()
    return float(spreads.median()), spreads


def poly_buy_price(price, half_spread):
    """The estimated Polymarket ask of a token whose last trade was at `price`.

        ask ~ last trade + half a typical spread
    A last trade is a mix of trades at the bid and at the ask, so on average
    it sits near the middle; buying costs about half a spread more. The result
    is kept between Polymarket's lowest and highest price (0.001 and 0.999).
    """
    return np.clip(np.asarray(price, dtype=float) + half_spread, POLY_MIN_PRICE, POLY_MAX_PRICE)


def kalshi_fees(prices, tickers):
    """Kalshi's taker fee per contract (src/arb.py) for each price paid, in dollars.

        fee per contract = M x 0.07 x P x (1 - P)    (https://kalshi.com/docs/kalshi-fee-schedule.pdf)
    computed by arb.kalshi_fee_per_contract() with each series' multiplier M
    (arb.kalshi_multiplier()), before Kalshi's rounding: for an order of many
    contracts the rounding adds less than $0.0001 per ORDER. The formula has
    no date in it: Kalshi's fee-change history has no change for our series
    (docs/arbitrage.md). Each different (price, M) is computed once.
    """
    keys = pd.DataFrame({"price": np.round(np.asarray(prices, dtype=float), 6),
                         "multiplier": [arb.kalshi_multiplier(ticker) for ticker in tickers]})
    unique = keys.drop_duplicates().dropna().copy()
    unique["fee"] = [float(arb.kalshi_fee_per_contract(price, multiplier))
                     for price, multiplier in zip(unique["price"], unique["multiplier"])]
    return keys.merge(unique, how="left", on=["price", "multiplier"])["fee"].to_numpy()


def poly_fees(prices, market_ids, timestamps, poly_markets):
    """Polymarket's taker fee per share (src/arb.py) for each price paid, in force at each timestamp.

        fee per share = feeRate x (p x (1 - p)) ** exponent   (https://docs.polymarket.com/trading/fees.md)
    computed by arb.polymarket_fee_per_share() from each market's own fee fields
    in poly_markets.parquet. It is ZERO before Fee Structure V2 (2026-03-30,
    https://docs.polymarket.com/changelog/predictions.md) and for markets
    without a fee schedule. That date is the only way the time enters the
    formula, so each (market, price, before/after 2026-03-30) is computed once.
    Before rounding (Polymarket rounds each fill's fee to 5 decimals).
    """
    keys = pd.DataFrame({"price": np.round(np.asarray(prices, dtype=float), 6),
                         "market_id": np.asarray(market_ids),
                         "timestamp": np.asarray(timestamps)})
    keys["after_v2"] = keys["timestamp"] >= arb.FEE_STRUCTURE_V2_START
    unique = keys.dropna(subset=["price"]).drop_duplicates(["price", "market_id", "after_v2"]).copy()
    markets = poly_markets.set_index("market_id")
    unique["fee"] = [float(arb.polymarket_fee_per_share(markets.loc[market_id], price, timestamp))
                     for price, market_id, timestamp in
                     zip(unique["price"], unique["market_id"], unique["timestamp"])]
    merged = keys.merge(unique[["price", "market_id", "after_v2", "fee"]], how="left",
                        on=["price", "market_id", "after_v2"])
    return merged["fee"].to_numpy()


EXECUTABLE_COLUMNS = [
    "yes_kalshi_cost", "yes_poly_cost", "yes_gross", "yes_fees", "yes_net",
    "no_kalshi_cost", "no_poly_cost", "no_gross", "no_fees", "no_net",
    "exec_gross", "exec_net", "exec_direction", "exec_capital",
]


def executable_gaps(panel, pairs, poly_markets, half_spread):
    """The executable-style gap of every pair-hour: the profit per contract of the better trade, after fees.

    panel:        the hourly table (build_panel()): kalshi_bid, kalshi_ask,
                  poly_price (in Kalshi YES terms), bar_end, pair_id
    pairs:        pairs.parquet (Kalshi ticker, Polymarket market, side of each pair)
    poly_markets: poly_markets.parquet (Polymarket's fee fields)
    half_spread:  half a typical Polymarket bid-ask spread, in dollars (e.g. 0.005)

    Two contracts that together always pay exactly $1 (docs/arbitrage.md):
      direction "yes": buy Kalshi YES at Kalshi's ask, and the Polymarket token
          that pays when Kalshi YES does NOT, at (1 - poly_price) + half_spread
      direction "no":  buy Kalshi NO at 1 - Kalshi's YES bid (a NO is bought from
          someone who bids for YES: https://docs.kalshi.com/getting_started/orderbook_responses),
          and the Polymarket token that pays when Kalshi YES does, at
          poly_price + half_spread
    For each direction (per contract, in dollars):
        gross = 1 - Kalshi cost - Polymarket cost
        net   = gross - Kalshi fee - Polymarket fee
    with the fees of kalshi_fees() and poly_fees() at the prices paid.
    exec_net is the better direction's net (only one direction can make money),
    exec_direction names it like arb.py does (e.g. "Kalshi YES + Polymarket NO"),
    and exec_capital is the cash it ties up per contract (both prices + both fees).
    A direction without the Kalshi quote it needs (no ask, or no bid) is empty.
    Returns a table with EXECUTABLE_COLUMNS, in the order of `panel`.
    """
    info = pairs.set_index("pair_id")[["kalshi_ticker", "poly_market_id", "poly_yes_equals"]]
    tickers = panel["pair_id"].map(info["kalshi_ticker"]).to_numpy()
    markets = panel["pair_id"].map(info["poly_market_id"]).to_numpy()
    sides = panel["pair_id"].map(info["poly_yes_equals"]).to_numpy()
    times = panel["bar_end"].to_numpy()
    poly = panel["poly_price"].to_numpy(dtype=float)

    out = pd.DataFrame(index=panel.index)
    legs = {"yes": (panel["kalshi_ask"].to_numpy(dtype=float), poly_buy_price(1 - poly, half_spread)),
            "no": (1 - panel["kalshi_bid"].to_numpy(dtype=float), poly_buy_price(poly, half_spread))}
    for name, (kalshi_cost, poly_cost) in legs.items():
        poly_cost = np.where(np.isnan(poly), np.nan, poly_cost)
        fees = kalshi_fees(kalshi_cost, tickers) + poly_fees(poly_cost, markets, times, poly_markets)
        out[f"{name}_kalshi_cost"] = kalshi_cost
        out[f"{name}_poly_cost"] = poly_cost
        out[f"{name}_gross"] = 1 - kalshi_cost - poly_cost
        out[f"{name}_fees"] = fees
        out[f"{name}_net"] = out[f"{name}_gross"] - fees

    yes_better = out["yes_net"].fillna(-np.inf) >= out["no_net"].fillna(-np.inf)
    out["exec_gross"] = np.where(yes_better, out["yes_gross"], out["no_gross"])
    out["exec_net"] = np.fmax(out["yes_net"], out["no_net"])     # fmax ignores an empty direction
    names = {"yes": [arb.direction_name("yes", arb.poly_outcome_to_buy("yes", side)) for side in sides],
             "no": [arb.direction_name("no", arb.poly_outcome_to_buy("no", side)) for side in sides]}
    out["exec_direction"] = np.where(yes_better, names["yes"], names["no"])
    out.loc[out["exec_net"].isna(), ["exec_direction", "exec_gross"]] = [None, np.nan]
    out["exec_capital"] = np.where(yes_better,
                                   out["yes_kalshi_cost"] + out["yes_poly_cost"] + out["yes_fees"],
                                   out["no_kalshi_cost"] + out["no_poly_cost"] + out["no_fees"])
    out.loc[out["exec_net"].isna(), "exec_capital"] = np.nan
    return out[EXECUTABLE_COLUMNS]


# ---------------------------------------------------------------------------
# 4. Persistence: AR(1) and half-life
# ---------------------------------------------------------------------------

def previous_hour(table, column, hours=1):
    """The value of `column` in the same pair `hours` hours earlier; NaN if that hour is not in `table`.

    table: rows with pair_id, bar_end and `column` (one row per pair and hour).
    Only the row EXACTLY `hours` hours earlier counts: if that hour was left
    out of `table` (e.g. a stale Polymarket price), the result is NaN, so a
    gap in the data never makes two distant hours look like neighbours.
    Returns an array in the order of `table`.
    """
    earlier = table[["pair_id", "bar_end", column]].copy()
    earlier["bar_end"] = earlier["bar_end"] + pd.Timedelta(hours=hours)
    merged = table[["pair_id", "bar_end"]].merge(earlier, on=["pair_id", "bar_end"], how="left")
    return merged[column].to_numpy()


def half_life(rho):
    """How many hours until half of a deviation is gone, for an AR(1) coefficient rho (per hour).

        gap(t) = a + rho x gap(t-1) + noise: a deviation from the pair's average
        shrinks to rho^h of its size after h hours, so half of it is gone when
        rho^h = 0.5, that is after  h = ln(0.5) / ln(rho)  hours.
    Examples: rho 0.5 -> 1 hour, rho 0.9 -> 6.6 hours, rho 0.99 -> 69 hours.
    rho <= 0: the deviation is gone (or flips sign) within the hour -> 0.
    rho >= 1: it never shrinks -> infinity.
    """
    if np.isnan(rho):
        return np.nan
    if rho <= 0:
        return 0.0
    if rho >= 1:
        return np.inf
    return float(np.log(0.5) / np.log(rho))


def ar1(gap, gap_before):
    """Fit gap(t) = a + rho x gap(t-1) + noise by ordinary least squares (OLS).

    gap, gap_before: the gap in each hour and in the hour before (NaN = no
                     pair of neighbouring hours; those rows are skipped).
    Returns a dict: rho and its standard error, a, n (number of hour pairs),
    the pair's long-run average gap a / (1 - rho), and half_life(rho) in hours.
    """
    import statsmodels.api as sm   # imported here: only this part of gaps.py needs it

    gap, gap_before = np.asarray(gap, dtype=float), np.asarray(gap_before, dtype=float)
    keep = ~np.isnan(gap) & ~np.isnan(gap_before)
    n = int(keep.sum())
    if n < 3 or np.ptp(gap_before[keep]) == 0:   # no variation in the lagged gap: nothing to fit
        return {"rho": np.nan, "rho_se": np.nan, "a": np.nan, "n": n, "long_run_mean": np.nan,
                "half_life": np.nan}
    regressors = np.column_stack([np.ones(n), gap_before[keep]])   # a column of 1s for a, then gap(t-1)
    fit = sm.OLS(gap[keep], regressors).fit()
    a, rho = fit.params
    return {"rho": float(rho), "rho_se": float(fit.bse[1]), "a": float(a), "n": n,
            "long_run_mean": float(a / (1 - rho)) if rho != 1 else np.nan,
            "half_life": half_life(rho)}


# ---------------------------------------------------------------------------
# 6. Price discovery: Granger causality
# ---------------------------------------------------------------------------

# A Granger test needs at least this many hours in which each venue's price moved.
MIN_PRICE_MOVES = 20


def granger_test(y, own_lags, other_lags):
    """Does the other venue's past help to predict y, beyond y's own past? (A Granger causality test.)

    y:           this venue's price change in hour t (one value per row)
    own_lags:    2-D array, column k = this venue's change in hour t - (k + 1)
    other_lags:  2-D array, column k = the other venue's change in hour t - (k + 1)
    Regression (OLS):  y(t) = c + sum_k b_k y(t-k) + sum_k g_k x(t-k) + noise
    Test: all g_k = 0, with an F-test that uses heteroskedasticity-robust
    (HC1) standard errors, because price changes are much larger around news
    than in quiet hours. Rows with any missing value are skipped.
    Returns a dict: F (the test statistic), p (its p-value), n (rows used).
    A small p (e.g. < 0.05) means: the other venue's past changes help to
    predict this venue's next change, i.e. the other venue "leads".
    """
    import statsmodels.api as sm   # imported here: only this part of gaps.py needs it

    y = np.asarray(y, dtype=float)
    own_lags = np.asarray(own_lags, dtype=float).reshape(len(y), -1)
    other_lags = np.asarray(other_lags, dtype=float).reshape(len(y), -1)
    keep = ~np.isnan(y) & ~np.isnan(own_lags).any(axis=1) & ~np.isnan(other_lags).any(axis=1)
    n, lags = int(keep.sum()), other_lags.shape[1]
    no_result = {"F": np.nan, "p": np.nan, "n": n}
    regressors = np.column_stack([np.ones(n), own_lags[keep], other_lags[keep]])
    # A test needs enough hours in which BOTH venues' prices actually moved, and
    # regressors that are not copies of each other (full rank); otherwise no result.
    moves = min(np.count_nonzero(y[keep]), np.count_nonzero(other_lags[keep][:, 0]))
    if moves < MIN_PRICE_MOVES or np.linalg.matrix_rank(regressors) < regressors.shape[1]:
        return no_result
    fit = sm.OLS(y[keep], regressors).fit(cov_type="HC1")
    # One row per g_k: pick the coefficient of the k-th lag of the other venue.
    tested = regressors.shape[1] - lags + np.arange(lags)
    if np.linalg.matrix_rank(fit.cov_params()[np.ix_(tested, tested)]) < lags:
        return no_result   # the robust variance of the tested coefficients is degenerate
    restriction = np.zeros((lags, regressors.shape[1]))
    restriction[np.arange(lags), tested] = 1
    test = fit.f_test(restriction)
    return {"F": float(np.squeeze(test.fvalue)), "p": float(test.pvalue), "n": n}


# ---------------------------------------------------------------------------
# 7. Resolution risk
# ---------------------------------------------------------------------------

def direction_payoffs(kalshi_result, poly_result, side):
    """What each direction of a cross-venue position paid at settlement, in dollars per contract pair.

    kalshi_result: how Kalshi's YES settled, "yes" or "no"
    poly_result:   how Polymarket's YES settled, "yes" or "no"
    side:          the pair's poly_yes_equals column
    Returns (pays_yes_direction, pays_no_direction):
      direction "yes" = Kalshi YES + the Polymarket token that pays when Kalshi YES fails
      direction "no"  = Kalshi NO  + the Polymarket token that pays when Kalshi YES happens
    (the same directions as executable_gaps()). Each Kalshi contract and each
    Polymarket token pays $1 if its outcome wins and $0 if not
    (https://docs.kalshi.com/getting_started/orderbook_responses,
    https://docs.polymarket.com/concepts/markets-events.md).
    If both venues settle the same outcome, each direction pays exactly $1.
    If they settle differently, one direction pays $2 and the other $0: the
    position that looked riskless is a bet.
    """
    for result in (kalshi_result, poly_result):
        if result not in ("yes", "no"):
            raise ValueError(f"unknown result {result!r}: expected 'yes' or 'no'")
    same_token = "yes" if side == "Kalshi YES" else "no"   # the Polymarket token that pays with Kalshi YES
    if side not in ("Kalshi YES", "Kalshi NO"):
        raise ValueError(f"unknown side {side!r}: expected 'Kalshi YES' or 'Kalshi NO'")
    kalshi_yes_paid = kalshi_result == "yes"
    same_token_paid = poly_result == same_token
    pays_yes_direction = int(kalshi_yes_paid) + int(not same_token_paid)
    pays_no_direction = int(not kalshi_yes_paid) + int(same_token_paid)
    return pays_yes_direction, pays_no_direction
