"""
make_review_sheet.py - Turn match_candidates.csv into an Excel sheet for checking the pairs by hand.

How to run it (from the project folder):
    python src/make_review_sheet.py

It reads  data/processed/match_candidates.csv   (made by src/match.py)
and makes data/processed/match_review.xlsx      (one row per candidate pair)

What the sheet looks like:
  * one row per pair, sorted by difference_score, highest first, so the
    trickiest pairs come first. Pairs with the same score keep the order of
    match_candidates.csv (by meeting or release date);
  * 11 columns: pair_id, kalshi_title, polymarket_title, side,
    suggested_verdict, reason, differences (one flag per line),
    kalshi_rules, polymarket_rules, verdict and note;
  * "verdict" and "note" are EMPTY: they are for you. "verdict" has a
    dropdown with same / different / unsure;
  * each row is coloured by match.py's suggested verdict:
    light red = different, light yellow = unsure, white = same;
  * the header row is bold and stays in view when you scroll down, every row
    has the same height, and long texts wrap inside their cell. Click a cell
    to read all of its text in the formula bar (or drag the row taller).

pair_id is "Kalshi ticker / Polymarket market id", e.g. "KXCPI-25OCT-T0.4 / 655625".
It stays the same when match.py is run again, so your verdicts can always be
matched to the right pair.

When you have filled in the sheet, save it (same name, same folder) and run
    python src/read_review_sheet.py
to save your verdicts in data/processed/match_verified.csv.

Safety: if match_review.xlsx already has a verdict or a note typed in, this
script stops instead of overwriting your work. To replace the file anyway:
    python src/make_review_sheet.py --overwrite

Excel files are written with openpyxl: https://openpyxl.readthedocs.io/en/stable/
(dropdown lists: https://openpyxl.readthedocs.io/en/stable/validation.html)
"""

import re
import sys

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

import match  # for the file locations and to read match_candidates.csv the same way match.py does

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

PROJECT_ROOT = match.PROJECT_ROOT
CANDIDATES_FILE = match.CANDIDATES_FILE                     # data/processed/match_candidates.csv
REVIEW_FILE = match.PROCESSED_FOLDER / "match_review.xlsx"
MAX_FILE_MB = match.MAX_FILE_MB                             # project rule: every file under 50 MB

VERDICTS = ["same", "different", "unsure"]  # the choices in the verdict dropdown, in this order

# The columns of the sheet, in this order:
#   (header in the sheet, column of match_candidates.csv it comes from, width in characters)
# pair_id is made by this script; verdict and note start empty, so they have no source column.
COLUMNS = [
    ("pair_id", None, 34),
    ("kalshi_title", "kalshi_title", 40),
    ("polymarket_title", "poly_question", 40),
    ("side", "poly_yes_equals", 12),            # Polymarket's YES equals "Kalshi YES" or "Kalshi NO"
    ("suggested_verdict", "suggested_verdict", 18),
    ("reason", "suggested_reason", 50),         # the one-line reason for the suggestion
    ("differences", "differences", 80),         # the flags, one per line, most points first
    ("kalshi_rules", "kalshi_rules", 70),
    ("polymarket_rules", "poly_rules", 70),
    ("verdict", None, 12),                      # yours: same / different / unsure
    ("note", None, 40),                         # yours: anything you like
]
HEADERS = [header for header, _, _ in COLUMNS]

ROW_HEIGHT = 90     # height of every pair's row, in points: about 6 lines of text
HEADER_HEIGHT = 20  # height of the header row: one line

# Row colours by suggested verdict, as hex RGB codes. Light shades, so the text stays easy to read.
ROW_COLOURS = {"different": "FFC7CE", "unsure": "FFEB9C", "same": "FFFFFF"}  # light red, light yellow, white
WHITE = "FFFFFF"


# ---------------------------------------------------------------------------
# The rows of the sheet
# ---------------------------------------------------------------------------

def pair_id(kalshi_ticker, poly_market_id):
    """The id of one pair: "Kalshi ticker / Polymarket market id", e.g. "KXCPI-25OCT-T0.4 / 655625"."""
    return f"{kalshi_ticker} / {poly_market_id}"


def one_flag_per_line(differences):
    """Put each flag of match.py's "differences" column on its own line.

    match.py separates the flags with " | ". The text is split only where the
    next flag's "[points]" follows, so a "|" inside an explanation stays.
    """
    return "\n".join(re.split(r" \| (?=\[\d+\] )", differences))


def review_table(candidates):
    """The rows of the review sheet: its 11 columns, sorted by difference_score (highest first).

    candidates is match_candidates.csv read as text. Pairs with the same score
    keep their order in the CSV ("stable" sorting).
    """
    candidates = candidates.copy()
    candidates["score"] = pd.to_numeric(candidates["difference_score"], errors="coerce")
    candidates = candidates.sort_values("score", ascending=False, kind="stable").reset_index(drop=True)

    table = pd.DataFrame(index=candidates.index)
    for header, source, _ in COLUMNS:
        if header == "pair_id":
            table[header] = [pair_id(k, p) for k, p in zip(candidates["kalshi_ticker"], candidates["poly_market_id"])]
        elif header == "differences":
            table[header] = candidates[source].map(one_flag_per_line)
        elif source:
            table[header] = candidates[source]
        else:
            table[header] = ""  # verdict and note: empty, for you to fill in
    return table


