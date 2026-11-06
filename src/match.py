"""
match.py - Find pairs of Kalshi and Polymarket markets that refer to the same outcome.

How to run it (from the project folder):
    python src/match.py

It reads two tables that the download scripts made:
    data/processed/kalshi_markets.parquet   (src/download_kalshi.py)
    data/processed/poly_markets.parquet     (src/download_polymarket.py)
and asks Kalshi's public API for the settlement source of each Kalshi series
(4 small GET requests, cached in data/raw/kalshi/).

What it does, step by step:
  1. Reads the KEY FIELDS of every market with simple text rules: what is
     measured (e.g. "CPI, change from the previous month"), for which meeting
     or data month, the scheduled meeting or release date, the threshold, the
     direction, and the YES outcome written in one common form.
  2. Pairs every Kalshi market with every Polymarket market of the same topic
     (Fed or CPI) whose dates are within 7 days of each other (Kalshi close
     time vs Polymarket end date OR actual close time), or whose key fields say
     they measure the same thing for the same meeting or month. The key fields
     are the main key; the dates are a sanity check.
  3. Scores how similar the two titles are (rapidfuzz token_set_ratio).
  4. Keeps as CANDIDATES the pairs whose YES outcomes are the same outcome:
     on the same side (Polymarket YES = Kalshi YES) or on opposite sides
     (Polymarket YES = Kalshi NO). Individual binary markets are matched, not
     whole events.
  5. Compares the two rules texts and flags differences (resolution source,
     timing, rounding, cancelled meetings, postponed releases, revisions ...)
     and data problems. For settled pairs it checks whether both venues paid
     the same.
  6. Suggests a verdict (same / unsure / different) with a one-line reason.
  7. Writes data/processed/match_candidates.csv for your review (verdicts and
     notes you typed earlier are kept) and data/processed/match_all_pairs.parquet
     (every pair that was compared), and prints a summary and the 10 trickiest
     candidates.

The meaning of every column, and the resolution risks found so far, are in
docs/matching.md.

Facts this code relies on (checked on 2026-11-04):
  * Both venues resolve CPI markets on the figure the Bureau of Labor
    Statistics publishes, which has ONE decimal (e.g. 0.3%). Kalshi's rules
    say "the Expiration Value is the single-decimal value published at the
    Source Agency"; Polymarket's say the BLS "reports to one decimal point
    ... this is the level of precision that will be used". So "more than
    0.2%" means "0.3% or more". (Rules texts: Kalshi rules_primary and
    rules_secondary, Polymarket description, as sent by the APIs.
    BLS release: https://www.bls.gov/bls/news-release/cpi.htm)
  * The Fed sets a target range and moves it in steps of 25 basis points
    (bp). Polymarket's Fed rules count any other move as the next 25 bp step
    up (its oldest events, Nov and Dec 2024, have a separate "Other" market).
    History of the range: https://www.federalreserve.gov/monetarypolicy/openmarket.htm
  * Kalshi market fields: https://docs.kalshi.com/api-reference/market/get-markets
    Kalshi series fields (settlement_sources, contract_terms_url):
    https://docs.kalshi.com/api-reference/market/get-series
    Polymarket market fields: https://docs.polymarket.com/market-data/market-details.md
"""

import math
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

import kalshi

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder above src/
PROCESSED_FOLDER = PROJECT_ROOT / "data" / "processed"
KALSHI_FILE = PROCESSED_FOLDER / "kalshi_markets.parquet"
POLY_FILE = PROCESSED_FOLDER / "poly_markets.parquet"
CANDIDATES_FILE = PROCESSED_FOLDER / "match_candidates.csv"
ALL_PAIRS_NAME = "match_all_pairs"  # saved as data/processed/match_all_pairs.parquet

MAX_DAYS_APART = 7          # dates further apart than this fail the sanity check
TITLE_ONLY_MIN_SCORE = 80   # keep a pair with unreadable fields if its titles score this high
MAX_FILE_MB = 50            # project rule: every committed file under 50 MB
EASTERN = "America/New_York"  # both venues state their times in US Eastern time

# What a market measures. Every quantity is counted in whole STEPS (integers),
# so that comparisons are exact (no rounding errors with decimals):
#   CPI changes in tenths of a percentage point (0.3% -> 3), because the BLS
#   publishes them with one decimal;
#   Fed rates and rate changes in basis points (3.75% -> 375, a 25 bp cut -> -25).
FED_CHANGE = "fed_change"       # change of the upper bound at one meeting, in bp
FED_LEVEL = "fed_level"         # upper bound right after one meeting, in bp
CPI_MOM = "cpi_mom"             # all-items CPI-U, % change from the previous month
CPI_YOY = "cpi_yoy"             # all-items CPI-U, % change from 12 months earlier
CORE_CPI_MOM = "core_cpi_mom"   # CPI without food and energy, from the previous month
CORE_CPI_YOY = "core_cpi_yoy"   # CPI without food and energy, from 12 months earlier

STEP = {FED_CHANGE: 25, FED_LEVEL: 25, CPI_MOM: 1, CPI_YOY: 1, CORE_CPI_MOM: 1, CORE_CPI_YOY: 1}
CPI_MEASURES = (CPI_MOM, CPI_YOY, CORE_CPI_MOM, CORE_CPI_YOY)
SHORT_NAMES = {FED_LEVEL: "upper bound", CPI_MOM: "CPI MoM", CPI_YOY: "CPI YoY",
               CORE_CPI_MOM: "core CPI MoM", CORE_CPI_YOY: "core CPI YoY"}

# Kalshi: each series we downloaded measures one thing.
KALSHI_SERIES = {
    "KXFEDDECISION": ("fed", FED_CHANGE),  # "Will the Federal Reserve Cut rates by 25bps ...?"
    "KXFED": ("fed", FED_LEVEL),           # "Will the upper bound ... be above 3.75% following ...?"
    "KXCPI": ("cpi", CPI_MOM),             # "Will CPI rise more than 0.2% in October 2026?"
    "KXCPIYOY": ("cpi", CPI_YOY),          # "Will the rate of CPI inflation be above 2.4% ...?"
}

# Polymarket: download_polymarket.py already sorted the events into market
# types. The kinds that end in "_other" or "_count" have no counterpart among
# the Kalshi series we downloaded (e.g. "Fed rate cut by July meeting?", which
# counts any cut over several months).
POLY_MEASURES = {
    ("fed", "decision"): FED_CHANGE,
    ("fed", "rate_level"): "fed_level_other",   # rate at the END OF A YEAR, or reached BEFORE a date
    ("fed", "cuts_count"): "fed_cuts_count",    # number of cuts in a year
    ("fed", "other"): "fed_other",              # cut/hike by a date, 3-meeting paths, emergency cut ...
    ("cpi", "cpi_mom"): CPI_MOM,
    ("cpi", "cpi_yoy"): CPI_YOY,
    ("cpi", "core_cpi_mom"): CORE_CPI_MOM,
    ("cpi", "core_cpi_yoy"): CORE_CPI_YOY,
    ("cpi", "other"): "cpi_other",              # highest inflation of a year
}

MONTH_NUMBERS = {name: number for number, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
# A month name, written out or short ("October", "Oct", "Sept"); used inside regular expressions.
MONTH = r"(January|February|March|April|May|June|July|August|September|October|November|December|" \
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)"

