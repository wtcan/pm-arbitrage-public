"""
Unit tests for src/kalshi.py. Run them from the project folder with:  pytest

None of these tests use the internet: they feed small, hand-made Kalshi
answers into the conversion functions and check the results.

Most tests are about the order-book conversion. Kalshi lists only bids, and
the asks follow from them (https://docs.kalshi.com/getting_started/orderbook_responses):
    best YES ask = 1 - highest NO bid
    best NO ask  = 1 - highest YES bid
"""

import math
import random

import pandas as pd
import pytest

import kalshi

# The example order book from Kalshi's documentation page
# https://docs.kalshi.com/getting_started/orderbook_responses
# The page says: best YES bid $0.42, best YES ask $1.00 - $0.56 = $0.44,
# spread $0.02. Lists are [price in dollars, number of contracts].
DOCS_EXAMPLE = {
    "orderbook_fp": {
        "yes_dollars": [
            ["0.0100", "200.00"], ["0.1500", "100.00"], ["0.2000", "50.00"],
            ["0.2500", "20.00"], ["0.3000", "11.00"], ["0.3100", "10.00"],
            ["0.3200", "10.00"], ["0.3300", "11.00"], ["0.3400", "9.00"],
            ["0.3500", "11.00"], ["0.4100", "10.00"], ["0.4200", "13.00"],
        ],
        "no_dollars": [
            ["0.0100", "100.00"], ["0.1600", "3.00"], ["0.2500", "50.00"],
            ["0.2800", "19.00"], ["0.3600", "5.00"], ["0.3700", "50.00"],
            ["0.3800", "300.00"], ["0.4400", "29.00"], ["0.4500", "20.00"],
            ["0.5600", "17.00"],
        ],
    }
}


def book(yes, no):
    """Build a Kalshi-style order-book answer from two lists of [price, count]."""
    return {"orderbook_fp": {"yes_dollars": yes, "no_dollars": no}}


def is_missing(value):
    return isinstance(value, float) and math.isnan(value)


# ---------------------------------------------------------------------------
# Order book: best bids and asks
# ---------------------------------------------------------------------------

def test_docs_example_best_prices():
    top, _ = kalshi.parse_orderbook(DOCS_EXAMPLE, "TEST")
    assert top["best_yes_bid"] == 0.42        # highest YES bid
    assert top["best_yes_bid_size"] == 13
    assert top["best_no_bid"] == 0.56         # highest NO bid
    assert top["best_no_bid_size"] == 17
    assert top["best_yes_ask"] == 0.44        # 1 - 0.56 (highest NO bid)
    assert top["best_yes_ask_size"] == 17     # the size of that NO bid
    assert top["best_no_ask"] == 0.58         # 1 - 0.42 (highest YES bid)
    assert top["best_no_ask_size"] == 13      # the size of that YES bid
    assert top["spread"] == 0.02              # 0.44 - 0.42, as on the docs page
    assert top["yes_bid_levels"] == 12
    assert top["no_bid_levels"] == 10
    assert top["ticker"] == "TEST"


def test_prices_are_exact_not_rounded_floats():
    # With plain floats, 1 - 0.56 gives 0.43999999999999995. The conversion
    # uses exact decimals, so the result equals 0.44 exactly.
    top, _ = kalshi.parse_orderbook(book([["0.4200", "1.00"]], [["0.5600", "1.00"]]))
    assert top["best_yes_ask"] == 0.44
    assert top["spread"] == 0.02


def test_order_of_the_lists_does_not_matter():
    # Kalshi's docs disagree on whether lists are sorted ascending or
    # best-first, so the result must not depend on the order.
    expected, _ = kalshi.parse_orderbook(DOCS_EXAMPLE)
    yes = list(reversed(DOCS_EXAMPLE["orderbook_fp"]["yes_dollars"]))
    no = list(DOCS_EXAMPLE["orderbook_fp"]["no_dollars"])
    random.Random(42).shuffle(no)
    result, _ = kalshi.parse_orderbook(book(yes, no))
    assert result == expected


