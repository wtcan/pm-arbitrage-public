"""
apply_independent_review.py - Combine an independent review of the match candidates with match.py's suggestions.

How to run it (from the project folder):
    python src/apply_independent_review.py
or, to replace files that already hold work typed by hand (see "Safety" below):
    python src/apply_independent_review.py --overwrite

It reads   data/processed/match_candidates.csv    (made by src/match.py, with its suggested_verdict)
           data/processed/independent_review.csv  (pass 1, see below)
and writes data/processed/match_verified.csv      (pass 2: the combined verdicts)
           data/processed/spot_check.xlsx         (10 random "same" pairs for you to check by hand)

The "automated two-pass" review:
  * Pass 1 (independent_review.csv): a second reviewer read both full rules
    texts of every pair WITHOUT looking at match.py's suggested_verdict,
    suggested_reason, difference_score or differences, and wrote down its own
    verdict (same / different / unsure) and a one-line reason.
  * Pass 2 (this script): the two verdicts are combined, deliberately
    cautiously:
      - "same"      only if BOTH the independent review and match.py say "same";
      - "unsure"    otherwise, if either of them says "unsure";
      - "different" otherwise (both say the pair is not the same).
    note = the independent reason, plus " (disagreement)" where the two
    verdicts differ.

match_verified.csv has exactly the format that src/read_review_sheet.py
writes (pair_id first, then all columns of match_candidates.csv, with verdict
and note filled in), plus one last column: review_method = "automated two-pass".
To keep only the pairs judged the same:
    pairs = pd.read_csv("data/processed/match_verified.csv", dtype=str, keep_default_na=False)
    same = pairs[pairs.verdict == "same"]

spot_check.xlsx lists 10 "same" pairs drawn at random with a fixed seed
(SPOT_CHECK_SEED), so the same 10 pairs come out every time. Fill in its
column my_check (dropdown: agree / disagree) after reading both rules texts.

Safety: the script stops instead of overwriting
  * a match_verified.csv that this script did not make (for example your own
    verdicts from read_review_sheet.py), and
  * a spot_check.xlsx in which my_check is already filled in,
unless you add --overwrite.

Excel files are written with openpyxl: https://openpyxl.readthedocs.io/en/stable/
(dropdown lists: https://openpyxl.readthedocs.io/en/stable/validation.html)
"""

import math
import sys

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

import match
import read_review_sheet
from make_review_sheet import PROJECT_ROOT, VERDICTS, pair_id

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

CANDIDATES_FILE = match.CANDIDATES_FILE                                 # data/processed/match_candidates.csv
INDEPENDENT_FILE = match.PROCESSED_FOLDER / "independent_review.csv"   # pass 1
VERIFIED_FILE = read_review_sheet.VERIFIED_FILE                         # data/processed/match_verified.csv
SPOT_CHECK_FILE = match.PROCESSED_FOLDER / "spot_check.xlsx"
MAX_FILE_MB = match.MAX_FILE_MB                                         # project rule: every file under 50 MB

REVIEW_METHOD = "automated two-pass"   # the value of the review_method column
DISAGREEMENT = " (disagreement)"       # added to the note where pass 1 and match.py disagree

SPOT_CHECK_SIZE = 10        # how many "same" pairs go into the spot check
SPOT_CHECK_SEED = 20261104  # fixed random seed (the date of the review), so the sample can be repeated
CHECKS = ["agree", "disagree"]  # the choices in the my_check dropdown

# The columns of the spot-check sheet, in this order:
#   (header in the sheet, column of match_verified.csv it comes from, width in characters)
SPOT_COLUMNS = [
    ("pair_id", "pair_id", 30),
    ("kalshi_title", "kalshi_title", 34),
    ("polymarket_title", "poly_question", 34),
    ("side", "poly_yes_equals", 11),       # Polymarket's YES equals "Kalshi YES" or "Kalshi NO"
    ("kalshi_rules", "kalshi_rules", 70),
    ("polymarket_rules", "poly_rules", 70),
    ("my_check", None, 12),                # yours: agree / disagree
]
SPOT_HEADERS = [header for header, _, _ in SPOT_COLUMNS]
MAX_ROW_HEIGHT = 409  # Excel's largest row height, in points
LINE_HEIGHT = 15      # points per line of wrapped text