# Columns of the review file, in this order. The rules texts come last because they are long.
CSV_COLUMNS = [
    "kalshi_ticker", "poly_market_id", "poly_url", "pair_type", "poly_yes_equals",
    "suggested_verdict", "suggested_reason", "verdict", "note", "difference_score", "differences",
    "kalshi_title", "poly_question", "title_similarity",
    "kalshi_measure", "poly_measure", "kalshi_month", "poly_month",
    "kalshi_event_date", "poly_event_date",
    "kalshi_threshold", "kalshi_direction", "kalshi_outcome",
    "poly_threshold", "poly_direction", "poly_outcome", "rate_before_meeting",
    "kalshi_close_time", "poly_end_date", "poly_closed_time", "days_apart", "dates_within_7_days",
    "kalshi_result", "poly_result", "results_agree", "kalshi_settled_value", "poly_settled_value",
    "kalshi_source", "poly_source", "kalshi_contract_terms",
    "kalshi_rules", "poly_rules",
]
ALL_PAIRS_COLUMNS = [
    "kalshi_ticker", "poly_market_id", "topic", "kalshi_title", "poly_question", "title_similarity",
    "kalshi_measure", "poly_measure", "kalshi_month", "poly_month", "kalshi_outcome", "poly_outcome",
    "relation", "pair_type", "same_question", "dates_within_7_days", "days_apart",
    "kalshi_close_time", "poly_end_date", "poly_closed_time", "is_candidate", "why_not_candidate",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def text(value):
    """A text value, or "" if it is missing (None or NaN)."""
    return value if isinstance(value, str) else ""


def bound(value):
    """A limit of a YES outcome as a whole number, or None for "no limit" (None or NaN)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return int(value)


def month_number(word):
    """'October', 'Oct', 'OCT' or 'Sept' -> 10, 10, 10, 9."""
    word = word.lower()[:3]
    for name, number in MONTH_NUMBERS.items():
        if name.startswith(word):
            return number
    return None


def month_in_text(value):
    """The first "Month YYYY" (or "YYYY Month") in a text, as "YYYY-MM"; None if there is none.

    "...after the April 2027 meeting?" -> "2027-04"; "...after 2024 November meeting?" -> "2024-11".
    A date with a day ("December 16, 2025") does not count: that is a date, not a month.
    """
    match = re.search(rf"\b{MONTH}\.? (\d{{4}})\b", text(value), re.IGNORECASE)
    if match:
        return f"{match.group(2)}-{month_number(match.group(1)):02d}"
    match = re.search(rf"\b(\d{{4}}) {MONTH}\b", text(value), re.IGNORECASE)
    if match:
        return f"{match.group(1)}-{month_number(match.group(2)):02d}"
    return None


def eastern_date(timestamp):
    """The calendar day of a UTC time in US Eastern time (both venues' official clock)."""
    if timestamp is None or pd.isna(timestamp):
        return None
    return timestamp.tz_convert(EASTERN).date()


def to_steps(number, measure):
    """A number as written in a title in whole steps: 0.3 (%) -> 3, 3.75 (%) -> 375, 25 (bp) -> 25."""
    if measure == FED_CHANGE:
        return int(round(number))          # already in basis points
    if measure == FED_LEVEL:
        return int(round(number * 100))    # percent -> basis points
    return int(round(number * 10))         # CPI: percent -> tenths of a percent


def format_value(value, measure):
    """A value in steps as readable text: 3 -> "0.3%", 375 -> "3.75%", -25 -> "-25 bp"."""
    if measure == FED_CHANGE:
        return f"{value:+d} bp" if value else "0 bp"
    if measure == FED_LEVEL:
        return f"{value / 100:.2f}%"
    return f"{value / 10:.1f}%"


def describe_outcome(measure, low, high):
    """The YES outcome in words, e.g. "CPI MoM ≥ 0.3%", "cut of 50 bp or more", "no change".

    low and high are the smallest and largest value for which YES wins, in
    steps (None = no limit).
    """
    low, high = bound(low), bound(high)
    if measure not in STEP or (low is None and high is None):
        return None
    if measure == FED_CHANGE:
        if low is not None and low == high:
            if low == 0:
                return "no change"
            return f"cut of {-low} bp" if low < 0 else f"hike of {low} bp"
        if low is None and high < 0:
            return f"cut of {-high} bp or more"
        if high is None and low > 0:
            return f"hike of {low} bp or more"
    name = SHORT_NAMES.get(measure, "change")
    if low is not None and low == high:
        return f"{name} = {format_value(low, measure)}"
    if low is None:
        return f"{name} ≤ {format_value(high, measure)}"
    if high is None:
        return f"{name} ≥ {format_value(low, measure)}"
    return f"{name} {format_value(low, measure)} to {format_value(high, measure)}"


def contains(low, high, value):
    """True if `value` lies inside the YES outcome [low, high] (None = no limit)."""
    return (low is None or value >= low) and (high is None or value <= high)


def domain(url):
    """The web domain of an address without "www.": "https://www.bls.gov/cpi/" -> "bls.gov"."""
    match = re.match(r"https?://(?:www\.)?([^/\s]+)", text(url))
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# Step 1a: key fields of a Kalshi market
# ---------------------------------------------------------------------------

def kalshi_month(event_ticker):
    """The meeting or data month in a Kalshi event ticker: "KXCPI-26OCT" -> "2026-10".

    Old tickers without "KX" (e.g. "FED-25JAN", "CPIYOY-24OCT") work the same way.
    """
    match = re.search(r"-(\d{2})([A-Z]{3})$", text(event_ticker))
    if not match or month_number(match.group(2)) is None:
        return None
    return f"20{match.group(1)}-{month_number(match.group(2)):02d}"


def kalshi_outcome(measure, title):
    """What a Kalshi market's YES needs, read from its title.

    Returns (threshold, direction, low, high): the number as written in the
    title, how it is used, and the smallest and largest value (in steps) for
    which YES wins (None = no limit). (None, None, None, None) if the title is
    not understood. Examples:
      "Will CPI rise more than 0.2% in October 2026?"            -> 0.2, "more than", 3, None
          (one-decimal figures: more than 0.2% means 0.3% or more)
      "...upper bound of the federal funds rate be above 3.75%..." -> 3.75, "more than", 400, None
      "...Cut rates by >25bps..."  -> 25, "cut, more than", None, -50
      "...Cut rates by 25bps..."   -> 25, "cut, exactly", -25, -25
      "...Hike rates by 0bps..."   -> 0, "no change", 0, 0   (Kalshi's "No change" bucket)
    """
    title = text(title)
    if measure in CPI_MEASURES or measure == FED_LEVEL:
        # CPI: "rise more than 0.2%", "be above 2.4%"; Fed level: "be above 3.75%"
        match = re.search(r"(?:more than|above) (-?\d+(?:\.\d+)?)%", title)
        if match:
            number = float(match.group(1))
            return number, "more than", to_steps(number, measure) + STEP[measure], None
    elif measure == FED_CHANGE:
        match = re.search(r"\b(Cut|Hike) rates by (>)?(\d+)\s*bps", title, re.IGNORECASE)
        if match:
            size = int(match.group(3))
            sign = -1 if match.group(1).lower() == "cut" else 1
            word = "cut" if sign < 0 else "hike"
            if size == 0:
                return 0.0, "no change", 0, 0
            if match.group(2):  # ">25bps": more than 25, i.e. 50 or more on the 25 bp grid
                limit = sign * (size + STEP[FED_CHANGE])
                low, high = (None, limit) if sign < 0 else (limit, None)
                return float(size), f"{word}, more than", low, high
            return float(size), f"{word}, exactly", sign * size, sign * size
    return None, None, None, None


def kalshi_rules_date(rules):
    """The first full date ("December 09, 2026", "Dec 9, 2026") in a Kalshi rules text.

    For CPI it is the scheduled release day ("...on the scheduled day of the
    data release (November 10, 2026)"), for the Fed the meeting day.
    """
    match = re.search(rf"\b{MONTH}\.? (\d{{1,2}}), (\d{{4}})", text(rules), re.IGNORECASE)
    if not match:
        return None
    return date(int(match.group(3)), month_number(match.group(1)), int(match.group(2)))


def kalshi_rule_features(rules):
    """What a Kalshi rules text says about the edge cases we compare.

    cancel_rule         Fed decision: what happens if the meeting is cancelled.
                        "all_no"   "all strikes in this market resolve to No" (older markets)
                        "hold_yes" "Fed maintains rate" (or "No change") resolves to Yes
    waits_after_shutdown  CPI: a release delayed by a government shutdown extends
                        the market "to the sooner of the release ... or six months
                        after the end of the government shutdown"
    one_decimal         the figure is read with one decimal
    lost_symbol         the text lost a ">" sign ("a Cut of   25bps" in a ">25bps" market)
    expires_week_after  Fed level: the market expires "one week following the last
                        day of that meeting" if no statement comes out
    """
    rules = text(rules)
    if re.search(r"all strikes in this market resolve to No", rules, re.IGNORECASE):
        cancel_rule = "all_no"
    elif re.search(r"(Fed maintains rate|No change)\W{0,3}(?:will )?resolves? to Yes", rules, re.IGNORECASE):
        cancel_rule = "hold_yes"
    else:
        cancel_rule = None
    return {
        "cancel_rule": cancel_rule,
        "waits_after_shutdown": "six months after the end of the" in rules,
        "one_decimal": bool(re.search(r"single-decimal|one-decimal|one decimal", rules, re.IGNORECASE)),
        "lost_symbol": bool(re.search(r"\bof\s{2,}\d+\s*bps", rules)),
        "expires_week_after": "one week following the last day of that meeting" in rules,
    }


def kalshi_sources(series_tickers):
    """{series: (settlement source text, contract terms link)} from Kalshi's series records.

    Kalshi keeps the official source on the series, e.g. KXCPI:
    "Bureau of Labor Statistics (https://www.bls.gov/cpi/)", and links the full
    contract terms (a PDF with more details than the market text).
    https://docs.kalshi.com/api-reference/market/get-series
    """
    sources = {}
    for ticker in series_tickers:
        try:
            series = kalshi.get_series(ticker)
        except (kalshi.KalshiError, kalshi.NetworkBlockedError) as error:
            print(f"   WARNING: could not read the settlement source of {ticker}: {error}")
            continue
        names = [f"{one.get('name')} ({one.get('url')})" for one in series.get("settlement_sources") or []]
        sources[ticker] = ("; ".join(names) or None, series.get("contract_terms_url"))
    return sources


def kalshi_table(markets, sources):
    """One row per Kalshi market with its key fields (columns start with "kalshi_")."""
    rows = []
    for market in markets.to_dict("records"):
        topic, measure = KALSHI_SERIES.get(market["series_ticker"], (None, None))
        threshold, direction, low, high = kalshi_outcome(measure, market["title"])
        rules = "\n\n".join(part for part in [text(market["rules_primary"]), text(market["rules_secondary"])]
                            if part)
        source, contract_terms = sources.get(market["series_ticker"], (None, None))
        row = {
            "kalshi_ticker": market["ticker"],
            "kalshi_event": market["event_ticker"],
            "topic": topic,
            "kalshi_title": market["title"],
            "kalshi_measure": measure,
            "kalshi_month": kalshi_month(market["event_ticker"]),
            # Kalshi closes these markets minutes before the announcement, on the
            # scheduled day (rules: "will always close at 8:29 AM ET on the
            # scheduled day of the data release"), so the close day is the event day.
            "kalshi_event_date": eastern_date(market["close_time"]),
            "kalshi_threshold": threshold,
            "kalshi_direction": direction,
            "kalshi_low": low,
            "kalshi_high": high,
            "kalshi_outcome": describe_outcome(measure, low, high),
            "kalshi_close_time": market["close_time"],
            "kalshi_settled_time": market["settlement_time"],
            "kalshi_result": market["result"] if market["result"] in ("yes", "no") else None,
            "kalshi_source": source,
            "kalshi_contract_terms": contract_terms,
            "kalshi_rules": rules,
            "kalshi_rules_date": kalshi_rules_date(rules),
            "kalshi_fields_ok": measure is not None and kalshi_month(market["event_ticker"]) is not None
                                and (low is not None or high is not None),
        }
        row.update({f"kalshi_{key}": value for key, value in kalshi_rule_features(rules).items()})
        rows.append(row)
    table = pd.DataFrame(rows)
    for column in ["kalshi_close_time", "kalshi_settled_time"]:
        table[column] = pd.to_datetime(table[column], utc=True)  # UTC, also if a column is empty
    return table


# ---------------------------------------------------------------------------
# Step 1b: key fields of a Polymarket market
# ---------------------------------------------------------------------------

def poly_month(question, rules, end_date):
    """The meeting or data month of a Polymarket market, as "YYYY-MM".

    1. "Month YYYY" in the question ("...after the April 2027 meeting?");
    2. else the first "Month YYYY" in the rules ("...(CPI-U) in October 2026
       according to...", "...prior to the Federal Reserve's April 2027 meeting");
    3. else the month named in the question with the year of the end date (a
       month after the end date's month belongs to the year before).
    """
    found = month_in_text(question) or month_in_text(rules)
    if found:
        return found
    match = re.search(rf"\b{MONTH}\b", text(question), re.IGNORECASE)
    if match and end_date is not None and not pd.isna(end_date):
        month = month_number(match.group(1))
        year = end_date.year - (1 if month > end_date.month else 0)
        return f"{year}-{month:02d}"
    return None


def poly_scheduled_date(rules):
    """The scheduled release day (CPI) or last meeting day (Fed) stated in Polymarket's rules.

    CPI: "...currently scheduled to be released on November 10, 2026, at 8:30 AM ET."
    Fed: "...its meeting scheduled for September 15-16, 2026 ..." (-> September 16)
         "...its meeting scheduled to conclude on April 28, 2027 ..."
    """
    rules = text(rules)
    match = re.search(rf"scheduled to be released on {MONTH} (\d{{1,2}}),? (\d{{4}})", rules, re.IGNORECASE)
    if match:
        return date(int(match.group(3)), month_number(match.group(1)), int(match.group(2)))
    match = re.search(rf"scheduled (?:for|to conclude on) {MONTH} (\d{{1,2}})"
                      rf"(?:\s*(?:-|–|to)\s*(?:{MONTH} )?(\d{{1,2}}))?,? (\d{{4}})", rules, re.IGNORECASE)
    if match:
        month = match.group(3) or match.group(1)  # "April 30 - May 1": the second month
        day = match.group(4) or match.group(2)    # the last day of the meeting
        return date(int(match.group(5)), month_number(month), int(day))
    return None


def poly_outcome(measure, question):
    """What a Polymarket market's YES needs, read from its question.

    Returns (threshold, direction, low, high) like kalshi_outcome(). Examples:
      "Will monthly inflation increase by 0.3% in October?"          -> 0.3, "exactly", 3, 3
      "Will monthly inflation increase by 0.9% or more in October?"  -> 0.9, "at least", 9, None
      "Will annual inflation increase by ≤2.8% in October?"          -> 2.8, "at most", None, 28
      "Will monthly inflation decrease by 0.7% or more in July?"     -> -0.7, "at most", None, -7
      "Will monthly inflation stay flat (0.0%) or decrease in ...?"  -> 0.0, "at most", None, 0
      "Annual inflation above 2.4% in October?"                      -> 2.4, "more than", 25, None
      "Will the Fed decrease interest rates by 50+ bps after ...?"   -> 50, "cut, at least", None, -50
      "Will there be no change in Fed interest rates after ...?"     -> 0, "no change", 0, 0
    "Will the FED change rates to another level ...?" (Polymarket's old "Other"
    bucket, for moves between the listed sizes) has no single outcome:
    (None, "between the listed sizes", None, None).
    """
    question = text(question)
    lower = question.lower()
    if measure == FED_CHANGE:
        if "no change" in lower:
            return 0.0, "no change", 0, 0
        if "another level" in lower:
            return None, "between the listed sizes", None, None
        match = re.search(r"\b(decrease|increase)s? interest rates by (\d+)(\+)? bps", lower)
        if match:
            size = int(match.group(2))
            sign = -1 if match.group(1) == "decrease" else 1
            word = "cut" if sign < 0 else "hike"
            if match.group(3):  # "50+ bps": 50 or more
                low, high = (None, -size) if sign < 0 else (size, None)
                return float(size), f"{word}, at least", low, high
            return float(size), f"{word}, exactly", sign * size, sign * size
    elif measure in CPI_MEASURES:
        match = re.search(r"(-?\d+(?:\.\d+)?)%", question)
        if match:
            number = float(match.group(1))
            decrease = "decrease by" in lower
            if decrease:
                number = -number  # "decrease by 0.6%" is a change of -0.6%
            value = to_steps(number, measure)
            if "≥" in question or "or more" in lower or "or greater" in lower or "%+" in question:
                # "decrease by 0.7% or more" means -0.7% or LOWER
                if decrease:
                    return number, "at most", None, value
                return number, "at least", value, None
            if "≤" in question or "or less" in lower or "or decrease" in lower:
                return number, "at most", None, value
            if "above" in lower or "more than" in lower:
                return number, "more than", value + STEP[measure], None
            return number, "exactly", value, value
    return None, None, None, None


def poly_rule_features(rules):
    """What a Polymarket rules text says about the edge cases we compare.

    no_statement_rule      Fed: "If no statement is released by <deadline>, this market
                           will resolve to the "No change" bracket" -> the deadline text
    rounds_up_25           Fed: a move that is not a multiple of 25 bp counts as the next
                           25 bp step: "will be rounded up to the nearest 25" (from the May
                           2025 meeting on) or ranges such as "decreased by between 26
                           (inclusive) and 50 (inclusive)" (January and March 2025)
    previous_month_fallback  CPI: if the BLS has not released the figure by the next
                           scheduled CPI report, the market "will resolve according to
                           the figures of the most recent previous month with available data"
    seasonal               CPI: "seasonally adjusted" or "not seasonally adjusted"
                           ("before seasonal adjustment"); None if not stated
    one_decimal            the figure is read with one decimal
    """
    rules = text(rules)
    no_statement = re.search(r"If no statement is released by (.+?), this market will resolve to "
                             r"the \W?No change\W? bracket", rules, re.IGNORECASE)
    if "before seasonal adjustment" in rules:
        seasonal = "not seasonally adjusted"
    elif "seasonally adjusted" in rules:
        seasonal = "seasonally adjusted"
    else:
        seasonal = None
    return {
        "no_statement_rule": no_statement.group(1) if no_statement else None,
        "rounds_up_25": bool(re.search(r"rounded up to the nearest 25|\(inclusive\) and \d+ \(inclusive\)", rules)),
        "previous_month_fallback": "most recent previous month with available data" in rules,
        "seasonal": seasonal,
        "one_decimal": bool(re.search(r"one decimal", rules, re.IGNORECASE)),
    }


def poly_source(resolution_source, rules):
    """Polymarket's resolution source: the market's own field, else the first web address in its rules."""
    if text(resolution_source):
        return resolution_source
    match = re.search(r"https?://[^\s)]+", text(rules))
    return match.group(0).rstrip(".,") if match else None


def poly_table(markets):
    """One row per Polymarket market with its key fields (columns start with "poly_")."""
    rows = []
    for market in markets.to_dict("records"):
        topic = market["topic"]
        measure = POLY_MEASURES.get((topic, market["market_type"]))
        threshold, direction, low, high = poly_outcome(measure, market["question"])
        rules = text(market["rules"])
        month = poly_month(market["question"], rules, market["end_date"])
        # Markets that are Polymarket-only kinds, and the "Other" bucket, are
        # read correctly even though they have no single outcome to compare.
        no_counterpart = measure not in STEP or direction == "between the listed sizes"
        row = {
            "poly_market_id": str(market["market_id"]),
            "poly_event_id": str(market["event_id"]),
            "poly_url": f"https://polymarket.com/event/{market['event_slug']}" if text(market["event_slug"]) else None,
            "topic": topic,
            "poly_question": market["question"],
            "poly_measure": measure,
            "poly_month": month,
            "poly_event_date": poly_scheduled_date(rules),
            "poly_threshold": threshold,
            "poly_direction": direction,
            "poly_low": low,
            "poly_high": high,
            "poly_outcome": describe_outcome(measure, low, high) or direction,
            "poly_end_date": market["end_date"],
            "poly_closed_time": market["closed_time"],
            "poly_result": market["resolved_outcome"] if market["resolved_outcome"] in ("yes", "no", "split")
                           else None,
            "poly_source": poly_source(market["resolution_source"], rules),
            "poly_source_field": text(market["resolution_source"]) or None,
            "poly_rules": rules,
            "poly_volume": market["volume"],
            # A Polymarket-only kind (e.g. "Fed rate hike in 2026?") counts as read:
            # we know what it measures, there is just nothing on Kalshi to compare.
            "poly_fields_ok": measure is not None and (
                measure not in STEP or (month is not None and (no_counterpart or low is not None
                                                               or high is not None))),
        }
        row.update({f"poly_{key}": value for key, value in poly_rule_features(rules).items()})
        rows.append(row)
    table = pd.DataFrame(rows)
    for column in ["poly_end_date", "poly_closed_time"]:
        table[column] = pd.to_datetime(table[column], utc=True)  # UTC, also if a column is empty
    table["poly_duplicate_events"] = duplicate_events(table)
    # How an event counts a Fed move that is not a multiple of 25 bp (e.g. 37.5 bp) is
    # stated in some of its markets only, so it is read per event:
    #   rounds up    the rules round up, or give ranges ("between 26 and 50 bp")
    #   has "Other"  the oldest events (Nov, Dec 2024) have an extra "Other" market
    rounds_up = table.poly_rounds_up_25
    other_bucket = table.poly_direction == "between the listed sizes"
    table["poly_event_rounds_up"] = table.poly_event_id.isin(table.loc[rounds_up, "poly_event_id"])
    table["poly_event_has_other"] = table.poly_event_id.isin(table.loc[other_bucket, "poly_event_id"])
    return table


def duplicate_events(poly):
    """For each Polymarket market: the OTHER events that list exactly the same outcome.

    Example: September 2026 has two "Fed Decision in September?" events with
    the same five questions. Returns a list with one text per market ("" if none).
    """
    keys = [f"{measure}|{month}|{bound(low)}|{bound(high)}" if bound(low) is not None or bound(high) is not None
            else None
            for measure, month, low, high in zip(poly.poly_measure, poly.poly_month, poly.poly_low, poly.poly_high)]
    events_per_key = {}
    for key, event in zip(keys, poly.poly_event_id):
        if key is not None:
            events_per_key.setdefault(key, set()).add(event)
    return [", ".join(sorted(events_per_key[key] - {event})) if key is not None else ""
            for key, event in zip(keys, poly.poly_event_id)]


# ---------------------------------------------------------------------------
# Step 2-4: pairs, title similarity and how the two YES outcomes relate
# ---------------------------------------------------------------------------

def relation(a_low, a_high, b_low, b_high, step):
    """How two YES outcomes relate.

    "same"        both YES win for exactly the same values
    "opposite"    one YES wins exactly when the other loses, e.g. "≤ 0.1%" and
                  "≥ 0.2%": the two outcomes split all values at the same point
    "overlap"     they share some values but not all (e.g. "= 0.3%" and "≥ 0.3%")
    "no overlap"  they never win together (e.g. "= 0.3%" and "≥ 0.4%")
    "unknown"     one of them is not known
    """
    a_low, a_high, b_low, b_high = bound(a_low), bound(a_high), bound(b_low), bound(b_high)
    if (a_low is None and a_high is None) or (b_low is None and b_high is None):
        return "unknown"
    if (a_low, a_high) == (b_low, b_high):
        return "same"
    if a_low is None and b_high is None and a_high is not None and b_low is not None and a_high + step == b_low:
        return "opposite"
    if b_low is None and a_high is None and b_high is not None and a_low is not None and b_high + step == a_low:
        return "opposite"
    lowest = max([x for x in (a_low, b_low) if x is not None], default=None)
    highest = min([x for x in (a_high, b_high) if x is not None], default=None)
    if lowest is not None and highest is not None and lowest > highest:
        return "no overlap"
    return "overlap"


def clean_title(title):
    """Lower case, punctuation -> spaces, but keep . % + - > ≥ ≤ which belong to numbers ("0.3%", "50+")."""
    return re.sub(r"[^a-z0-9.%+\->≥≤]+", " ", text(title).lower()).strip()


def title_similarity(kalshi_title, poly_question):
    """How similar two titles are, 0-100 (rapidfuzz token_set_ratio on cleaned titles).

    token_set_ratio compares the sets of words, so word order and extra words
    in one title matter little. https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html
    """
    return fuzz.token_set_ratio(clean_title(kalshi_title), clean_title(poly_question))


def rate_before_each_meeting(kalshi_markets):
    """{meeting month: (upper bound before that meeting in bp, previous meeting month, its close time)}.

    Read from Kalshi's settled rate-LEVEL markets (KXFED): "above 3.75%"
    settled Yes and "above 4.00%" settled No means the upper bound was 4.00%
    after that meeting. The rate before a meeting is the rate after the
    previous meeting (assuming no move between meetings). It is known only
    once the previous meeting has settled, so for meetings further ahead
    there is no entry.
    """
    levels = kalshi_markets[kalshi_markets.kalshi_measure == FED_LEVEL]
    after = {}  # meeting month -> (level, close time)
    for month, group in levels.groupby("kalshi_month"):
        settled = [(bound(low), bound(high), result) for low, high, result
                   in zip(group.kalshi_low, group.kalshi_high, group.kalshi_result) if result in ("yes", "no")]
        if not settled or len(settled) < len(group):
            continue  # not (fully) settled yet
        values, grid = settled_values(settled, FED_LEVEL)
        if len(values) == 1 and grid[0] < values[0] < grid[-1]:  # one exact value, not "above all strikes"
            after[month] = (values[0], group.kalshi_close_time.max())
    meetings = sorted(levels.kalshi_month.dropna().unique())
    before = {}
    for previous, current in zip(meetings, meetings[1:]):
        if previous in after:
            before[current] = (after[previous][0], previous, after[previous][1])
    return before


def settled_values(outcomes, measure, extra_limits=()):
    """The values of the measure that agree with how a group of markets settled.

    outcomes: list of (low, high, result), result "yes" or "no".
    A value agrees with a market if the market's YES contains the value and
    it settled yes, or does not contain it and it settled no. We test every
    value on a grid (in steps) a little wider than all the limits involved.
    Returns (agreeing values, the grid used). Example: Kalshi CPI October 2025,
    "more than 0.1%" yes and "more than 0.2%" no -> [2] (= 0.2%).
    """
    step = STEP[measure]
    limits = [x for low, high, _ in outcomes for x in (low, high) if x is not None] + list(extra_limits)
    if not limits:
        return [], []
    grid = list(range(min(limits) - 2 * step, max(limits) + 3 * step, step))
    values = [value for value in grid
              if all(contains(low, high, value) == (result == "yes") for low, high, result in outcomes)]
    return values, grid


def describe_values(values, grid, measure):
    """Settled values as text: "= 0.2%", "≥ 3.2%" (all the way to the grid's end), "contradictory"."""
    if not values:
        return "contradictory results"
    low, high = min(values), max(values)
    if low == grid[0] and high == grid[-1]:
        return "not decided"
    if low == grid[0]:
        return f"≤ {format_value(high, measure)}"
    if high == grid[-1]:
        return f"≥ {format_value(low, measure)}"
    if low == high:
        return f"= {format_value(low, measure)}"
    return f"{format_value(low, measure)} to {format_value(high, measure)}"


def build_pairs(kalshi_markets, poly_markets, before):
    """Every pair worth comparing, with its title score and outcome relation.

    A pair is compared if both markets have the same topic and
      - their dates are within MAX_DAYS_APART days (Kalshi close time vs the
        Polymarket end date OR its actual close time: some Polymarket end
        dates are a month off), or
      - their key fields say they are about the same question: same measure
        (or Kalshi rate LEVEL vs Polymarket rate CHANGE) and same month.
    """
    left = kalshi_markets[["kalshi_ticker", "topic", "kalshi_measure", "kalshi_month", "kalshi_close_time"]]
    right = poly_markets[["poly_market_id", "topic", "poly_measure", "poly_month", "poly_end_date",
                          "poly_closed_time"]]
    pairs = left.merge(right, on="topic")
    one_day = pd.Timedelta(days=1)
    days_to_end = (pairs.kalshi_close_time - pairs.poly_end_date).abs() / one_day
    days_to_close = (pairs.kalshi_close_time - pairs.poly_closed_time).abs() / one_day
    pairs["days_apart"] = pd.concat([days_to_end, days_to_close], axis=1).min(axis=1).round(1)
    pairs["dates_within_7_days"] = pairs.days_apart <= MAX_DAYS_APART
    same_measure = pairs.kalshi_measure == pairs.poly_measure
    level_vs_change = (pairs.kalshi_measure == FED_LEVEL) & (pairs.poly_measure == FED_CHANGE)
    pairs["same_question"] = ((same_measure | level_vs_change) & pairs.kalshi_month.notna()
                              & (pairs.kalshi_month == pairs.poly_month))
    pairs = pairs[pairs.dates_within_7_days | pairs.same_question]
    pairs = pairs[["kalshi_ticker", "poly_market_id", "days_apart", "dates_within_7_days", "same_question"]]
    pairs = pairs.merge(kalshi_markets, on="kalshi_ticker").merge(poly_markets, on=["poly_market_id", "topic"])

    rows = []
    for pair in pairs.to_dict("records"):
        pair["title_similarity"] = round(title_similarity(pair["kalshi_title"], pair["poly_question"]), 1)
        pair.update(classify_pair(pair, before))
        rows.append(pair)
    return pd.DataFrame(rows)


def classify_pair(pair, before):
    """Pair type, outcome relation and whether the pair is a candidate (with the reason if not)."""
    k_measure, p_measure = pair["kalshi_measure"], pair["poly_measure"]
    p_low, p_high = bound(pair["poly_low"]), bound(pair["poly_high"])
    result = {"pair_type": "direct", "rate_before_meeting": None, "prior_level": None,
              "prior_month": None, "prior_known_since": None}

    if k_measure == FED_LEVEL and p_measure == FED_CHANGE:
        # Polymarket's change at this meeting, turned into a level with the rate before the meeting.
        result["pair_type"] = "level_vs_change"
        prior = before.get(pair["kalshi_month"])
        if prior is not None:
            level, previous_month, known_since = prior
            result.update(prior_level=level, prior_month=previous_month, prior_known_since=known_since,
                          rate_before_meeting=f"{format_value(level, FED_LEVEL)} (after the {previous_month} meeting)")
            p_low = None if p_low is None else p_low + level
            p_high = None if p_high is None else p_high + level
        else:
            p_low = p_high = None
    step = STEP.get(k_measure, 1)
    result["relation"] = relation(pair["kalshi_low"], pair["kalshi_high"], p_low, p_high, step)
    result["poly_low_compared"], result["poly_high_compared"] = p_low, p_high

    # Is it a candidate? The key fields decide; title similarity is only a fallback.
    unreadable = not (pair["kalshi_fields_ok"] and pair["poly_fields_ok"])
    if pair["same_question"] and result["relation"] in ("same", "opposite"):
        result["is_candidate"], result["why_not_candidate"] = True, None
    elif unreadable and pair["dates_within_7_days"] and pair["title_similarity"] >= TITLE_ONLY_MIN_SCORE:
        result["pair_type"] = "title_only"
        result["is_candidate"], result["why_not_candidate"] = True, None
    else:
        result["is_candidate"] = False
        if unreadable:
            why = "key fields could not be read"
        elif k_measure != p_measure and result["pair_type"] != "level_vs_change":
            why = f"different quantity (Kalshi {k_measure}, Polymarket {p_measure})"
        elif pair["kalshi_month"] != pair["poly_month"]:
            why = f"different month (Kalshi {pair['kalshi_month']}, Polymarket {pair['poly_month']})"
        elif result["pair_type"] == "level_vs_change" and result["prior_level"] is None:
            why = "rate before this meeting not known (previous meeting not settled yet, or not in our data)"
        elif result["relation"] == "unknown":
            why = f"Polymarket outcome has no single Kalshi equivalent ({pair['poly_outcome']})"
        else:
            why = (f"outcomes differ ({result['relation']}): Kalshi {pair['kalshi_outcome']}, "
                   f"Polymarket {pair['poly_outcome']}")
        result["why_not_candidate"] = why
    return result


# ---------------------------------------------------------------------------
# Step 5: compare the rules and flag differences
# ---------------------------------------------------------------------------

# Flags that make the suggested verdict "unsure" even when the outcomes are
# the same on paper: the comparison needs an assumption, or the data shows
# that something went wrong.
UNSURE_FLAGS = {"level_vs_change", "settled_on_different_values", "poly_closed_early", "dates_far_apart"}


def flag(code, points, short, detail):
    """One flag: a code, how much it adds to difference_score, a short and a full explanation."""
    return {"code": code, "points": points, "short": short, "detail": detail}


def event_outcomes(table, event_column, prefix):
    """{event: [(low, high, result), ...]} of the settled markets with a known outcome."""
    events = {}
    for event, low, high, result in zip(table[event_column], table[f"{prefix}_low"], table[f"{prefix}_high"],
                                        table[f"{prefix}_result"]):
        low, high = bound(low), bound(high)
        if result in ("yes", "no") and (low is not None or high is not None):
            events.setdefault(event, []).append((low, high, result))
    return events


def settlement_flags(pair, kalshi_events, poly_events):
    """Did the two venues pay the same? Fills pair["results_agree"] and the settled values.

    Two checks:
      * this pair: both settled, and Polymarket's YES won exactly when it should
        (Kalshi YES won, for "same"; Kalshi YES lost, for "opposite");
      * the whole meeting or month: which values agree with how ALL settled
        markets of each venue's event paid. If the two sets have nothing in
        common, the venues settled the event on different values (this can
        happen even when this one pair paid the same).
    """
    flags = []
    measure = pair["kalshi_measure"]
    k_result, p_result = pair["kalshi_result"], pair["poly_result"]
    pair["results_agree"] = None
    if k_result in ("yes", "no") and p_result in ("yes", "no") and pair["relation"] in ("same", "opposite"):
        poly_should_win = (k_result == "yes") == (pair["relation"] == "same")
        pair["results_agree"] = (p_result == "yes") == poly_should_win
        if not pair["results_agree"]:
            flags.append(flag("paid_differently", 10, f"settled differently (Kalshi YES {k_result}, Polymarket YES "
                              f"{p_result})", f"This pair settled differently: Kalshi's YES settled {k_result}, "
                              f"Polymarket's YES settled {p_result}, although they are the same outcome on paper."))

    pair["kalshi_settled_value"] = pair["poly_settled_value"] = None
    k_outcomes = kalshi_events.get(pair["kalshi_event"], [])
    p_outcomes = poly_events.get(pair["poly_event_id"], [])
    if pair["pair_type"] == "level_vs_change":
        if bound(pair["prior_level"]) is None:
            return flags
        shift = bound(pair["prior_level"])  # Polymarket's changes as levels
        p_outcomes = [(None if low is None else low + shift, None if high is None else high + shift, result)
                      for low, high, result in p_outcomes]
    if measure not in STEP or not k_outcomes or not p_outcomes:
        return flags
    limits = [x for low, high, _ in k_outcomes + p_outcomes for x in (low, high) if x is not None]
    k_values, grid = settled_values(k_outcomes, measure, limits)
    p_values, _ = settled_values(p_outcomes, measure, limits)
    pair["kalshi_settled_value"] = describe_values(k_values, grid, measure)
    pair["poly_settled_value"] = describe_values(p_values, grid, measure)
    if k_values and p_values and not set(k_values) & set(p_values):
        what = "meeting" if measure in (FED_CHANGE, FED_LEVEL) else "month"
        flags.append(flag("settled_on_different_values", 6,
                          f"the venues settled this {what} on different values (Kalshi "
                          f"{pair['kalshi_settled_value']}, Polymarket {pair['poly_settled_value']})",
                          f"The two venues settled this {what} on different values: Kalshi's markets paid as if "
                          f"{SHORT_NAMES.get(measure, 'the change')} {pair['kalshi_settled_value']}, Polymarket's as "
                          f"if {pair['poly_settled_value']}."))
    return flags


def data_flags(pair):
    """Data problems: duplicates, markets that closed early, wrong or far-apart dates, odd sources."""
    flags = []
    k_close, p_end, p_closed = pair["kalshi_close_time"], pair["poly_end_date"], pair["poly_closed_time"]
    event = "meeting" if pair["topic"] == "fed" else "release"
    one_day = pd.Timedelta(days=1)

    if not pair["dates_within_7_days"]:
        end_text = f"{p_end:%Y-%m-%d}" if pd.notna(p_end) else "missing"
        closed_text = f"{p_closed:%Y-%m-%d}" if pd.notna(p_closed) else "not yet"
        flags.append(flag("dates_far_apart", 3, f"the dates are {pair['days_apart']:.0f} days apart",
                          f"Sanity check failed: Kalshi closes {k_close:%Y-%m-%d}, Polymarket's end date is "
                          f"{end_text} and it closed {closed_text}: more than {MAX_DAYS_APART} days apart."))
    if pd.notna(p_closed) and p_closed < k_close - one_day:
        days = (k_close - p_closed) / one_day
        flags.append(flag("poly_closed_early", 5, f"this Polymarket market closed on {p_closed:%Y-%m-%d}, "
                          f"{days:.0f} days before the {event}",
                          f"This Polymarket market closed on {p_closed:%Y-%m-%d %H:%M} UTC, {days:.0f} days before "
                          f"the {event} (Kalshi closes {k_close:%Y-%m-%d}). Probably a duplicate or cancelled "
                          f"event: do not use its prices after that date."))
    if text(pair["poly_duplicate_events"]):
        flags.append(flag("poly_duplicate", 2, "Polymarket lists the same outcome in another event",
                          f"Polymarket lists exactly the same outcome for the same {event} in other events too "
                          f"(event ids {pair['poly_duplicate_events']}): check which one is the real one."))
    if pd.notna(p_end) and abs(p_end - k_close) > 3 * one_day:
        flags.append(flag("poly_end_date_off", 1, "Polymarket's end date is off",
                          f"Polymarket's end date ({p_end:%Y-%m-%d}) is {abs(p_end - k_close) / one_day:.0f} days "
                          f"from the {event} ({pair['kalshi_event_date']}); the end date in Polymarket's data is "
                          f"wrong, use the date in its rules."))
    k_day, p_day = pair["kalshi_event_date"], pair["poly_event_date"]
    if pd.notna(k_day) and pd.notna(p_day) and k_day != p_day:
        gap = (k_day - p_day).days
        if 360 <= abs(gap) <= 370:
            flags.append(flag("poly_rules_date_typo", 1, "the year in Polymarket's rules is wrong",
                              f"Polymarket's rules give the {event} date as {p_day:%B %d, %Y}; the {event} was on "
                              f"{k_day:%B %d, %Y} (a typo in the year)."))
        elif pair["topic"] == "cpi" and 0 < gap <= 45:
            pass  # a CPI release moved to a later day: reported as release_was_postponed (cpi_rule_flags)
        else:
            flags.append(flag("scheduled_dates_differ", 2, f"scheduled {event} dates differ",
                              f"The {event} dates differ: Kalshi's market closes on {k_day} (its event day), "
                              f"Polymarket's rules say {p_day}."))
    source_field = text(pair["poly_source_field"])
    if source_field and domain(source_field) not in ("bls.gov", "federalreserve.gov"):
        flags.append(flag("poly_source_unusual", 2, f"Polymarket's resolution source is {domain(source_field)}",
                          f"Polymarket's resolution-source field points to {source_field} (not the BLS or the "
                          f"Fed); its rules text still names the official source. Kalshi: {pair['kalshi_source']}."))
    if pair["kalshi_lost_symbol"]:
        words = re.search(r"\w+ of\s{2,}\d+\s*bps", pair["kalshi_rules"])
        flags.append(flag("kalshi_text_lost_symbol", 1, "Kalshi's rules text lost the '>' sign",
                          f"Kalshi's rules text lost the '>' sign (it reads 'does a {words.group(0)}'); the title "
                          f"and the ticker (C26/H26) show that this market means MORE than 25 bp."))
    return flags


def fed_rule_flags(pair):
    """Rule differences for the Fed: cancelled or moved meetings, rounding, emergency meetings."""
    flags = []
    k_rule, p_deadline = text(pair["kalshi_cancel_rule"]), text(pair["poly_no_statement_rule"])
    p_wait = f"waits for a statement until {p_deadline}" if p_deadline else "says nothing about it"
    if pair["poly_event_rounds_up"] and pair["pair_type"] == "level_vs_change":
        before = bound(pair["prior_level"]) / 100  # in percent
        flags.append(flag("rounding", 1, "Polymarket rounds odd-sized moves up to the next 25 bp; Kalshi uses the "
                          "actual level", f"A move that is not a multiple of 25 bp: Polymarket rounds the change up "
                          f"to the next 25 bp, Kalshi compares the actual level. Example: a 37.5 bp cut from "
                          f"{before:.2f}% counts as a 50 bp cut on Polymarket but leaves the upper bound at "
                          f"{before - 0.375:.3f}%, above {before - 0.5:.2f}% on Kalshi, so the two can pay "
                          f"differently."))
    elif pair["poly_event_rounds_up"]:
        flags.append(flag("rounding", 1, "Polymarket rounds odd-sized moves up to the next 25 bp; Kalshi's text is "
                          "silent", "A move that is not a multiple of 25 bp (e.g. 12.5 bp): Polymarket rounds it up "
                          "to the next 25 bp (its rules say so, or give ranges such as 'between 26 (inclusive) and 50 "
                          "(inclusive)' bp); Kalshi's market text does not say (see its contract terms)."))
    elif pair["poly_event_has_other"]:
        flags.append(flag("rounding", 1, "Polymarket puts odd-sized moves into a separate 'Other' market; Kalshi's "
                          "text is silent", "A move between the listed sizes (e.g. 37.5 bp) resolves Polymarket's "
                          "separate 'Other' market (older rules); Kalshi's market text does not say (see its "
                          "contract terms)."))
    else:
        flags.append(flag("rounding", 1, "neither text says how an odd-sized move is counted",
                          "A move between the listed sizes (e.g. 37.5 bp): neither Polymarket's (older) rules nor "
                          "Kalshi's market text say how it is counted."))
    if pair["pair_type"] == "level_vs_change":
        level = format_value(bound(pair["prior_level"]), FED_LEVEL)
        flags.append(flag("level_vs_change", 4, f"Kalshi measures the rate LEVEL after the meeting, Polymarket the "
                          f"CHANGE at it (same only if the rate before is {level} and nothing moves between meetings)",
                          f"Kalshi: upper bound AFTER the meeting (it counts any move, also between meetings). "
                          f"Polymarket: change AT this meeting versus the level before it. Same outcome only if the "
                          f"rate before the meeting is {level} - known after the {pair['prior_month']} meeting "
                          f"({pair['prior_known_since']:%Y-%m-%d}), so compare prices only from then on - and the Fed "
                          f"makes no move between meetings."))
        if pair["kalshi_expires_week_after"]:
            flags.append(flag("cancelled_meeting", 1, "if no statement comes out, the venues wait different times",
                              f"If no statement comes out: Kalshi's market expires one week after the meeting on the "
                              f"rate then in force; Polymarket {p_wait} and then resolves to 'No change'."))
        return flags

    no_change_pair = pair["kalshi_low"] == 0 and pair["kalshi_high"] == 0
    if k_rule == "all_no" and p_deadline:
        if no_change_pair:
            flags.append(flag("cancelled_meeting", 4, "if the meeting is cancelled, Kalshi pays No on every outcome "
                              "but Polymarket pays Yes on 'No change'",
                              f"If the meeting is cancelled: Kalshi's (older) rule says all strikes resolve to No; "
                              f"Polymarket {p_wait}, then resolves to 'No change' (Yes for this market)."))
        else:
            flags.append(flag("cancelled_meeting", 1, "if the meeting is cancelled both pay No, at different times",
                              f"If the meeting is cancelled: Kalshi's (older) rule says all strikes resolve to No; "
                              f"Polymarket {p_wait}, then resolves to 'No change' (No for this market)."))
    elif k_rule == "hold_yes" and p_deadline:
        flags.append(flag("cancelled_meeting", 2, "a cancelled or moved meeting is handled differently",
                          f"If the meeting is cancelled or moved: Kalshi resolves at once ('Fed maintains rate' = Yes "
                          f"if the meeting does not take place on its scheduled date); Polymarket {p_wait} and only "
                          f"then resolves to 'No change'. A meeting moved by a few days could pay differently."))
    else:
        flags.append(flag("cancelled_meeting", 2, "the texts differ on what happens if the meeting is cancelled",
                          f"Cancelled meeting: Kalshi's market text {'says ' + k_rule if k_rule else 'says nothing'}; "
                          f"Polymarket {p_wait}."))
    flags.append(flag("emergency_meeting", 0, "neither text mentions emergency meetings",
                      "Not covered by either text: emergency (unscheduled) meetings. Both count only the decision of "
                      "this scheduled meeting (Kalshi: the move 'on' the meeting day; Polymarket: the change versus "
                      "the level just before the meeting)."))
    return flags


def cpi_rule_flags(pair):
    """Rule differences for CPI: postponed releases, seasonal adjustment, precision, revisions."""
    flags = []
    if pair["kalshi_waits_after_shutdown"] and pair["poly_previous_month_fallback"]:
        flags.append(flag("postponed_release", 2, "a postponed release is handled differently",
                          "If the release is postponed: Polymarket waits until the next scheduled CPI report and, if "
                          "the figure is still missing, uses the most recent previous month with data; Kalshi "
                          "extends the market until the release (at most six months after a government shutdown)."))
    elif pair["kalshi_waits_after_shutdown"]:
        flags.append(flag("postponed_release", 2, "Polymarket's rules say nothing about a postponed release",
                          "If the release is postponed: Polymarket's rules (older version) say nothing; Kalshi extends "
                          "the market until the release (at most six months after a government shutdown)."))
    postponed = []
    if pd.notna(pair["kalshi_rules_date"]) and pd.notna(pair["kalshi_event_date"]) \
            and pair["kalshi_rules_date"] != pair["kalshi_event_date"]:
        postponed.append(f"Kalshi's text names {pair['kalshi_rules_date']} but the market closed on "
                         f"{pair['kalshi_event_date']}")
    k_day, p_day = pair["kalshi_event_date"], pair["poly_event_date"]
    if pd.notna(k_day) and pd.notna(p_day) and 0 < (k_day - p_day).days <= 45:
        postponed.append(f"Polymarket's rules still give the original date {p_day} but Kalshi's market closed on "
                         f"{k_day}")
    if pd.notna(pair["kalshi_settled_time"]) and pair["kalshi_settled_time"] - pair["kalshi_close_time"] > pd.Timedelta(days=2):
        postponed.append(f"Kalshi settled only on {pair['kalshi_settled_time']:%Y-%m-%d}")
    if pd.notna(pair["poly_closed_time"]) and pair["poly_closed_time"] - pair["kalshi_close_time"] > pd.Timedelta(days=3):
        postponed.append(f"Polymarket resolved only on {pair['poly_closed_time']:%Y-%m-%d}")
    if postponed:
        flags.append(flag("release_was_postponed", 2, "this release WAS delayed",
                          "This release was delayed or unusual: " + "; ".join(postponed) + "."))
    if text(pair["poly_seasonal"]) and pair["kalshi_measure"] in CPI_MEASURES:
        flags.append(flag("seasonal_adjustment", 1, "only Polymarket says whether the figure is seasonally adjusted",
                          f"Polymarket says the figure is {pair['poly_seasonal']}; Kalshi's market text does not "
                          f"say (see its contract terms: {pair['kalshi_contract_terms']})."))
    if not (pair["kalshi_one_decimal"] and pair["poly_one_decimal"]):
        flags.append(flag("precision", 1, "one text does not state the one-decimal rounding",
                          f"Kalshi's text {'states' if pair['kalshi_one_decimal'] else 'does not state'} that the "
                          f"figure is read with one decimal; Polymarket's "
                          f"{'does' if pair['poly_one_decimal'] else 'does not'}."))
    flags.append(flag("revisions", 0, "neither text covers later revisions",
                      "Not covered by either text: later revisions of the figure (both resolve on the first release)."))
    return flags


def timing_flags(pair):
    """When each venue stops trading: compare prices only while both are open."""
    k_close, p_end = pair["kalshi_close_time"], pair["poly_end_date"]
    if pd.isna(p_end):
        return []
    hours = (p_end - k_close) / pd.Timedelta(hours=1)
    if abs(hours) < 1:
        return []
    when = "after" if hours > 0 else "before"
    return [flag("trading_end", 0, f"Polymarket's end date is {abs(hours):.0f} h {when} Kalshi's close",
                 f"Kalshi stops trading at {k_close.tz_convert(EASTERN):%Y-%m-%d %H:%M} ET, just before the "
                 f"announcement; Polymarket's end date is {p_end:%Y-%m-%d %H:%M} UTC ({abs(hours):.0f} hours "
                 f"{when}). Compare prices only before Kalshi's close.")]


def pair_flags(pair, kalshi_events, poly_events):
    """All flags of one candidate pair, most important first."""
    flags = settlement_flags(pair, kalshi_events, poly_events) + data_flags(pair)
    if pair["topic"] == "fed":
        flags += fed_rule_flags(pair)
    else:
        flags += cpi_rule_flags(pair)
    flags += timing_flags(pair)
    return sorted(flags, key=lambda one: -one["points"])


def suggest(pair, flags):
    """Suggested verdict and a one-line reason.

    different  the venues paid differently on this pair
    unsure     same outcome on paper, but it rests on an assumption (rate level vs
               change), or the data shows a problem (closed early, dates far apart,
               the venues settled this month/meeting on different values), or the
               key fields could not be read
    same       same outcome; any rule differences only matter in rare edge cases
               (they are all listed in the flags)
    """
    codes = {one["code"] for one in flags}
    important = [one for one in flags if one["points"] > 0]
    if pair["pair_type"] == "title_only":
        return "unsure", (f"Key fields could not be read; the titles are similar (score "
                          f"{pair['title_similarity']:.0f}): check by hand")
    side = "Polymarket YES = Kalshi YES" if pair["relation"] == "same" else "Polymarket YES = Kalshi NO"
    outcome = pair["kalshi_outcome"]
    if "paid_differently" in codes:
        others = [one["short"] for one in important if one["code"] != "paid_differently"][:1]
        first = important[0]["short"]
        return "different", "; ".join([first[0].upper() + first[1:]] + others)
    unsure = [one for one in important if one["code"] in UNSURE_FLAGS]
    if unsure:
        return "unsure", f"Same outcome on paper ({outcome}; {side}), but {unsure[0]['short']}"
    if important:
        return "same", f"Same outcome ({outcome}; {side}); edge cases to check: {important[0]['short']}"
    return "same", f"Same outcome ({outcome}; {side}); no rule differences found"


def make_candidates(pairs, kalshi_markets, poly_markets):
    """The candidate pairs with their flags, suggested verdict, reason and difference score."""
    if pairs.empty:
        return pd.DataFrame(columns=CSV_COLUMNS)
    kalshi_events = event_outcomes(kalshi_markets, "kalshi_event", "kalshi")
    poly_events = event_outcomes(poly_markets, "poly_event_id", "poly")
    rows = []
    for pair in pairs[pairs.is_candidate].to_dict("records"):
        flags = pair_flags(pair, kalshi_events, poly_events)
        pair["suggested_verdict"], pair["suggested_reason"] = suggest(pair, flags)
        pair["difference_score"] = sum(one["points"] for one in flags)
        pair["differences"] = " | ".join(f"[{one['points']}] {one['code']}: {one['detail']}" for one in flags)
        pair["flag_codes"] = [one["code"] for one in flags if one["points"] > 0]
        if pair["pair_type"] == "title_only":
            pair["poly_yes_equals"] = None
        else:
            pair["poly_yes_equals"] = "Kalshi YES" if pair["relation"] == "same" else "Kalshi NO"
        pair["verdict"] = ""
        pair["note"] = ""
        rows.append(pair)
    if not rows:
        return pd.DataFrame(columns=CSV_COLUMNS)
    candidates = pd.DataFrame(rows)
    return candidates.sort_values(["kalshi_event_date", "kalshi_measure", "kalshi_ticker", "poly_market_id"],
                                  ignore_index=True)


# ---------------------------------------------------------------------------
# Step 7: save, keeping the verdicts you typed
# ---------------------------------------------------------------------------

def read_review_file(path):
    """Read match_candidates.csv back, also after it was saved by Excel.

    Excel may save with ";" instead of "," (European settings) and in its own
    encoding instead of UTF-8; both are tried. All values are read as text.
    """
    for encoding in ["utf-8-sig", "cp1252"]:
        for separator in [",", ";"]:
            try:
                table = pd.read_csv(path, dtype=str, keep_default_na=False, sep=separator, encoding=encoding)
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
            if {"kalshi_ticker", "poly_market_id", "verdict", "note"} <= set(table.columns):
                return table
    raise ValueError(f"Could not read {path}: it needs the columns kalshi_ticker, poly_market_id, verdict, note")


def keep_earlier_verdicts(candidates, path):
    """Copy the verdicts and notes typed in an earlier match_candidates.csv into the new table.

    A typed row whose pair is no longer a candidate is not thrown away: it is
    added at the end, marked in suggested_reason. Returns (table, number of
    verdicts/notes carried over, number of old rows added at the end).
    """
    if not path.exists():
        return candidates, 0, 0
    old = read_review_file(path)
    typed = old[(old.verdict.str.strip() != "") | (old.note.str.strip() != "")]
    if typed.empty:
        return candidates, 0, 0
    typed_by_pair = {(row.kalshi_ticker, row.poly_market_id): (row.verdict, row.note) for row in typed.itertuples()}
    keys = list(zip(candidates.kalshi_ticker, candidates.poly_market_id))
    candidates = candidates.copy()
    candidates["verdict"] = [typed_by_pair.get(key, ("", ""))[0] for key in keys]
    candidates["note"] = [typed_by_pair.get(key, ("", ""))[1] for key in keys]
    new_keys = set(keys)
    leftover = typed[[key not in new_keys for key in zip(typed.kalshi_ticker, typed.poly_market_id)]].copy()
    if len(leftover):
        leftover["suggested_reason"] = "NO LONGER FOUND by match.py; kept because you typed a verdict or note. " \
                                       + leftover["suggested_reason"]
        candidates = pd.concat([candidates, leftover], ignore_index=True)
    return candidates, len(typed) - len(leftover), len(leftover)


def save_review_file(candidates, path):
    """Write the review CSV in UTF-8 with a byte-order mark, so Excel shows ≤, ≥ and ’ correctly."""
    table = candidates.reindex(columns=CSV_COLUMNS)
    temporary = path.with_name(path.name + ".tmp")
    table.to_csv(temporary, index=False, encoding="utf-8-sig")
    size_mb = temporary.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        temporary.unlink()
        raise ValueError(f"{path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB limit")
    temporary.replace(path)
    return path


def trickiest(candidates, count=10):
    """The candidates whose rules differ most, showing as many different problems as possible.

    Only the highest-scoring pair of each pair of events (one Kalshi event,
    one Polymarket event) is considered, and every event is shown once. The
    list is filled in four rounds, each going down the difference_score:
      1. pairs on which the two venues actually paid differently;
      2. one pair per main problem (its flag with the most points);
      3. pairs that show a problem not shown yet;
      4. the highest scores left.
    """
    ranked = candidates[candidates.difference_score > 0].sort_values(
        ["difference_score", "title_similarity"], ascending=False)
    ranked = ranked.drop_duplicates(["kalshi_event", "poly_event_id"])  # keeps the highest score
    picked, seen_events, main_flags, shown_flags = [], set(), set(), set()

    def main_flag(row):
        return row.flag_codes[0] if len(row.flag_codes) else None

    rounds = [lambda row: "paid_differently" in row.flag_codes,
              lambda row: main_flag(row) not in main_flags,
              lambda row: bool(set(row.flag_codes) - shown_flags),
              lambda row: True]
    for wanted in rounds:
        for index, row in ranked.iterrows():
            events = {row.kalshi_event, row.poly_event_id}
            if len(picked) < count and index not in picked and not events & seen_events and wanted(row):
                picked.append(index)
                seen_events |= events
                main_flags.add(main_flag(row))
                shown_flags.update(row.flag_codes)
    return ranked.loc[picked].sort_values("difference_score", ascending=False)


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

def print_summary(kalshi_markets, poly_markets, pairs, candidates):
    """What was compared and found, in numbers."""
    print(f"\n   {len(pairs):,} pairs compared: {int(pairs.dates_within_7_days.sum()):,} with dates within "
          f"{MAX_DAYS_APART} days, {int(pairs.same_question.sum()):,} about the same question by their key fields "
          f"({int((pairs.same_question & ~pairs.dates_within_7_days).sum())} of these with dates further apart)")
    # Title similarity alone cannot tell the candidates from the rejected pairs about the same question:
    same_question = pairs[pairs.same_question & (pairs.pair_type == "direct")]
    for label, group in [("candidates", same_question[same_question.is_candidate]),
                         ("rejected (outcomes differ)", same_question[~same_question.is_candidate])]:
        scores = group.title_similarity
        print(f"   title similarity of same-question pairs, {label:<26}: median {scores.median():.0f}, "
              f"middle half {scores.quantile(0.25):.0f}-{scores.quantile(0.75):.0f} ({len(group):,} pairs)")
    print(f"   {len(candidates)} candidates. Suggested verdicts by pair type:")
    table = pd.crosstab(candidates.pair_type, candidates.suggested_verdict, margins=True, margins_name="total")
    print("      " + table.to_string().replace("\n", "\n      "))

    matched = set(candidates.poly_market_id)
    unmatched = poly_markets[~poly_markets.poly_market_id.isin(matched)]
    print(f"\n   Polymarket markets without a candidate ({len(unmatched)} of {len(poly_markets)}), by measure:")
    for measure, group in unmatched.groupby("poly_measure", dropna=False):
        if measure in STEP:
            exact = int((group.poly_direction.fillna("").str.contains("exactly")).sum())
            note = f"{exact} are single buckets ('exactly ...'), which have no single-market twin on Kalshi"
            if measure in (CORE_CPI_MOM, CORE_CPI_YOY):
                note = "no Kalshi core-CPI series downloaded"
        else:
            note = "no counterpart among the Kalshi series we downloaded"
        print(f"      {str(measure):<16} {len(group):>4}  ({note})")
    unreadable_k = int((~kalshi_markets.kalshi_fields_ok).sum())
    unreadable_p = int((~poly_markets.poly_fields_ok).sum())
    print(f"   Key fields could not be read for {unreadable_k} Kalshi and {unreadable_p} Polymarket markets.")


def print_trickiest(candidates, count=10):
    """Print the pairs whose rules differ most."""
    print(f"\nThe {count} trickiest candidates (highest difference_score, different problems first):")
    for number, row in enumerate(trickiest(candidates, count).itertuples(), start=1):
        print(f"\n{number:>2}. score {row.difference_score}  suggested: {row.suggested_verdict}  "
              f"[{row.pair_type}, Polymarket YES = {row.poly_yes_equals}]")
        print(f"    Kalshi     {row.kalshi_ticker}: {row.kalshi_title}  (result: {row.kalshi_result})")
        print(f"    Polymarket {row.poly_market_id}: {row.poly_question}  (result: {row.poly_result})")
        print(f"    Reason: {row.suggested_reason}")
        for one in row.differences.split(" | "):
            if not one.startswith("[0]"):
                print(f"      - {one}")


def main():
    pd.set_option("display.width", 250)
    for path, script in [(KALSHI_FILE, "src/download_kalshi.py"), (POLY_FILE, "src/download_polymarket.py")]:
        if not path.exists():
            sys.exit(f"Missing {path.relative_to(PROJECT_ROOT)}: run python {script} first.")

    print("STEP 1: key fields of every market")
    raw_kalshi = pd.read_parquet(KALSHI_FILE)
    sources = kalshi_sources(sorted(raw_kalshi.series_ticker.dropna().unique()))
    kalshi_markets = kalshi_table(raw_kalshi, sources)
    poly_markets = poly_table(pd.read_parquet(POLY_FILE))
    print(f"   Kalshi: {len(kalshi_markets)} markets; Polymarket: {len(poly_markets)} markets")
    before = rate_before_each_meeting(kalshi_markets)
    print(f"   Fed upper bound before each meeting known for {len(before)} meetings "
          f"(from Kalshi's settled rate-level markets)")

    print("\nSTEP 2-4: pairs, title similarity, outcome comparison")
    pairs = build_pairs(kalshi_markets, poly_markets, before)

    print("STEP 5-6: rule comparison and suggested verdicts")
    candidates = make_candidates(pairs, kalshi_markets, poly_markets)
    print_summary(kalshi_markets, poly_markets, pairs, candidates)

    print("\nSTEP 7: saving")
    review, carried, kept = keep_earlier_verdicts(candidates, CANDIDATES_FILE)
    if carried or kept:
        print(f"   kept {carried} verdicts/notes you typed earlier"
              + (f"; {kept} typed rows are no longer candidates and were added at the end" if kept else ""))
    path = save_review_file(review, CANDIDATES_FILE)
    print(f"   {path.relative_to(PROJECT_ROOT)}: {len(review)} rows, {path.stat().st_size / 1e6:.1f} MB")
    path = kalshi.save_table(pairs.reindex(columns=ALL_PAIRS_COLUMNS), ALL_PAIRS_NAME)
    print(f"   {path.relative_to(PROJECT_ROOT)}: {len(pairs):,} rows, {path.stat().st_size / 1e6:.1f} MB")

    print_trickiest(candidates)


if __name__ == "__main__":
    main()
