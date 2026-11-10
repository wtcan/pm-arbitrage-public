"""
build_pairs.py - Make the table of verified pairs, data/processed/pairs.parquet, from match_verified.csv.

How to run it (from the project folder):
    python src/build_pairs.py

It reads   data/processed/match_verified.csv      (made by src/apply_independent_review.py,
                                                   or by src/read_review_sheet.py from your own review)
           data/processed/kalshi_markets.parquet   (for Kalshi's event ticker)
           data/processed/poly_markets.parquet     (for Polymarket's condition id and token ids)
and writes data/processed/pairs.parquet           (one row per pair whose verdict is "same")
and prints how many pairs there are per topic and month.

Columns of pairs.parquet (all times in UTC):
    pair_id                 "Kalshi ticker / Polymarket market id"
    topic                   "fed" or "cpi"
    measure                 "fed_change" (Fed decision), "cpi_mom" or "cpi_yoy"
    month                   the meeting month or the data month, "YYYY-MM"
    event_date              the scheduled meeting or release day (a date; Kalshi's markets close on it)
    kalshi_outcome          when Kalshi's YES wins, e.g. "cut of 25 bp" or "CPI MoM ≥ 0.4%"
    poly_yes_equals         "Kalshi YES": Polymarket's YES wins exactly when Kalshi's YES wins;
                            "Kalshi NO":  Polymarket's YES wins exactly when Kalshi's YES loses.
                            The two legs that always pay exactly $1 together are:
                              Kalshi YES: Kalshi YES + Polymarket NO  (or Kalshi NO + Polymarket YES)
                              Kalshi NO:  Kalshi YES + Polymarket YES (or Kalshi NO + Polymarket NO)
    kalshi_ticker, kalshi_event_ticker, kalshi_title
    poly_market_id, poly_condition_id, poly_event_id, poly_question, poly_url
    poly_yes_token_id, poly_no_token_id   the token ids for Polymarket's order books and prices
    kalshi_close_time       when Kalshi stops trading (just before the announcement):
                            compare prices only before this time
    poly_end_date           Polymarket's end date (sometimes wrong, see docs/matching.md, risk 4)
    poly_closed_time        when Polymarket closed the market (empty if still open)
    kalshi_result, poly_result   "yes" or "no" once settled, empty (missing) before
    note                    the reviewer's one-line reason
    review_method           how the verdict was made, e.g. "automated two-pass"
"""

import sys

import pandas as pd

import match
from read_review_sheet import VERIFIED_FILE

PROJECT_ROOT = match.PROJECT_ROOT
KALSHI_FILE = match.PROCESSED_FOLDER / "kalshi_markets.parquet"
POLY_FILE = match.PROCESSED_FOLDER / "poly_markets.parquet"
PAIRS_FILE = match.PROCESSED_FOLDER / "pairs.parquet"
MAX_FILE_MB = match.MAX_FILE_MB  # project rule: every file under 50 MB

TOPICS = {"fed_change": "fed", "cpi_mom": "cpi", "cpi_yoy": "cpi"}  # measure -> topic
TIME_COLUMNS = ["kalshi_close_time", "poly_end_date", "poly_closed_time"]
COLUMNS = ["pair_id", "topic", "measure", "month", "event_date", "kalshi_outcome", "poly_yes_equals",
           "kalshi_ticker", "kalshi_event_ticker", "kalshi_title",
           "poly_market_id", "poly_condition_id", "poly_event_id", "poly_question", "poly_url",
           "poly_yes_token_id", "poly_no_token_id",
           "kalshi_close_time", "poly_end_date", "poly_closed_time",
           "kalshi_result", "poly_result", "note", "review_method"]


