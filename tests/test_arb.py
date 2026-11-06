"""
Unit tests for src/arb.py: the two venues' fee formulas, the arbitrage
arithmetic and the returns. Run them from the project folder with:  pytest

None of these tests use the internet or the real data files: they use small,
hand-made order books whose results can be worked out on paper (the
expected numbers are written out in the comments).

Fee values from the venues' own documentation:
  * Kalshi's fee table: 100 contracts at $0.55 cost $1.74 in fees
    (https://kalshi.com/docs/kalshi-fee-schedule.pdf, read by hand on 2026-11-04,
    because the cloud session cannot download it)
  * Kalshi's rounding example: one contract at $0.055 with a $0.00363825 model
    fee costs $0.06 in total, a $0.005 fee
    (https://docs.kalshi.com/getting_started/fee_rounding.md)
  * Polymarket's Economics table (rate 0.05), 100 shares: $0.10 -> $0.45,
    $0.30 -> $1.05, $0.50 -> $1.25, $0.90 -> $0.45
    (https://docs.polymarket.com/trading/fees.md)
"""

import math
from decimal import Decimal as D

import pandas as pd
import pytest

import arb
import kalshi
import polymarket

AFTER_V2 = pd.Timestamp("2026-11-04 12:00", tz="UTC")    # Polymarket charges Economics fees
BEFORE_V2 = pd.Timestamp("2026-03-29 23:59", tz="UTC")   # no Economics fees yet

# Polymarket markets in the format of poly_markets.parquet (only the fee columns).
ECONOMICS_MARKET = {"market_id": "econ", "fees_enabled": True, "fee_rate": 0.05, "fee_exponent": 1.0,
                    "fee_taker_only": True, "fee_rebate_rate": 0.25}
NO_FEE_MARKET = {"market_id": "old", "fees_enabled": False, "fee_rate": math.nan,
                 "fee_exponent": math.nan, "fee_taker_only": pd.NA, "fee_rebate_rate": math.nan}


def market_with(**changes):
    """ECONOMICS_MARKET with some fields changed."""
    return {**ECONOMICS_MARKET, **changes}


# ---------------------------------------------------------------------------
# Kalshi's fee
# ---------------------------------------------------------------------------

def test_kalshi_fee_matches_kalshi_fee_table():
    # 0.07 x 100 x 0.55 x 0.45 = 1.7325, rounded up to a whole cent = $1.74 (Kalshi's table)
    assert arb.kalshi_fee(0.55, 100, precision=arb.CENT) == D("1.74")


def test_kalshi_fee_rounds_to_a_centicent_by_default():
    # The schedule's rule: cost + fee rounded up to $0.0001. 55.00 + 1.7325 = 56.7325 is
    # already a whole number of centicents, so the fee stays $1.7325.
    assert arb.kalshi_fee(0.55, 100) == D("1.7325")
    # 1 contract at $0.19: 0.07 x 0.19 x 0.81 = 0.010773 -> 0.19 + 0.010773 = 0.200773 -> 0.2008
    assert arb.kalshi_fee(0.19, 1) == D("0.0108")


def test_kalshi_fee_matches_kalshi_rounding_example():
    # docs.kalshi.com fee rounding page, "FCM-cleared fill": revenue -$0.055 and a model fee
    # of $0.00363825 = 0.07 x 0.055 x 0.945, i.e. one contract at $0.055.
    assert arb.kalshi_fee_per_contract("0.055") == D("0.00363825")
    assert arb.round_up(arb.kalshi_fee_per_contract("0.055"), arb.MICRODOLLAR) == D("0.003639")  # trade fee
    # The balance changes by -$0.06: trade fee + rounding fee = $0.005.
    assert arb.kalshi_fee("0.055", 1, precision=arb.CENT) == D("0.005")


def test_kalshi_fee_has_no_float_errors():
    # With floats 0.07 * 100 * 0.5 * 0.5 = 1.7500000000000002, which would round up to $1.76.
    assert arb.kalshi_fee(0.50, 100, precision=arb.CENT) == D("1.75")