INSTRUCTIONS = [
    "How to do the spot check",
    "",
    "Each row is a pair that the automated two-pass review judged the same. Read both rules texts and decide",
    "whether buying YES on one venue and the matching side on the other ALWAYS pays exactly once. Check:",
    "1. Same measured quantity: headline CPI-U (not core CPI), month-on-month vs year-on-year; for the Fed,",
    "   the change of the upper bound of the target range at that meeting.",
    "2. Same data month or the same FOMC meeting.",
    "3. Same threshold. CPI is published with one decimal, so Kalshi's 'more than 0.3%' = '0.4% or more'.",
    "4. Correct side. side = 'Kalshi YES': Polymarket's YES wins exactly when Kalshi's YES wins.",
    "   side = 'Kalshi NO': Polymarket's YES wins exactly when Kalshi's YES loses.",
    "Rare edge cases (for example a cancelled FOMC meeting or a delayed BLS release) are allowed.",
    "",
    "Choose 'agree' or 'disagree' in the column my_check, then save the file (same name, same folder).",
]


# ---------------------------------------------------------------------------
# Pass 2: combine the two verdicts
# ---------------------------------------------------------------------------

def combine(independent, suggested):
    """The combined verdict: "same" only if both say same; else "unsure" if either says unsure; else "different"."""
    if independent == "same" and suggested == "same":
        return "same"
    if "unsure" in (independent, suggested):
        return "unsure"
    return "different"


def read_independent_review(path):
    """Read independent_review.csv (pair_id, verdict, reason) as tidied-up text.

    Raises ValueError if a column is missing, or a pair_id is empty or
    repeated, or a verdict is not same, different or unsure.
    """
    review = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [column for column in ["pair_id", "verdict", "reason"] if column not in review.columns]
    if missing:
        raise ValueError(f"{path.name} has no column {', '.join(missing)}")
    review = pd.DataFrame({"pair_id": review["pair_id"].str.strip(),
                           "verdict": review["verdict"].str.strip().str.lower(),  # "Same " -> "same"
                           "reason": review["reason"].str.strip()})
    problems = [f"row {row}: the pair_id is empty" for row in review.index[review["pair_id"] == ""] + 2]
    problems += [f"{pid}: the verdict '{verdict}' is not same, different or unsure"
                 for pid, verdict in zip(review["pair_id"], review["verdict"]) if verdict not in VERDICTS]
    problems += [f"{pid}: appears more than once" for pid in review["pair_id"][review["pair_id"].duplicated()].unique()]
    if problems:
        raise ValueError(f"Please fix {path.name}:\n   " + "\n   ".join(problems))
    return review


def combined_review(independent, candidates):
    """The combined verdict and note of every candidate pair, as (pair_id, verdict, note).

    independent: the result of read_independent_review(). candidates:
    match_candidates.csv read as text. Raises ValueError unless both files
    hold exactly the same pairs and every suggested_verdict is valid.
    """
    ids = pd.Series([pair_id(k, p) for k, p in zip(candidates["kalshi_ticker"], candidates["poly_market_id"])],
                    index=candidates.index)
    problems = [f"{pid}: in match_candidates.csv but not reviewed" for pid in ids[~ids.isin(independent["pair_id"])]]
    problems += [f"{pid}: reviewed but not in match_candidates.csv"
                 for pid in independent["pair_id"][~independent["pair_id"].isin(ids)]]
    problems += [f"{pid}: suggested_verdict '{suggested}' is not same, different or unsure"
                 for pid, suggested in zip(ids, candidates["suggested_verdict"]) if suggested not in VERDICTS]
    if problems:
        raise ValueError("The independent review does not fit match_candidates.csv:\n   " + "\n   ".join(problems))

    mine = independent.set_index("pair_id").loc[ids]  # in the order of match_candidates.csv
    suggested = candidates["suggested_verdict"].tolist()
    verdicts = [combine(m, s) for m, s in zip(mine["verdict"], suggested)]
    notes = [reason + (DISAGREEMENT if m != s else "") for reason, m, s in zip(mine["reason"], mine["verdict"], suggested)]
    return pd.DataFrame({"pair_id": ids.tolist(), "verdict": verdicts, "note": notes})


