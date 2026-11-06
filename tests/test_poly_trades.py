"""
Unit tests for the Polymarket trade functions of src/polymarket.py
(keep_trade_fields, get_trades, trades_table, trade_bars) and for
src/download_poly_trades.py. Run them from the project folder with:  pytest

None of these tests use the internet: they use small, hand-made trades shaped
like the rows of the Data API's GET /v2/trades.

The bar formulas tested here. Polymarket's Data API defines "price" as the
execution price per share in USDC (dollars) and "size" as the filled quantity
in shares: https://data-api.polymarket.com/v2/openapi.json (schema Trade)
    notional = sum(price x size)     volume = sum(size)     vwap = notional / volume
    last_price = price of the last trade in the bar
"""

import json
import math

import pandas as pd
import pytest

import download_poly_trades
import polymarket

YES_TOKEN, NO_TOKEN = "111", "222"

# Fields of a real trade row that identify the trader (values made up).
IDENTITY = {
    "proxy_wallet": "0xWALLET", "name": "SOMEONE", "pseudonym": "Some-Pseudonym", "bio": "MY BIO",
    "profile_image": "https://example.com/me.png", "profile_image_optimized": "https://example.com/me-small.png",
    "transaction_hash": "0xTXHASH",
}
MARKET_LABELS = {"title": "Fed decreases interest rates?", "slug": "fed-decreases", "event_slug": "fed",
                 "icon": "https://example.com/fed.png"}


def api_trade(timestamp, price, size, outcome="Yes", side="BUY"):
    """One trade row as GET /v2/trades sends it (identity fields included)."""
    return {"side": side, "token_id": YES_TOKEN if outcome == "Yes" else NO_TOKEN,
            "condition_id": "0xc0", "size": size, "price": price, "timestamp": timestamp,
            "outcome": outcome, "outcome_index": 0 if outcome == "Yes" else 1,
            **IDENTITY, **MARKET_LABELS}


def utc(text):
    return pd.Timestamp(text, tz="UTC")


def seconds(text):
    """Unix seconds of a UTC time, as the API sends timestamps."""
    return int(utc(text).timestamp())


def trades(*rows):
    """A table like the one download_poly_trades makes: (market_id, outcome, time, price, size) per row."""
    return pd.DataFrame([{"market_id": market_id, "outcome": outcome, "timestamp": utc(time),
                          "price": price, "size": size} for market_id, outcome, time, price, size in rows])


# ---------------------------------------------------------------------------
# Trader identity fields never reach the disk
# ---------------------------------------------------------------------------

def test_keep_trade_fields_drops_identity_and_keeps_pagination():
    page = {"data": [api_trade(1734561206, 0.45, 10)],
            "pagination": {"has_more": True, "next_cursor": "abc"}}
    cleaned = polymarket.keep_trade_fields(page)
    assert cleaned["pagination"] == page["pagination"]           # needed for the next page
    assert list(cleaned["data"][0]) == polymarket.TRADE_COLUMNS   # only the allowed fields
    assert not set(IDENTITY) & set(cleaned["data"][0])
    assert cleaned["data"][0]["price"] == 0.45
    assert polymarket.keep_trade_fields({"data": [], "pagination": {}})["data"] == []


