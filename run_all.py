"""
run_all.py - Rebuild every table, figure and results file of the project, in order.

How to run it (from the project folder, after  pip install -r requirements.txt):

    python run_all.py                 # everything from scratch: downloads, matching,
                                      # verification, pairs, trades, notebook (a few hours)
    python run_all.py --no-download   # skip the four download steps and rebuild
                                      # everything from the tables already in data/processed/
                                      # (a few minutes; this reproduces the numbers in docs/)
    python run_all.py --from match    # start at one step, e.g. after fixing something
    python run_all.py --list          # show the steps and stop

The steps, in order (each one is an ordinary script you can also run by hand;
--no-download skips the steps marked [download]):

    tests       pytest: check the code before spending time on downloads
    check       src/check_connection.py      can we reach both venues' public APIs?                  [download]
    kalshi      src/download_kalshi.py       Kalshi Fed and CPI markets, order books, hourly candles  [download]
    polymarket  src/download_polymarket.py   Polymarket Fed and CPI markets, order books, prices     [download]
    match       src/match.py                 candidate pairs with the same outcome on both venues
    verify      src/apply_independent_review.py   two-pass verification -> match_verified.csv
    pairs       src/build_pairs.py           the verified pairs -> pairs.parquet
    trades      src/download_poly_trades.py  every Polymarket trade of the verified pairs, hourly bars [download]
    arb         src/arb.py                   the worked fee example (docs/arbitrage.md)
    notebook    notebooks/01_historical_gaps.ipynb   all statistics, the 3 figures and docs/results.md

Two things to know before a run FROM SCRATCH:
  1. The sample moves with the calendar. The download scripts keep the markets
     of the last 24 months, counted from the day you run them, and they see
     markets that did not exist on 2026-11-04. A fresh download therefore gives
     a somewhat different sample, and the numbers will differ from docs/.
     To reproduce the numbers in docs/ exactly, use --no-download with the
     tables of 2026-11-04 in data/processed/.
  2. Verification needs a review of each candidate pair. The first pass of the
     two-pass review (data/processed/independent_review.csv) is a reviewer's
     judgement, not something code can download. It covers exactly the 201
     candidates of 2026-11-04, and src/apply_independent_review.py stops if the
     candidates differ. Then review the candidates yourself (the message tells
     you how) and continue with:  python run_all.py --from pairs

Project rules (CLAUDE.md): this file calls no API itself. The scripts it runs
only make public GET requests, throttled to at most 5 per second, with retries
and a cache in data/raw/. No keys, no accounts, no orders.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
PROCESSED = PROJECT / "data" / "processed"
PYTHON = sys.executable   # the same Python that runs this file

NOTEBOOK = "notebooks/01_historical_gaps.ipynb"

# What to do when the verification step stops (see point 2 at the top).
REVIEW_HELP = """\
The verification step stopped; the message above says why.
* "Missing ... independent_review.csv", or the candidates are not the 201 pairs
  of 2026-11-04 that this file covers (a new download finds other markets):
  review the candidates yourself, then continue:
      python src/make_review_sheet.py     # writes data/processed/match_review.xlsx
      (open it in Excel, fill in the verdict column: same / different / unsure, save)
      python src/read_review_sheet.py     # writes data/processed/match_verified.csv
      python run_all.py --from pairs