# ---------------------------------------------------------------------------
# The Excel file
# ---------------------------------------------------------------------------

def save_review_sheet(table, path):
    """Write the rows to an Excel file with the layout described at the top of this file."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "review"
    sheet.append(HEADERS)
    for row in table[HEADERS].itertuples(index=False):
        sheet.append([None if value == "" else value for value in row])  # None = a truly empty cell
    last_row = len(table) + 1  # row 1 is the header

    # Every cell: long text wraps and starts at the top; thin light-grey lines between the cells.
    wrap = Alignment(wrap_text=True, vertical="top")
    grey = Side(style="thin", color="BFBFBF")
    border = Border(left=grey, right=grey, top=grey, bottom=grey)
    for cells in sheet.iter_rows(min_row=1, max_row=last_row, max_col=len(HEADERS)):
        for cell in cells:
            cell.alignment = wrap
            cell.border = border
            if cell.data_type == "f":
                cell.data_type = "s"  # openpyxl takes text that starts with "=" for a formula: keep it text

    # Header: bold, and frozen, so it stays in view when you scroll down.
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.row_dimensions[1].height = HEADER_HEIGHT
    sheet.freeze_panes = "A2"

    # Each pair's row: a fixed height, coloured by the suggested verdict.
    for row_number, suggested in enumerate(table["suggested_verdict"], start=2):
        fill = PatternFill(fill_type="solid", fgColor=ROW_COLOURS.get(suggested, WHITE))
        for cell in sheet[row_number]:
            cell.fill = fill
        sheet.row_dimensions[row_number].height = ROW_HEIGHT

    # Column widths.
    for number, (_, _, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(number)].width = width

    # Dropdown in the verdict column. Excel refuses anything else typed there; an empty cell is fine.
    verdict_letter = get_column_letter(HEADERS.index("verdict") + 1)
    dropdown = DataValidation(type="list", formula1='"' + ",".join(VERDICTS) + '"', allow_blank=True,
                              showErrorMessage=True, errorTitle="Not a verdict",
                              error="Choose same, different or unsure from the list, or leave the cell empty.")
    dropdown.add(f"{verdict_letter}2:{verdict_letter}{last_row}")
    sheet.add_data_validation(dropdown)

    # Filter arrows in the header, e.g. to show only the "unsure" pairs.
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{last_row}"

    workbook.save(path)
    size_mb = path.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        path.unlink()
        raise ValueError(f"{path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB limit")
    return path


def count_typed_rows(path):
    """How many rows of an existing review sheet have a verdict or a note typed in (0 if there is no file)."""
    if not path.exists():
        return 0
    sheet = pd.read_excel(path, dtype=str, keep_default_na=False)
    typed = pd.Series(False, index=sheet.index)
    for column in ["verdict", "note"]:
        if column in sheet.columns:
            typed = typed | (sheet[column].str.strip() != "")
    return int(typed.sum())


def make_sheet(candidates_path=CANDIDATES_FILE, review_path=REVIEW_FILE, overwrite=False):
    """Make the review sheet from match_candidates.csv. Returns its rows as a table.

    Stops (FileExistsError) if the sheet already has verdicts or notes typed
    in, unless overwrite=True.
    """
    typed = count_typed_rows(review_path)
    if typed and not overwrite:
        raise FileExistsError(
            f"{review_path.name} already has a verdict or note in {typed} rows, and making it again would "
            f"delete them.\n"
            f"   To save your verdicts:       python src/read_review_sheet.py\n"
            f"   To replace the file anyway:  python src/make_review_sheet.py --overwrite")
    candidates = match.read_review_file(candidates_path)
    if candidates.empty:
        raise ValueError(f"{candidates_path.name} has no pairs: nothing to review")
    table = review_table(candidates)
    save_review_sheet(table, review_path)
    return table


def main():
    if not CANDIDATES_FILE.exists():
        sys.exit(f"Missing {CANDIDATES_FILE.relative_to(PROJECT_ROOT)}: run python src/match.py first.")
    try:
        table = make_sheet(overwrite="--overwrite" in sys.argv[1:])
    except (FileExistsError, ValueError) as problem:
        sys.exit(f"Stopped: {problem}")

    counts = table["suggested_verdict"].value_counts()
    print(f"{REVIEW_FILE.relative_to(PROJECT_ROOT)}: {len(table)} pairs, sorted by difference_score "
          f"(highest first)")
    print(f"   suggested verdicts: {counts.get('different', 0)} different (red), "
          f"{counts.get('unsure', 0)} unsure (yellow), {counts.get('same', 0)} same (white)")
    print("Fill in 'verdict' and 'note', save the file, then run:  python src/read_review_sheet.py")


if __name__ == "__main__":
    main()
