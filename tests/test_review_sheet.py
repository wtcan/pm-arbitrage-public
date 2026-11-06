"""
Unit tests for src/make_review_sheet.py and src/read_review_sheet.py.
Run them from the project folder with:  pytest

None of these tests use the internet or the real data files. Each test
writes a small match_candidates.csv (4 pairs) into a temporary folder, turns
it into a review sheet, types verdicts into the sheet the way you would in
Excel (with openpyxl), and reads them back.
"""

import pandas as pd
import pytest
from openpyxl import load_workbook

import make_review_sheet
import match
import read_review_sheet

# Four candidate pairs, in the order match.py writes them (by date):
#   (kalshi_ticker, poly_market_id, poly_yes_equals, suggested_verdict, difference_score, differences)
# Their scores are 3, 26, 8 and 3, so the sheet must list them as 654401 (26), 2118742 (8),
# then 655625 and 2118744 (both 3, kept in the order of the CSV).
PAIRS = [
    ("KXCPI-25OCT-T0.4", "655625", "Kalshi YES", "same", "3",
     "[2] postponed_release: Polymarket waits | Kalshi extends | [1] precision: one decimal | [0] revisions: none"),
    ("KXCPIYOY-25OCT-T3.1", "654401", "Kalshi YES", "different", "26",
     "[10] paid_differently: settled differently | [6] settled_on_different_values: 3.0% vs 3.2%"),
    ("KXFED-26SEP-T3.25", "2118742", "Kalshi NO", "unsure", "8",
     "[4] level_vs_change: level vs change | [4] cancelled_meeting: differs"),
    ("KXFEDDECISION-26SEP-H0", "2118744", "Kalshi YES", "same", "3",
     "[2] cancelled_meeting: differs | [1] rounding: rounds up"),
]
NEW_PAIR = ("KXCPI-25NOV-T0.1", "680561", "Kalshi NO", "same", "4", "[2] postponed_release: differs")