def build_pairs(verified, kalshi_markets, poly_markets):
    """One row per "same" pair of match_verified.csv, with the market ids needed to fetch prices.

    verified: match_verified.csv read as text. kalshi_markets, poly_markets:
    the two market tables. Raises ValueError if a "same" pair is not a
    direct pair of one known measure, or a market is missing from its table.
    """
    same = verified[verified["verdict"] == "same"].copy()
    if "review_method" not in same.columns:
        same["review_method"] = "manual review sheet"  # match_verified.csv made by read_review_sheet.py

    problems = [f"{pid}: Kalshi measures {k} but Polymarket {p}"
                for pid, k, p in zip(same["pair_id"], same["kalshi_measure"], same["poly_measure"]) if k != p]
    problems += [f"{pid}: unknown measure {k}"
                 for pid, k in zip(same["pair_id"], same["kalshi_measure"]) if k not in TOPICS]
    problems += [f"{pid}: Kalshi market {t} is not in {KALSHI_FILE.name}"
                 for pid, t in zip(same["pair_id"], same["kalshi_ticker"]) if t not in set(kalshi_markets["ticker"])]
    problems += [f"{pid}: Polymarket market {m} is not in {POLY_FILE.name}"
                 for pid, m in zip(same["pair_id"], same["poly_market_id"]) if m not in set(poly_markets["market_id"])]
    if problems:
        raise ValueError("Cannot build the pairs table:\n   " + "\n   ".join(problems))

    kalshi = kalshi_markets.set_index("ticker")
    poly = poly_markets.set_index("market_id")
    pairs = pd.DataFrame({
        "pair_id": same["pair_id"],
        "topic": same["kalshi_measure"].map(TOPICS),
        "measure": same["kalshi_measure"],
        "month": same["kalshi_month"],
        # Kalshi's date: its markets close on the day of the meeting or release. (Polymarket's
        # date comes from its rules text, which has typos, see docs/matching.md, risk 4.)
        "event_date": pd.to_datetime(same["kalshi_event_date"]).dt.date,
        "kalshi_outcome": same["kalshi_outcome"],
        "poly_yes_equals": same["poly_yes_equals"],
        "kalshi_ticker": same["kalshi_ticker"],
        "kalshi_event_ticker": kalshi.loc[same["kalshi_ticker"], "event_ticker"].to_numpy(),
        "kalshi_title": same["kalshi_title"],
        "poly_market_id": same["poly_market_id"],
        "poly_condition_id": poly.loc[same["poly_market_id"], "condition_id"].to_numpy(),
        "poly_event_id": poly.loc[same["poly_market_id"], "event_id"].to_numpy(),
        "poly_question": same["poly_question"],
        "poly_url": same["poly_url"],
        "poly_yes_token_id": poly.loc[same["poly_market_id"], "yes_token_id"].to_numpy(),
        "poly_no_token_id": poly.loc[same["poly_market_id"], "no_token_id"].to_numpy(),
    })
    for column in TIME_COLUMNS:  # text such as "2024-11-07 18:55:00+00:00" -> a UTC time; empty -> missing
        # ISO8601: some times have fractions of a second ("20:01:40.284977+00:00"), others do not.
        pairs[column] = pd.to_datetime(same[column].replace("", None), utc=True, format="ISO8601")
    for column in ["kalshi_result", "poly_result"]:
        pairs[column] = same[column].replace("", None)  # empty = not settled yet -> missing
    pairs["note"] = same["note"]
    pairs["review_method"] = same["review_method"]
    return pairs[COLUMNS].reset_index(drop=True)


def counts_per_month(pairs):
    """Number of pairs per month (rows) and topic/measure (columns), with totals."""
    table = pd.crosstab(pairs["month"], [pairs["topic"], pairs["measure"]], margins=True, margins_name="all")
    return table


def save_pairs(pairs, path):
    """Write pairs.parquet; stop if it would break the project's 50 MB limit."""
    pairs.to_parquet(path, index=False)
    size_mb = path.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        path.unlink()
        raise ValueError(f"{path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB limit")
    return path


def main():
    for path, script in [(VERIFIED_FILE, "src/apply_independent_review.py or src/read_review_sheet.py"),
                         (KALSHI_FILE, "src/download_kalshi.py"), (POLY_FILE, "src/download_polymarket.py")]:
        if not path.exists():
            sys.exit(f"Missing {path.relative_to(PROJECT_ROOT)}: run python {script} first.")
    verified = pd.read_csv(VERIFIED_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    try:
        pairs = build_pairs(verified, pd.read_parquet(KALSHI_FILE), pd.read_parquet(POLY_FILE))
    except ValueError as problem:
        sys.exit(f"Stopped: {problem}")
    save_pairs(pairs, PAIRS_FILE)

    print(f"{PAIRS_FILE.relative_to(PROJECT_ROOT)}: {len(pairs)} pairs with verdict 'same' "
          f"(of {len(verified)} in {VERIFIED_FILE.name})")
    print(f"   per topic: {pairs['topic'].value_counts().to_dict()}; per measure: {pairs['measure'].value_counts().to_dict()}")
    print(f"   settled on both venues: {(pairs['kalshi_result'].notna() & pairs['poly_result'].notna()).sum()}, "
          f"not settled yet: {pairs['kalshi_result'].isna().sum()}")
    print("\nPairs per month:")
    print(counts_per_month(pairs).to_string())


if __name__ == "__main__":
    main()
