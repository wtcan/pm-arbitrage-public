"""
Unit tests for src/match.py. Run them from the project folder with:  pytest

None of these tests use the internet. They feed real titles and rules
sentences from both venues, and small hand-made market tables in the same
format as kalshi_markets.parquet and poly_markets.parquet, into match.py.

The key formula: both venues read CPI with ONE decimal, so Kalshi's "more
than 0.2%" means "0.3% or more". Sources: Kalshi's rules ("the Expiration
Value is the single-decimal value published at the Source Agency"),
Polymarket's rules ("which BLS reports to one decimal point ... this is the
level of precision that will be used"), and the BLS release itself:
https://www.bls.gov/bls/news-release/cpi.htm
For the Fed, moves come in steps of 25 bp:
https://www.federalreserve.gov/monetarypolicy/openmarket.htm
"""

from datetime import date

import pandas as pd
import pytest

import match


def utc(text):
    return pd.Timestamp(text, tz="UTC")


# ---------------------------------------------------------------------------
# Hand-made market tables in the format of the downloaded Parquet files
# ---------------------------------------------------------------------------

KALSHI_CPI_RULES_1 = "If the Consumer Price Index (CPI) increases by more than {x}% (single-decimal) in October 2026, " \
                     "then the market resolves to Yes."
KALSHI_CPI_RULES_2 = ("The market will always close at 8:29 AM ET on the scheduled day of the data release (November 10, "
                      "2026). Please note: the Expiration Value is the single-decimal value published at the Source "
                      "Agency. In the case of a delay in data caused by a federal government shutdown impacting the "
                      "reliability of the Source Agency, the market’s latest Expiration Date will be extended to the "
                      "sooner of the release of the Underlying or six months after the end of the government shutdown.")
KALSHI_FED_RULES_2 = ("The outcomes are mutually exclusive: at most one can resolve to Yes. If the scheduled FOMC meeting "
                      "is canceled and does not occur on its scheduled date, “Fed maintains rate” resolves to Yes and "
                      "all other outcomes resolve to No.")
POLY_CPI_RULES = ("This market will resolve to the one-month percent change in the seasonally adjusted Consumer Price "
                  "Index for All Urban Consumers (CPI-U) in October 2026 according to the monthly BLS report. The "
                  "resolution source for this market will be the BLS Consumer Price Index report released for October "
                  "2026, currently scheduled to be released on November 10, 2026, at 8:30 AM ET. Note: ... which BLS "
                  "reports to one decimal point (e.g. 0.4%). If the BLS does not release the relevant figures on the "
                  "scheduled date, this market may remain open up until the scheduled release time of the next CPI "
                  "report. If the information is not released by that time, this market will resolve according to "
                  "the figures of the most recent previous month with available data.")
POLY_FED_RULES = ("This market will resolve to the amount of basis points the upper bound of the target federal funds "
                  "rate is changed by versus the level it was prior to the Federal Reserve's September 2026 meeting. "
                  "If the target federal funds rate is changed to a level not expressed in the displayed options, the "
                  "change will be rounded up to the nearest 25 and will resolve to the relevant bracket. The "
                  "resolution source for this market is the FOMC’s statement after its meeting scheduled for "
                  "September 15-16, 2026. If no statement is released by the end date of the next scheduled meeting, "
                  "this market will resolve to the \"No change\" bracket.")


def kalshi_market(ticker, title, close="2026-11-10 13:29", result=None, rules_1="", rules_2="",
                  settled=None, series=None):
    """One row of kalshi_markets.parquet (only the columns match.py uses)."""
    event = ticker.rsplit("-", 1)[0]
    return {"ticker": ticker, "event_ticker": event, "series_ticker": series or event.split("-")[0],
            "title": title, "rules_primary": rules_1, "rules_secondary": rules_2, "close_time": utc(close),
            "settlement_time": utc(settled) if settled else pd.NaT, "result": result}


def poly_market(market_id, question, market_type="cpi_mom", topic="cpi", event_id="E1", rules=POLY_CPI_RULES,
                end="2026-11-11 04:59", closed=None, result=None, source=None):
    """One row of poly_markets.parquet (only the columns match.py uses)."""
    return {"market_id": market_id, "event_id": event_id, "event_slug": f"event-{event_id}", "topic": topic,
            "market_type": market_type, "question": question, "rules": rules, "end_date": utc(end),
            "closed_time": utc(closed) if closed else pd.NaT, "resolved_outcome": result,
            "resolution_source": source, "volume": 1000.0}


def kalshi_cpi(threshold, result=None):
    return kalshi_market(f"KXCPI-26OCT-T{threshold}", f"Will CPI rise more than {threshold}% in October 2026?",
                         result=result, rules_1=KALSHI_CPI_RULES_1.format(x=threshold), rules_2=KALSHI_CPI_RULES_2)


