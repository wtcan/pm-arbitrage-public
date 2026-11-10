"""
read_review_sheet.py - Read your filled-in match_review.xlsx back and save your verdicts as a CSV.

How to run it (from the project folder):
    python src/read_review_sheet.py
or, if your filled-in file has another name or is in another folder:
    python src/read_review_sheet.py path/to/my_review.xlsx

It reads   data/processed/match_review.xlsx     (made by src/make_review_sheet.py, filled in by you)
           data/processed/match_candidates.csv  (made by src/match.py)
and writes data/processed/match_verified.csv

match_verified.csv has one row per pair: pair_id first, then all the columns
of match_candidates.csv in the same order, with "verdict" and "note" filled
in from YOUR sheet. Pairs you have not reviewed yet have an empty verdict.
The rows are in the order of match_candidates.csv (by meeting or release
date). To keep only the pairs you checked as the same:
    pairs = pd.read_csv("data/processed/match_verified.csv", dtype=str, keep_default_na=False)
    same = pairs[pairs.verdict == "same"]

Checks:
  * verdicts are tidied up ("Same " -> "same"). Anything other than same,
    different, unsure or an empty cell stops the script, with a list of the
    Excel rows to fix. So does a pair_id that is empty or appears twice;
  * a pair from your sheet that match.py no longer finds (after a re-run) is
    not lost: if you typed a verdict or note for it, it is added at the end
    of match_verified.csv, marked "NO LONGER FOUND" in suggested_reason;
  * pairs that match.py found after the sheet was made are listed; their
    verdict is empty.
"""

import sys
from pathlib import Path

import pandas as pd

import match
from make_review_sheet import MAX_FILE_MB, PROJECT_ROOT, REVIEW_FILE, VERDICTS, pair_id

VERIFIED_FILE = match.PROCESSED_FOLDER / "match_verified.csv"


def read_verdicts(path):
    """pair_id, verdict and note of every row of the filled-in review sheet, as tidied-up text.

    Raises ValueError, listing the Excel rows to fix, if a column is missing,
    a pair_id is empty or appears twice, or a verdict is not same, different
    or unsure.
    """
    sheet = pd.read_excel(path, dtype=str, keep_default_na=False)
    missing = [column for column in ["pair_id", "verdict", "note"] if column not in sheet.columns]
    if missing:
        raise ValueError(f"{path.name} has no column {', '.join(missing)}. Was it made by make_review_sheet.py?")

    review = pd.DataFrame({
        "pair_id": sheet["pair_id"].str.strip(),
        "verdict": sheet["verdict"].str.strip().str.lower(),  # "Same " -> "same"
        "note": sheet["note"].str.strip(),
    })
    review.index = review.index + 2  # the row number in Excel (row 1 is the header)
    review = review[(review["pair_id"] != "") | (review["verdict"] != "") | (review["note"] != "")]  # skip empty rows

    problems = []
    for row, pid, verdict in zip(review.index, review["pair_id"], review["verdict"]):
        if pid == "":
            problems.append(f"row {row}: the pair_id is empty")
        if verdict not in VERDICTS + [""]:
            problems.append(f"row {row} ({pid}): the verdict '{verdict}' is not same, different or unsure")
    filled = review[review["pair_id"] != ""]
    for pid in filled["pair_id"][filled["pair_id"].duplicated()].unique():
        rows = ", ".join(str(row) for row in filled.index[filled["pair_id"] == pid])
        problems.append(f"rows {rows}: the same pair_id {pid}")
    if problems:
        raise ValueError(f"Please fix these rows of {path.name} and run again:\n   " + "\n   ".join(problems))
    return review.reset_index(drop=True)


def verified_table(review, candidates):
    """match_candidates.csv with pair_id in front, and the verdict and note from your review sheet.

    review: the result of read_verdicts(). candidates: match_candidates.csv read as text.
    Returns (table, pairs kept although no longer in match_candidates.csv,
             pairs of match_candidates.csv that are not in your sheet).
    """
    table = candidates.copy()
    table.insert(0, "pair_id", [pair_id(k, p) for k, p in zip(table["kalshi_ticker"], table["poly_market_id"])])
    yours = review.set_index("pair_id")
    table["verdict"] = table["pair_id"].map(yours["verdict"]).fillna("")
    table["note"] = table["pair_id"].map(yours["note"]).fillna("")
    not_in_sheet = table.loc[~table["pair_id"].isin(review["pair_id"]), "pair_id"].tolist()

    # Pairs you typed something for that match.py no longer finds: keep them, at the end.
    typed = (review["verdict"] != "") | (review["note"] != "")
    gone = review[typed & ~review["pair_id"].isin(table["pair_id"])].copy()
    if len(gone):
        gone["kalshi_ticker"] = gone["pair_id"].str.partition(" / ")[0]   # the text before " / "
        gone["poly_market_id"] = gone["pair_id"].str.partition(" / ")[2]  # the text after it
        gone["suggested_reason"] = "NO LONGER FOUND: this pair is not in match_candidates.csv any more"
        table = pd.concat([table, gone], ignore_index=True).fillna("")
    return table, gone["pair_id"].tolist(), not_in_sheet


def save_verified(table, path):
    """Write match_verified.csv in UTF-8 with a byte-order mark, so that Excel shows ≤, ≥ and ’ correctly."""
    table.to_csv(path, index=False, encoding="utf-8-sig")
    size_mb = path.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        path.unlink()
        raise ValueError(f"{path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB limit")
    return path


def main():
    review_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REVIEW_FILE
    for path, script in [(review_path, "src/make_review_sheet.py"), (match.CANDIDATES_FILE, "src/match.py")]:
        if not path.exists():
            sys.exit(f"Missing {path}: run python {script} first.")
    try:
        review = read_verdicts(review_path)
    except ValueError as problem:
        sys.exit(str(problem))
    candidates = match.read_review_file(match.CANDIDATES_FILE)
    table, kept, not_in_sheet = verified_table(review, candidates)
    save_verified(table, VERIFIED_FILE)

    verdicts = table["verdict"]
    reviewed = verdicts != ""
    print(f"Read {len(review)} pairs from {review_path.name}")
    print(f"   your verdicts: {(verdicts == 'same').sum()} same, {(verdicts == 'different').sum()} different, "
          f"{(verdicts == 'unsure').sum()} unsure, {(~reviewed).sum()} not reviewed yet")
    compared = reviewed & (table["suggested_verdict"] != "")
    disagree = compared & (verdicts != table["suggested_verdict"])
    print(f"   {disagree.sum()} of your {compared.sum()} verdicts differ from match.py's suggested verdict")
    if kept:
        print(f"WARNING: {len(kept)} pairs you typed a verdict or note for are no longer in "
              f"match_candidates.csv (was match.py run again?). They are kept at the end of "
              f"match_verified.csv: {', '.join(kept)}")
    if not_in_sheet:
        print(f"NOTE: {len(not_in_sheet)} pairs of match_candidates.csv are not in your sheet "
              f"(match.py found them after the sheet was made); their verdict is empty: {', '.join(not_in_sheet)}")
    print(f"{VERIFIED_FILE.relative_to(PROJECT_ROOT)}: {len(table)} rows")


if __name__ == "__main__":
    main()