def verified_table(independent, candidates):
    """match_verified.csv: read_review_sheet.py's format with the combined verdicts, plus review_method."""
    review = combined_review(independent, candidates)
    table, kept, not_reviewed = read_review_sheet.verified_table(review, candidates)
    assert kept == [] and not_reviewed == []  # combined_review() checked that both hold the same pairs
    table["review_method"] = REVIEW_METHOD
    return table


def disagreements(independent, candidates):
    """The pairs on which the independent review and match.py disagree, with both verdicts and both reasons."""
    table = candidates.copy()
    table.insert(0, "pair_id", [pair_id(k, p) for k, p in zip(table["kalshi_ticker"], table["poly_market_id"])])
    table = table.merge(independent.rename(columns={"verdict": "independent_verdict", "reason": "independent_reason"}),
                        on="pair_id")
    columns = ["pair_id", "independent_verdict", "suggested_verdict", "independent_reason", "suggested_reason"]
    return table.loc[table["independent_verdict"] != table["suggested_verdict"], columns].reset_index(drop=True)


def made_by_this_script(path):
    """True if there is no file yet, or if it is a match_verified.csv written by this script."""
    if not path.exists():
        return True
    table = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return "review_method" in table.columns and (table["review_method"] == REVIEW_METHOD).all()


# ---------------------------------------------------------------------------
# The spot check
# ---------------------------------------------------------------------------

def spot_check_rows(table, size=SPOT_CHECK_SIZE, seed=SPOT_CHECK_SEED):
    """`size` random pairs with verdict "same" (fixed seed), in the order of match_verified.csv."""
    same = table[table["verdict"] == "same"]
    if len(same) < size:
        raise ValueError(f"only {len(same)} pairs are 'same', fewer than the {size} wanted for the spot check")
    picked = same.sample(n=size, random_state=seed).sort_index()  # sort_index: back to the order of the file
    rows = pd.DataFrame(index=picked.index)
    for header, source, _ in SPOT_COLUMNS:
        rows[header] = picked[source] if source else ""  # my_check: empty, for you
    return rows.reset_index(drop=True)


def row_height(texts, widths):
    """Points needed to show every line of the longest text in a row, at most Excel's maximum."""
    lines = 1
    for text, width in zip(texts, widths):
        # Each paragraph wraps into ceil(length / width) lines; an empty line still takes one line.
        needed = sum(max(1, math.ceil(len(paragraph) / width)) for paragraph in str(text).split("\n"))
        lines = max(lines, needed)
    return min(MAX_ROW_HEIGHT, LINE_HEIGHT * lines + 5)


