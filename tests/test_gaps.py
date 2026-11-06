"""
Unit tests for src/gaps.py: the hourly alignment of both venues and the gap
formulas of notebooks/01_historical_gaps.ipynb. Run them with:  pytest

The tests use small hand-made tables (no internet, no data files), so every
expected number can be checked on paper; the comments show the arithmetic.
Sources of the price rules tested here:
  * Kalshi candles exist only for hours in which something changed:
    https://docs.kalshi.com/api-reference/market/get-market-candlesticks
  * A Polymarket YES and NO token pay $1 together, and the NO book mirrors the
    YES book: https://docs.polymarket.com/concepts/markets-events.md,
    https://docs.polymarket.com/market-data/prices-order-books.md
"""

import math

import numpy as np
import pandas as pd
import pytest

import gaps


def utc(text):
    """A UTC timestamp from text, e.g. utc("2026-01-01 10:00")."""
    return pd.Timestamp(text, tz="UTC")


def candle(period_end, bid, ask, price, volume):
    """One row of kalshi_candles.parquet (only the columns gaps.py uses)."""
    return {"ticker": "K", "period_end": utc(period_end), "yes_bid_close": bid, "yes_ask_close": ask,
            "price_close": price, "volume": volume}


def bar(bar_end, outcome, last_price, last_trade_time, volume):
    """One row of poly_trade_bars.parquet (only the columns gaps.py uses)."""
    return {"market_id": "P", "outcome": outcome, "bar_end": utc(bar_end), "last_price": last_price,
            "last_trade_time": utc(last_trade_time), "volume": volume}


# ---------------------------------------------------------------------------
# 1. The hourly grid and the Kalshi side
# ---------------------------------------------------------------------------

def test_hourly_grid_includes_both_ends():
    grid = gaps.hourly_grid("2026-01-01 10:00", "2026-01-01 12:00")
    assert list(grid) == [utc("2026-01-01 10:00"), utc("2026-01-01 11:00"), utc("2026-01-01 12:00")]
    assert len(gaps.hourly_grid("2026-01-01 12:00", "2026-01-01 10:00")) == 0


def test_kalshi_hourly_carries_the_last_candle_forward_and_keeps_empty_quotes_empty():
    candles = pd.DataFrame([
        candle("2026-01-01 10:00", 0.40, 0.50, 0.45, 10),
        # at 12:00 nobody bids any more (NaN) and nothing trades (no price)
        candle("2026-01-01 12:00", math.nan, 0.60, math.nan, 0),
    ])
    grid = gaps.hourly_grid("2026-01-01 10:00", "2026-01-01 14:00")
    table = gaps.kalshi_hourly(candles, grid)

    # 11:00 has no candle: the 10:00 candle still holds
    assert table["kalshi_bid"].tolist()[:2] == [0.40, 0.40]
    # from 12:00 on the bid is EMPTY: the old 0.40 bid is not filled in
    assert table["kalshi_bid"].iloc[2:].isna().all()
    assert table["kalshi_ask"].tolist() == [0.50, 0.50, 0.60, 0.60, 0.60]
    # the last trade (0.45 at 10:00) still counts after the trade-less 12:00 candle
    assert table["kalshi_last"].tolist() == [0.45] * 5
    # volume belongs to its own hour only
    assert table["kalshi_volume"].tolist() == [10, 0, 0, 0, 0]
    assert table["kalshi_hours_since_candle"].tolist() == [0, 1, 0, 1, 2]


def test_kalshi_hourly_before_the_first_candle_is_empty():
    candles = pd.DataFrame([candle("2026-01-01 11:00", 0.40, 0.50, 0.45, 10)])
    table = gaps.kalshi_hourly(candles, gaps.hourly_grid("2026-01-01 10:00", "2026-01-01 11:00"))
    assert math.isnan(table["kalshi_ask"].iloc[0])
    assert table["kalshi_ask"].iloc[1] == 0.50


def test_kalshi_mid_needs_both_quotes():
    assert gaps.kalshi_mid(0.40, 0.44) == pytest.approx(0.42)
    assert math.isnan(gaps.kalshi_mid(math.nan, 0.44))     # nobody bids: no mid
    mids = gaps.kalshi_mid(pd.Series([0.10, 0.50]), pd.Series([0.12, math.nan]))
    assert mids.iloc[0] == pytest.approx(0.11) and math.isnan(mids.iloc[1])


