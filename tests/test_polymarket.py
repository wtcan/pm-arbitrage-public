"""
Unit tests for src/polymarket.py and the event-selection rules of
src/download_polymarket.py. Run them from the project folder with:  pytest

None of these tests use the internet: they feed small, hand-made Polymarket
answers (shaped like the examples in the official docs, or shortened real
answers) into the functions and check the results.

The order-book formulas tested here, from
https://docs.polymarket.com/market-data/prices-order-books.md and
https://docs.polymarket.com/concepts/prices-orderbook.md:
    best bid = highest bid, best ask = lowest ask (of the same outcome's book)
    spread   = best ask - best bid
    the NO book mirrors the YES book: NO bid at p = YES ask at 1 - p
"""

import json
import math
import random

import pandas as pd
import pytest
import requests

import download_polymarket
import kalshi
import polymarket


def is_missing(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def levels(*pairs):
    """[("0.45", "100"), ...] -> [{"price": "0.45", "size": "100"}, ...], as the CLOB sends them."""
    return [{"price": price, "size": size} for price, size in pairs]


# The two order books of the "Fetch Multiple Order Books" example in
# https://docs.polymarket.com/market-data/prices-order-books.md
# The CLOB lists bids from the lowest price up and asks from the highest
# price down, so the best prices are the LAST entries.
DOCS_YES_BOOK = {
    "bids": levels(("0.01", "2151131.59"), ("0.02", "139963.89"), ("0.03", "208169.44")),
    "asks": levels(("0.99", "218442.27"), ("0.98", "13229.55"), ("0.97", "4338.7")),
}
DOCS_NO_BOOK = {
    "bids": levels(("0.01", "218442.27"), ("0.02", "13229.55"), ("0.03", "4338.7")),
    "asks": levels(("0.99", "2151131.59"), ("0.98", "139963.89"), ("0.97", "208169.44")),
}

# Shortened real answers for the "No change" market of "Fed Decision in
# December?" (market 3215007), downloaded on 2026-11-04.
REAL_YES_BOOK = {
    "bids": levels(("0.19", "40822.79"), ("0.2", "73271.22"), ("0.21", "22963.2")),
    "asks": levels(("0.24", "48832.21"), ("0.23", "180152.85"), ("0.22", "54074.85")),
}
REAL_NO_BOOK = {
    "bids": levels(("0.76", "48832.21"), ("0.77", "180152.85"), ("0.78", "54074.85")),
    "asks": levels(("0.81", "40822.79"), ("0.8", "73271.22"), ("0.79", "22963.2")),
}


# ---------------------------------------------------------------------------
# Order book: best bids and asks
# ---------------------------------------------------------------------------

def test_docs_example_best_prices():
    top, _ = polymarket.parse_orderbook(DOCS_YES_BOOK, DOCS_NO_BOOK, "M1")
    assert top["best_yes_bid"] == 0.03 and top["best_yes_bid_size"] == 208169.44   # highest YES bid
    assert top["best_yes_ask"] == 0.97 and top["best_yes_ask_size"] == 4338.7      # lowest YES ask
    assert top["best_no_bid"] == 0.03 and top["best_no_bid_size"] == 4338.7
    assert top["best_no_ask"] == 0.97 and top["best_no_ask_size"] == 208169.44
    assert top["spread"] == 0.94                                                   # 0.97 - 0.03
    assert (top["yes_bid_levels"], top["yes_ask_levels"]) == (3, 3)
    assert top["market_id"] == "M1"


def test_real_answer_best_prices_and_mirror():
    top, _ = polymarket.parse_orderbook(REAL_YES_BOOK, REAL_NO_BOOK)
    assert (top["best_yes_bid"], top["best_yes_ask"]) == (0.21, 0.22)
    assert (top["best_no_bid"], top["best_no_ask"]) == (0.78, 0.79)
    assert top["spread"] == 0.01
    # The NO book is the YES book seen from the other side.
    assert top["best_no_bid"] == pytest.approx(1 - top["best_yes_ask"])
    assert top["best_no_ask"] == pytest.approx(1 - top["best_yes_bid"])
    assert top["best_no_bid_size"] == top["best_yes_ask_size"] == 54074.85


def test_order_of_the_lists_does_not_matter():
    # The docs page lists bids lowest-first; the OpenAPI example lists them
    # highest-first. The result must not depend on the order.
    expected, _ = polymarket.parse_orderbook(REAL_YES_BOOK, REAL_NO_BOOK)
    shuffled = {side: list(entries) for side, entries in REAL_YES_BOOK.items()}
    shuffled["bids"].reverse()
    random.Random(42).shuffle(shuffled["asks"])
    result, _ = polymarket.parse_orderbook(shuffled, REAL_NO_BOOK)
    assert result == expected


def test_prices_are_exact_not_rounded_floats():
    # With plain floats 0.46 - 0.45 = 0.010000000000000009; exact decimals give 0.01.
    # (The bid/ask example of the CLOB OpenAPI spec for GET /book.)
    yes_book = {"bids": levels(("0.45", "100"), ("0.44", "200")), "asks": levels(("0.46", "150"), ("0.47", "250"))}
    top, _ = polymarket.parse_orderbook(yes_book, None)
    assert top["spread"] == 0.01


def test_sub_cent_prices():
    yes_book = {"bids": levels(("0.425", "5")), "asks": levels(("0.4275", "8"))}
    top, _ = polymarket.parse_orderbook(yes_book, None)
    assert top["best_yes_ask"] == 0.4275 and top["spread"] == 0.0025


def test_one_side_empty():
    # Bids but no asks: no YES ask and no spread (NaN); the bid is still there.
    top, depth = polymarket.parse_orderbook({"bids": levels(("0.30", "10")), "asks": []}, None)
    assert top["best_yes_bid"] == 0.30
    assert is_missing(top["best_yes_ask"]) and is_missing(top["best_yes_ask_size"])
    assert is_missing(top["spread"])
    assert len(depth) == 1


@pytest.mark.parametrize("yes_book, no_book", [
    (None, None),                                    # no book at all (HTTP 404)
    ({}, {}),                                        # fields missing
    ({"bids": [], "asks": []}, {"bids": None, "asks": None}),  # empty lists / nulls
])
def test_empty_order_book(yes_book, no_book):
    top, depth = polymarket.parse_orderbook(yes_book, no_book, "EMPTY")
    for name in ["best_yes_bid", "best_yes_ask", "best_no_bid", "best_no_ask", "spread"]:
        assert is_missing(top[name])
    assert top["yes_bid_levels"] == top["no_ask_levels"] == 0
    assert depth.empty and list(depth.columns) == polymarket.ORDERBOOK_LEVEL_COLUMNS


def test_same_format_as_kalshi():
    # The same columns as kalshi.py (market_id in place of ticker), plus two
    # extra counts at the end because Polymarket also lists asks.
    top, depth = polymarket.parse_orderbook(DOCS_YES_BOOK, DOCS_NO_BOOK)
    assert list(top) == polymarket.ORDERBOOK_TOP_COLUMNS
    renamed = [column.replace("market_id", "ticker") for column in polymarket.ORDERBOOK_TOP_COLUMNS]
    assert renamed[:len(kalshi.ORDERBOOK_TOP_COLUMNS)] == kalshi.ORDERBOOK_TOP_COLUMNS
    assert renamed[len(kalshi.ORDERBOOK_TOP_COLUMNS):] == ["yes_ask_levels", "no_ask_levels"]
    assert [c.replace("market_id", "ticker") for c in depth.columns] == kalshi.ORDERBOOK_LEVEL_COLUMNS


# ---------------------------------------------------------------------------
# Order book: full depth and the mirror check
# ---------------------------------------------------------------------------

def ladder(depth, outcome, side):
    rows = depth[(depth["outcome"] == outcome) & (depth["side"] == side)]
    return rows.sort_values("level")


def test_full_depth_is_kept_best_first():
    top, depth = polymarket.parse_orderbook(DOCS_YES_BOOK, DOCS_NO_BOOK, "M1")
    assert len(depth) == 12 and set(depth["market_id"]) == {"M1"}
    for outcome in ["yes", "no"]:
        bids = ladder(depth, outcome, "bid")["price"].tolist()
        asks = ladder(depth, outcome, "ask")["price"].tolist()
        assert bids == sorted(bids, reverse=True)   # best bid = highest, first
        assert asks == sorted(asks)                 # best ask = lowest, first
        assert ladder(depth, outcome, "bid")["level"].tolist() == [1, 2, 3]
        for side in ["bid", "ask"]:
            best = ladder(depth, outcome, side).iloc[0]
            assert best["price"] == top[f"best_{outcome}_{side}"]
            assert best["quantity"] == top[f"best_{outcome}_{side}_size"]
    # no contracts lost
    assert ladder(depth, "yes", "bid")["quantity"].sum() == pytest.approx(2151131.59 + 139963.89 + 208169.44)


def test_books_are_mirrored():
    assert polymarket.books_are_mirrored(DOCS_YES_BOOK, DOCS_NO_BOOK)
    assert polymarket.books_are_mirrored(REAL_YES_BOOK, REAL_NO_BOOK)
    changed_size = {"bids": levels(("0.76", "48832.21"), ("0.77", "180152.85"), ("0.78", "1")),
                    "asks": REAL_NO_BOOK["asks"]}
    assert not polymarket.books_are_mirrored(REAL_YES_BOOK, changed_size)
    changed_price = {"bids": REAL_NO_BOOK["bids"], "asks": levels(("0.81", "40822.79"), ("0.8", "73271.22"))}
    assert not polymarket.books_are_mirrored(REAL_YES_BOOK, changed_price)
    assert not polymarket.books_are_mirrored(REAL_YES_BOOK, None)


def test_get_orderbook_returns_none_when_there_is_no_book(monkeypatch):
    def no_book(base_url, path, params=None, use_cache=True):
        assert (base_url, path, use_cache) == (polymarket.CLOB_URL, "/book", False)  # never cached
        raise polymarket.NotFoundError("HTTP 404: No orderbook exists for the requested token id")
    monkeypatch.setattr(polymarket, "polite_get", no_book)
    assert polymarket.get_orderbook("123") is None


def test_orderbook_snapshot(monkeypatch):
    books = {"y1": REAL_YES_BOOK, "n1": REAL_NO_BOOK, "y2": None, "n2": None}
    monkeypatch.setattr(polymarket, "get_orderbook", lambda token_id: books[token_id])
    markets = pd.DataFrame({"market_id": ["1", "2"], "yes_token_id": ["y1", "y2"], "no_token_id": ["n1", "n2"]})
    top, depth = polymarket.orderbook_snapshot(markets)
    assert top["market_id"].tolist() == ["1"]   # market 2 has no book at all: skipped
    assert top.loc[0, "best_yes_ask"] == 0.22 and len(depth) == 12
    assert str(top["snapshot_time"].dt.tz) == "UTC"


# ---------------------------------------------------------------------------
# Reading a market: token ids, resolution, category
# ---------------------------------------------------------------------------

def test_token_ids_v1_market():
    # CTF ("v1") markets: clobTokenIds is a JSON text, YES first.
    market = {"version": "v1", "outcomes": "[\"Yes\", \"No\"]",
              "clobTokenIds": "[\"107505882767731489358\", \"7305630249804085635\"]", "positionIds": None}
    assert polymarket.token_ids(market) == ("107505882767731489358", "7305630249804085635")


def test_token_ids_v2_market_uses_position_ids():
    # v2 markets: positionIds is a list. Both fields can be filled: the version decides.
    market = {"version": "v2", "outcomes": "[\"Yes\", \"No\"]", "clobTokenIds": "[\"1\", \"2\"]",
              "positionIds": ["651150819117105875331414918119047680898421632356043490229292782433651916800",
                              "651150819117105875331414918119047680898421632356043490229292782433651916801"]}
    yes, no = polymarket.token_ids(market)
    assert yes.endswith("800") and no.endswith("801")


def test_token_ids_follow_the_outcome_labels():
    market = {"version": "v1", "outcomes": ["No", "Yes"], "clobTokenIds": "[\"no-token\", \"yes-token\"]"}
    assert polymarket.token_ids(market) == ("yes-token", "no-token")


@pytest.mark.parametrize("ids", [None, "", "[]", "[\"only-one\"]"])
def test_token_ids_missing(ids):
    assert polymarket.token_ids({"version": "v1", "clobTokenIds": ids}) == (None, None)


@pytest.mark.parametrize("market, expected", [
    ({"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": "[\"1\", \"0\"]"}, ("yes", 1.0)),
    ({"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": "[\"0\", \"1\"]"}, ("no", 0.0)),
    ({"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": "[\"0.5\", \"0.5\"]"}, ("split", 0.5)),
    ({"closed": True, "resolutionStatus": "resolved", "outcomePrices": "[\"1\", \"0\"]"}, ("yes", 1.0)),  # v2
    ({"closed": True, "umaResolutionStatus": "resolved", "outcomes": "[\"No\", \"Yes\"]",
      "outcomePrices": "[\"1\", \"0\"]"}, ("no", 0.0)),
])
def test_resolved_outcome(market, expected):
    assert polymarket.resolved_outcome(market) == expected


@pytest.mark.parametrize("market", [
    {"closed": False, "outcomePrices": "[\"0.21\", \"0.79\"]"},                               # still open
    {"closed": True, "umaResolutionStatus": "proposed", "outcomePrices": "[\"0.999\", \"0.001\"]"},  # waiting for UMA
    {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": None},
])
def test_unresolved_markets_have_no_outcome(market):
    outcome, payout = polymarket.resolved_outcome(market)
    assert outcome is None and math.isnan(payout)


def test_category_from_tags():
    tags = [{"slug": "fed"}, {"slug": "fomc"}, {"slug": "economy"}, {"slug": "fed-rates"}]
    assert polymarket.category_from_tags(tags) == "Economics"
    tags += [{"slug": "politics"}, {"slug": "finance"}]
    assert polymarket.category_from_tags(tags) == "Economics|Finance|Politics"
    assert polymarket.category_from_tags([{"slug": "fed-rates"}]) is None
    assert polymarket.category_from_tags(None) is None


# ---------------------------------------------------------------------------
# The markets table
# ---------------------------------------------------------------------------

EVENT = {
    "id": "606422", "slug": "fed-decision-in-october", "title": "Fed Decision in October?",
    "description": "Event rules text.", "startDate": "2026-06-17T23:00:00Z", "endDate": "2026-10-29T03:59:00Z",
    "series": [{"slug": "fomc"}],
    "tags": [{"slug": "fed"}, {"slug": "economy"}, {"slug": "fed-rates"}, {"slug": "politics"}],
    "markets": [
        {   # shortened real record: "No change" won
            "id": "2589812", "conditionId": "0xdf9b", "question": "Will there be no change in Fed interest rates?",
            "groupItemTitle": "No change", "description": "Market rules text.",
            "resolutionSource": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"1\", \"0\"]",
            "clobTokenIds": "[\"111\", \"420\"]", "positionIds": None, "version": "v1",
            "closed": True, "closedTime": "2026-10-28 20:01:40.290246+00", "umaResolutionStatus": "resolved",
            "startDate": "2026-06-18T00:02:51.727205Z", "endDate": "2026-10-29T03:59:00Z",
            "feesEnabled": True, "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.25},
            "negRisk": True, "orderPriceMinTickSize": 0.001, "volumeNum": 31993966.63, "category": None,
        },
        {   # open market without a fee schedule or a description of its own
            "id": "3", "conditionId": "0xabc", "question": "Will the Fed cut by 25 bps?",
            "groupItemTitle": "25 bps decrease", "description": "",
            "outcomes": "[\"Yes\", \"No\"]", "outcomePrices": "[\"0.2\", \"0.8\"]",
            "clobTokenIds": "[\"5\", \"6\"]", "version": "v1", "closed": False,
            "endDate": "2026-12-10T04:59:00Z", "feesEnabled": False, "feeSchedule": None, "volume": "1000.5",
        },
    ],
}


def test_markets_table():
    table = polymarket.markets_table([EVENT], {"606422": ("fed", "decision")})
    assert list(table.columns) == polymarket.MARKET_COLUMNS
    resolved, open_market = table.iloc[0], table.iloc[1]
    assert (resolved["market_id"], resolved["condition_id"], resolved["event_id"]) == ("2589812", "0xdf9b", "606422")
    assert (resolved["topic"], resolved["market_type"], resolved["series_slug"]) == ("fed", "decision", "fomc")
    assert (resolved["yes_token_id"], resolved["no_token_id"]) == ("111", "420")
    assert (resolved["resolved_outcome"], resolved["yes_payout"]) == ("yes", 1.0)
    assert resolved["rules"] == "Market rules text."
    assert resolved["category"] == "Economics|Politics"            # from the event's tags
    assert resolved["tags"] == "fed|economy|fed-rates|politics"
    assert resolved["outcome_labels"] == "Yes|No" and resolved["outcome_label"] == "No change"
    # the full fee schedule, as the market reports it
    assert bool(resolved["fees_enabled"]) and bool(resolved["fee_taker_only"])
    assert (resolved["fee_rate"], resolved["fee_exponent"], resolved["fee_rebate_rate"]) == (0.05, 1.0, 0.25)
    assert resolved["closed_time"] == pd.Timestamp("2026-10-28T20:01:40.290246Z")
    # open market: no outcome yet, no fee schedule, rules and start date taken from the event
    assert pd.isna(open_market["resolved_outcome"]) and math.isnan(open_market["yes_payout"])
    assert not open_market["fees_enabled"] and math.isnan(open_market["fee_rate"])
    assert pd.isna(open_market["fee_taker_only"])
    assert open_market["rules"] == "Event rules text."
    assert open_market["start_date"] == pd.Timestamp("2026-06-17T23:00:00Z")
    assert open_market["volume"] == 1000.5
    for column in polymarket.MARKET_TIME_COLUMNS:
        assert str(table[column].dt.tz) == "UTC"                   # project rule: times in UTC
    for column in polymarket.MARKET_BOOLEAN_COLUMNS:
        assert table[column].dtype == "boolean"


def test_markets_table_without_labels():
    table = polymarket.markets_table([EVENT])
    assert table["topic"].isna().all() and len(table) == 2


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

# The last three points of the 3-hour series of the YES token of "Fed
# decreases interest rates by 25 bps after December 2024 meeting?" (resolved
# YES): two 3-hour buckets, then the settlement point (payout 1, width 0).
OLD_POINTS = [
    {"timestamp": 1734480000, "price": 0.9695, "resolution_seconds": 10800},  # 2024-12-18 00:00 UTC
    {"timestamp": 1734523200, "price": 0.9505, "resolution_seconds": 10800},  # 2024-12-18 12:00
    {"timestamp": 1734534000, "price": 1.0, "resolution_seconds": 0},         # 2024-12-18 15:00
]


def test_price_history_of_a_resolved_market():
    table = polymarket.parse_price_history(OLD_POINTS, "504586", "yes", 10800, resolved=True)
    assert list(table.columns) == polymarket.PRICE_COLUMNS
    assert table["point_type"].tolist() == ["bucket", "bucket", "settlement"]
    assert table["timestamp"].tolist() == [pd.Timestamp("2024-12-18T00:00:00Z"), pd.Timestamp("2024-12-18T12:00:00Z"),
                                           pd.Timestamp("2024-12-18T15:00:00Z")]
    assert str(table["timestamp"].dt.tz) == "UTC"
    assert set(table["bucket_seconds"]) == {10800} and set(table["market_id"]) == {"504586"}
    assert table["price"].between(0, 1).all()


def test_price_history_of_an_open_market_ends_with_the_last_tick():
    table = polymarket.parse_price_history(OLD_POINTS, "1", "no", 3600, resolved=False)
    assert table["point_type"].tolist() == ["bucket", "bucket", "last_tick"]
    assert set(table["outcome"]) == {"no"} and set(table["bucket_seconds"]) == {3600}


def test_price_history_is_sorted_and_may_be_empty():
    table = polymarket.parse_price_history(list(reversed(OLD_POINTS)), "1", "yes", 10800, resolved=True)
    assert table["timestamp"].is_monotonic_increasing and table["point_type"].iloc[-1] == "settlement"
    empty = polymarket.parse_price_history([], "1", "yes", 10800)
    assert empty.empty and list(empty.columns) == polymarket.PRICE_COLUMNS


def test_price_history_requests(monkeypatch):
    asked = []
    monkeypatch.setattr(polymarket, "get_data_api_pages", lambda path, params: asked.append((path, params)) or [])
    polymarket.get_price_history("123")
    polymarket.get_price_history("123", polymarket.RECENT_BUCKET_SECONDS, "1m")
    assert asked[0] == ("/v2/prices-history",
                        {"token_id": "123", "interval": "max", "bucket_seconds": 10800, "limit": 10000})
    assert (asked[1][1]["interval"], asked[1][1]["bucket_seconds"]) == ("1m", 3600)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def test_trades_table_drops_profile_fields():
    profile = {"name": "someone", "pseudonym": "Some-Name", "bio": "hello", "proxy_wallet": "0x1234",
               "profile_image": "https://example.com/a.png", "icon": "https://example.com/b.png"}
    trades = [  # newest first, as the API sends them (shortened real rows)
        {"side": "BUY", "token_id": "1156", "condition_id": "0xa11a", "size": 199.49, "price": 0.999,
         "timestamp": 1734561206, "outcome": "Yes", "outcome_index": 0, "transaction_hash": "0x3f9c", **profile},
        {"side": "SELL", "token_id": "1046", "condition_id": "0xa11a", "size": 100.0, "price": 0.001,
         "timestamp": 1734560960, "outcome": "No", "outcome_index": 1, "transaction_hash": "0x3ff5", **profile},
    ]
    table = polymarket.trades_table(trades)
    assert list(table.columns) == polymarket.TRADE_COLUMNS
    assert not set(profile) & set(table.columns)
    assert "transaction_hash" not in table.columns                     # leads to the wallets on-chain
    assert table["timestamp"].is_monotonic_increasing                  # oldest first
    assert table.loc[0, "timestamp"] == pd.Timestamp("2024-12-18T22:29:20Z")
    assert table.loc[1, ["side", "outcome", "price", "size"]].tolist() == ["BUY", "Yes", 0.999, 199.49]


def test_get_trades_request(monkeypatch):
    asked = []
    monkeypatch.setattr(polymarket, "get_data_api_pages",
                        lambda path, params, max_pages=None, clean=None:
                        asked.append((path, params, max_pages, clean)) or [])
    polymarket.get_trades("0xa11a", max_pages=2)
    # Every page is cleaned (identity fields dropped) before it is cached.
    assert asked == [("/v2/trades", {"condition": "0xa11a", "limit": 1000}, 2, polymarket.keep_trade_fields)]


# ---------------------------------------------------------------------------
# Pagination and search
# ---------------------------------------------------------------------------

def test_get_keyset_pages_follows_the_cursor(monkeypatch):
    pages = {None: {"events": [1, 2], "next_cursor": "page2"},
             "page2": {"events": [3], "next_cursor": "page3"},
             "page3": {"events": [4], "next_cursor": None}}  # no cursor = last page
    monkeypatch.setattr(polymarket, "polite_get", lambda base, path, params: pages[params["after_cursor"]])
    assert polymarket.get_keyset_pages("/events/keyset", "events") == [1, 2, 3, 4]


def test_get_keyset_pages_stops_if_the_cursor_repeats(monkeypatch):
    pages = {None: {"events": [1], "next_cursor": "x"}, "x": {"events": [2], "next_cursor": "x"}}
    monkeypatch.setattr(polymarket, "polite_get", lambda base, path, params: pages[params["after_cursor"]])
    with pytest.raises(polymarket.PolymarketError):
        polymarket.get_keyset_pages("/events/keyset", "events")


def test_get_data_api_pages_follows_the_cursor(monkeypatch):
    pages = {None: {"data": [1, 2], "pagination": {"has_more": True, "next_cursor": "c2"}},
             "c2": {"data": [3], "pagination": {"has_more": True, "next_cursor": "c3"}},
             "c3": {"data": [4], "pagination": {"has_more": False, "next_cursor": None}}}
    monkeypatch.setattr(polymarket, "polite_get",
                        lambda base, path, params, clean=None: pages[params["cursor"]])
    assert polymarket.get_data_api_pages("/v2/trades", {"condition": "0x1"}) == [1, 2, 3, 4]
    assert polymarket.get_data_api_pages("/v2/trades", max_pages=2) == [1, 2, 3]


def test_search_events_reads_every_page_once(monkeypatch):
    pages = {1: {"events": [{"id": "1"}, {"id": "2"}], "pagination": {"hasMore": True}},
             2: {"events": [{"id": "2"}, {"id": "3"}], "pagination": {"hasMore": False}}}
    monkeypatch.setattr(polymarket, "polite_get", lambda base, path, params: pages[params["page"]])
    assert [event["id"] for event in polymarket.search_events("cpi")] == ["1", "2", "3"]


def test_list_events_and_markets_ask_for_open_and_closed(monkeypatch):
    asked = []
    monkeypatch.setattr(polymarket, "get_keyset_pages", lambda path, key, params: asked.append(params) or [])
    polymarket.list_events(tag_slug="cpi")
    assert asked[-1]["closed"] is None                       # left out = open and closed events
    assert asked[-1]["version"] == ["v1", "v2"] and asked[-1]["limit"] == 100
    polymarket.list_events(series_id=35, closed=True)
    assert asked[-1]["closed"] == "true" and asked[-1]["series_id"] == 35
    asked.clear()
    polymarket.list_markets(tag_id=100196)                   # Gamma's market list shows only open ones by default
    assert [params["closed"] for params in asked] == ["false", "true"]


# ---------------------------------------------------------------------------
# Polite requests: retries, errors, network blocks, cache
# ---------------------------------------------------------------------------

class FakeResponse:
    """Stands in for a requests response."""

    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self.text = json.dumps(body)
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """Replace the network: answers are taken from `queue`, requests recorded in `calls`."""
    monkeypatch.setattr(polymarket, "RAW_FOLDER", tmp_path)
    sleeps = []
    monkeypatch.setattr(polymarket.time, "sleep", sleeps.append)
    server = {"queue": [], "calls": [], "sleeps": sleeps}

    def fake_get(url, params=None, headers=None, timeout=None):
        server["calls"].append((url, params))
        answer = server["queue"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(polymarket.SESSION, "get", fake_get)
    return server


def test_polite_get_retries_429_and_5xx_then_caches(fake_server):
    fake_server["queue"] += [FakeResponse(429, {"error": "slow down"}, {"Retry-After": "5"}),
                             FakeResponse(503, {"error": "busy"}),
                             FakeResponse(200, {"data": [1]})]
    params = {"limit": 1, "cursor": None}
    assert polymarket.polite_get(polymarket.DATA_URL, "/v2/trades", params) == {"data": [1]}
    assert len(fake_server["calls"]) == 3
    assert "cursor" not in fake_server["calls"][0][1]     # None parameters are left out
    assert 5 in fake_server["sleeps"]                     # waited as long as Retry-After asked
    # The same request again comes from the cache: no new call.
    assert polymarket.polite_get(polymarket.DATA_URL, "/v2/trades", params) == {"data": [1]}
    assert len(fake_server["calls"]) == 3


def test_polite_get_does_not_retry_404_or_400(fake_server):
    fake_server["queue"] += [FakeResponse(404, {"error": "No orderbook exists for the requested token id"}),
                             FakeResponse(400, {"error": "Invalid token id"})]
    with pytest.raises(polymarket.NotFoundError):
        polymarket.polite_get(polymarket.CLOB_URL, "/book", {"token_id": "1"}, use_cache=False)
    with pytest.raises(polymarket.PolymarketError) as error:
        polymarket.polite_get(polymarket.CLOB_URL, "/book", {"token_id": "x"}, use_cache=False)
    assert error.type is polymarket.PolymarketError and len(fake_server["calls"]) == 2


def test_polite_get_names_the_blocked_domain(fake_server):
    # In a cloud session, a domain missing from the allowed list gives a proxy 403.
    fake_server["queue"].append(requests.exceptions.ProxyError("Tunnel connection failed: 403 Forbidden"))
    with pytest.raises(polymarket.NetworkBlockedError, match="data-api.polymarket.com"):
        polymarket.polite_get(polymarket.DATA_URL, "/v2/trades", {"limit": 1})


def test_cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr(polymarket, "RAW_FOLDER", tmp_path)
    path = polymarket.cache_path(polymarket.GAMMA_URL, "/events/keyset",
                                 {"tag_slug": "cpi", "version": ["v1", "v2"], "limit": 100})
    assert path == tmp_path / "gamma-api" / "events" / "keyset" / "limit=100_tag_slug=cpi_version=v1_version=v2.json"
    # Long queries (77-digit token ids, cursors) get a short name made unique by a hash.
    token = "4" * 77
    first = polymarket.cache_path(polymarket.DATA_URL, "/v2/prices-history", {"token_id": token, "cursor": "a" * 60})
    second = polymarket.cache_path(polymarket.DATA_URL, "/v2/prices-history", {"token_id": token, "cursor": "b" * 60})
    assert first != second and len(first.name) < 120 and first.parent.name == "prices-history"


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def test_save_table_round_trip_and_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(polymarket, "PROCESSED_FOLDER", tmp_path)
    table = pd.DataFrame({"market_id": ["A", "B"], "price": [0.42, 0.58],
                          "time": pd.to_datetime(["2026-01-01T00:00:00Z"] * 2, utc=True)})
    path = polymarket.save_table(table, "example")
    pd.testing.assert_frame_equal(pd.read_parquet(path), table)
    # A file over the limit is refused, and the older file is kept.
    monkeypatch.setattr(polymarket, "MAX_FILE_MB", 0)
    with pytest.raises(ValueError):
        polymarket.save_table(table.iloc[:1], "example")
    assert len(pd.read_parquet(path)) == 2
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Which events the download keeps (download_polymarket.classify_event)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("Fed decision in December?", ("fed", "decision")),
    ("Fed Decision in March?", ("fed", "decision")),
    ("Fed Interest Rates: November 2024", ("fed", "decision")),
    ("What will the Fed rate be at the end of 2026?", ("fed", "rate_level")),
    ("What will Fed Rate hit before 2027?", ("fed", "rate_level")),
    ("How many Fed rate cuts in 2026?", ("fed", "cuts_count")),
    ("Fed rate cut by...?", ("fed", "other")),
    ("Fed emergency rate cut in 2025?", ("fed", "other")),
    ("Another Fed rate hike in 2026?", ("fed", "other")),
    ("Fed decisions (Sep–Dec)", ("fed", "other")),
    ("What will be the next Fed rate change?", ("fed", "other")),
    ("October Inflation US - Monthly", ("cpi", "cpi_mom")),
    ("January Inflation - Monthly", ("cpi", "cpi_mom")),
    ("March Inflation US - Annual (Higher Brackets)", ("cpi", "cpi_yoy")),
    ("Annual inflation above 2.4% in October?", ("cpi", "cpi_yoy")),
    ("Core CPI (ex food and energy) MoM - May 2026", ("cpi", "core_cpi_mom")),
    ("Core CPI YoY - October 2026", ("cpi", "core_cpi_yoy")),
    ("How high will inflation get in 2025?", ("cpi", "other")),
])
def test_events_we_keep(title, expected):
    assert download_polymarket.classify_event(title) == expected


@pytest.mark.parametrize("title", [
    "How many dissent at the next Fed meeting?",
    "Fed decision & dissent combo in December?",
    "'No change' or '25 bps cut' favored for Fed decision in January on December 10?",
    "Fed Derivative: \"25bps cut\" Odds by December 5?",
    "Fed cuts 25 bps in March Odds >15% by February 13?",
    "Who will Trump nominate as Fed Chair?",
    "What will Powell say during December Press Conference?",
    "How high will 10-year Treasury yield go in October?",
    "ECB Interest Rates: June 2025",
    "No Cut October",
    "Will Fed end QT before May?",
    "October Inflation China - Annual",
    "November Inflation U.K. - Monthly",
    "October Inflation UK - Annual",
    "Argentina Monthly Inflation - October",
    "Japan Core CPI YoY in 2026",
    "Price of Dozen Eggs in October?",
    "PPI YoY - October 2026",
    "Core PCE YoY - September 2026",
    "BLS delays another CPI release before 2027?",
    "",
])
def test_events_we_leave_out(title):
    assert download_polymarket.classify_event(title) is None


def test_fed_market_types_are_the_agreed_ones():
    fed_types = {market_type for market_type, _ in download_polymarket.FED_RULES}
    assert fed_types == {"decision", "rate_level", "cuts_count", "other"}