def test_real_kalshi_answer_matches_kalshi_top_of_book():
    # Shortened real answer for KXFEDDECISION-26DEC-H26 (2026-11-04). For the
    # same moment, Kalshi's market object showed yes_bid 0.01, yes_ask 0.02,
    # no_bid 0.98, no_ask 0.99. The conversion must reproduce these.
    raw = book(yes=[["0.0100", "58571.29"]],
               no=[["0.0100", "768311.00"], ["0.9600", "74858.03"],
                   ["0.9700", "50672.24"], ["0.9800", "108809.34"]])
    top, _ = kalshi.parse_orderbook(raw)
    assert (top["best_yes_bid"], top["best_yes_ask"]) == (0.01, 0.02)
    assert (top["best_no_bid"], top["best_no_ask"]) == (0.98, 0.99)
    assert top["best_yes_ask_size"] == 108809.34  # fractional contracts are kept


def test_sub_cent_prices():
    # Some markets trade in steps smaller than one cent.
    top, _ = kalshi.parse_orderbook(book([["0.4250", "5.00"]], [["0.5650", "8.00"]]))
    assert top["best_yes_ask"] == 0.435
    assert top["best_no_ask"] == 0.575
    assert top["spread"] == 0.01


def test_one_side_empty():
    # No NO bids: nobody offers to sell YES, so there is no YES ask (NaN).
    # The NO ask still follows from the YES bids.
    top, levels = kalshi.parse_orderbook(book([["0.3000", "10.00"]], []))
    assert top["best_yes_bid"] == 0.30
    assert is_missing(top["best_yes_ask"]) and is_missing(top["best_yes_ask_size"])
    assert is_missing(top["best_no_bid"])
    assert top["best_no_ask"] == 0.70
    assert is_missing(top["spread"])
    assert len(levels) == 2  # one YES bid and the NO ask made from it


@pytest.mark.parametrize("raw", [
    book([], []),                                            # both lists empty
    {"orderbook_fp": {"yes_dollars": None, "no_dollars": None}},  # lists sent as null
    {"orderbook_fp": {}},                                    # lists missing
    {},                                                      # nothing at all
])
def test_empty_order_book(raw):
    top, levels = kalshi.parse_orderbook(raw, "EMPTY")
    for name in ["best_yes_bid", "best_yes_ask", "best_no_bid", "best_no_ask", "spread"]:
        assert is_missing(top[name])
    assert top["yes_bid_levels"] == 0 and top["no_bid_levels"] == 0
    assert levels.empty
    assert list(levels.columns) == kalshi.ORDERBOOK_LEVEL_COLUMNS


def test_top_has_the_table_columns():
    top, _ = kalshi.parse_orderbook(DOCS_EXAMPLE)
    assert list(top) == kalshi.ORDERBOOK_TOP_COLUMNS


# ---------------------------------------------------------------------------
# Order book: full depth
# ---------------------------------------------------------------------------

def ladder(levels, outcome, side):
    """The rows of one ladder (e.g. the YES asks), best level first."""
    rows = levels[(levels["outcome"] == outcome) & (levels["side"] == side)]
    return rows.sort_values("level")


def test_full_depth_is_kept():
    _, levels = kalshi.parse_orderbook(DOCS_EXAMPLE, "TEST")
    # 12 YES bids + 10 NO bids, and one ask on the other side for each bid
    assert len(levels) == 12 + 10 + 10 + 12
    assert len(ladder(levels, "yes", "bid")) == 12
    assert len(ladder(levels, "yes", "ask")) == 10
    assert len(ladder(levels, "no", "bid")) == 10
    assert len(ladder(levels, "no", "ask")) == 12
    # No contracts lost: each ask ladder holds exactly the other side's bids
    assert ladder(levels, "yes", "ask")["quantity"].sum() == 593  # all NO bids
    assert ladder(levels, "no", "ask")["quantity"].sum() == 455   # all YES bids
    assert ladder(levels, "yes", "bid")["quantity"].sum() == 455
    assert set(levels["ticker"]) == {"TEST"}


def test_depth_ladders_are_best_first():
    _, levels = kalshi.parse_orderbook(DOCS_EXAMPLE)
    for outcome in ["yes", "no"]:
        bids = ladder(levels, outcome, "bid")["price"].tolist()
        asks = ladder(levels, outcome, "ask")["price"].tolist()
        assert bids == sorted(bids, reverse=True)  # best bid = highest, first
        assert asks == sorted(asks)                # best ask = lowest, first
        assert ladder(levels, outcome, "bid")["level"].tolist() == list(range(1, len(bids) + 1))


