"""
Unit tests for src/apply_independent_review.py.
Run them from the project folder with:  pytest

None of these tests use the internet or the real data files. Each test
writes a small match_candidates.csv and a small independent review into a
temporary folder.
"""

import pandas as pd
import pytest
from openpyxl import load_workbook

import apply_independent_review as air
import match
import read_review_sheet

# Five candidate pairs: (kalshi_ticker, poly_market_id, suggested_verdict, independent verdict)
PAIRS = [
    ("KXCPI-25OCT-T0.4", "655625", "unsure", "different"),     # disagreement -> unsure
    ("KXCPIYOY-25OCT-T3.1", "654401", "different", "different"),
    ("KXFED-26SEP-T3.25", "2252242", "unsure", "unsure"),
    ("KXFEDDECISION-26SEP-H0", "2252244", "same", "same"),
    ("KXCPI-26OCT-T0.1", "5691223", "same", "unsure"),          # disagreement -> unsure
]


def write_candidates(folder, pairs=PAIRS):
    """A small match_candidates.csv in match.py's format, read back as text."""
    rows = [{"kalshi_ticker": ticker, "poly_market_id": poly_id, "poly_yes_equals": "Kalshi YES",
             "suggested_verdict": suggested, "suggested_reason": f"Suggested reason for {ticker}",
             "kalshi_title": f"Kalshi title {ticker}", "poly_question": f"Polymarket question {poly_id}",
             "kalshi_rules": f"Kalshi rules of {ticker}.", "poly_rules": f"Polymarket rules of {poly_id}.",
             "verdict": "", "note": ""}
            for ticker, poly_id, suggested, _ in pairs]
    path = match.save_review_file(pd.DataFrame(rows), folder / "match_candidates.csv")
    return match.read_review_file(path)


def independent_review(pairs=PAIRS):
    return pd.DataFrame({"pair_id": [f"{t} / {p}" for t, p, _, _ in pairs],
                         "verdict": [mine for _, _, _, mine in pairs],
                         "reason": [f"My reason for {t}" for t, _, _, _ in pairs]})


# ---------------------------------------------------------------------------
# Combining the two verdicts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("independent, suggested, combined", [
    ("same", "same", "same"),            # the only way to get "same"
    ("same", "unsure", "unsure"), ("unsure", "same", "unsure"),
    ("same", "different", "different"), ("different", "same", "different"),
    ("different", "unsure", "unsure"), ("unsure", "different", "unsure"),
    ("unsure", "unsure", "unsure"), ("different", "different", "different"),
])
def test_combined_verdict(independent, suggested, combined):
    assert air.combine(independent, suggested) == combined