def tables(kalshi_rows, poly_rows):
    """match.py's market tables (key fields read) from hand-made rows."""
    return (match.kalshi_table(pd.DataFrame(kalshi_rows), sources={}),
            match.poly_table(pd.DataFrame(poly_rows)))


# ---------------------------------------------------------------------------
# Kalshi: key fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event_ticker, expected", [
    ("KXCPI-26OCT", "2026-10"),
    ("KXFEDDECISION-27JAN", "2027-01"),
    ("FED-25JAN", "2025-01"),          # old tickers without "KX"
    ("FEDDECISION-24NOV", "2024-11"),
    ("CPIYOY-24OCT", "2024-10"),
    ("KXCPI", None),
    (None, None),
])
def test_kalshi_month(event_ticker, expected):
    assert match.kalshi_month(event_ticker) == expected


@pytest.mark.parametrize("measure, title, expected", [
    # CPI: one-decimal figures, so "more than 0.2%" means 0.3% or more (3 tenths)
    (match.CPI_MOM, "Will CPI rise more than 0.2% in October 2026?", (0.2, "more than", 3, None)),
    (match.CPI_MOM, "Will CPI rise more than -0.1% in October 2025?", (-0.1, "more than", 0, None)),
    (match.CPI_YOY, "Will the rate of CPI inflation be above 2.4% for the year ending in November 2026?",
     (2.4, "more than", 25, None)),
    # Fed level: steps of 25 bp, so "above 3.75%" means 4.00% or more
    (match.FED_LEVEL, "Will the upper bound of the federal funds rate be above 3.75% following the Fed's Dec 9, "
                      "2026 meeting?", (3.75, "more than", 400, None)),
    (match.FED_CHANGE, "Will the Federal Reserve Cut rates by >25bps at their December 2026 meeting?",
     (25.0, "cut, more than", None, -50)),
    (match.FED_CHANGE, "Will the Federal Reserve Cut rates by 25bps at their December 2026 meeting?",
     (25.0, "cut, exactly", -25, -25)),
    (match.FED_CHANGE, "Will the Federal Reserve Hike rates by 0bps at their January 2025 meeting?",
     (0.0, "no change", 0, 0)),
    (match.FED_CHANGE, "Will the Federal Reserve Hike rates by 25bps at their December 2026 meeting?",
     (25.0, "hike, exactly", 25, 25)),
    (match.FED_CHANGE, "Will the Federal Reserve Hike rates by >25bps at their December 2026 meeting?",
     (25.0, "hike, more than", 50, None)),
    (match.FED_CHANGE, "Something else entirely?", (None, None, None, None)),
])
def test_kalshi_outcome(measure, title, expected):
    assert match.kalshi_outcome(measure, title) == expected


def test_kalshi_rules_date():
    assert match.kalshi_rules_date(KALSHI_CPI_RULES_2) == date(2026, 11, 10)
    assert match.kalshi_rules_date("If the Federal Reserve does a Cut of 25bps on December 09, 2026, then the "
                                   "market resolves to Yes.") == date(2026, 12, 9)
    assert match.kalshi_rules_date("...greater than 5.00% following the Federal Reserve's Dec 9, 2026 "
                                   "meeting...") == date(2026, 12, 9)
    assert match.kalshi_rules_date("no date here") is None


def test_kalshi_rule_features():
    new = match.kalshi_rule_features(KALSHI_FED_RULES_2)
    assert new["cancel_rule"] == "hold_yes"
    old = match.kalshi_rule_features("Clarification 10:00 AM ET 1/24/25: If a scheduled FOMC meeting is canceled and "
                                     "does not occur on its scheduled date, then all strikes in this market resolve "
                                     "to No.")
    assert old["cancel_rule"] == "all_no"
    note = match.kalshi_rule_features('Note 4/28/25: ... then the strike for "Fed maintains rate" will resolve to '
                                      'Yes and all others will resolve to No.')
    assert note["cancel_rule"] == "hold_yes"
    cpi = match.kalshi_rule_features(KALSHI_CPI_RULES_1.format(x=0.2) + " " + KALSHI_CPI_RULES_2)
    assert cpi["waits_after_shutdown"] and cpi["one_decimal"] and cpi["cancel_rule"] is None
    # The ">" sign got lost in some Kalshi rules texts: "a Hike of   25bps" in a ">25bps" market
    assert match.kalshi_rule_features("If the Federal Reserve does a Hike of   25bps on December 09, 2026")["lost_symbol"]
    assert not match.kalshi_rule_features("If the Federal Reserve does a Hike of 25bps on December 09, 2026")[
        "lost_symbol"]