def test_kalshi_fee_uses_the_multiplier():
    # M = 0.5: 0.5 x 1.7325 = 0.86625 -> $0.87 (whole cents), $0.8663 (centicents)
    assert arb.kalshi_fee(0.55, 100, multiplier=0.5, precision=arb.CENT) == D("0.87")
    assert arb.kalshi_fee(0.55, 100, multiplier=0.5) == D("0.8663")


def test_kalshi_fee_for_fractional_contracts():
    # 8.14 contracts at $0.55: fee 8.14 x 0.017325 = 0.1410255 -> up to $0.000001: 0.141026;
    # cost 4.477 + 0.141026 = 4.618026 -> up to $0.0001: 4.6181 -> fee 0.1411
    assert arb.kalshi_fee(0.55, "8.14") == D("0.1411")


def test_kalshi_fee_is_rounded_once_per_order():
    # Two fills of 1 contract at $0.05: 0.07 x 0.05 x 0.95 = 0.003325 each.
    # One order: cost 0.10 + 0.00665 -> $0.11, a fee of $0.01.
    assert arb.kalshi_fee_for_fills([(0.05, 1), (0.05, 1)], precision=arb.CENT) == D("0.01")
    # Two separate orders would pay $0.01 each.
    assert 2 * arb.kalshi_fee(0.05, 1, precision=arb.CENT) == D("0.02")


def test_kalshi_fee_is_zero_at_prices_0_and_1():
    assert arb.kalshi_fee(1, 10) == 0
    assert arb.kalshi_fee(0, 10) == 0


def test_kalshi_multiplier_of_our_series():
    for ticker in ["KXFEDDECISION-26DEC-H0", "KXCPI-26OCT-T0.1", "KXCPIYOY-26SEP-T2.9",
                   "FEDDECISION-24NOV-C25", "CPIYOY-24OCT-T2.4"]:
        assert arb.kalshi_multiplier(ticker) == 1
    assert arb.kalshi_multiplier("KXSOMETHINGNEW-26DEC") == arb.KALSHI_DEFAULT_MULTIPLIER == 1


# ---------------------------------------------------------------------------
# Polymarket's fee
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("price, fee", [(0.10, "0.45"), (0.30, "1.05"), (0.50, "1.25"), (0.90, "0.45")])
def test_polymarket_fee_matches_the_docs_economics_table(price, fee):
    # 100 shares x 0.05 x p x (1 - p), e.g. 100 x 0.05 x 0.5 x 0.5 = 1.25
    assert arb.polymarket_fee(ECONOMICS_MARKET, price, 100, AFTER_V2) == D(fee)


def test_polymarket_fee_is_rounded_to_5_decimals():
    # 0.05 x 0.999 x 0.001 = 0.00004995 -> 0.00005
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.999, 1, AFTER_V2) == D("0.00005")
    # 0.05 x 0.9999 x 0.0001 = 0.0000049995 -> 0: "anything smaller rounds to zero"
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.9999, 1, AFTER_V2) == 0


def test_polymarket_fee_uses_the_markets_own_rate():
    # The March 2026 CPI markets have rate 0.03: 100 x 0.03 x 0.25 = 0.75
    assert arb.polymarket_fee(market_with(fee_rate=0.03), 0.50, 100, AFTER_V2) == D("0.75")
    # The October 2026 Fed markets have rate 0 ("zero_fees")
    assert arb.polymarket_fee(market_with(fee_rate=0.0, fee_rebate_rate=0.0), 0.50, 100, AFTER_V2) == 0


def test_polymarket_fee_exponent_applies_to_the_price_component():
    # 100 x 0.05 x (0.5 x 0.5) ** 2 = 0.3125
    assert arb.polymarket_fee(market_with(fee_exponent=2.0), 0.50, 100, AFTER_V2) == D("0.3125")


def test_polymarket_fee_is_zero_without_a_fee_schedule():
    assert arb.polymarket_fee(NO_FEE_MARKET, 0.50, 100, AFTER_V2) == 0
    unknown = {**NO_FEE_MARKET, "fees_enabled": pd.NA}
    assert arb.polymarket_fee(unknown, 0.50, 100, AFTER_V2) == 0