def test_verified_table_has_read_review_sheets_format_plus_review_method(tmp_path):
    candidates = write_candidates(tmp_path)
    table = air.verified_table(independent_review(), candidates)

    assert list(table.columns) == ["pair_id"] + list(candidates.columns) + ["review_method"]
    assert table["kalshi_ticker"].tolist() == candidates["kalshi_ticker"].tolist()  # the order of the CSV
    assert table["pair_id"].iloc[0] == "KXCPI-25OCT-T0.4 / 655625"
    assert table["verdict"].tolist() == ["unsure", "different", "unsure", "same", "unsure"]
    assert table["note"].tolist() == [
        "My reason for KXCPI-25OCT-T0.4 (disagreement)",
        "My reason for KXCPIYOY-25OCT-T3.1",
        "My reason for KXFED-26SEP-T3.25",
        "My reason for KXFEDDECISION-26SEP-H0",
        "My reason for KXCPI-26OCT-T0.1 (disagreement)",
    ]
    assert set(table["review_method"]) == {"automated two-pass"}
    assert table["suggested_verdict"].tolist() == [s for _, _, s, _ in PAIRS]  # the suggestion is kept as it was

    saved = read_review_sheet.save_verified(table, tmp_path / "match_verified.csv")
    again = pd.read_csv(saved, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    pd.testing.assert_frame_equal(again, table, check_dtype=False)


def test_disagreements_show_both_verdicts_and_both_reasons(tmp_path):
    differ = air.disagreements(independent_review(), write_candidates(tmp_path))
    assert differ["pair_id"].tolist() == ["KXCPI-25OCT-T0.4 / 655625", "KXCPI-26OCT-T0.1 / 5691223"]
    assert differ.iloc[0].tolist() == ["KXCPI-25OCT-T0.4 / 655625", "different", "unsure",
                                       "My reason for KXCPI-25OCT-T0.4", "Suggested reason for KXCPI-25OCT-T0.4"]


def test_a_pair_without_an_independent_verdict_stops(tmp_path):
    candidates = write_candidates(tmp_path)
    with pytest.raises(ValueError, match=r"KXCPI-26OCT-T0.1 / 5691223: in match_candidates.csv but not reviewed"):
        air.verified_table(independent_review(PAIRS[:4]), candidates)
    extra = pd.concat([independent_review(), pd.DataFrame({"pair_id": ["X / 1"], "verdict": ["same"],
                                                           "reason": ["?"]})])
    with pytest.raises(ValueError, match=r"X / 1: reviewed but not in match_candidates.csv"):
        air.verified_table(extra, candidates)


def test_the_independent_review_is_tidied_and_checked(tmp_path):
    path = tmp_path / "independent_review.csv"
    pd.DataFrame({"pair_id": [" A / 1 ", "B / 2"], "verdict": [" Same ", "unsure"], "reason": ["r1 ", "r2"]}) \
        .to_csv(path, index=False, encoding="utf-8-sig")
    review = air.read_independent_review(path)
    assert review.values.tolist() == [["A / 1", "same", "r1"], ["B / 2", "unsure", "r2"]]

    pd.DataFrame({"pair_id": ["A / 1", "A / 1"], "verdict": ["yes", "same"], "reason": ["", ""]}) \
        .to_csv(path, index=False)
    with pytest.raises(ValueError) as problem:
        air.read_independent_review(path)
    assert "A / 1: the verdict 'yes' is not same, different or unsure" in str(problem.value)
    assert "A / 1: appears more than once" in str(problem.value)


def test_your_own_match_verified_csv_is_not_overwritten(tmp_path):
    path = tmp_path / "match_verified.csv"
    assert air.made_by_this_script(path)  # no file yet
    candidates = write_candidates(tmp_path)
    read_review_sheet.save_verified(air.verified_table(independent_review(), candidates), path)
    assert air.made_by_this_script(path)
    review = pd.DataFrame({"pair_id": ["KXCPI-25OCT-T0.4 / 655625"], "verdict": ["same"], "note": [""]})
    own, _, _ = read_review_sheet.verified_table(review, candidates)  # what read_review_sheet.py writes
    read_review_sheet.save_verified(own, path)
    assert not air.made_by_this_script(path)


# ---------------------------------------------------------------------------
# The spot check
# ---------------------------------------------------------------------------

def verified_with_same_pairs(count_same=15, count_other=5):
    """A verified table with `count_same` "same" pairs and `count_other` "unsure" pairs, mixed."""
    rows = []
    for number in range(count_same + count_other):
        verdict = "unsure" if number % 4 == 3 and count_other > 0 else "same"
        if verdict == "unsure":
            count_other -= 1
        rows.append({"pair_id": f"K{number} / {number}", "kalshi_title": f"Kalshi title {number}",
                     "poly_question": f"Polymarket question {number}", "poly_yes_equals": "Kalshi NO",
                     "kalshi_rules": f"Kalshi rules {number}.\n\nMore.", "poly_rules": "Polymarket rules " * 30,
                     "verdict": verdict})
    return pd.DataFrame(rows)


def test_spot_check_takes_ten_same_pairs_with_a_fixed_seed():
    table = verified_with_same_pairs()
    rows = air.spot_check_rows(table)
    assert list(rows.columns) == air.SPOT_HEADERS
    assert len(rows) == 10 and rows["pair_id"].is_unique
    same_ids = table.loc[table["verdict"] == "same", "pair_id"].tolist()
    assert set(rows["pair_id"]) <= set(same_ids)                                   # only "same" pairs
    assert rows["pair_id"].tolist() == [p for p in same_ids if p in set(rows["pair_id"])]  # in file order
    assert rows["pair_id"].tolist() == air.spot_check_rows(table)["pair_id"].tolist()  # same seed, same pairs
    assert rows["my_check"].tolist() == [""] * 10
    assert rows["side"].tolist() == ["Kalshi NO"] * 10
    assert rows["polymarket_title"].iloc[0].startswith("Polymarket question")


def test_spot_check_needs_enough_same_pairs():
    with pytest.raises(ValueError, match="only 9 pairs are 'same'"):
        air.spot_check_rows(verified_with_same_pairs(count_same=9, count_other=0))


def test_spot_check_sheet(tmp_path):
    path = air.save_spot_check(air.spot_check_rows(verified_with_same_pairs()), tmp_path / "spot_check.xlsx")
    workbook = load_workbook(path)
    assert workbook.sheetnames == ["spot_check", "instructions"]
    sheet = workbook["spot_check"]
    assert [cell.value for cell in sheet[1]] == ["pair_id", "kalshi_title", "polymarket_title", "side",
                                                 "kalshi_rules", "polymarket_rules", "my_check"]
    assert sheet.max_row == 11
    assert [sheet.cell(row, 7).value for row in range(2, 12)] == [None] * 10  # my_check is empty
    assert sheet.cell(2, 5).value.startswith("Kalshi rules")                  # the full rules texts
    [dropdown] = sheet.data_validations.dataValidation
    assert (dropdown.type, dropdown.formula1, str(dropdown.sqref)) == ("list", '"agree,disagree"', "G2:G11")
    assert sheet.freeze_panes == "A2"
    assert all(cell.alignment.wrap_text for row in sheet.iter_rows() for cell in row)
    assert all(15 < sheet.row_dimensions[row].height <= 409 for row in range(2, 12))


def test_row_height_grows_with_the_text_up_to_excels_maximum():
    assert air.row_height(["short"], [70]) == 20                 # one line
    assert air.row_height(["a" * 140 + "\n\n" + "b"], [70]) == 65  # 2 + 1 (empty) + 1 lines
    assert air.row_height(["x" * 100_000], [70]) == 409


def test_checks_you_typed_are_counted(tmp_path):
    path = tmp_path / "spot_check.xlsx"
    assert air.count_typed_checks(path) == 0  # no file yet
    air.save_spot_check(air.spot_check_rows(verified_with_same_pairs()), path)
    assert air.count_typed_checks(path) == 0
    workbook = load_workbook(path)
    workbook["spot_check"].cell(3, 7).value = "agree"
    workbook.save(path)
    assert air.count_typed_checks(path) == 1