# ---------------------------------------------------------------------------
# Polymarket: key fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("measure, question, expected", [
    # Every wording found in the Fed decision and CPI markets on 2026-11-04
    (match.CPI_MOM, "Will monthly inflation increase by 0.3% in October?", (0.3, "exactly", 3, 3)),
    (match.CPI_MOM, "Will monthly inflation increase by 0.9% or more in October?", (0.9, "at least", 9, None)),
    (match.CPI_MOM, "Will monthly inflation increase by 0.1% or less in January?", (0.1, "at most", None, 1)),
    (match.CPI_MOM, "Will monthly inflation increase by ≥1.1% in April?", (1.1, "at least", 11, None)),
    (match.CPI_MOM, "Will monthly inflation decrease by 0.6% in July?", (-0.6, "exactly", -6, -6)),
    (match.CPI_MOM, "Will monthly inflation decrease by 0.7% or more in July?", (-0.7, "at most", None, -7)),
    (match.CPI_MOM, "Will monthly inflation stay flat (0.0%) in July?", (0.0, "exactly", 0, 0)),
    (match.CPI_MOM, "Will monthly inflation stay flat (0.0%) or decrease in September?", (0.0, "at most", None, 0)),
    (match.CPI_YOY, "Will annual inflation increase by ≤2.8% in October?", (2.8, "at most", None, 28)),
    (match.CPI_YOY, "Will annual inflation increase by ≥3.2% in October?", (3.2, "at least", 32, None)),
    (match.CPI_YOY, "Will annual inflation be 3.4% in May?", (3.4, "exactly", 34, 34)),
    (match.CPI_YOY, "Will US annual inflation be 2.9% or greater in December?", (2.9, "at least", 29, None)),
    (match.CPI_YOY, "Annual inflation above 2.4% in October?", (2.4, "more than", 25, None)),
    (match.CORE_CPI_MOM, "Will Core CPI MoM be -0.3% or less in May?", (-0.3, "at most", None, -3)),
    (match.FED_CHANGE, "Will the Fed decrease interest rates by 50+ bps after the April 2027 meeting?",
     (50.0, "cut, at least", None, -50)),
    (match.FED_CHANGE, "Fed decreases interest rates by 50 bps after November 2024 meeting?",
     (50.0, "cut, exactly", -50, -50)),
    (match.FED_CHANGE, "Will the Fed increase interest rates by 25+ bps after the March 2026 meeting?",
     (25.0, "hike, at least", 25, None)),
    (match.FED_CHANGE, "Will the Fed increase interest rates by 25 bps after the June 2026 meeting?",
     (25.0, "hike, exactly", 25, 25)),
    (match.FED_CHANGE, "Will there be no change in Fed interest rates after the March 2026 meeting?",
     (0.0, "no change", 0, 0)),
    (match.FED_CHANGE, "No change in Fed interest rates after 2024 November meeting?", (0.0, "no change", 0, 0)),
    (match.FED_CHANGE, "Will the FED change rates to another level after Nov meeting?",
     (None, "between the listed sizes", None, None)),
])
def test_poly_outcome(measure, question, expected):
    assert match.poly_outcome(measure, question) == expected


def test_poly_month():
    assert match.poly_month("Will the Fed decrease interest rates by 25 bps after the April 2027 meeting?", "",
                            None) == "2027-04"
    assert match.poly_month("No change in Fed interest rates after 2024 November meeting?", "", None) == "2024-11"
    # no year in the question: taken from the rules
    assert match.poly_month("Will monthly inflation increase by 0.3% in October?", POLY_CPI_RULES, None) == "2026-10"
    # a date with a day ("December 16, 2025") is not a month; the meeting month comes later in the text
    assert match.month_in_text("between December 16, 2025 and the FOMC meeting for July 2027") == "2027-07"
    # last resort: the month in the question, with the year of the end date (December data ends in January)
    assert match.poly_month("Will monthly inflation increase by 0.4% in December?", "no month here",
                            utc("2025-01-15 12:00")) == "2024-12"


@pytest.mark.parametrize("rules, expected", [
    (POLY_CPI_RULES, date(2026, 11, 10)),
    (POLY_FED_RULES, date(2026, 9, 16)),  # last day of the meeting
    ("...its meeting scheduled for November 6 - 7, 2024 according to the official calendar", date(2024, 11, 7)),
    ("...its meeting scheduled to conclude on April 28, 2027 according to the official calendar", date(2027, 4, 28)),
    ("...its meeting scheduled for April 30 - May 1, 2025 according to ...", date(2025, 5, 1)),
    ("no date", None),
])
def test_poly_scheduled_date(rules, expected):
    assert match.poly_scheduled_date(rules) == expected


def test_poly_rule_features():
    fed = match.poly_rule_features(POLY_FED_RULES)
    assert fed["no_statement_rule"] == "the end date of the next scheduled meeting"
    assert fed["rounds_up_25"]
    old_fed = match.poly_rule_features("...is decreased by exactly 50 basis points below the level it was prior to "
                                       "the meeting. ... If no statement is released by December 18, 2024, 11:59 PM "
                                       "ET, this market will resolve to the \"No change\" bracket.")
    assert not old_fed["rounds_up_25"] and old_fed["no_statement_rule"] == "December 18, 2024, 11:59 PM ET"
    cpi = match.poly_rule_features(POLY_CPI_RULES)
    assert cpi["previous_month_fallback"] and cpi["one_decimal"]
    assert cpi["seasonal"] == "seasonally adjusted"
    yoy = match.poly_rule_features("This is a market about inflation over the 12-month period ending October 2026, "
                                   "before seasonal adjustment, as reported by the Bureau of Labor Statistics.")
    assert yoy["seasonal"] == "not seasonally adjusted"
    assert not yoy["previous_month_fallback"]