def test_polymarket_fee_is_zero_before_fee_structure_v2():
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.50, 100, BEFORE_V2) == 0
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.50, 100, arb.FEE_STRUCTURE_V2_START) == D("1.25")
    # A time without a zone counts as UTC.
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.50, 100, "2026-03-29 23:59") == 0


def test_polymarket_makers_pay_nothing():
    assert arb.polymarket_fee(ECONOMICS_MARKET, 0.50, 100, AFTER_V2, taker=False) == 0
    assert arb.polymarket_fee(market_with(fee_taker_only=pd.NA), 0.50, 100, AFTER_V2, taker=False) == 0
    with pytest.raises(ValueError, match="charges makers"):
        arb.polymarket_fee(market_with(fee_taker_only=False), 0.50, 100, AFTER_V2, taker=False)


# ---------------------------------------------------------------------------
# Order books and directions
# ---------------------------------------------------------------------------

def test_poly_outcome_to_buy():
    # "Kalshi YES": Polymarket YES = Kalshi YES -> buy the other label
    assert arb.poly_outcome_to_buy("yes", "Kalshi YES") == "no"
    assert arb.poly_outcome_to_buy("no", "Kalshi YES") == "yes"
    # "Kalshi NO": Polymarket YES = Kalshi NO -> buy the same label
    assert arb.poly_outcome_to_buy("yes", "Kalshi NO") == "yes"
    assert arb.poly_outcome_to_buy("no", "Kalshi NO") == "no"
    with pytest.raises(ValueError):
        arb.poly_outcome_to_buy("yes", "Kalshi MAYBE")


def book(*pairs):
    """[("0.45", "100"), ...] -> the CLOB's [{"price": "0.45", "size": "100"}, ...]."""
    return [{"price": price, "size": size} for price, size in pairs]


def test_ask_ladder_never_adds_polymarket_yes_and_no_depth():
    # Mirrored books: the NO ask 0.55 x 100 is the same order as the YES bid 0.45 x 100.
    yes_book = {"bids": book(("0.45", "100")), "asks": book(("0.55", "30"))}
    no_book = {"bids": book(("0.45", "30")), "asks": book(("0.55", "100"))}
    assert polymarket.books_are_mirrored(yes_book, no_book)
    _, levels = polymarket.parse_orderbook(yes_book, no_book, "m", AFTER_V2)
    assert arb.ask_ladder(levels, "no") == [(D("0.55"), D("100"))]   # 100, not 200
    assert arb.ask_ladder(levels, "yes") == [(D("0.55"), D("30"))]
    # Walking against a deep Kalshi book: at most the 100 NO shares on offer.
    depth = arb.walk_books([(D("0.30"), D("1000"))], arb.ask_ladder(levels, "no"), 1,
                           ECONOMICS_MARKET, AFTER_V2)
    assert depth["size"] == 100


def test_ask_ladder_reads_kalshi_asks_from_the_other_sides_bids():
    raw = {"orderbook_fp": {"yes_dollars": [["0.38", "100.00"]],
                            "no_dollars": [["0.55", "200.00"], ["0.58", "50.00"]]}}
    _, levels = kalshi.parse_orderbook(raw, "KXTEST-1", AFTER_V2)
    # YES asks = 1 - NO bids, cheapest first; NO ask = 1 - YES bid
    assert arb.ask_ladder(levels, "yes") == [(D("0.42"), D("50")), (D("0.45"), D("200"))]
    assert arb.ask_ladder(levels, "no") == [(D("0.62"), D("100"))]


# ---------------------------------------------------------------------------
# Top of book and depth
# ---------------------------------------------------------------------------