def test_depth_asks_mirror_the_other_sides_bids():
    _, levels = kalshi.parse_orderbook(DOCS_EXAMPLE)
    yes_asks = ladder(levels, "yes", "ask")
    no_bids = ladder(levels, "no", "bid")
    # level 1: the NO bid at 0.56 for 17 contracts is a YES ask at 0.44
    assert yes_asks.iloc[0][["price", "quantity"]].tolist() == [0.44, 17]
    # level 10: the NO bid at 0.01 for 100 contracts is a YES ask at 0.99
    assert yes_asks.iloc[-1][["price", "quantity"]].tolist() == [0.99, 100]
    # every level: ask price = 1 - bid price, same quantity
    for (_, ask), (_, bid) in zip(yes_asks.iterrows(), no_bids.iterrows()):
        assert ask["price"] == pytest.approx(1 - bid["price"])
        assert ask["quantity"] == bid["quantity"]


def test_depth_and_top_agree():
    top, levels = kalshi.parse_orderbook(DOCS_EXAMPLE)
    for outcome in ["yes", "no"]:
        for side in ["bid", "ask"]:
            best = ladder(levels, outcome, side).iloc[0]
            assert best["price"] == top[f"best_{outcome}_{side}"]
            assert best["quantity"] == top[f"best_{outcome}_{side}_size"]


def test_all_prices_between_0_and_1():
    top, levels = kalshi.parse_orderbook(DOCS_EXAMPLE)
    assert levels["price"].between(0, 1).all()
    for name in ["best_yes_bid", "best_yes_ask", "best_no_bid", "best_no_ask"]:
        assert 0 <= top[name] <= 1


# ---------------------------------------------------------------------------
# Live or historical endpoint? (cutoff rule)
# ---------------------------------------------------------------------------

def test_is_historical():
    # Rule: markets that settled before the cutoff are only in /historical/...
    # https://docs.kalshi.com/getting_started/historical_data
    cutoff = pd.Timestamp("2026-09-05T00:00:00Z")
    assert kalshi.is_historical({"settlement_ts": "2025-12-10T19:08:51.628715Z"}, cutoff)
    assert not kalshi.is_historical({"settlement_ts": "2026-09-16T18:08:08Z"}, cutoff)
    assert not kalshi.is_historical({"settlement_ts": None}, cutoff)  # not settled yet
    assert not kalshi.is_historical({}, cutoff)


# ---------------------------------------------------------------------------
# Candlesticks
# ---------------------------------------------------------------------------

# The same hourly candle, once in the live format and once in the historical
# format (the historical endpoint drops the "_dollars" / "_fp" endings).
LIVE_CANDLE = {
    "end_period_ts": 1765389600,
    "yes_bid": {"open_dollars": "0.0300", "high_dollars": "0.0400", "low_dollars": "0.0300", "close_dollars": "0.0400"},
    "yes_ask": {"open_dollars": "0.0500", "high_dollars": "0.0500", "low_dollars": "0.0400", "close_dollars": "0.0450"},
    "price": {"open_dollars": "0.0400", "high_dollars": "0.0500", "low_dollars": "0.0400", "close_dollars": "0.0500",
              "mean_dollars": "0.0450", "previous_dollars": "0.0300"},
    "volume_fp": "120.00",
    "open_interest_fp": "5000.50",
}
HISTORICAL_CANDLE = {
    "end_period_ts": 1765389600,
    "yes_bid": {"open": "0.0300", "high": "0.0400", "low": "0.0300", "close": "0.0400"},
    "yes_ask": {"open": "0.0500", "high": "0.0500", "low": "0.0400", "close": "0.0450"},
    "price": {"open": "0.0400", "high": "0.0500", "low": "0.0400", "close": "0.0500",
              "mean": "0.0450", "previous": "0.0300"},
    "volume": "120.00",
    "open_interest": "5000.50",
}


def test_live_and_historical_candles_give_the_same_row():
    live = kalshi.parse_candles([LIVE_CANDLE], "T", 60, "live")
    historical = kalshi.parse_candles([HISTORICAL_CANDLE], "T", 60, "historical")
    assert list(live.columns) == kalshi.CANDLE_COLUMNS
    numbers = [column for column in kalshi.CANDLE_COLUMNS if column != "source"]
    pd.testing.assert_frame_equal(live[numbers], historical[numbers])
    row = live.iloc[0]
    assert row["yes_bid_close"] == 0.04 and row["yes_ask_close"] == 0.045
    assert row["price_mean"] == 0.045 and row["volume"] == 120 and row["open_interest"] == 5000.5
    assert row["period_end"] == pd.Timestamp("2025-12-10T18:00:00Z")  # stored in UTC