def test_poly_source_falls_back_to_the_rules():
    assert match.poly_source("https://www.youtube.com/watch?v=x", POLY_FED_RULES) == "https://www.youtube.com/watch?v=x"
    assert match.poly_source(None, "Source: https://www.bls.gov/bls/news-release/cpi.htm. More") == \
        "https://www.bls.gov/bls/news-release/cpi.htm"
    assert match.domain("https://www.federalreserve.gov/monetarypolicy/openmarket.htm") == "federalreserve.gov"


# ---------------------------------------------------------------------------
# Comparing outcomes
# ---------------------------------------------------------------------------

def outcome_relation(kalshi_title, poly_question, measure=match.CPI_MOM):
    _, _, k_low, k_high = match.kalshi_outcome(measure, kalshi_title)
    _, _, p_low, p_high = match.poly_outcome(measure, poly_question)
    return match.relation(k_low, k_high, p_low, p_high, match.STEP[measure])


def test_more_than_on_one_decimal_data_equals_the_next_tenth_or_more():
    # The key formula (sources at the top of this file): "more than 0.2%" == "0.3% or more"
    assert outcome_relation("Will CPI rise more than 0.2% in October 2026?",
                            "Will monthly inflation increase by 0.3% or more in October?") == "same"
    assert outcome_relation("Will CPI rise more than 0.8% in October 2026?",
                            "Will monthly inflation increase by ≥0.9% in October?") == "same"


def test_complementary_outcomes_are_opposite_sides():
    # Polymarket "0.1% or less" wins exactly when Kalshi "more than 0.1%" loses
    assert outcome_relation("Will CPI rise more than 0.1% in October 2026?",
                            "Will monthly inflation increase by 0.1% or less in October?") == "opposite"
    assert outcome_relation("Will the rate of CPI inflation be above 2.8% for the year ending in October 2025?",
                            "Will annual inflation increase by ≤2.8% in October?", match.CPI_YOY) == "opposite"


def test_same_number_but_different_outcome():
    # The traps title similarity falls for: same number, different meaning
    assert outcome_relation("Will CPI rise more than 0.3% in October 2026?",
                            "Will monthly inflation increase by 0.3% in October?") == "no overlap"
    assert outcome_relation("Will CPI rise more than 0.2% in October 2026?",
                            "Will monthly inflation increase by 0.3% in October?") == "overlap"
    assert outcome_relation("Will the Federal Reserve Cut rates by >25bps at their December 2026 meeting?",
                            "Will the Fed decrease interest rates by 25 bps after the December 2026 meeting?",
                            match.FED_CHANGE) == "no overlap"


def test_fed_decision_buckets():
    fed = match.FED_CHANGE
    assert outcome_relation("Will the Federal Reserve Cut rates by >25bps at their December 2026 meeting?",
                            "Will the Fed decrease interest rates by 50+ bps after the December 2026 meeting?",
                            fed) == "same"
    assert outcome_relation("Will the Federal Reserve Hike rates by 0bps at their January 2025 meeting?",
                            "No change in Fed interest rates after January 2025 meeting?", fed) == "same"
    # Polymarket's older "25+ bps increase" covers Kalshi's "Hike 25bps" AND "Hike >25bps": no single twin
    assert outcome_relation("Will the Federal Reserve Hike rates by 25bps at their March 2026 meeting?",
                            "Will the Fed increase interest rates by 25+ bps after the March 2026 meeting?",
                            fed) == "overlap"
    # the "Other" bucket has no outcome to compare
    assert outcome_relation("Will the Federal Reserve Hike rates by 25bps at their November 2024 meeting?",
                            "Will the FED change rates to another level after Nov meeting?", fed) == "unknown"


def test_describe_outcome():
    assert match.describe_outcome(match.CPI_MOM, 3, None) == "CPI MoM ≥ 0.3%"
    assert match.describe_outcome(match.CPI_YOY, None, 28) == "CPI YoY ≤ 2.8%"
    assert match.describe_outcome(match.CPI_MOM, -6, -6) == "CPI MoM = -0.6%"
    assert match.describe_outcome(match.FED_CHANGE, None, -50) == "cut of 50 bp or more"
    assert match.describe_outcome(match.FED_CHANGE, 0, 0) == "no change"
    assert match.describe_outcome(match.FED_CHANGE, 25, 25) == "hike of 25 bp"
    assert match.describe_outcome(match.FED_LEVEL, 400, None) == "upper bound ≥ 4.00%"
    assert match.describe_outcome(match.CPI_MOM, None, None) is None