* "you already filled in my_check": the verdicts would change, and redoing them
  would replace your filled-in spot check. Save a copy of
  data/processed/spot_check.xlsx first, then run
      python src/apply_independent_review.py --overwrite
      python run_all.py --from pairs"""

NETWORK_HELP = """\
A venue could not be reached. If the error mentions a proxy (for example
"Tunnel connection failed: 403 Forbidden"), the network blocks that domain:
add it to the allowed domains of your environment (see CLAUDE.md for the
API hosts) and run again. Downloads are cached in data/raw/, so a second run
continues where the first one stopped."""

# Steps whose failure is only a warning: nothing later needs their output.
# (The arb example downloads fresh books only if the saved ones are missing,
# and its three example markets close in November and December 2026.)
OPTIONAL_STEPS = ["arb"]

# One entry per step: (name, what it does, command, is it a download?, help if it fails).
# Commands are lists of words, as subprocess.run() wants them. None means the
# command is decided when the step starts (see arb_command() and verify_command()).
STEPS = [
    ("tests", "unit tests (pytest)",
     [PYTHON, "-m", "pytest", "-q"], False, None),
    ("check", "can we reach both venues' public APIs?",
     [PYTHON, "src/check_connection.py"], True, NETWORK_HELP),
    ("kalshi", "download Kalshi's Fed and CPI markets, order books and hourly candles",
     [PYTHON, "src/download_kalshi.py"], True, NETWORK_HELP),
    ("polymarket", "download Polymarket's Fed and CPI markets, order books and price history",
     [PYTHON, "src/download_polymarket.py"], True, NETWORK_HELP),
    ("match", "find candidate pairs with the same outcome on both venues",
     [PYTHON, "src/match.py"], False, NETWORK_HELP),
    ("verify", "two-pass verification of the candidates -> match_verified.csv",
     None, False, REVIEW_HELP),   # see verify_command()
    ("pairs", "keep the verified pairs -> pairs.parquet",
     [PYTHON, "src/build_pairs.py"], False, None),
    ("trades", "download every Polymarket trade of the verified pairs, in hourly bars",
     [PYTHON, "src/download_poly_trades.py"], True, NETWORK_HELP),
    ("arb", "the worked fee example of docs/arbitrage.md",
     None, False, None),   # the command depends on the saved order books, see arb_command()
    ("notebook", "run the analysis notebook: statistics, figures/ and docs/results.md",
     [PYTHON, "-m", "jupyter", "nbconvert", "--to", "notebook", "--execute", "--inplace",
      "--ExecutePreprocessor.timeout=-1", NOTEBOOK], False, None),
]
STEP_NAMES = [step[0] for step in STEPS]

# The tables the --no-download run needs: they are made by the download steps.
DOWNLOADED_TABLES = ["kalshi_markets.parquet", "kalshi_candles.parquet", "poly_markets.parquet",
                     "poly_prices.parquet", "poly_orderbooks.parquet", "poly_trade_bars.parquet"]


def arb_command():
    """The arb step: print the example from the saved order books, or download fresh ones if there are none.

    The saved books (data/processed/arb_examples.parquet, 2026-11-04 23:37 UTC)
    are the worked example of docs/arbitrage.md. Downloading again would replace
    them with today's books, so that only happens when the file is missing.
    """
    if (PROCESSED / "arb_examples.parquet").exists():
        return [PYTHON, "src/arb.py"]
    return [PYTHON, "src/arb.py", "--download"]


def verify_command():
    """The verify step: run the two-pass review, unless that would replace work typed by hand.

    src/apply_independent_review.py refuses, on purpose, to overwrite
      * a match_verified.csv made from your own review sheet (src/read_review_sheet.py), and
      * a spot_check.xlsx in which you have filled in my_check.
    run_all.py never forces it (it never adds --overwrite). Instead:
      * your own review is used as it is (build_pairs.py works with either);
      * with a filled-in spot check, the step is skipped if match_verified.csv already
        holds exactly what the review would write now. If not, the script runs and
        stops with its own message, and REVIEW_HELP says what to do.
    Returns (command or None, message): None = nothing to run, the message says why.
    """
    sys.path.insert(0, str(PROJECT / "src"))
    import apply_independent_review as review   # our own module in src/

    command = [PYTHON, "src/apply_independent_review.py"]
    if not review.made_by_this_script(review.VERIFIED_FILE):
        return None, "match_verified.csv holds your own review (src/read_review_sheet.py): it is used as it is."
    if review.count_typed_checks(review.SPOT_CHECK_FILE) == 0:
        return command, None
    try:
        candidates = review.match.read_review_file(review.CANDIDATES_FILE)
        table = review.verified_table(review.read_independent_review(review.INDEPENDENT_FILE), candidates)
        saved = review.VERIFIED_FILE.read_text(encoding="utf-8-sig") if review.VERIFIED_FILE.exists() else ""
    except Exception:          # a missing or broken file: the script itself explains the problem
        return command, None
    # Compare as text, line endings ignored (Windows writes \r\n).
    if table.to_csv(index=False).replace("\r\n", "\n") == saved.replace("\r\n", "\n"):
        return None, ("match_verified.csv is already up to date; your filled-in spot check "
                      "(spot_check.xlsx) is kept as it is.")
    return command, None


def run_step(name, description, command, help_text):
    """Run one step as its own program, show its output, and return True if it worked."""
    print("\n" + "=" * 78)
    print(f"STEP {name}: {description}")
    print("   $ " + " ".join(Path(word).name if word == PYTHON else word for word in command))
    print("=" * 78, flush=True)
    started = time.monotonic()
    result = subprocess.run(command, cwd=PROJECT)   # output goes straight to the screen
    minutes = (time.monotonic() - started) / 60
    if result.returncode == 0:
        print(f"--> {name}: done in {minutes:.1f} min", flush=True)
        return True
    print(f"\n--> {name}: FAILED (exit code {result.returncode}) after {minutes:.1f} min")
    if help_text:
        print(help_text)
    return False


def main():
    parser = argparse.ArgumentParser(description="Rebuild every table, figure and results file, in order.")
    parser.add_argument("--no-download", action="store_true",
                        help="skip the download steps and use the tables already in data/processed/")
    parser.add_argument("--from", dest="start", choices=STEP_NAMES, default=STEP_NAMES[0],
                        help="start at this step (default: the first)")
    parser.add_argument("--list", action="store_true", help="show the steps and stop")
    args = parser.parse_args()

    if args.list:
        for name, description, _, is_download, _ in STEPS:
            print(f"  {name:<11} {description}{'  [skipped with --no-download]' if is_download else ''}")
        return 0

    # Folders that git does not keep (data/ is not in the public repository).
    for folder in ["data/raw", "data/processed", "logs"]:
        (PROJECT / folder).mkdir(parents=True, exist_ok=True)

    if args.no_download:
        missing = [name for name in DOWNLOADED_TABLES if not (PROCESSED / name).exists()]
        if missing:
            print("--no-download needs the downloaded tables, but these are missing from data/processed/:")
            print("   " + ", ".join(missing))
            print("Run without --no-download to download them (see README.md).")
            return 1

    steps = STEPS[STEP_NAMES.index(args.start):]
    if args.no_download:
        steps = [step for step in steps if not step[3]]
    print("Steps to run: " + ", ".join(step[0] for step in steps))

    started = time.monotonic()
    for name, description, command, _, help_text in steps:
        if command is None and name == "arb":
            command = arb_command()
        if command is None and name == "verify":
            command, message = verify_command()
            if command is None:
                print(f"\nSTEP {name}: {description}\n--> {name}: nothing to do. {message}", flush=True)
                continue
        if not run_step(name, description, command, help_text):
            if name in OPTIONAL_STEPS:
                print(f"--> {name} is optional (nothing later needs it): carrying on.", flush=True)
                continue
            later = STEP_NAMES[STEP_NAMES.index(name) + 1:]
            print(f"\nStopped. After fixing the problem, continue with:  python run_all.py --from {name}"
                  f"{' --no-download' if args.no_download else ''}")
            if later:
                print(f"(Steps not run yet: {', '.join(later)}.)")
            return 1

    print("\n" + "=" * 78)
    print(f"All steps done in {(time.monotonic() - started) / 60:.1f} min. Results:")
    print("   docs/results.md        all tables of the analysis")
    print("   figures/*.png, *.pdf   the three figures")
    print("   data/processed/        the clean tables (Parquet)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