def test_top_of_book_profit_per_contract():
    # Kalshi YES 0.40 x 10, Polymarket NO 0.50 x 8 -> 8 contracts at both best asks.
    top = arb.top_of_book([(D("0.40"), D("10"))], [(D("0.50"), D("8"))], 1, ECONOMICS_MARKET, AFTER_V2)
    assert top["size"] == 8
    assert top["gross_gap"] == D("0.10")                      # 1 - 0.40 - 0.50
    # Kalshi: 8 x 0.07 x 0.40 x 0.60 = 0.1344 -> 3.20 + 0.1344 = 3.3344 (already whole centicents)
    assert top["kalshi_fee"] == D("0.1344") / 8               # 0.0168 per contract
    # Polymarket: 8 x 0.05 x 0.50 x 0.50 = 0.1 -> 0.0125 per share
    assert top["poly_fee"] == D("0.0125")
    assert top["net_profit"] == D("0.10") - D("0.0168") - D("0.0125")       # 0.0707
    assert top["capital"] == D("0.40") + D("0.50") + D("0.0168") + D("0.0125")
    assert arb.top_of_book([], [(D("0.50"), D("8"))], 1, ECONOMICS_MARKET, AFTER_V2) is None


def test_walk_books_buys_cheapest_first_until_a_book_runs_out():
    # No fees (M = 0, Polymarket market without fees), to check the walking itself:
    #   step 1: 0.40 + 0.50 -> 0.10 profit x 5 contracts
    #   step 2: 0.40 + 0.55 -> 0.05 x 5   (Polymarket's first level used up)
    #   step 3: 0.42 + 0.55 -> 0.03 x 10  (Kalshi's first level used up, then all of Kalshi)
    kalshi_asks = [(D("0.40"), D("10")), (D("0.42"), D("10"))]
    poly_asks = [(D("0.50"), D("5")), (D("0.55"), D("100"))]
    depth = arb.walk_books(kalshi_asks, poly_asks, 0, NO_FEE_MARKET, AFTER_V2)
    assert [step["contracts"] for step in depth["steps"]] == [5, 5, 10]
    assert depth["size"] == 20
    assert depth["total_profit"] == D("1.05")                 # 0.5 + 0.25 + 0.3
    assert depth["capital"] == D("8.20") + D("10.75")         # Kalshi 4.00 + 4.20; Polymarket 2.50 + 8.25
    assert depth["stop"] is None                              # Kalshi ran out of offers


def test_walk_books_stops_when_the_next_contract_loses_money():
    # Step 3 would be 0.50 + 0.55 = 1.05 > $1: stop after 10 contracts (profit 0.5 + 0.25).
    kalshi_asks = [(D("0.40"), D("10")), (D("0.50"), D("10"))]
    poly_asks = [(D("0.50"), D("5")), (D("0.55"), D("100"))]
    depth = arb.walk_books(kalshi_asks, poly_asks, 0, NO_FEE_MARKET, AFTER_V2)
    assert depth["size"] == 10
    assert depth["total_profit"] == D("0.75")
    assert depth["stop"]["profit_per_contract"] == D("-0.05")


def test_walk_books_with_fees():
    # 10 contracts: Kalshi 0.40 (fee 10 x 0.07 x 0.40 x 0.60 = 0.168), Polymarket 0.50
    # (fee 10 x 0.05 x 0.25 = 0.125): profit 10 - 4.00 - 5.00 - 0.168 - 0.125 = 0.707
    depth = arb.walk_books([(D("0.40"), D("10"))], [(D("0.50"), D("10"))], 1, ECONOMICS_MARKET, AFTER_V2)
    assert depth["size"] == 10
    assert depth["kalshi_fee"] == D("0.168")
    assert depth["poly_fee"] == D("0.125")
    assert depth["total_profit"] == D("0.707")


def test_walk_books_without_profit_buys_nothing():
    depth = arb.walk_books([(D("0.60"), D("10"))], [(D("0.45"), D("10"))], 1, ECONOMICS_MARKET, AFTER_V2)
    assert depth["size"] == 0
    assert depth["total_profit"] == 0
    assert depth["steps"] == []