# ---------------------------------------------------------------------------
# 1. The Polymarket side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("side, outcome, price, expected", [
    ("Kalshi YES", "yes", 0.30, 0.30),   # Polymarket YES = Kalshi YES
    ("Kalshi YES", "no", 0.30, 0.70),    # NO at 0.30 = YES at 1 - 0.30
    ("Kalshi NO", "yes", 0.30, 0.70),    # Polymarket YES = Kalshi NO, so Kalshi YES = 1 - 0.30
    ("Kalshi NO", "no", 0.30, 0.30),     # Polymarket NO = Kalshi YES
])
def test_poly_in_kalshi_yes_terms(side, outcome, price, expected):
    assert gaps.poly_in_kalshi_yes_terms(price, outcome, side) == pytest.approx(expected)


def test_poly_in_kalshi_yes_terms_works_on_columns_and_rejects_unknown_sides():
    result = gaps.poly_in_kalshi_yes_terms(pd.Series([0.2, 0.2]), pd.Series(["yes", "no"]), "Kalshi YES")
    assert result == pytest.approx([0.2, 0.8])
    with pytest.raises(ValueError):
        gaps.poly_in_kalshi_yes_terms(0.2, "yes", "same")


def test_poly_hourly_uses_the_latest_trade_of_either_token_and_records_its_age():
    bars = pd.DataFrame([
        bar("2026-01-01 11:00", "yes", 0.40, "2026-01-01 10:20", 100),
        bar("2026-01-01 11:00", "no", 0.55, "2026-01-01 10:50", 50),   # later: this one counts
    ])
    grid = gaps.hourly_grid("2026-01-01 11:00", "2026-01-01 13:00")
    table = gaps.poly_hourly(bars, "Kalshi YES", grid)
    # NO at 0.55 -> Kalshi YES terms 1 - 0.55 = 0.45, carried forward
    assert table["poly_price"].tolist() == pytest.approx([0.45, 0.45, 0.45])
    assert table["poly_token"].tolist() == ["no", "no", "no"]
    # last trade 10:50: 10 minutes old at 11:00, 2 h 10 min old at 13:00
    assert table["poly_age_hours"].tolist() == pytest.approx([10 / 60, 1 + 10 / 60, 2 + 10 / 60])
    # YES and NO shares of the hour are added up; later hours traded nothing
    assert table["poly_volume"].tolist() == [150, 0, 0]


def test_poly_hourly_prefers_the_same_token_when_both_trade_in_the_same_second():
    bars = pd.DataFrame([
        bar("2026-01-01 11:00", "yes", 0.40, "2026-01-01 10:20", 1),
        bar("2026-01-01 11:00", "no", 0.62, "2026-01-01 10:20", 1),
    ])
    grid = gaps.hourly_grid("2026-01-01 11:00", "2026-01-01 11:00")
    # side "Kalshi NO": the NO token pays when Kalshi YES pays, so its 0.62 is used as it is
    table = gaps.poly_hourly(bars, "Kalshi NO", grid)
    assert table["poly_price"].iloc[0] == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# 1. The common window and the panel
# ---------------------------------------------------------------------------

def make_pair(kalshi_close, poly_close=None, side="Kalshi YES"):
    """A row of pairs.parquet with only the columns gaps.py uses."""
    return {"pair_id": "K / P", "measure": "cpi_mom", "poly_yes_equals": side, "kalshi_ticker": "K",
            "poly_market_id": "P", "kalshi_close_time": utc(kalshi_close),
            "poly_closed_time": utc(poly_close) if poly_close else pd.NaT}


def test_pair_window_drops_the_hour_that_contains_kalshis_close():
    candles = pd.DataFrame([candle("2026-01-01 08:00", 0.4, 0.5, 0.45, 1)])
    bars = pd.DataFrame([bar("2026-01-01 09:00", "yes", 0.4, "2026-01-01 08:30", 1)])
    # Kalshi closes at 13:29 (one minute before an 8:30 ET CPI release): the 13:00-14:00
    # bar would hold Polymarket trades made after the news, so the last bar ends at 13:00.
    pair = make_pair("2026-01-01 13:29", poly_close="2026-01-01 16:00")
    first, last = gaps.pair_window(pair, candles, bars, data_end=utc("2026-02-01 00:00"))
    assert first == utc("2026-01-01 09:00")   # Polymarket's first bar is the later start
    assert last == utc("2026-01-01 13:00")