def test_candle_without_trades_has_missing_trade_prices():
    # Live answers send "price": {} when nothing traded; historical answers
    # send nulls but keep the previous trade price.
    live = dict(LIVE_CANDLE, price={})
    historical = dict(HISTORICAL_CANDLE, price={"open": None, "high": None, "low": None,
                                                "close": None, "mean": None, "previous": "0.0300"})
    for row in [kalshi.parse_candles([live], "T", 60, "live").iloc[0],
                kalshi.parse_candles([historical], "T", 60, "historical").iloc[0]]:
        assert is_missing(row["price_close"])
        assert row["yes_bid_close"] == 0.04  # bids and asks are still there
    assert kalshi.parse_candles([historical], "T", 60, "historical").iloc[0]["price_previous"] == 0.03


def test_no_quote_markers_become_missing():
    # Kalshi writes a YES bid of 0 when nobody bids and a YES ask of 1 when
    # nobody offers. Like an empty side of an order book, these become NaN.
    live = dict(LIVE_CANDLE,
                yes_bid={"open_dollars": "0.0000", "high_dollars": "0.0000",
                         "low_dollars": "0.0000", "close_dollars": "0.0000"},
                yes_ask={"open_dollars": "1.0000", "high_dollars": "1.0000",
                         "low_dollars": "1.0000", "close_dollars": "1.0000"})
    historical = dict(HISTORICAL_CANDLE,
                      yes_bid={"open": "0.0000", "high": "0.0000", "low": "0.0000", "close": "0.0000"},
                      yes_ask={"open": "1.0000", "high": "1.0000", "low": "1.0000", "close": "1.0000"})
    for row in [kalshi.parse_candles([live], "T", 60, "live").iloc[0],
                kalshi.parse_candles([historical], "T", 60, "historical").iloc[0]]:
        for column in kalshi.CANDLE_BID_COLUMNS + kalshi.CANDLE_ASK_COLUMNS:
            assert is_missing(row[column]), column
        assert row["price_close"] == 0.05 and row["price_previous"] == 0.03  # trades untouched
        assert row["volume"] == 120


def test_no_quote_markers_within_one_period():
    # Bids appear during the hour: the bid opens at 0 (no bid) and closes at
    # 0.30. Only the marker values become NaN; the real prices stay.
    candle = dict(HISTORICAL_CANDLE,
                  yes_bid={"open": "0.0000", "high": "0.3000", "low": "0.0000", "close": "0.3000"},
                  yes_ask={"open": "0.3500", "high": "1.0000", "low": "0.3500", "close": "0.3500"})
    row = kalshi.parse_candles([candle], "T", 60, "historical").iloc[0]
    assert is_missing(row["yes_bid_open"]) and is_missing(row["yes_bid_low"])
    assert row["yes_bid_high"] == 0.30 and row["yes_bid_close"] == 0.30
    assert is_missing(row["yes_ask_high"])
    assert row["yes_ask_open"] == row["yes_ask_low"] == row["yes_ask_close"] == 0.35


def test_extreme_real_quotes_are_kept():
    # A bid of 0.01 and an ask of 0.99 are real quotes, not markers.
    candle = dict(LIVE_CANDLE,
                  yes_bid={"open_dollars": "0.0100", "high_dollars": "0.0100",
                           "low_dollars": "0.0100", "close_dollars": "0.0100"},
                  yes_ask={"open_dollars": "0.9900", "high_dollars": "0.9900",
                           "low_dollars": "0.9900", "close_dollars": "0.9900"})
    row = kalshi.parse_candles([candle], "T", 60, "live").iloc[0]
    assert row["yes_bid_low"] == 0.01 and row["yes_ask_high"] == 0.99


def test_blank_no_quote_markers_does_not_change_its_input():
    table = kalshi.parse_candles([LIVE_CANDLE], "T", 60, "live")
    table.loc[0, "yes_bid_close"] = 0.0
    before = table.copy()
    blanked = kalshi.blank_no_quote_markers(table)
    assert is_missing(blanked.loc[0, "yes_bid_close"])
    pd.testing.assert_frame_equal(table, before)


