"""
snapshot.py - Every 5 minutes, save both venues' full order books for every open pair.

What one cycle does (cycles start on the clock's 5-minute marks: 00:00, 00:05,
00:10 ... UTC):
  1. Reads data/processed/pairs.parquet and keeps the pairs that are OPEN on
     both venues:
       Kalshi      the market has not closed yet (kalshi_close_time is later than now)
       Polymarket  the market's "closed" flag is False. The flag is asked live
                   from Polymarket's Gamma API once per cycle (one request).
  2. For each open pair, sends three requests AT THE SAME MOMENT, in parallel:
       the Kalshi order book, the Polymarket YES book and the Polymarket NO book,
     and records when each request was sent and when its answer arrived.
  3. Adds one row per pair to the day's file
       data/processed/snapshots/snapshots_YYYY-MM-DD.parquet   (UTC day; git-ignored)
     The books are stored exactly as the venues sent them (raw JSON text), so
     nothing is lost. The raw answers are kept there, so there is no separate
     cache in data/raw/ (order books must be fresh every time anyway).

It does NOT compute prices or arbitrage: src/arb.py will do that later, from
these files (read a stored book with json.loads() and pass it to
kalshi.parse_orderbook() or polymarket.parse_orderbook()).

How to run it (from the project folder; full guide: docs/snapshot.md):
    python src/snapshot.py              # run until stopped, one cycle every 5 minutes
    python src/snapshot.py --once       # one cycle right now, then stop (for testing)
    python src/snapshot.py --minutes 10 # another interval
Stop it with Ctrl+C, or with:  kill $(cat logs/snapshot.pid)

Project rules followed (see CLAUDE.md):
  * Only public GET market-data requests: no account, no keys, no orders.
  * Polite: the pairs are spaced 0.6 s apart, so 3 requests per pair make at
    most 5 requests per second. 429 and 5xx answers are retried after a pause.
  * All times are in UTC.

Timing: what "as close together in time as possible" means here
    Each book is taken somewhere between the moment its request was sent and
    the moment its answer arrived. We use the middle of that interval as the
    book's time, and save
        time_gap_seconds = Polymarket YES book's time - Kalshi book's time
    (positive = Polymarket's book was taken later). Kalshi answers in about
    0.05 s and Polymarket in about 0.15 s, so the gap is usually around 0.05 s.
    Both times come from this computer's clock, so a wrong clock cancels out.

Keeping going: every request has a short timeout and at most 3 tries. A book
that cannot be downloaded is still saved as a row, with the reason in its
"error" column. If three pairs in a row get no connection at all, the
internet is probably down: the rest of the cycle is skipped and the next
cycle tries again. Any other unexpected error is written to the log and the
program carries on with the next cycle.
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

KALSHI_URL = "https://external-api.kalshi.com/trade-api/v2"  # order books
CLOB_URL = "https://clob.polymarket.com"                     # Polymarket order books
GAMMA_URL = "https://gamma-api.polymarket.com"               # Polymarket market information

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder above src/
PAIRS_FILE = PROJECT_ROOT / "data" / "processed" / "pairs.parquet"
POLY_MARKETS_FILE = PROJECT_ROOT / "data" / "processed" / "poly_markets.parquet"
SNAPSHOT_FOLDER = PROJECT_ROOT / "data" / "processed" / "snapshots"  # git-ignored
LOG_FOLDER = PROJECT_ROOT / "logs"                                   # git-ignored
LOG_FILE = LOG_FOLDER / "snapshot.log"
PID_FILE = LOG_FOLDER / "snapshot.pid"  # holds the process number of the running copy

CYCLE_MINUTES = 5           # one cycle every 5 minutes
PAIR_SPACING_SECONDS = 0.6  # start a new pair at most every 0.6 s: 3 requests / 0.6 s = 5 per second
TIMEOUT_SECONDS = (5, 15)   # give up on a try after 5 s without a connection or 15 s without an answer
MAX_TRIES = 3               # the first try + up to 2 retries (after 1 s, then 2 s)
MAX_PAUSE_SECONDS = 10      # never pause longer than this between tries
OFFLINE_PAIRS_LIMIT = 3     # this many pairs in a row without any connection = internet is down
MAX_LATE_SECONDS = 60       # a cycle that would start more than 60 s late is skipped (computer asleep)
GAMMA_IDS_PER_REQUEST = 50  # ask Gamma about at most 50 markets per request
LOG_DAYS_KEPT = 30          # keep the log files of the last 30 days
MAX_FILE_MB = 50            # project limit for data files: a warning is logged above it

# Tell the servers what is calling them (polite, and some servers require it).
HEADERS = {"User-Agent": "pm-arbitrage academic research script (read-only)"}

# The three books saved for every pair. These names start the snapshot columns.
BOOKS = ["kalshi", "poly_yes", "poly_no"]

# One connection ("session") per book, plus one for Gamma. The three books of
# a pair are downloaded in parallel, so each needs its own connection.
SESSIONS = {name: requests.Session() for name in BOOKS + ["gamma"]}

log = logging.getLogger("snapshot")

# Set when the user asks the program to stop (Ctrl+C or "kill"). The program
# then finishes the pair it is working on, saves, and ends.
STOP = threading.Event()


def now_utc():
    """The current time in UTC, to the microsecond."""
    return pd.Timestamp.now(tz="UTC")


# ---------------------------------------------------------------------------
# One request: short timeout, a few tries, and a record of what happened
# ---------------------------------------------------------------------------

def fetch(session_name, url, params=None):
    """Send one GET request and describe what happened, in a dict.

    session_name: which connection to use ("kalshi", "poly_yes", "poly_no" or "gamma")
    The answer is a dict with:
        status         "ok", "no_book" (HTTP 404: e.g. the market has closed) or "error"
        text           the answer exactly as received (only if status is "ok")
        data           the same answer read as JSON (only if status is "ok")
        error          what went wrong, in words (None if status is "ok")
        sent_time      UTC time the last try was sent
        received_time  UTC time its answer arrived (or the try failed)
        tries          how many tries were made
        offline        True if no try reached the server at all (internet down?)

    Retried after a pause of 1 s, then 2 s (longer if the server asks for it
    with "Retry-After", at most 10 s): HTTP 429 ("too many requests"), HTTP
    5xx (server error), no connection, a timeout, or a broken answer.
    Not retried: 404 and other errors that trying again would not fix.
    This never raises an error: problems are reported in the dict.
    """
    session = SESSIONS[session_name]
    outcome = {"status": "error", "text": None, "data": None, "error": None,
               "sent_time": None, "received_time": None, "tries": 0, "offline": True}

    for attempt in range(1, MAX_TRIES + 1):
        outcome["tries"] = attempt
        outcome["sent_time"] = now_utc()
        server_pause = None
        try:
            response = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.ProxyError as error:
            outcome["received_time"] = now_utc()
            if "403" in str(error):
                # Only in a cloud session (all traffic passes through a proxy there):
                # the domain is not on the environment's list of allowed domains.
                domain = url.split("/")[2]
                outcome["error"] = (f"the network blocked {domain}: add it to the cloud "
                                    f"environment's allowed domains")
                outcome["offline"] = False
                return outcome
            outcome["error"] = "no connection (network proxy error)"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            outcome["received_time"] = now_utc()
            outcome["error"] = f"no connection ({type(error).__name__})"
        except requests.exceptions.RequestException as error:
            # Any other problem on the way, e.g. the connection broke in the middle of
            # the answer (ChunkedEncodingError). The server was reached, so we are online.
            outcome["received_time"] = now_utc()
            outcome["offline"] = False
            outcome["error"] = f"request failed ({type(error).__name__})"
        else:
            outcome["received_time"] = now_utc()
            outcome["offline"] = False  # the server answered: the internet works
            code = response.status_code
            if code == 200:
                try:
                    outcome["data"] = response.json()
                except ValueError:
                    outcome["error"] = "the answer was not valid JSON (cut off on the way?)"
                else:
                    outcome["status"], outcome["text"], outcome["error"] = "ok", response.text, None
                    return outcome
            elif code == 404:
                outcome["status"] = "no_book"
                outcome["error"] = "HTTP 404: not found (no order book; has the market closed?)"
                return outcome
            elif code == 429 or code >= 500:
                outcome["error"] = f"HTTP {code}"
                server_pause = seconds_to_wait(response.headers.get("Retry-After"))
            else:
                outcome["error"] = f"HTTP {code}: {response.text[:200]}"
                return outcome

        if attempt == MAX_TRIES or STOP.is_set():  # no tries left, or the user wants to stop
            break
        pause = 2 ** (attempt - 1)  # 1 s, then 2 s
        if server_pause is not None:
            pause = max(pause, server_pause)
        time.sleep(min(pause, MAX_PAUSE_SECONDS))
    return outcome


def seconds_to_wait(retry_after):
    """The number in a "Retry-After" header (seconds), or None if there is none."""
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Which pairs are open?
# ---------------------------------------------------------------------------

def load_known_closed_flags():
    """Polymarket's "closed" flag of every market as of the last download: {market_id: True/False}.

    Read from data/processed/poly_markets.parquet. Only used until Gamma has
    answered once (for example if the program starts without internet).
    """
    if not POLY_MARKETS_FILE.exists():
        return {}
    table = pd.read_parquet(POLY_MARKETS_FILE, columns=["market_id", "closed"])
    return {str(market_id): bool(closed)
            for market_id, closed in zip(table["market_id"], table["closed"]) if not pd.isna(closed)}


def check_polymarket_closed(market_ids, closed_flags):
    """Ask Gamma for the current "closed" flag of these markets; update closed_flags in place.

    Asked with closed=false, Gamma's market list returns only the markets
    that are still open, so a market we asked about that is missing from the
    answer has closed. Both protocol versions ("v1", "v2") are asked for,
    because Gamma would otherwise list only v1 markets.
    https://docs.polymarket.com/market-data/discover-markets.md (List markets)
    Returns True if Gamma answered. If not, closed_flags keeps its old values
    and False is returned.
    """
    market_ids = sorted({str(market_id) for market_id in market_ids})
    new_flags = {}
    for start in range(0, len(market_ids), GAMMA_IDS_PER_REQUEST):
        chunk = market_ids[start:start + GAMMA_IDS_PER_REQUEST]
        params = ([("id", market_id) for market_id in chunk]
                  + [("version", "v1"), ("version", "v2"), ("closed", "false"), ("limit", 100)])
        answer = fetch("gamma", GAMMA_URL + "/markets/keyset", params)
        if answer["status"] != "ok":
            log.warning("Could not ask Polymarket which markets are closed (%s); "
                        "using the last known flags", answer["error"])
            return False
        listed = {str(market.get("id")): market.get("closed")
                  for market in (answer["data"] or {}).get("markets") or []}
        for market_id in chunk:
            # closed unless Gamma lists the market with closed = False
            new_flags[market_id] = listed.get(market_id) is not False
    closed_flags.update(new_flags)
    return True


def select_open_pairs(pairs, now, closed_flags):
    """The pairs that both venues still trade at time `now` (UTC).

        Kalshi:      kalshi_close_time is later than now
        Polymarket:  the market's "closed" flag is False (a market whose flag
                     is unknown counts as closed)
    """
    kalshi_open = pairs["kalshi_close_time"] > now
    poly_open = pairs["poly_market_id"].astype(str).map(lambda market_id: closed_flags.get(market_id) is False)
    return pairs[kalshi_open & poly_open.astype(bool)]


# ---------------------------------------------------------------------------
# One pair: three books at the same moment, turned into one row
# ---------------------------------------------------------------------------

def fetch_pair_books(pair, pool):
    """Download the pair's three order books at the same moment, in parallel.

    pair: one row of pairs.parquet (a dict). pool: a ThreadPoolExecutor with
    3 workers, so all three requests go out together.
    Returns {"kalshi": ..., "poly_yes": ..., "poly_no": ...}: the dicts of fetch().
    Order books are always all price levels:
      Kalshi:     GET /markets/{ticker}/orderbook (depth 0 = all levels, the default)
                  https://docs.kalshi.com/api-reference/market/get-market-orderbook
      Polymarket: GET /book?token_id=... (one book per outcome token)
                  https://docs.polymarket.com/market-data/prices-order-books.md
    """
    jobs = {
        "kalshi": (f"{KALSHI_URL}/markets/{pair['kalshi_ticker']}/orderbook", None),
        "poly_yes": (f"{CLOB_URL}/book", {"token_id": pair["poly_yes_token_id"]}),
        "poly_no": (f"{CLOB_URL}/book", {"token_id": pair["poly_no_token_id"]}),
    }
    running = {book: pool.submit(fetch, book, url, params) for book, (url, params) in jobs.items()}
    return {book: job.result() for book, job in running.items()}


def book_time(result):
    """When a book was taken: the middle between sending the request and receiving the answer.

    None if the book was not downloaded.
    """
    if result["status"] != "ok":
        return None
    return result["sent_time"] + (result["received_time"] - result["sent_time"]) / 2


def time_gap_seconds(kalshi_result, poly_result):
    """Seconds from the Kalshi book to the Polymarket book (positive = Polymarket's was later).

    NaN if either book is missing.
    """
    kalshi_time, poly_time = book_time(kalshi_result), book_time(poly_result)
    if kalshi_time is None or poly_time is None:
        return float("nan")
    return (poly_time - kalshi_time).total_seconds()


def polymarket_server_time(result):
    """The time Polymarket writes into its book answer ("timestamp", Unix milliseconds), in UTC.

    https://docs.polymarket.com/market-data/prices-order-books.md (field "timestamp")
    This is NOT the time of the answer: in our live test (2026-11-04) it was
    0.2 to 68 seconds before the answer arrived, the same in the YES and NO
    book. It seems to be the time the book last changed.
    None if the book is missing or has no usable timestamp.
    """
    if result["status"] != "ok" or not isinstance(result["data"], dict):
        return None
    try:
        return pd.Timestamp(int(result["data"]["timestamp"]), unit="ms", tz="UTC")
    except (KeyError, TypeError, ValueError):
        return None


# The columns of the snapshot files, in order. For each of the three books
# (kalshi, poly_yes, poly_no):
#   <book>_sent_time, <book>_received_time   UTC times of the request (its last try)
#   <book>_status   "ok", "no_book" (HTTP 404) or "error"
#   <book>_tries    number of tries
#   <book>_error    what went wrong (empty if ok)
#   <book>_book     the answer exactly as the venue sent it (JSON text; empty if not ok)
BOOK_FIELDS = ["sent_time", "received_time", "status", "tries", "error", "book"]
SNAPSHOT_COLUMNS = (
    ["cycle_time", "pair_id", "kalshi_ticker", "poly_market_id", "poly_yes_token_id",
     "poly_no_token_id", "time_gap_seconds", "poly_yes_server_time", "poly_no_server_time"]
    + [f"{book}_{field}" for book in BOOKS for field in BOOK_FIELDS]
)
TIME_COLUMNS = (["cycle_time", "poly_yes_server_time", "poly_no_server_time"]
                + [f"{book}_{field}" for book in BOOKS for field in ["sent_time", "received_time"]])
INTEGER_COLUMNS = [f"{book}_tries" for book in BOOKS]
FLOAT_COLUMNS = ["time_gap_seconds"]


def make_row(cycle_time, pair, results):
    """One row of the snapshot file: the pair's ids, the timing and the three raw books."""
    row = {
        "cycle_time": cycle_time,
        "pair_id": pair["pair_id"],
        "kalshi_ticker": pair["kalshi_ticker"],
        "poly_market_id": str(pair["poly_market_id"]),
        "poly_yes_token_id": str(pair["poly_yes_token_id"]),
        "poly_no_token_id": str(pair["poly_no_token_id"]),
        "time_gap_seconds": time_gap_seconds(results["kalshi"], results["poly_yes"]),
        "poly_yes_server_time": polymarket_server_time(results["poly_yes"]),
        "poly_no_server_time": polymarket_server_time(results["poly_no"]),
    }
    for book in BOOKS:
        result = results[book]
        row[f"{book}_sent_time"] = result["sent_time"]
        row[f"{book}_received_time"] = result["received_time"]
        row[f"{book}_status"] = result["status"]
        row[f"{book}_tries"] = result["tries"]
        row[f"{book}_error"] = result["error"]
        row[f"{book}_book"] = result["text"]
    return row


def rows_to_table(rows):
    """Turn a list of rows into a table with fixed column types.

    Fixed types make every cycle's table look exactly the same, so the tables
    can be stacked into one file: times are UTC timestamps (microseconds),
    tries are whole numbers, the gap is a decimal number, all else is text.
    """
    table = pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)
    for column in SNAPSHOT_COLUMNS:
        if column in TIME_COLUMNS:
            table[column] = pd.to_datetime(table[column], utc=True).astype("datetime64[us, UTC]")
        elif column in INTEGER_COLUMNS:
            table[column] = table[column].astype("Int64")
        elif column in FLOAT_COLUMNS:
            table[column] = table[column].astype("float64")
        else:
            table[column] = table[column].astype("str")
    return table


