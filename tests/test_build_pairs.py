"""
Unit tests for src/build_pairs.py.
Run them from the project folder with:  pytest

None of these tests use the internet or the real data files: they use a
small match_verified table and two small market tables made up below.
"""

import datetime

import pandas as pd
import pytest

import build_pairs


def verified_table():
    """Three reviewed pairs: one "same" (settled), one "same" (not settled yet), one "unsure"."""
    common = {"poly_yes_equals": "Kalshi NO", "kalshi_title": "Kalshi title", "poly_question": "Poly question",
              "poly_url": "https://polymarket.com/event/x", "note": "my note", "review_method": "automated two-pass"}
    rows = [
        {"pair_id": "KXCPI-25DEC-T0.1 / 833215", "verdict": "same", "kalshi_ticker": "KXCPI-25DEC-T0.1",
         "poly_market_id": "833215", "kalshi_measure": "cpi_mom", "poly_measure": "cpi_mom", "kalshi_month": "2025-12",
         "kalshi_event_date": "2026-01-13", "kalshi_outcome": "CPI MoM ≥ 0.2%",
         "kalshi_close_time": "2026-01-13 13:29:00+00:00", "poly_end_date": "2025-12-13 00:00:00+00:00",
         "poly_closed_time": "2026-01-13 16:43:27.5+00:00", "kalshi_result": "yes", "poly_result": "no"},
        {"pair_id": "KXFEDDECISION-26DEC-H0 / 3215007", "verdict": "same", "kalshi_ticker": "KXFEDDECISION-26DEC-H0",
         "poly_market_id": "3215007", "kalshi_measure": "fed_change", "poly_measure": "fed_change",
         "kalshi_month": "2026-12", "kalshi_event_date": "2026-12-09", "kalshi_outcome": "no change",
         "kalshi_close_time": "2026-12-09 18:55:00+00:00", "poly_end_date": "2026-12-09 00:00:00+00:00",
         "poly_closed_time": "", "kalshi_result": "", "poly_result": ""},
        {"pair_id": "KXFED-26DEC-T3.50 / 3215005", "verdict": "unsure", "kalshi_ticker": "KXFED-26DEC-T3.50",
         "poly_market_id": "3215005", "kalshi_measure": "fed_level", "poly_measure": "fed_change",
         "kalshi_month": "2026-12", "kalshi_event_date": "2026-12-09", "kalshi_outcome": "upper bound ≥ 3.75%",
         "kalshi_close_time": "2026-12-09 18:55:00+00:00", "poly_end_date": "2026-12-09 00:00:00+00:00",
         "poly_closed_time": "", "kalshi_result": "", "poly_result": ""},
    ]
    return pd.DataFrame([{**common, **row} for row in rows])


def market_tables():
    kalshi = pd.DataFrame({"ticker": ["KXCPI-25DEC-T0.1", "KXFEDDECISION-26DEC-H0", "KXFED-26DEC-T3.50"],
                           "event_ticker": ["KXCPI-25DEC", "KXFEDDECISION-26DEC", "KXFED-26DEC"]})
    poly = pd.DataFrame({"market_id": ["833215", "3215007", "3215005"],
                         "condition_id": ["0xaaa", "0xbbb", "0xccc"], "event_id": ["97910", "300", "300"],
                         "yes_token_id": ["111", "222", "333"], "no_token_id": ["444", "555", "666"]})
    return kalshi, poly


def test_only_same_pairs_with_the_ids_to_fetch_prices():
    pairs = build_pairs.build_pairs(verified_table(), *market_tables())
    assert list(pairs.columns) == build_pairs.COLUMNS
    assert pairs["pair_id"].tolist() == ["KXCPI-25DEC-T0.1 / 833215", "KXFEDDECISION-26DEC-H0 / 3215007"]
    assert pairs["topic"].tolist() == ["cpi", "fed"]
    assert pairs["measure"].tolist() == ["cpi_mom", "fed_change"]
    assert pairs["month"].tolist() == ["2025-12", "2026-12"]
    assert pairs["kalshi_event_ticker"].tolist() == ["KXCPI-25DEC", "KXFEDDECISION-26DEC"]
    assert pairs["poly_condition_id"].tolist() == ["0xaaa", "0xbbb"]
    assert pairs["poly_yes_token_id"].tolist() == ["111", "222"]
    assert pairs["poly_no_token_id"].tolist() == ["444", "555"]
    assert pairs["poly_event_id"].tolist() == ["97910", "300"]
    assert pairs["event_date"].tolist() == [datetime.date(2026, 1, 13), datetime.date(2026, 12, 9)]


def test_times_are_utc_and_unsettled_pairs_have_missing_values():
    pairs = build_pairs.build_pairs(verified_table(), *market_tables())
    for column in build_pairs.TIME_COLUMNS:
        assert str(pairs[column].dt.tz) == "UTC"
    assert pairs["kalshi_close_time"].iloc[0] == pd.Timestamp("2026-01-13 13:29:00", tz="UTC")
    assert pairs["poly_closed_time"].iloc[0] == pd.Timestamp("2026-01-13 16:43:27.5", tz="UTC")  # with a fraction
    assert pd.isna(pairs["poly_closed_time"].iloc[1])  # still open
    assert pairs["kalshi_result"].iloc[0] == "yes" and pairs["poly_result"].iloc[0] == "no"
    assert pairs["kalshi_result"].isna().tolist() == [False, True]


def test_a_manual_review_gets_its_own_review_method():
    verified = verified_table().drop(columns="review_method")  # as written by read_review_sheet.py
    pairs = build_pairs.build_pairs(verified, *market_tables())
    assert set(pairs["review_method"]) == {"manual review sheet"}


def test_a_same_pair_that_cannot_be_used_stops():
    verified = verified_table()
    verified.loc[2, "verdict"] = "same"  # a level-vs-change pair: Kalshi's measure differs from Polymarket's
    with pytest.raises(ValueError, match="KXFED-26DEC-T3.50 / 3215005: Kalshi measures fed_level"):
        build_pairs.build_pairs(verified, *market_tables())
    kalshi, poly = market_tables()
    with pytest.raises(ValueError, match="Polymarket market 833215 is not in poly_markets.parquet"):
        build_pairs.build_pairs(verified_table(), kalshi, poly.iloc[1:])


def test_counts_per_month_and_topic():
    counts = build_pairs.counts_per_month(build_pairs.build_pairs(verified_table(), *market_tables()))
    assert counts.loc["2025-12", ("cpi", "cpi_mom")] == 1
    assert counts.loc["2026-12", ("fed", "fed_change")] == 1
    assert counts.loc["all", ("all", "")] == 2


def test_saved_file_keeps_the_types(tmp_path):
    pairs = build_pairs.build_pairs(verified_table(), *market_tables())
    again = pd.read_parquet(build_pairs.save_pairs(pairs, tmp_path / "pairs.parquet"))
    pd.testing.assert_frame_equal(again, pairs, check_dtype=False)
    assert str(again["kalshi_close_time"].dt.tz) == "UTC"