HEADERS = ["pair_id", "kalshi_title", "polymarket_title", "side", "suggested_verdict", "reason", "differences",
           "kalshi_rules", "polymarket_rules", "verdict", "note"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_candidates(folder, pairs=PAIRS):
    """A small match_candidates.csv in match.py's format (all its columns; verdict and note empty)."""
    rows = [{"kalshi_ticker": ticker, "poly_market_id": poly_id, "poly_yes_equals": side,
             "suggested_verdict": suggested, "suggested_reason": f"Reason for {ticker}",
             "difference_score": score, "differences": differences,
             "kalshi_title": f"Kalshi title {ticker}", "poly_question": f"Polymarket question {poly_id}",
             "kalshi_rules": f"Kalshi rules of {ticker}.\n\nSecond paragraph.",
             "poly_rules": f"Polymarket rules of {poly_id}.", "verdict": "", "note": ""}
            for ticker, poly_id, side, suggested, score, differences in pairs]
    return match.save_review_file(pd.DataFrame(rows), folder / "match_candidates.csv")


def make_test_sheet(folder, pairs=PAIRS, overwrite=False):
    """Write the small CSV and make the review sheet from it; returns the sheet's path."""
    review_path = folder / "match_review.xlsx"
    make_review_sheet.make_sheet(write_candidates(folder, pairs), review_path, overwrite=overwrite)
    return review_path


def open_sheet(path):
    return load_workbook(path).active


def column(sheet, header):
    """The values of one column of the sheet (below the header), found by its header."""
    number = [cell.value for cell in sheet[1]].index(header) + 1
    return [sheet.cell(row, number).value for row in range(2, sheet.max_row + 1)]


def type_in(path, answers):
    """Type verdicts and notes into the sheet, like you would in Excel. answers: {pair_id: (verdict, note)}."""
    workbook = load_workbook(path)
    sheet = workbook.active
    headers = [cell.value for cell in sheet[1]]
    for row in range(2, sheet.max_row + 1):
        pid = sheet.cell(row, headers.index("pair_id") + 1).value
        if pid in answers:
            verdict, note = answers[pid]
            sheet.cell(row, headers.index("verdict") + 1).value = verdict
            sheet.cell(row, headers.index("note") + 1).value = note
    workbook.save(path)


def read_back(folder):
    """Read the filled-in sheet back. Returns (match_verified table, pairs kept, pairs not in the sheet)."""
    review = read_review_sheet.read_verdicts(folder / "match_review.xlsx")
    candidates = match.read_review_file(folder / "match_candidates.csv")
    return read_review_sheet.verified_table(review, candidates)


# ---------------------------------------------------------------------------
# make_review_sheet.py
# ---------------------------------------------------------------------------

def test_pair_id_is_kalshi_ticker_slash_polymarket_id():
    assert make_review_sheet.pair_id("KXCPI-25OCT-T0.4", "655625") == "KXCPI-25OCT-T0.4 / 655625"


def test_each_flag_goes_on_its_own_line():
    # The "|" inside the first explanation is not followed by "[points]", so it stays.
    text = "[2] postponed_release: Polymarket waits | Kalshi extends | [1] precision: one decimal | [0] revisions: none"
    assert make_review_sheet.one_flag_per_line(text) == \
        "[2] postponed_release: Polymarket waits | Kalshi extends\n[1] precision: one decimal\n[0] revisions: none"


def test_sheet_has_the_eleven_columns_and_the_highest_score_first(tmp_path):
    sheet = open_sheet(make_test_sheet(tmp_path))
    assert [cell.value for cell in sheet[1]] == HEADERS
    assert column(sheet, "pair_id") == ["KXCPIYOY-25OCT-T3.1 / 654401", "KXFED-26SEP-T3.25 / 2118742",
                                        "KXCPI-25OCT-T0.4 / 655625", "KXFEDDECISION-26SEP-H0 / 2118744"]
    assert column(sheet, "side") == ["Kalshi YES", "Kalshi NO", "Kalshi YES", "Kalshi YES"]
    assert column(sheet, "suggested_verdict") == ["different", "unsure", "same", "same"]
    assert column(sheet, "kalshi_title")[0] == "Kalshi title KXCPIYOY-25OCT-T3.1"
    assert column(sheet, "polymarket_title")[0] == "Polymarket question 654401"
    assert column(sheet, "reason")[0] == "Reason for KXCPIYOY-25OCT-T3.1"
    assert column(sheet, "differences")[0] == \
        "[10] paid_differently: settled differently\n[6] settled_on_different_values: 3.0% vs 3.2%"
    assert column(sheet, "kalshi_rules")[0] == "Kalshi rules of KXCPIYOY-25OCT-T3.1.\n\nSecond paragraph."
    assert column(sheet, "polymarket_rules")[0] == "Polymarket rules of 654401."
    assert column(sheet, "verdict") == [None] * 4  # empty, for you
    assert column(sheet, "note") == [None] * 4


def test_sheet_layout(tmp_path):
    sheet = open_sheet(make_test_sheet(tmp_path))
    assert sheet.freeze_panes == "A2"  # the top row is frozen
    assert all(cell.font.b for cell in sheet[1])  # bold header
    assert all(cell.alignment.wrap_text for row in sheet.iter_rows() for cell in row)
    assert [sheet.row_dimensions[row].height for row in range(2, 6)] == [make_review_sheet.ROW_HEIGHT] * 4
    widths = [sheet.column_dimensions[letter].width for letter in "ABCDEFGHIJK"]
    assert widths == [width for _, _, width in make_review_sheet.COLUMNS]


def test_rows_are_coloured_by_the_suggested_verdict(tmp_path):
    sheet = open_sheet(make_test_sheet(tmp_path))
    # In score order the rows are: different (red), unsure (yellow), same, same (white). All 11 cells.
    for row, colour in zip(range(2, 6), ["FFC7CE", "FFEB9C", "FFFFFF", "FFFFFF"]):
        assert {cell.fill.fgColor.rgb[-6:] for cell in sheet[row]} == {colour}


def test_verdict_column_has_a_dropdown(tmp_path):
    sheet = open_sheet(make_test_sheet(tmp_path))
    [dropdown] = sheet.data_validations.dataValidation
    assert dropdown.type == "list"
    assert dropdown.formula1 == '"same,different,unsure"'
    assert str(dropdown.sqref) == "J2:J5"  # the verdict column, every pair's row
    assert dropdown.showErrorMessage and dropdown.allow_blank


def test_text_that_starts_with_an_equals_sign_stays_text(tmp_path):
    pairs = [("KXFED-26SEP-T3.25", "2118742", "Kalshi NO", "same", "1", "=0 bp is not a formula")]
    sheet = open_sheet(make_test_sheet(tmp_path, pairs))
    cell = sheet.cell(2, HEADERS.index("differences") + 1)
    assert (cell.value, cell.data_type) == ("=0 bp is not a formula", "s")


def test_making_the_sheet_again_does_not_delete_your_verdicts(tmp_path):
    path = make_test_sheet(tmp_path)
    make_test_sheet(tmp_path)  # nothing typed yet: making it again is fine
    type_in(path, {"KXCPI-25OCT-T0.4 / 655625": ("same", None)})
    with pytest.raises(FileExistsError):
        make_test_sheet(tmp_path)
    assert column(open_sheet(path), "verdict").count("same") == 1  # your verdict is still there
    make_test_sheet(tmp_path, overwrite=True)  # ... unless you ask for a new sheet
    assert column(open_sheet(path), "verdict") == [None] * 4


# ---------------------------------------------------------------------------
# read_review_sheet.py
# ---------------------------------------------------------------------------

def test_your_verdicts_and_notes_come_back(tmp_path):
    path = make_test_sheet(tmp_path)
    type_in(path, {"KXCPIYOY-25OCT-T3.1 / 654401": ("different", "shutdown: no October CPI"),
                   "KXCPI-25OCT-T0.4 / 655625": (" Same ", "N/A"),       # messy typing is tidied up
                   "KXFED-26SEP-T3.25 / 2118742": (None, "look again")})  # a note without a verdict
    table, kept, not_in_sheet = read_back(tmp_path)

    candidates = match.read_review_file(tmp_path / "match_candidates.csv")
    assert list(table.columns) == ["pair_id"] + list(candidates.columns)
    assert table["kalshi_ticker"].tolist() == candidates["kalshi_ticker"].tolist()  # the order of the CSV
    assert table["verdict"].tolist() == ["same", "different", "", ""]
    assert table["note"].tolist() == ["N/A", "shutdown: no October CPI", "look again", ""]
    assert (kept, not_in_sheet) == ([], [])

    saved = read_review_sheet.save_verified(table, tmp_path / "match_verified.csv")
    again = pd.read_csv(saved, dtype=str, keep_default_na=False)
    pd.testing.assert_frame_equal(again, table, check_dtype=False)


def test_a_wrong_verdict_stops_with_the_row_to_fix(tmp_path):
    path = make_test_sheet(tmp_path)
    type_in(path, {"KXFED-26SEP-T3.25 / 2118742": ("yes", None)})  # row 3 of the sheet
    with pytest.raises(ValueError, match=r"row 3 \(KXFED-26SEP-T3.25 / 2118742\): the verdict 'yes'"):
        read_review_sheet.read_verdicts(path)


def test_a_repeated_or_missing_pair_id_stops(tmp_path):
    path = make_test_sheet(tmp_path)
    workbook = load_workbook(path)
    sheet = workbook.active
    sheet["A5"] = sheet["A2"].value  # row 2 copied over row 5
    sheet["J7"] = "same"             # a verdict below the table (row 6 stays empty and is skipped)
    workbook.save(path)
    with pytest.raises(ValueError) as problem:
        read_review_sheet.read_verdicts(path)
    assert "rows 2, 5: the same pair_id KXCPIYOY-25OCT-T3.1 / 654401" in str(problem.value)
    assert "row 7: the pair_id is empty" in str(problem.value)
    assert "row 6" not in str(problem.value)


def test_a_pair_that_match_py_no_longer_finds_keeps_your_verdict(tmp_path):
    path = make_test_sheet(tmp_path)
    type_in(path, {"KXFEDDECISION-26SEP-H0 / 2118744": ("same", "fine")})
    # match.py is run again: two pairs are gone (one with a verdict, one without), one pair is new.
    write_candidates(tmp_path, [PAIRS[0], PAIRS[1], NEW_PAIR])
    table, kept, not_in_sheet = read_back(tmp_path)

    assert kept == ["KXFEDDECISION-26SEP-H0 / 2118744"]
    assert not_in_sheet == ["KXCPI-25NOV-T0.1 / 680561"]
    assert table["pair_id"].tolist() == ["KXCPI-25OCT-T0.4 / 655625", "KXCPIYOY-25OCT-T3.1 / 654401",
                                         "KXCPI-25NOV-T0.1 / 680561", "KXFEDDECISION-26SEP-H0 / 2118744"]
    last = table.iloc[-1]
    assert (last.kalshi_ticker, last.poly_market_id, last.verdict, last.note) == \
        ("KXFEDDECISION-26SEP-H0", "2118744", "same", "fine")
    assert last.suggested_reason.startswith("NO LONGER FOUND")
    assert table["verdict"].tolist() == ["", "", "", "same"]