def test_walk_books_skips_a_trade_that_rounding_makes_worthless():
    # 1 contract: 1 - 0.50 - 0.48 - 0.0175 (Kalshi fee) = 0.0025 before rounding.
    # Rounded to whole cents Kalshi charges $0.02, and the profit is gone:
    depth = arb.walk_books([(D("0.50"), D("1"))], [(D("0.48"), D("1"))], 1, NO_FEE_MARKET, AFTER_V2,
                           precision=arb.CENT)
    assert depth["size"] == 0
    # Rounded to centicents it stays: 1 - 0.50 - 0.48 - 0.0175 = 0.0025
    depth = arb.walk_books([(D("0.50"), D("1"))], [(D("0.48"), D("1"))], 1, NO_FEE_MARKET, AFTER_V2)
    assert depth["size"] == 1
    assert depth["total_profit"] == D("0.0025")


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------

def test_return_on_capital_and_annualised_return():
    assert arb.return_on_capital(D("0.02"), D("0.98")) == pytest.approx(0.02 / 0.98)
    assert math.isnan(arb.return_on_capital(0, 0))
    assert arb.annualised_return(0.02, 36.5) == pytest.approx(0.20)   # 2% in 36.5 days = 20% a year
    assert math.isnan(arb.annualised_return(0.02, 0))
    assert math.isnan(arb.annualised_return(0.02, math.nan))


def test_resolution_time_is_the_later_of_the_two_dates():
    pair = {"kalshi_close_time": pd.Timestamp("2026-12-09 18:59", tz="UTC"),
            "poly_end_date": pd.Timestamp("2026-12-09 23:59", tz="UTC")}
    assert arb.resolution_time(pair) == pd.Timestamp("2026-12-09 23:59", tz="UTC")
    pair["poly_end_date"] = pd.NaT
    assert arb.resolution_time(pair) == pd.Timestamp("2026-12-09 18:59", tz="UTC")
    assert arb.days_until_resolution(pd.Timestamp("2026-12-08 06:59", tz="UTC"),
                                     arb.resolution_time(pair)) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Two order-book snapshots -> one row
# ---------------------------------------------------------------------------

KALSHI_TIME = pd.Timestamp("2026-11-04 12:00:00", tz="UTC")
POLY_TIME = pd.Timestamp("2026-11-04 12:00:01", tz="UTC")
# A "Kalshi NO" pair: Polymarket YES = Kalshi NO. Resolution 30 days after POLY_TIME.
PAIR = {"pair_id": "KXTEST-26NOV-T1 / 123", "poly_yes_equals": "Kalshi NO",
        "kalshi_ticker": "KXTEST-26NOV-T1", "kalshi_title": "Kalshi title", "poly_question": "Poly question",
        "kalshi_close_time": pd.Timestamp("2026-12-04 11:00", tz="UTC"),
        "poly_end_date": pd.Timestamp("2026-12-04 12:00:01", tz="UTC")}


def example_books():
    """Kalshi: YES bid 0.38 x 100; NO bids 0.58 x 50 and 0.55 x 200 -> YES asks 0.42 x 50, 0.45 x 200.
    Polymarket: YES bid 0.50 x 80, YES asks 0.53 x 40 and 0.56 x 500; the NO book mirrors it."""
    raw = {"orderbook_fp": {"yes_dollars": [["0.38", "100.00"]],
                            "no_dollars": [["0.58", "50.00"], ["0.55", "200.00"]]}}
    _, kalshi_levels = kalshi.parse_orderbook(raw, PAIR["kalshi_ticker"], KALSHI_TIME)
    yes_book = {"bids": book(("0.50", "80")), "asks": book(("0.53", "40"), ("0.56", "500"))}
    no_book = {"bids": book(("0.47", "40"), ("0.44", "500")), "asks": book(("0.50", "80"))}
    _, poly_levels = polymarket.parse_orderbook(yes_book, no_book, "123", POLY_TIME)
    return kalshi_levels, poly_levels