def test_title_similarity_cannot_tell_these_apart():
    # Why the key fields decide: a true pair and a false pair score the same
    true_pair = match.title_similarity("Will the Federal Reserve Cut rates by 25bps at their December 2026 meeting?",
                                       "Will the Fed decrease interest rates by 25 bps after the December 2026 meeting?")
    false_pair = match.title_similarity(
        "Will the Federal Reserve Cut rates by >25bps at their December 2026 meeting?",
        "Will the Fed decrease interest rates by 25 bps after the December 2026 meeting?")
    assert abs(true_pair - false_pair) < 5
    assert match.title_similarity("Will CPI rise more than 0.3%?", "will cpi rise more than 0.3%") == 100
    assert match.clean_title("Cut rates by >25bps, 0.3%?") == "cut rates by >25bps 0.3%"


# ---------------------------------------------------------------------------
# Settled values and the rate before each meeting
# ---------------------------------------------------------------------------

def test_settled_values_of_kalshi_thresholds():
    # Kalshi CPI October 2025: "more than -0.1, 0.0, 0.1%" Yes, "more than 0.2, 0.3, 0.4%" No -> 0.2%
    outcomes = [(0, None, "yes"), (1, None, "yes"), (2, None, "yes"), (3, None, "no"), (4, None, "no"),
                (5, None, "no")]
    values, grid = match.settled_values(outcomes, match.CPI_MOM)
    assert values == [2]
    assert match.describe_values(values, grid, match.CPI_MOM) == "= 0.2%"


def test_settled_values_of_buckets_and_open_ends():
    # Polymarket buckets: "0.3%" Yes, the others No -> 0.3%
    buckets = [(None, 1, "no"), (2, 2, "no"), (3, 3, "yes"), (4, 4, "no"), (5, None, "no")]
    assert match.settled_values(buckets, match.CPI_MOM)[0] == [3]
    # Kalshi YoY October 2025: every threshold up to "above 3.1%" settled Yes -> "≥ 3.2%"
    all_yes = [(x, None, "yes") for x in range(25, 33)]
    values, grid = match.settled_values(all_yes, match.CPI_YOY)
    assert match.describe_values(values, grid, match.CPI_YOY) == "≥ 3.2%"
    # results that cannot all be true
    contradictory = [(3, 3, "yes"), (4, 4, "yes")]
    assert match.settled_values(contradictory, match.CPI_MOM)[0] == []
    assert match.describe_values([], [1, 2], match.CPI_MOM) == "contradictory results"


def test_rate_before_each_meeting():
    title = "Will the upper bound of the federal funds rate be above {x}% following the Fed's meeting?"
    rows = []
    for event, close, level_after in [("KXFED-26JUL", "2026-07-29 17:55", 3.75), ("KXFED-26SEP", "2026-09-16 17:55", 4.00),
                                      ("KXFED-26OCT", "2026-10-28 17:55", None)]:
        for strike in [3.25, 3.50, 3.75, 4.00, 4.25]:
            result = None if level_after is None else ("yes" if level_after > strike else "no")
            rows.append(kalshi_market(f"{event}-T{strike:.2f}", title.format(x=f"{strike:.2f}"), close=close,
                                      result=result, series="KXFED"))
    before = match.rate_before_each_meeting(match.kalshi_table(pd.DataFrame(rows), sources={}))
    assert before["2026-09"][:2] == (375, "2026-07")   # 3.75% before September, from the July meeting
    assert before["2026-10"][:2] == (400, "2026-09")   # 4.00% before October
    assert "2026-07" not in before                      # no earlier meeting in the data


# ---------------------------------------------------------------------------
# Pairs and candidates
# ---------------------------------------------------------------------------

def test_candidates_are_chosen_by_key_fields():
    kalshi_markets, poly_markets = tables(
        [kalshi_cpi(0.1), kalshi_cpi(0.2), kalshi_cpi(0.3), kalshi_cpi(0.8)],
        [poly_market("P1", "Will monthly inflation increase by 0.1% or less in October?"),
         poly_market("P2", "Will monthly inflation increase by 0.3% in October?"),
         poly_market("P3", "Will monthly inflation increase by 0.9% or more in October?")])
    pairs = match.build_pairs(kalshi_markets, poly_markets, before={})
    assert len(pairs) == 12  # every pair: same topic, dates 1 day apart
    candidates = pairs[pairs.is_candidate].set_index(["kalshi_ticker", "poly_market_id"])
    assert set(candidates.index) == {("KXCPI-26OCT-T0.1", "P1"), ("KXCPI-26OCT-T0.8", "P3")}
    assert candidates.loc[("KXCPI-26OCT-T0.1", "P1"), "relation"] == "opposite"
    assert candidates.loc[("KXCPI-26OCT-T0.8", "P3"), "relation"] == "same"
    rejected = pairs.set_index(["kalshi_ticker", "poly_market_id"]).loc[("KXCPI-26OCT-T0.3", "P2")]
    assert "outcomes differ" in rejected.why_not_candidate