def test_build_panel_marks_polymarket_prices_older_than_24_hours():
    candles = pd.DataFrame([candle("2026-01-01 00:00", 0.4, 0.5, 0.45, 1)])
    bars = pd.DataFrame([bar("2026-01-01 01:00", "yes", 0.42, "2026-01-01 00:30", 1)])
    pairs = pd.DataFrame([make_pair("2026-01-03 00:10")])
    panel = gaps.build_panel(pairs, candles, bars, data_end=utc("2026-02-01 00:00"))
    # bar ends from 01:00 on 1 Jan to 00:00 on 3 Jan; the price is from 00:30 on 1 Jan
    assert panel["bar_end"].iloc[0] == utc("2026-01-01 01:00")
    assert panel["bar_end"].iloc[-1] == utc("2026-01-03 00:00")
    fresh = panel.set_index("bar_end")["poly_fresh"]
    assert fresh[utc("2026-01-02 00:00")]          # 23.5 hours old: kept
    assert not fresh[utc("2026-01-02 01:00")]      # 24.5 hours old: left out
    assert panel["topic"].iloc[0] == "CPI MoM"
    assert panel["hours_to_resolution"].iloc[-1] == pytest.approx(10 / 60)


# ---------------------------------------------------------------------------
# 2. Gaps
# ---------------------------------------------------------------------------

ECONOMICS_MARKET = {"market_id": "P", "fees_enabled": True, "fee_rate": 0.05, "fee_exponent": 1.0,
                    "fee_taker_only": True}
AFTER_V2 = "2026-11-04 12:00"     # Polymarket charges Economics fees (from 2026-03-30 on)
BEFORE_V2 = "2026-03-01 12:00"    # no Polymarket fee yet


def gap_inputs(bid, ask, poly_price, when=AFTER_V2, side="Kalshi YES"):
    """A one-row panel, the matching pairs table and Polymarket's fee fields."""
    panel = pd.DataFrame({"pair_id": ["K / P"], "bar_end": [utc(when)], "kalshi_bid": [bid],
                          "kalshi_ask": [ask], "poly_price": [poly_price]})
    pairs = pd.DataFrame([{"pair_id": "K / P", "kalshi_ticker": "KXCPI-26OCT-T0.1",
                           "poly_market_id": "P", "poly_yes_equals": side}])
    return panel, pairs, pd.DataFrame([ECONOMICS_MARKET])


def test_raw_gap_is_polymarket_minus_kalshi_mid():
    # Polymarket 0.47; Kalshi bid 0.40, ask 0.42 -> mid 0.41 -> gap +0.06
    assert gaps.raw_gap(0.47, gaps.kalshi_mid(0.40, 0.42)) == pytest.approx(0.06)


def test_typical_spread_is_the_median_of_the_chosen_books():
    books = pd.DataFrame({"market_id": ["a", "b", "c", "d"],
                          "best_yes_bid": [0.40, 0.10, 0.001, 0.30],
                          "best_yes_ask": [0.41, 0.12, 0.002, 0.90]})
    # a: 0.01, b: 0.02, c: 0.001 -> median 0.01; d is not one of ours
    median, spreads = gaps.typical_spread(books, ["a", "b", "c"])
    assert median == pytest.approx(0.01)
    assert len(spreads) == 3


def test_poly_buy_price_adds_half_a_spread_and_stays_inside_0_and_1():
    assert gaps.poly_buy_price(0.47, 0.005) == pytest.approx(0.475)
    assert gaps.poly_buy_price(0.998, 0.005) == pytest.approx(0.999)
    assert gaps.poly_buy_price(-0.004, 0.0) == pytest.approx(0.001)