# ---------------------------------------------------------------------------
# Saving: one Parquet file per UTC day
# ---------------------------------------------------------------------------

def day_file(cycle_time):
    """The snapshot file of the cycle's UTC day, e.g. .../snapshots/snapshots_2026-11-05.parquet."""
    return SNAPSHOT_FOLDER / f"snapshots_{cycle_time:%Y-%m-%d}.parquet"


def append_to_day_file(new_rows, path):
    """Add rows to a day's Parquet file (made if it does not exist); return its size in MB.

    A Parquet file cannot be added to in place, so the day's file is read,
    the new rows are put under the old ones, and everything is written to a
    temporary file that then replaces the old file in a single step. If the
    program is stopped halfway, the old file is still complete.
    If the old file cannot be read, it is renamed (".unreadable-...") so
    that no data is overwritten, and a new file is started.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    table = new_rows
    if path.exists():
        try:
            old_rows = pd.read_parquet(path)
        except Exception as error:  # e.g. a file damaged by a disk problem
            moved = path.with_name(f"{path.name}.unreadable-{now_utc():%Y%m%dT%H%M%S}")
            path.rename(moved)
            log.error("Could not read %s (%s): renamed it to %s and started a new file",
                      path.name, error, moved.name)
        else:
            table = pd.concat([old_rows, new_rows], ignore_index=True)
    temporary = path.with_name(path.name + ".tmp")
    table.to_parquet(temporary, index=False, compression="zstd")
    os.replace(temporary, path)
    return path.stat().st_size / 1_000_000


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------

def run_cycle(cycle_time, closed_flags, pool):
    """Download and save the books of every open pair once. Returns the number of rows saved.

    cycle_time:    the 5-minute mark this cycle belongs to (UTC)
    closed_flags:  {Polymarket market_id: closed?}, updated from Gamma here
    pool:          a ThreadPoolExecutor with 3 workers
    """
    started = time.monotonic()
    pairs = pd.read_parquet(PAIRS_FILE)
    now = now_utc()

    # Which pairs are open? Ask Gamma only about pairs that Kalshi still trades.
    kalshi_open = pairs[pairs["kalshi_close_time"] > now]
    check_polymarket_closed(kalshi_open["poly_market_id"], closed_flags)
    open_pairs = select_open_pairs(pairs, now, closed_flags)
    closed_on_poly = len(kalshi_open) - len(open_pairs)
    if not len(open_pairs):
        log.info("Cycle %s: no open pairs (Kalshi still trades %d; Polymarket has closed %d of those)",
                 f"{cycle_time:%Y-%m-%d %H:%M}", len(kalshi_open), closed_on_poly)
        return 0

    rows = []
    offline_in_a_row = 0
    for pair in open_pairs.to_dict("records"):
        if STOP.is_set():
            log.info("Stop requested: saving the %d pairs done so far", len(rows))
            break
        pair_started = time.monotonic()
        try:
            results = fetch_pair_books(pair, pool)
            rows.append(make_row(cycle_time, pair, results))
        except Exception:
            # Something unexpected with this one pair: log it, keep the other pairs' rows.
            log.exception("%s: unexpected error; this pair is skipped in this cycle", pair["pair_id"])
            continue
        problems = [f"{book}: {results[book]['error']} ({results[book]['tries']} tries)"
                    for book in BOOKS if results[book]["status"] != "ok"]
        if problems:  # one log line per pair, naming each book that is missing
            log.warning("%s: book not saved - %s", pair["pair_id"], "; ".join(problems))

        # No connection at all for any of the three books: is the internet down?
        if all(results[book]["offline"] for book in BOOKS):
            offline_in_a_row += 1
        else:
            offline_in_a_row = 0
        if offline_in_a_row >= OFFLINE_PAIRS_LIMIT:
            log.warning("No connection for %d pairs in a row: the internet seems to be down. "
                        "Skipping the rest of this cycle; trying again at the next one.", offline_in_a_row)
            break

        # Be polite: start the next pair at least PAIR_SPACING_SECONDS after this one.
        time.sleep(max(0.0, pair_started + PAIR_SPACING_SECONDS - time.monotonic()))

    if not rows:
        return 0
    table = rows_to_table(rows)
    path = day_file(cycle_time)
    size_mb = append_to_day_file(table, path)
    log_summary(cycle_time, table, len(open_pairs), time.monotonic() - started, path, size_mb)
    if size_mb >= MAX_FILE_MB:
        log.warning("%s is %.1f MB, over the project's %d MB limit for data files",
                    path.name, size_mb, MAX_FILE_MB)
    return len(table)


def log_summary(cycle_time, table, open_count, seconds, path, size_mb):
    """One log line per cycle: how many books arrived, the time gaps, the file size."""
    ok_counts = ", ".join(f"{book} {int((table[f'{book}_status'] == 'ok').sum())}/{len(table)}"
                          for book in BOOKS)
    gaps = table["time_gap_seconds"].dropna()
    if len(gaps):
        gap_text = f"median {gaps.median():+.3f} s, largest {gaps.abs().max():.3f} s"
    else:
        gap_text = "none (no pair with both books)"
    log.info("Cycle %s: %d of %d open pairs saved; books ok: %s; time gap Polymarket - Kalshi: %s; "
             "took %.1f s; %s is now %.2f MB",
             f"{cycle_time:%Y-%m-%d %H:%M}", len(table), open_count, ok_counts, gap_text,
             seconds, path.name, size_mb)


# ---------------------------------------------------------------------------
# The schedule: one cycle on every 5-minute mark
# ---------------------------------------------------------------------------

def next_mark(now, minutes=CYCLE_MINUTES):
    """The first 5-minute mark of the clock AFTER `now` (UTC), e.g. 12:03:10 -> 12:05:00.

    A time exactly on a mark gives the next one (12:05:00 -> 12:10:00).
    """
    return now.floor(f"{minutes}min") + pd.Timedelta(minutes=minutes)


def wait_until(moment):
    """Sleep until `moment` (UTC), or until a stop is requested.

    Sleeps in steps of at most 1 second and checks the clock each time, so a
    computer that was asleep notices the time quickly when it wakes up.
    """
    while not STOP.is_set():
        seconds_left = (moment - now_utc()).total_seconds()
        if seconds_left <= 0:
            return
        STOP.wait(min(seconds_left, 1.0))


def run_forever(minutes=CYCLE_MINUTES):
    """Run one cycle on every mark of the clock until a stop is requested."""
    closed_flags = load_known_closed_flags()
    with ThreadPoolExecutor(max_workers=len(BOOKS)) as pool:
        cycle_time = next_mark(now_utc(), minutes)
        log.info("Started (process %d): a cycle every %d minutes; the first at %s UTC",
                 os.getpid(), minutes, f"{cycle_time:%H:%M}")
        while not STOP.is_set():
            wait_until(cycle_time)
            if STOP.is_set():
                break
            late = (now_utc() - cycle_time).total_seconds()
            if late > MAX_LATE_SECONDS:
                log.warning("Missed the cycle of %s (%.0f s late: was the computer asleep?)",
                            f"{cycle_time:%Y-%m-%d %H:%M}", late)
            else:
                try:
                    run_cycle(cycle_time, closed_flags, pool)
                except Exception:
                    # Anything unexpected: write it to the log and carry on.
                    log.exception("The cycle of %s failed; trying again at the next one",
                                  f"{cycle_time:%Y-%m-%d %H:%M}")
            cycle_time = next_mark(now_utc(), minutes)


def run_once():
    """Run one cycle right now (its cycle_time is the current second, not a 5-minute mark)."""
    with ThreadPoolExecutor(max_workers=len(BOOKS)) as pool:
        return run_cycle(now_utc().floor("s"), load_known_closed_flags(), pool)


# ---------------------------------------------------------------------------
# Logging, one copy at a time, and stopping cleanly
# ---------------------------------------------------------------------------

def setup_logging():
    """Write log lines (in UTC) to logs/snapshot.log and to the screen.

    A new log file is started every day at midnight UTC; the old ones are
    named snapshot.log.YYYY-MM-DD and kept for 30 days.
    """
    if log.handlers:  # already set up
        return
    LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s UTC  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    formatter.converter = time.gmtime  # log times in UTC
    to_file = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", utc=True, backupCount=LOG_DAYS_KEPT, encoding="utf-8")
    to_screen = logging.StreamHandler(sys.stdout)
    for handler in [to_file, to_screen]:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def process_is_running(pid):
    """True if a process with this number exists on this computer."""
    try:
        os.kill(pid, 0)  # signal 0 sends nothing: it only checks that the process exists
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, but belongs to another user
    return True


def take_pid_lock():
    """Write our process number to logs/snapshot.pid, unless another copy is running.

    Two copies would add the same rows twice and could overwrite each
    other's saves, so only one may run. Returns (True, None) if we may run,
    or (False, the other copy's process number).
    """
    LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            other = int(PID_FILE.read_text().strip())
        except ValueError:
            other = None
        if other and other != os.getpid() and process_is_running(other):
            return False, other
    PID_FILE.write_text(str(os.getpid()))
    return True, None


def release_pid_lock():
    """Delete logs/snapshot.pid if it is ours."""
    try:
        if PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except (FileNotFoundError, ValueError):
        pass


def request_stop(signal_number, frame):
    """Called on Ctrl+C or "kill": stop after the current pair. A second Ctrl+C stops at once."""
    if STOP.is_set():
        raise KeyboardInterrupt
    STOP.set()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Every 5 minutes, save both venues' order books for every open pair.")
    parser.add_argument("--once", action="store_true", help="run one cycle now, then stop (for testing)")
    parser.add_argument("--minutes", type=int, default=CYCLE_MINUTES,
                        help=f"minutes between cycles (default {CYCLE_MINUTES})")
    args = parser.parse_args(argv)
    if args.minutes < 1:
        parser.error("--minutes must be at least 1")

    setup_logging()
    allowed, other = take_pid_lock()
    if not allowed:
        log.error("Another copy is already running (process %d). Stop it first: kill %d", other, other)
        return 1
    signal.signal(signal.SIGINT, request_stop)   # Ctrl+C
    signal.signal(signal.SIGTERM, request_stop)  # "kill <process number>"
    try:
        if args.once:
            try:
                saved = run_once()
            except Exception:
                log.exception("The cycle failed")
                return 1
            log.info("Done: %d rows saved", saved)
        else:
            run_forever(args.minutes)
    finally:
        release_pid_lock()
        log.info("Stopped.")
    return 0


# This block runs only when the file is started as a script
# ("python src/snapshot.py"), not when another file (or a test) imports it.
if __name__ == "__main__":
    sys.exit(main())