def test_date_window_uses_end_date_or_actual_close():
    # Polymarket's end date is a month early, but it closed on the release day: still compared
    kalshi_markets, poly_markets = tables(
        [kalshi_cpi(0.8)],
        [poly_market("P1", "Will monthly inflation increase by 0.9% or more in October?", end="2026-10-10 00:00",
                     closed="2026-11-10 15:00"),
         poly_market("P2", "Will Core CPI MoM be 0.9% or more in October?", market_type="core_cpi_mom",
                     end="2026-10-10 00:00")])
    pairs = match.build_pairs(kalshi_markets, poly_markets, before={}).set_index("poly_market_id")
    assert pairs.loc["P1", "dates_within_7_days"] and pairs.loc["P1", "is_candidate"]
    assert "P2" not in pairs.index  # different measure and dates 31 days apart: not compared at all


def test_level_vs_change_pairs_need_the_rate_before_the_meeting():
    kalshi_markets, poly_markets = tables(
        [kalshi_market("KXFED-26SEP-T3.25", "Will the upper bound of the federal funds rate be above 3.25% "
                       "following the Fed's Sep 16, 2026 meeting?", close="2026-09-16 17:55", series="KXFED")],
        [poly_market("P1", "Will the Fed decrease interest rates by 50+ bps after the September 2026 meeting?",
                     market_type="decision", topic="fed", rules=POLY_FED_RULES, end="2026-09-16 00:00")])
    # rate before September 3.75%: a cut of 50+ bp means an upper bound of 3.25% or less = NOT above 3.25%
    pairs = match.build_pairs(kalshi_markets, poly_markets, before={"2026-09": (375, "2026-07", utc("2026-07-29"))})
    pair = pairs.iloc[0]
    assert pair.pair_type == "level_vs_change" and pair.relation == "opposite" and pair.is_candidate
    assert pair.rate_before_meeting == "3.75% (after the 2026-07 meeting)"
    # without the rate before the meeting there is nothing to compare
    pairs = match.build_pairs(kalshi_markets, poly_markets, before={})
    assert not pairs.iloc[0].is_candidate
    assert "rate before this meeting not known" in pairs.iloc[0].why_not_candidate


def test_duplicate_events_are_found():
    poly = match.poly_table(pd.DataFrame([
        poly_market("A1", "Will there be no change in Fed interest rates after the September 2026 meeting?",
                    market_type="decision", topic="fed", event_id="432066", rules=POLY_FED_RULES),
        poly_market("B1", "Will there be no change in Fed interest rates after the September 2026 meeting?",
                    market_type="decision", topic="fed", event_id="481717", rules=POLY_FED_RULES),
        poly_market("B2", "Will the Fed increase interest rates by 25 bps after the September 2026 meeting?",
                    market_type="decision", topic="fed", event_id="481717", rules=POLY_FED_RULES)]))
    assert poly.set_index("poly_market_id").poly_duplicate_events.to_dict() == {"A1": "481717", "B1": "432066",
                                                                                "B2": ""}


# ---------------------------------------------------------------------------
# Flags and suggested verdicts
# ---------------------------------------------------------------------------

def candidates_for(kalshi_rows, poly_rows, before=None):
    kalshi_markets, poly_markets = tables(kalshi_rows, poly_rows)
    pairs = match.build_pairs(kalshi_markets, poly_markets, before or {})
    return match.make_candidates(pairs, kalshi_markets, poly_markets)


def test_a_clean_cpi_pair_is_same_with_its_edge_cases_listed():
    candidates = candidates_for([kalshi_cpi(0.8)],
                                [poly_market("P1", "Will monthly inflation increase by 0.9% or more in October?")])
    pair = candidates.iloc[0]
    assert pair.suggested_verdict == "same"
    assert pair.poly_yes_equals == "Kalshi YES"
    assert "postponed_release" in pair.differences and "revisions" in pair.differences
    assert pair.verdict == "" and pair.note == ""  # yours to fill in


def test_paid_differently_is_different():
    # Same outcome on paper ("≥ 0.9%"), but Kalshi's YES paid and Polymarket's did not
    # (this happened for October 2025 CPI YoY: Kalshi "above 3.1%" Yes, Polymarket "≥3.2%" No)
    candidates = candidates_for([kalshi_cpi(0.8, result="yes")],
                                [poly_market("P1", "Will monthly inflation increase by 0.9% or more in October?",
                                             result="no", closed="2026-11-10 15:00")])
    pair = candidates.iloc[0]
    assert pair.results_agree is not None and not pair.results_agree
    assert pair.suggested_verdict == "different"
    assert "paid_differently" in pair.differences