def save_spot_check(rows, path):
    """Write the spot-check sheet (and a sheet with instructions) to an Excel file."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "spot_check"
    sheet.append(SPOT_HEADERS)
    for row in rows[SPOT_HEADERS].itertuples(index=False):
        sheet.append([None if value == "" else value for value in row])  # None = a truly empty cell
    last_row = len(rows) + 1  # row 1 is the header

    # Every cell: long text wraps and starts at the top; thin light-grey lines between the cells.
    wrap = Alignment(wrap_text=True, vertical="top")
    grey = Side(style="thin", color="BFBFBF")
    border = Border(left=grey, right=grey, top=grey, bottom=grey)
    for cells in sheet.iter_rows(min_row=1, max_row=last_row, max_col=len(SPOT_HEADERS)):
        for cell in cells:
            cell.alignment = wrap
            cell.border = border
            if cell.data_type == "f":
                cell.data_type = "s"  # openpyxl takes text that starts with "=" for a formula: keep it text
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"  # the header stays in view

    # Column widths, and rows tall enough to read both rules texts without clicking.
    widths = [width for _, _, width in SPOT_COLUMNS]
    for number, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(number)].width = width
    for row_number, row in enumerate(rows[SPOT_HEADERS].itertuples(index=False), start=2):
        sheet.row_dimensions[row_number].height = row_height(row, widths)

    # Dropdown in my_check. Excel refuses anything else typed there; an empty cell is fine.
    letter = get_column_letter(SPOT_HEADERS.index("my_check") + 1)
    dropdown = DataValidation(type="list", formula1='"' + ",".join(CHECKS) + '"', allow_blank=True,
                              showErrorMessage=True, errorTitle="Not a check",
                              error="Choose agree or disagree from the list, or leave the cell empty.")
    dropdown.add(f"{letter}2:{letter}{last_row}")
    sheet.add_data_validation(dropdown)

    # A second sheet that explains what to check.
    help_sheet = workbook.create_sheet("instructions")
    for line in INSTRUCTIONS:
        help_sheet.append([line])
    help_sheet["A1"].font = Font(bold=True)
    help_sheet.column_dimensions["A"].width = 110

    workbook.save(path)
    size_mb = path.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        path.unlink()
        raise ValueError(f"{path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB limit")
    return path


def count_typed_checks(path):
    """How many rows of an existing spot-check sheet have my_check filled in (0 if there is no file)."""
    if not path.exists():
        return 0
    sheet = pd.read_excel(path, sheet_name="spot_check", dtype=str, keep_default_na=False)
    return int((sheet["my_check"].str.strip() != "").sum()) if "my_check" in sheet.columns else 0


# ---------------------------------------------------------------------------
# Run it
# ---------------------------------------------------------------------------

def main():
    overwrite = "--overwrite" in sys.argv[1:]
    for path, made_by in [(CANDIDATES_FILE, "python src/match.py"), (INDEPENDENT_FILE, "the independent review")]:
        if not path.exists():
            sys.exit(f"Missing {path.relative_to(PROJECT_ROOT)} (made by {made_by}).")
    candidates = match.read_review_file(CANDIDATES_FILE)
    try:
        independent = read_independent_review(INDEPENDENT_FILE)
        table = verified_table(independent, candidates)
        spot = spot_check_rows(table)
    except ValueError as problem:
        sys.exit(f"Stopped: {problem}")

    # Check both files before writing either, so a stop never leaves one of them half-updated.
    if not overwrite and not made_by_this_script(VERIFIED_FILE):
        sys.exit(f"Stopped: {VERIFIED_FILE.name} holds verdicts that this script did not make (your own review?).\n"
                 f"   To replace it anyway:  python src/apply_independent_review.py --overwrite")
    typed = count_typed_checks(SPOT_CHECK_FILE)
    if typed and not overwrite:
        sys.exit(f"Stopped: you already filled in my_check in {typed} rows of {SPOT_CHECK_FILE.name}.\n"
                 f"   To replace it anyway:  python src/apply_independent_review.py --overwrite")
    read_review_sheet.save_verified(table, VERIFIED_FILE)
    save_spot_check(spot, SPOT_CHECK_FILE)

    def counts(verdicts):
        return ", ".join(f"{(verdicts == v).sum()} {v}" for v in VERDICTS)

    print(f"Independent review (pass 1): {counts(independent['verdict'])}")
    print(f"match.py's suggestions:      {counts(candidates['suggested_verdict'])}")
    print(f"Combined (pass 2):           {counts(table['verdict'])}")
    differ = disagreements(independent, candidates)
    print(f"\n{len(differ)} disagreements:")
    for row in differ.itertuples(index=False):
        print(f"* {row.pair_id}\n"
              f"    independent: {row.independent_verdict} - {row.independent_reason}\n"
              f"    match.py:    {row.suggested_verdict} - {row.suggested_reason}")
    print(f"\n{VERIFIED_FILE.relative_to(PROJECT_ROOT)}: {len(table)} rows")
    print(f"{SPOT_CHECK_FILE.relative_to(PROJECT_ROOT)}: {len(spot)} random 'same' pairs (seed {SPOT_CHECK_SEED}): "
          f"{', '.join(spot['pair_id'])}")


if __name__ == "__main__":
    main()