def test_executable_gap_by_hand_after_polymarket_fees_started():
    panel, pairs, markets = gap_inputs(bid=0.40, ask=0.42, poly_price=0.47)
    row = gaps.executable_gaps(panel, pairs, markets, half_spread=0.005).iloc[0]
    # Direction yes: Kalshi YES at 0.42 + Polymarket NO at 1 - 0.47 + 0.005 = 0.535
    #   gross = 1 - 0.42 - 0.535 = 0.045
    #   Kalshi fee 0.07 x 0.42 x 0.58 = 0.017052; Polymarket fee 0.05 x 0.535 x 0.465 = 0.01243875
    #   net = 0.045 - 0.017052 - 0.01243875 = 0.01550925
    assert row["yes_poly_cost"] == pytest.approx(0.535)
    assert row["yes_gross"] == pytest.approx(0.045)
    assert row["yes_fees"] == pytest.approx(0.017052 + 0.01243875)
    assert row["yes_net"] == pytest.approx(0.01550925)
    # Direction no: Kalshi NO at 1 - 0.40 = 0.60 + Polymarket YES at 0.475
    #   gross = 1 - 0.60 - 0.475 = -0.075; fees 0.0168 + 0.01246875 -> net -0.10426875
    assert row["no_gross"] == pytest.approx(-0.075)
    assert row["no_net"] == pytest.approx(-0.10426875)
    # The better direction is "yes"; capital = 0.42 + 0.535 + fees
    assert row["exec_net"] == pytest.approx(0.01550925)
    assert row["exec_gross"] == pytest.approx(0.045)
    assert row["exec_direction"] == "Kalshi YES + Polymarket NO"
    assert row["exec_capital"] == pytest.approx(0.42 + 0.535 + 0.017052 + 0.01243875)


def test_executable_gap_has_no_polymarket_fee_before_march_2026():
    panel, pairs, markets = gap_inputs(bid=0.40, ask=0.42, poly_price=0.47, when=BEFORE_V2)
    row = gaps.executable_gaps(panel, pairs, markets, half_spread=0.005).iloc[0]
    # only Kalshi's fee: 0.045 - 0.017052 = 0.027948
    assert row["yes_net"] == pytest.approx(0.027948)


def test_executable_gap_uses_the_side_to_name_the_polymarket_token():
    # side "Kalshi NO": the Polymarket token that pays when Kalshi YES fails is Polymarket YES
    panel, pairs, markets = gap_inputs(bid=0.40, ask=0.42, poly_price=0.47, side="Kalshi NO")
    row = gaps.executable_gaps(panel, pairs, markets, half_spread=0.005).iloc[0]
    assert row["exec_direction"] == "Kalshi YES + Polymarket YES"
    assert row["exec_net"] == pytest.approx(0.01550925)   # same prices, same money


def test_executable_gap_without_a_kalshi_bid_can_only_buy_kalshi_yes():
    panel, pairs, markets = gap_inputs(bid=math.nan, ask=0.42, poly_price=0.30)
    row = gaps.executable_gaps(panel, pairs, markets, half_spread=0.005).iloc[0]
    assert math.isnan(row["no_net"])
    # yes: 1 - 0.42 - 0.705 = -0.125, minus fees: still the best (only) direction
    assert row["exec_direction"] == "Kalshi YES + Polymarket NO"
    assert row["exec_gross"] == pytest.approx(-0.125)
    # without any Kalshi quote there is nothing to trade
    panel, pairs, markets = gap_inputs(bid=math.nan, ask=math.nan, poly_price=0.30)
    row = gaps.executable_gaps(panel, pairs, markets, half_spread=0.005).iloc[0]
    assert math.isnan(row["exec_net"]) and pd.isna(row["exec_direction"])


# ---------------------------------------------------------------------------
# 4. Persistence
# ---------------------------------------------------------------------------

def test_previous_hour_only_uses_the_hour_exactly_before():
    table = pd.DataFrame({"pair_id": ["A", "A", "A", "B"],
                          "bar_end": [utc("2026-01-01 10:00"), utc("2026-01-01 11:00"),
                                      utc("2026-01-01 13:00"), utc("2026-01-01 11:00")],
                          "gap": [0.01, 0.02, 0.03, 0.04]})
    before = gaps.previous_hour(table, "gap")
    # A 10:00 has no hour before; A 11:00 -> A 10:00; A 13:00: 12:00 is missing; B is another pair
    assert np.isnan(before[0]) and before[1] == 0.01 and np.isnan(before[2]) and np.isnan(before[3])