def test_opposite_sides_that_pay_consistently_agree():
    candidates = candidates_for([kalshi_cpi(0.1, result="yes")],
                                [poly_market("P1", "Will monthly inflation increase by 0.1% or less in October?",
                                             result="no", closed="2026-11-10 15:00")])
    assert candidates.iloc[0].results_agree == True  # noqa: E712
    assert candidates.iloc[0].suggested_verdict == "same"


def test_venues_settling_a_month_on_different_values_is_unsure():
    # Kalshi settles October at 0.2%, Polymarket at 0.3%; this pair ("≥ 0.5%") still paid No on both
    kalshi_rows = [kalshi_cpi(x, result=("yes" if x < 0.2 else "no")) for x in [-0.1, 0.0, 0.1, 0.2, 0.3, 0.4]]
    poly_rows = [poly_market("P1", "Will monthly inflation increase by 0.3% in October?", result="yes"),
                 poly_market("P2", "Will monthly inflation increase by 0.5% or more in October?", result="no")]
    candidates = candidates_for(kalshi_rows, poly_rows).set_index("poly_market_id")
    pair = candidates.loc["P2"]
    assert pair.results_agree == True  # noqa: E712
    assert pair.kalshi_settled_value == "= 0.2%" and pair.poly_settled_value == "= 0.3%"
    assert pair.suggested_verdict == "unsure"
    assert "settled_on_different_values" in pair.differences


def test_polymarket_market_closed_months_early_is_unsure():
    kalshi_rows = [kalshi_market("KXFEDDECISION-26SEP-C25", "Will the Federal Reserve Cut rates by 25bps at their "
                                 "September 2026 meeting?", close="2026-09-16 17:59", rules_2=KALSHI_FED_RULES_2)]
    poly_rows = [poly_market("P1", "Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?",
                             market_type="decision", topic="fed", rules=POLY_FED_RULES, end="2026-09-16 00:00",
                             closed="2026-05-20 14:37")]
    pair = candidates_for(kalshi_rows, poly_rows).iloc[0]
    assert pair.suggested_verdict == "unsure"
    assert "poly_closed_early" in pair.differences and "119 days before the meeting" in pair.differences


def test_cancelled_meeting_old_kalshi_rule_on_the_no_change_bucket():
    old_rule = ("This market is mutually exclusive. Clarification 10:00 AM ET 1/24/25: If a scheduled FOMC meeting is "
                "canceled and does not occur on its scheduled date, then all strikes in this market resolve to No.")
    kalshi_rows = [kalshi_market("KXFEDDECISION-25JAN-H0", "Will the Federal Reserve Hike rates by 0bps at their "
                                 "January 2025 meeting?", close="2025-01-29 18:55", rules_2=old_rule)]
    poly_rows = [poly_market("P1", "No change in Fed interest rates after January 2025 meeting?", market_type="decision",
                             topic="fed", rules=POLY_FED_RULES.replace("September 2026", "January 2025")
                             .replace("September 15-16, 2026", "January 28-29, 2025"), end="2025-01-29 12:00")]
    pair = candidates_for(kalshi_rows, poly_rows).iloc[0]
    assert "[4] cancelled_meeting" in pair.differences
    assert "Kalshi pays No on every outcome but Polymarket pays Yes" in pair.suggested_reason
    assert pair.suggested_verdict == "same"  # a rule difference in an edge case: listed, verdict stays "same"


ROUND_UP = (" If the target federal funds rate is changed to a level not expressed in the displayed options, the "
            "change will be rounded up to the nearest 25 and will resolve to the relevant bracket.")
RANGES = " ... is decreased by between 1 (inclusive) and 25 (inclusive) or more basis points below the level ..."


@pytest.mark.parametrize("with_other_market, extra_rules, expected", [
    (False, ROUND_UP, "rounds it up to the next 25 bp"),     # rules from the May 2025 meeting on
    (False, RANGES, "rounds it up to the next 25 bp"),       # Jan and Mar 2025: ranges such as 1-25 bp
    (True, "", "separate 'Other' market"),                   # Nov and Dec 2024 events
    (False, "", "neither Polymarket's (older) rules"),       # (no such event in the data)
])
def test_rounding_flag_follows_the_polymarket_rules_version(with_other_market, extra_rules, expected):
    rules = ("This market will resolve to “Yes” if following the Federal Reserve's November 2024 meeting the upper "
             "bound of the target federal funds rate is decreased by exactly 25 basis points below the level it was "
             "prior to the meeting. The resolution source for this market is the FOMC’s statement after its meeting "
             "scheduled for November 6 - 7, 2024. If no statement is released by December 18, 2024, 11:59 PM ET, "
             "this market will resolve to the \"No change\" bracket.") + extra_rules
    kalshi_rows = [kalshi_market("FEDDECISION-24NOV-C25", "Will the Federal Reserve Cut rates by 25bps at their "
                                 "November 2024 meeting?", close="2024-11-07 18:55", rules_2=KALSHI_FED_RULES_2,
                                 series="KXFEDDECISION")]  # old ticker, kept in the KX series
    poly_rows = [poly_market("P1", "Fed decreases interest rates by 25 bps after November 2024 meeting?",
                             market_type="decision", topic="fed", event_id="11827", rules=rules,
                             end="2024-11-07 12:00")]
    if with_other_market:
        poly_rows.append(poly_market("P2", "Will the FED change rates to another level after Nov meeting?",
                                     market_type="decision", topic="fed", event_id="11827", rules=rules,
                                     end="2024-11-07 12:00"))
    pair = candidates_for(kalshi_rows, poly_rows).set_index("poly_market_id").loc["P1"]
    assert "] rounding: " in pair.differences and expected in pair.differences