def test_snapshot_row_by_hand():
    kalshi_levels, poly_levels = example_books()
    row = arb.snapshot_row(PAIR, kalshi_levels, poly_levels, ECONOMICS_MARKET)
    assert list(row) == arb.ROW_COLUMNS
    # Best direction: Kalshi YES 0.42 + Polymarket YES 0.53 (= Kalshi NO) -> gap 0.05.
    # (The other one, Kalshi NO 0.62 + Polymarket NO 0.50, loses 0.12.)
    assert row["direction"] == "Kalshi YES + Polymarket YES"
    assert row["timestamp"] == POLY_TIME
    assert row["seconds_apart"] == 1
    assert row["gross_gap"] == pytest.approx(0.05)
    # 40 contracts at both best asks. Kalshi: 40 x 0.07 x 0.42 x 0.58 = 0.68208 -> cost 16.80
    # + 0.68208 = 17.48208 -> 17.4821, fee 0.6821 = 0.0170525 each. Polymarket: 0.05 x 0.53 x 0.47
    # = 0.012455 each.
    assert row["top_size"] == 40
    assert row["kalshi_fee_per_contract"] == pytest.approx(0.0170525)
    assert row["poly_fee_per_contract"] == pytest.approx(0.012455)
    assert row["fees"] == pytest.approx(0.0295075)
    assert row["net_profit_per_contract"] == pytest.approx(0.0204925)
    # Depth: the next step (0.42 + 0.56) loses 0.009372 per contract -> 40 contracts.
    # Capital 16.80 + 21.20 + 0.6821 + 0.4982 = 39.1803; profit 40 - 39.1803 = 0.8197.
    assert row["max_size"] == 40
    assert row["capital_locked"] == pytest.approx(39.1803)
    assert row["total_profit"] == pytest.approx(0.8197)
    assert row["days_to_resolution"] == pytest.approx(30)
    assert row["return_on_capital"] == pytest.approx(0.8197 / 39.1803)
    assert row["annualised_return"] == pytest.approx(0.8197 / 39.1803 * 365 / 30)
    assert row["top_return_on_capital"] == pytest.approx(0.0204925 / 0.9795075)


def test_the_two_gaps_add_up_to_minus_both_spreads():
    # Kalshi spread 0.42 - 0.38 = 0.04, Polymarket spread 0.53 - 0.50 = 0.03: gaps 0.05 and -0.12.
    kalshi_levels, poly_levels = example_books()
    gaps = [arb.check_direction(PAIR, outcome, kalshi_levels, poly_levels, ECONOMICS_MARKET, POLY_TIME)
            ["top"]["gross_gap"] for outcome in ("yes", "no")]
    assert gaps == [D("0.05"), D("-0.12")]
    assert sum(gaps) == -(D("0.04") + D("0.03"))


def test_snapshot_row_without_offers():
    kalshi_levels, poly_levels = example_books()
    empty = poly_levels[poly_levels["side"] == "bid"]   # Polymarket: bids only, nothing to buy
    row = arb.snapshot_row(PAIR, kalshi_levels, empty, ECONOMICS_MARKET)
    assert math.isnan(row["gross_gap"])
    assert math.isnan(row["max_size"])


def test_example_rows_and_worked_example_from_a_saved_table(capsys):
    # The format of arb_examples.parquet: both venues' levels of each pair in one table.
    kalshi_levels, poly_levels = example_books()
    books = pd.concat([kalshi_levels.rename(columns={"ticker": "market"}).assign(pair_id=PAIR["pair_id"], venue="kalshi"),
                       poly_levels.rename(columns={"market_id": "market"}).assign(pair_id=PAIR["pair_id"], venue="polymarket")],
                      ignore_index=True)[arb.EXAMPLE_COLUMNS]
    pairs = pd.DataFrame([{**PAIR, "poly_market_id": "123"}])
    poly_markets = pd.DataFrame([{**ECONOMICS_MARKET, "market_id": "123"}])
    rows = arb.example_rows(books, pairs, poly_markets)
    assert len(rows) == 1
    assert rows.loc[0, "total_profit"] == pytest.approx(0.8197)

    kalshi_part, poly_part = arb.split_books(books, PAIR["pair_id"])
    arb.print_worked_example(PAIR, kalshi_part, poly_part, ECONOMICS_MARKET)
    printed = capsys.readouterr().out
    assert "gross gap = 1 - 0.42 - 0.53 = 0.05" in printed
    assert "max size 40 contracts" in printed