def test_candle_windows_respect_the_5000_candle_limit():
    hour = 3600
    start, end = 1_700_000_000, 1_700_000_000 + 12_000 * hour  # 12,000 hours
    windows = kalshi.candle_windows(start, end, 60)
    assert windows[0][0] == start and windows[-1][1] == end   # whole range covered
    for window_start, window_end in windows:
        assert (window_end - window_start) / hour <= kalshi.MAX_CANDLES_PER_REQUEST
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == previous_end + 1                  # touching, no overlap
    assert len(windows) == 3
    assert kalshi.candle_windows(start, start + 10 * hour, 60) == [(start, start + 10 * hour)]
    assert kalshi.candle_windows(start, start, 60) == []       # empty range: nothing to ask


# ---------------------------------------------------------------------------
# Pagination, the markets table, saving
# ---------------------------------------------------------------------------

def test_get_all_pages_follows_the_cursor(monkeypatch):
    pages = {None: {"markets": [1, 2], "cursor": "page2"},
             "page2": {"markets": [3], "cursor": "page3"},
             "page3": {"markets": [4], "cursor": ""}}  # empty cursor = last page
    monkeypatch.setattr(kalshi, "polite_get", lambda path, params: pages[params["cursor"]])
    assert kalshi.get_all_pages("/markets", "markets") == [1, 2, 3, 4]


def test_get_all_pages_stops_if_the_cursor_repeats(monkeypatch):
    pages = {None: {"markets": [1], "cursor": "x"}, "x": {"markets": [2], "cursor": "x"}}
    monkeypatch.setattr(kalshi, "polite_get", lambda path, params: pages[params["cursor"]])
    with pytest.raises(kalshi.KalshiError):
        kalshi.get_all_pages("/markets", "markets")


def test_markets_table():
    markets = [{
        "ticker": "KXFED-25DEC-T3.75", "event_ticker": "KXFED-25DEC", "series_ticker": "KXFED",
        "title": "Will the upper bound be above 3.75%?", "yes_sub_title": "Above 3.75%",
        "status": "finalized", "result": "yes", "open_time": "2025-01-01T15:00:00Z",
        "close_time": "2025-12-10T18:59:00Z", "settlement_ts": "2025-12-10T19:08:51.628715Z",
        "rules_primary": "If ...", "rules_secondary": "Note ...", "floor_strike": 3.75,
        "last_price_dollars": "0.9900", "volume_fp": "1000.00", "source": "historical",
    }, {
        "ticker": "KXFED-26DEC-T3.00", "event_ticker": "KXFED-26DEC", "series_ticker": "KXFED",
        "status": "active", "result": "", "open_time": "2026-01-01T15:00:00Z",
        "close_time": "2026-12-09T18:59:00Z", "source": "live",
    }]
    events = [{"event_ticker": "KXFED-25DEC", "title": "Fed rate after the Dec 2025 meeting"}]
    series = [{"ticker": "KXFED", "category": "Economics"}]
    table = kalshi.markets_table(markets, events, series)
    assert list(table.columns) == kalshi.MARKET_COLUMNS
    assert table["category"].tolist() == ["Economics", "Economics"]
    assert table.loc[0, "event_title"] == "Fed rate after the Dec 2025 meeting"
    assert table.loc[0, "last_price"] == 0.99 and table.loc[0, "floor_strike"] == 3.75
    assert pd.isna(table.loc[1, "result"])           # "" = not known yet -> missing
    assert pd.isna(table.loc[1, "settlement_time"])  # not settled yet
    for column in kalshi.MARKET_TIME_COLUMNS:
        assert str(table[column].dt.tz) == "UTC"     # project rule: times in UTC
    assert table.loc[0, "close_time"] == pd.Timestamp("2025-12-10T18:59:00Z")


def test_save_table_round_trip_and_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(kalshi, "PROCESSED_FOLDER", tmp_path)
    table = pd.DataFrame({"ticker": ["A", "B"], "price": [0.42, 0.58],
                          "time": pd.to_datetime(["2026-01-01T00:00:00Z"] * 2, utc=True)})
    path = kalshi.save_table(table, "example")
    pd.testing.assert_frame_equal(pd.read_parquet(path), table)
    # A file over the limit is refused, and the older file is kept.
    monkeypatch.setattr(kalshi, "MAX_FILE_MB", 0)
    with pytest.raises(ValueError):
        kalshi.save_table(table.iloc[:1], "example")
    assert len(pd.read_parquet(path)) == 2
    assert not list(tmp_path.glob("*.tmp"))