@pytest.mark.parametrize("rho, expected", [
    (0.5, 1.0),                              # 0.5^1 = 0.5
    (0.9, math.log(0.5) / math.log(0.9)),    # = 6.58 hours
    (0.0, 0.0), (-0.2, 0.0),                 # gone within the hour
    (1.0, math.inf), (1.05, math.inf),       # never shrinks
])
def test_half_life(rho, expected):
    assert gaps.half_life(rho) == pytest.approx(expected)


def test_ar1_recovers_a_known_coefficient():
    # A gap that halves towards 0.01 every hour, with no noise: gap(t) = 0.005 + 0.5 x gap(t-1)
    values = [0.09]
    for _ in range(20):
        values.append(0.005 + 0.5 * values[-1])
    gap = np.array(values[1:])
    before = np.array(values[:-1])
    result = gaps.ar1(gap, before)
    assert result["rho"] == pytest.approx(0.5)
    assert result["a"] == pytest.approx(0.005)
    assert result["long_run_mean"] == pytest.approx(0.01)     # 0.005 / (1 - 0.5)
    assert result["half_life"] == pytest.approx(1.0)
    assert result["n"] == 20
    # rows without a previous hour are skipped
    assert gaps.ar1(np.append(gap, 0.5), np.append(before, np.nan))["n"] == 20


# ---------------------------------------------------------------------------
# 6. Granger causality
# ---------------------------------------------------------------------------

def lag_columns(values, lags):
    """Column k = values shifted by k + 1 (NaN at the start), as gaps.granger_test() expects."""
    return np.column_stack([pd.Series(values).shift(k + 1).to_numpy() for k in range(lags)])


def test_granger_test_finds_a_leader_and_not_the_follower():
    rng = np.random.default_rng(20261105)
    leader = rng.normal(size=2000)
    # The follower copies half of the leader's change one hour later, plus its own noise.
    follower = 0.5 * np.roll(leader, 1) + rng.normal(size=2000)
    follower[0] = 0.0
    leads = gaps.granger_test(follower, lag_columns(follower, 3), lag_columns(leader, 3))
    follows = gaps.granger_test(leader, lag_columns(leader, 3), lag_columns(follower, 3))
    assert leads["p"] < 0.001                 # the leader's past predicts the follower
    assert follows["p"] > 0.05                # but not the other way round
    assert leads["n"] == 2000 - 3             # the first 3 rows have no full set of lags


def test_granger_test_without_movement_on_the_other_venue_gives_no_result():
    y = np.random.default_rng(1).normal(size=50)
    result = gaps.granger_test(y, lag_columns(y, 1), np.zeros((50, 1)))
    assert np.isnan(result["p"])


def test_granger_test_needs_enough_price_moves():
    y = np.random.default_rng(2).normal(size=200)
    other = np.zeros(200)
    other[[10, 50, 90]] = 0.01          # the other venue moved in only 3 hours
    result = gaps.granger_test(y, lag_columns(y, 1), lag_columns(other, 1))
    assert np.isnan(result["p"])


# ---------------------------------------------------------------------------
# 7. Resolution risk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kalshi, poly, side, expected", [
    ("yes", "yes", "Kalshi YES", (1, 1)),   # same outcome on both venues: each direction pays $1
    ("no", "no", "Kalshi YES", (1, 1)),
    ("yes", "no", "Kalshi NO", (1, 1)),     # opposite labels, but the same outcome
    # KXCPIYOY-25OCT-T3.1 / 654401: Kalshi YES paid, Polymarket YES (the same outcome) did not.
    # Kalshi YES + Polymarket NO paid twice; Kalshi NO + Polymarket YES lost both legs.
    ("yes", "no", "Kalshi YES", (2, 0)),
    # KXFEDDECISION-26SEP-H0 / 2118744: Kalshi NO paid, Polymarket YES paid as well.
    ("no", "yes", "Kalshi YES", (0, 2)),
    ("yes", "yes", "Kalshi NO", (2, 0)),
])
def test_direction_payoffs(kalshi, poly, side, expected):
    assert gaps.direction_payoffs(kalshi, poly, side) == expected


def test_direction_payoffs_rejects_unknown_results():
    with pytest.raises(ValueError):
        gaps.direction_payoffs("", "yes", "Kalshi YES")