def test_level_vs_change_is_always_unsure():
    kalshi_rows = [kalshi_market("KXFED-26SEP-T3.25", "Will the upper bound of the federal funds rate be above 3.25% "
                                 "following the Fed's Sep 16, 2026 meeting?", close="2026-09-16 17:55", series="KXFED")]
    poly_rows = [poly_market("P1", "Will the Fed decrease interest rates by 50+ bps after the September 2026 meeting?",
                             market_type="decision", topic="fed", rules=POLY_FED_RULES, end="2026-09-16 00:00")]
    pair = candidates_for(kalshi_rows, poly_rows, before={"2026-09": (375, "2026-07", utc("2026-07-29"))}).iloc[0]
    assert pair.pair_type == "level_vs_change" and pair.poly_yes_equals == "Kalshi NO"
    assert pair.suggested_verdict == "unsure" and "rate LEVEL" in pair.suggested_reason


def test_wrong_polymarket_dates_are_flagged():
    # December 2025 CPI: end date a month early, and a typo in the year of the release date in the rules
    rules = POLY_CPI_RULES.replace("October 2026", "December 2025").replace("November 10, 2026", "January 13, 2025")
    kalshi_rows = [kalshi_market("KXCPI-25DEC-T0.4", "Will CPI rise more than 0.4% in December 2025?",
                                 close="2026-01-13 13:29", rules_2=KALSHI_CPI_RULES_2)]
    poly_rows = [poly_market("P1", "Will monthly inflation increase by 0.5% or more in December?", rules=rules,
                             end="2025-12-13 00:00", closed="2026-01-13 16:43")]
    pair = candidates_for(kalshi_rows, poly_rows).iloc[0]
    assert "poly_end_date_off" in pair.differences
    assert "poly_rules_date_typo" in pair.differences


# ---------------------------------------------------------------------------
# Saving: your verdicts are kept
# ---------------------------------------------------------------------------

def test_typed_verdicts_survive_a_rerun(tmp_path):
    candidates = candidates_for([kalshi_cpi(0.1), kalshi_cpi(0.8)],
                                [poly_market("P1", "Will monthly inflation increase by 0.1% or less in October?"),
                                 poly_market("P3", "Will monthly inflation increase by 0.9% or more in October?")])
    path = tmp_path / "match_candidates.csv"
    match.save_review_file(candidates, path)
    typed = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    typed.loc[typed.poly_market_id == "P3", ["verdict", "note"]] = ["same", "checked 4 Nov"]
    typed.loc[len(typed)] = {**typed.iloc[0].to_dict(), "poly_market_id": "OLD", "verdict": "different"}
    typed.to_csv(path, index=False, encoding="utf-8-sig")

    review, carried, kept = match.keep_earlier_verdicts(candidates, path)
    rows = review.set_index("poly_market_id")
    assert rows.loc["P3", "verdict"] == "same" and rows.loc["P3", "note"] == "checked 4 Nov"
    assert rows.loc["P1", "verdict"] == ""
    assert carried == 1 and kept == 1  # the "OLD" pair is gone from the matches, but your verdict is kept
    assert rows.loc["OLD", "suggested_reason"].startswith("NO LONGER FOUND")


def test_review_file_saved_by_excel_with_semicolons_can_be_read(tmp_path):
    path = tmp_path / "match_candidates.csv"
    path.write_bytes("kalshi_ticker;poly_market_id;verdict;note\nKXCPI-26OCT-T0.8;P3;same;café\n".encode("cp1252"))
    table = match.read_review_file(path)
    assert table.loc[0, "note"] == "café" and table.loc[0, "verdict"] == "same"


def test_review_file_keeps_the_column_order(tmp_path):
    candidates = candidates_for([kalshi_cpi(0.8)],
                                [poly_market("P3", "Will monthly inflation increase by 0.9% or more in October?")])
    path = match.save_review_file(candidates, tmp_path / "match_candidates.csv")
    header = path.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
    assert header == match.CSV_COLUMNS
    assert header.index("kalshi_rules") + 1 == header.index("poly_rules")  # the two rules texts side by side