class FakeResponse:
    """Stands in for a requests response."""

    def __init__(self, body):
        self.status_code = 200
        self.text = json.dumps(body)
        self.headers = {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def fake_data_api(tmp_path, monkeypatch):
    """Replace the network with two pages of trades; record the requests in `calls`."""
    monkeypatch.setattr(polymarket, "RAW_FOLDER", tmp_path)
    monkeypatch.setattr(polymarket.time, "sleep", lambda seconds: None)
    pages = {
        None: {"data": [api_trade(1734561206, 0.46, 5), api_trade(1734561200, 0.45, 10)],
               "pagination": {"has_more": True, "next_cursor": "page2"}},
        "page2": {"data": [api_trade(1734560960, 0.55, 20, outcome="No", side="SELL")],
                  "pagination": {"has_more": False, "next_cursor": None}},
    }
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        return FakeResponse(pages[params.get("cursor")])

    monkeypatch.setattr(polymarket.SESSION, "get", fake_get)
    return calls


def all_cached_text(folder):
    return " ".join(path.read_text() for path in folder.rglob("*.json"))


def test_get_trades_never_caches_identity_fields(fake_data_api, tmp_path):
    rows = polymarket.get_trades("0xc0")
    assert len(rows) == 3 and len(fake_data_api) == 2            # both pages were followed
    assert all(set(row) == set(polymarket.TRADE_COLUMNS) for row in rows)
    cached = all_cached_text(tmp_path)
    assert len(list(tmp_path.rglob("*.json"))) == 2              # one cache file per page ...
    for value in list(IDENTITY.values()) + list(MARKET_LABELS.values()):
        assert value not in cached                               # ... without any identity field

    again = polymarket.get_trades("0xc0")                         # second time: from the cache
    assert again == rows and len(fake_data_api) == 2


def test_an_old_cache_file_with_identity_fields_is_cleaned_on_reading(fake_data_api, tmp_path):
    params = {"condition": "0xc0", "limit": polymarket.TRADE_PAGE_SIZE}
    old_file = polymarket.cache_path(polymarket.DATA_URL, "/v2/trades", params)
    old_file.parent.mkdir(parents=True)
    old_file.write_text(json.dumps({"data": [api_trade(1734561206, 0.46, 5)],
                                    "pagination": {"next_cursor": None}}))
    rows = polymarket.get_trades("0xc0")
    assert fake_data_api == []                                    # answered from the old file
    assert not set(IDENTITY) & set(rows[0])


# ---------------------------------------------------------------------------
# trades_table: the order of trades within the same second
# ---------------------------------------------------------------------------

def test_trades_table_keeps_the_order_of_trades_in_the_same_second():
    # Newest first, as the API sends them: two trades in second 100, one in second 90.
    rows = [api_trade(100, 0.30, 1), api_trade(100, 0.20, 1), api_trade(90, 0.10, 1)]
    table = polymarket.trades_table(rows)
    assert table["price"].tolist() == [0.10, 0.20, 0.30]          # oldest first, true order kept
    assert table["timestamp"].is_monotonic_increasing


def test_trades_table_without_trades():
    table = polymarket.trades_table([])
    assert table.empty and list(table.columns) == polymarket.TRADE_COLUMNS
    assert isinstance(table["timestamp"].dtype, pd.DatetimeTZDtype)


# ---------------------------------------------------------------------------
# trade_bars: 1-hour bars
# ---------------------------------------------------------------------------

def test_trade_bars_hand_computed_example():
    table = trades(
        ("m1", "yes", "2026-01-05 12:00:05", 0.40, 100),
        ("m1", "no", "2026-01-05 12:30:00", 0.55, 20),
        ("m1", "yes", "2026-01-05 12:59:59", 0.50, 300),
        ("m1", "yes", "2026-01-05 13:00:00", 0.60, 10),   # exactly on the hour: belongs to 13:00-14:00
        ("m1", "yes", "2026-01-05 15:10:00", 0.70, 5),    # nothing from 14:00 to 15:00: no bar there
    )
    bars = polymarket.trade_bars(table)
    assert list(bars.columns) == polymarket.BAR_COLUMNS
    assert bars[["outcome", "bar_start"]].values.tolist() == [
        ["no", utc("2026-01-05 12:00")], ["yes", utc("2026-01-05 12:00")],
        ["yes", utc("2026-01-05 13:00")], ["yes", utc("2026-01-05 15:00")]]

    yes_noon = bars.iloc[1]
    assert yes_noon["bar_end"] == utc("2026-01-05 13:00")
    assert yes_noon["n_trades"] == 2
    assert yes_noon["volume"] == 400                               # 100 + 300 shares
    assert yes_noon["notional"] == pytest.approx(190)              # 0.40 x 100 + 0.50 x 300 dollars
    assert yes_noon["vwap"] == pytest.approx(0.475)                # 190 / 400
    assert yes_noon["last_price"] == 0.50
    assert yes_noon["last_trade_time"] == utc("2026-01-05 12:59:59")

    no_noon = bars.iloc[0]                                         # NO has its own bar
    assert (no_noon["last_price"], no_noon["vwap"], no_noon["volume"]) == (0.55, pytest.approx(0.55), 20)
    assert bars.iloc[2]["last_price"] == 0.60 and bars.iloc[2]["n_trades"] == 1


def test_vwap_is_not_the_simple_average():
    bars = polymarket.trade_bars(trades(("m1", "yes", "2026-01-05 12:10", 0.20, 1),
                                        ("m1", "yes", "2026-01-05 12:20", 0.80, 3)))
    assert bars.loc[0, "vwap"] == pytest.approx((0.20 * 1 + 0.80 * 3) / 4)   # 0.65, not 0.50


def test_trade_bars_last_price_follows_the_api_order_within_a_second():
    # Two YES trades in the same second; the API (newest first) says 0.52 came last.
    rows = [api_trade(seconds("2026-01-05 12:30:00"), 0.52, 1),
            api_trade(seconds("2026-01-05 12:30:00"), 0.48, 1)]
    table = polymarket.trades_table(rows)
    table["market_id"], table["outcome"] = "m1", "yes"
    assert polymarket.trade_bars(table).loc[0, "last_price"] == 0.52


def test_trade_bars_keep_markets_apart_and_do_not_need_sorted_input():
    table = trades(("m2", "yes", "2026-01-05 12:40", 0.30, 1),
                   ("m1", "yes", "2026-01-05 12:50", 0.90, 1),
                   ("m1", "yes", "2026-01-05 12:10", 0.10, 1))
    bars = polymarket.trade_bars(table)
    assert bars["market_id"].tolist() == ["m1", "m2"]
    assert bars["last_price"].tolist() == [0.90, 0.30]            # the latest m1 trade, not the last row
    assert bars["n_trades"].tolist() == [2, 1]


def test_trade_bars_use_utc_and_milliseconds_like_the_kalshi_candles():
    bars = polymarket.trade_bars(trades(("m1", "yes", "2026-01-05 12:10", 0.20, 1)))
    for column in ["bar_start", "bar_end", "last_trade_time"]:
        assert str(bars[column].dtype) == "datetime64[ms, UTC]"


def test_trade_bars_of_other_widths():
    table = trades(("m1", "yes", "2026-01-05 12:10", 0.20, 1), ("m1", "yes", "2026-01-05 12:20", 0.30, 1))
    assert len(polymarket.trade_bars(table, bar="15min")) == 2
    assert len(polymarket.trade_bars(table, bar="1D")) == 1


def test_trade_bars_without_trades():
    empty = pd.DataFrame({"market_id": pd.Series(dtype="str"), "outcome": pd.Series(dtype="str"),
                          "timestamp": pd.Series(dtype="datetime64[s, UTC]"),
                          "price": pd.Series(dtype="float64"), "size": pd.Series(dtype="float64")})
    bars = polymarket.trade_bars(empty)
    assert bars.empty and list(bars.columns) == polymarket.BAR_COLUMNS


# ---------------------------------------------------------------------------
# download_poly_trades: outcome labels, checks, coverage
# ---------------------------------------------------------------------------

def test_label_outcomes_uses_the_token_ids():
    table = polymarket.trades_table([api_trade(100, 0.4, 1), api_trade(90, 0.6, 1, outcome="No")])
    labelled, unknown, disagree = download_poly_trades.label_outcomes(table, YES_TOKEN, NO_TOKEN)
    assert labelled["outcome"].tolist() == ["no", "yes"] and (unknown, disagree) == (0, 0)

    # Swapped token ids: every label now disagrees with the API's own label.
    _, _, disagree = download_poly_trades.label_outcomes(table, NO_TOKEN, YES_TOKEN)
    assert disagree == 2
    # A token that belongs to neither outcome is left unlabelled and counted.
    labelled, unknown, _ = download_poly_trades.label_outcomes(table, YES_TOKEN, "999")
    assert unknown == 1 and labelled["outcome"].isna().sum() == 1


def test_check_trades():
    good = trades(("m1", "yes", "2026-01-05 12:10", 0.20, 1), ("m1", "yes", "2026-01-05 12:20", 1.0, 3))
    assert download_poly_trades.check_trades(good) == []
    bad = trades(("m1", "yes", "2026-01-05 12:10", 1.20, 1), ("m1", "yes", "2026-01-05 12:20", 0.5, 0))
    assert len(download_poly_trades.check_trades(bad)) == 2


def test_coverage_table():
    markets = pd.DataFrame({
        "market_id": ["mA", "mB"], "topic": ["fed", "cpi"], "month": ["2026-01", "2026-02"],
        "question": ["A?", "B?"], "start_date": [utc("2025-12-01"), utc("2026-01-01")],
        "closed_time": [utc("2026-01-07"), pd.NaT], "gamma_volume": [100.0, 5.0]})
    table = trades(
        ("mA", "yes", "2026-01-05 10:00", 0.50, 10),
        ("mA", "no", "2026-01-05 10:30", 0.40, 10),
        ("mA", "yes", "2026-01-05 13:30", 0.60, 10),   # 3-hour gap before this one
        ("mA", "yes", "2026-01-06 14:00", 0.70, 10),   # 24.5-hour gap before this one
    )
    coverage = download_poly_trades.coverage_table(table, markets).set_index("market_id")
    a = coverage.loc["mA"]
    assert (a["first_trade"], a["last_trade"]) == (utc("2026-01-05 10:00"), utc("2026-01-06 14:00"))
    assert (a["n_trades"], a["n_yes_trades"], a["n_no_trades"]) == (4, 3, 1)
    assert a["hours_with_trades"] == 3                       # 10:00, 13:00 and 14:00 the next day
    assert a["hours_in_span"] == 29                          # 10:00 on day 1 to 14:00 on day 2, both counted
    assert a["share_hours_with_trades"] == pytest.approx(3 / 29)
    assert a["longest_gap_hours"] == pytest.approx(24.5)
    assert a["longest_gap_from"] == utc("2026-01-05 13:30")
    assert a["gaps_over_24h"] == 1
    assert a["volume"] == 40                                 # shares
    assert a["notional"] == pytest.approx(5 + 4 + 6 + 7)     # dollars
    assert a["volume_vs_gamma"] == pytest.approx(40 / 100)

    b = coverage.loc["mB"]                                   # a market without any trade
    assert b["n_trades"] == 0 and pd.isna(b["first_trade"]) and math.isnan(b["longest_gap_hours"])
    assert "| mA | fed | 2026-01 | 2026-01-05 10:00 |" in download_poly_trades.coverage_markdown(
        coverage.reset_index())
