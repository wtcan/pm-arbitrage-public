"""
Unit tests for src/snapshot.py. Run them from the project folder with:  pytest

None of these tests use the internet. Fake "sessions" and fake fetch
functions stand in for Kalshi and Polymarket, so we can make them answer
whatever we need: a normal book, "too many requests", a server error, a lost
connection, a missing book ...

The one formula in snapshot.py is the time gap between the two venues' books:
    book time        = the middle of its request = sent + (received - sent) / 2
    time_gap_seconds = Polymarket YES book time - Kalshi book time
It is a measurement of our own requests, not a price or fee formula, so it
has no documentation URL.
"""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
import requests

import kalshi
import polymarket
import snapshot

T = pd.Timestamp  # short name: T("2026-11-05 12:00", tz="UTC")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    """Looks like a requests answer: status_code, text, headers and json()."""

    def __init__(self, status_code=200, text="{}", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Answers each get() with the next item of `answers`: a FakeResponse, or an error to raise."""

    def __init__(self, answers, delay=0.0):
        self.answers = list(answers)
        self.delay = delay  # seconds to wait before answering, like a real server
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "time": time.monotonic()})
        if self.delay:
            time.sleep(self.delay)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


KALSHI_BOOK = {"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"], ["0.4100", "10.00"]],
                                "no_dollars": [["0.5600", "17.00"]]}}
POLY_YES_BOOK = {"timestamp": "1793835099062", "hash": "abc",
                 "bids": [{"price": "0.40", "size": "100"}, {"price": "0.41", "size": "50"}],
                 "asks": [{"price": "0.45", "size": "70"}, {"price": "0.44", "size": "30"}]}
POLY_NO_BOOK = {"timestamp": "1793835099062", "hash": "def",
                "bids": [{"price": "0.55", "size": "70"}, {"price": "0.56", "size": "30"}],
                "asks": [{"price": "0.60", "size": "100"}, {"price": "0.59", "size": "50"}]}


def ok_result(data, sent, seconds=0.1):
    """A fetch() result for a book that arrived `seconds` after it was asked for."""
    sent = T(sent, tz="UTC")
    return {"status": "ok", "text": json.dumps(data), "data": data, "error": None,
            "sent_time": sent, "received_time": sent + pd.Timedelta(seconds=seconds),
            "tries": 1, "offline": False}


def failed_result(error="no connection (ConnectionError)", offline=True, status="error"):
    """A fetch() result for a book that could not be downloaded."""
    now = T("2026-11-05 12:00", tz="UTC")
    return {"status": status, "text": None, "data": None, "error": error,
            "sent_time": now, "received_time": now, "tries": 3, "offline": offline}


def good_results():
    """The three results of one pair whose books all arrived."""
    return {"kalshi": ok_result(KALSHI_BOOK, "2026-11-05 12:00:00.000", 0.10),
            "poly_yes": ok_result(POLY_YES_BOOK, "2026-11-05 12:00:00.002", 0.20),
            "poly_no": ok_result(POLY_NO_BOOK, "2026-11-05 12:00:00.003", 0.20)}


def make_pairs(count, kalshi_close="2027-01-01", first_market_id=100):
    """A small pairs table shaped like pairs.parquet (only the columns snapshot.py uses)."""
    return pd.DataFrame({
        "pair_id": [f"KX-{n} / {first_market_id + n}" for n in range(count)],
        "kalshi_ticker": [f"KX-{n}" for n in range(count)],
        "poly_market_id": [str(first_market_id + n) for n in range(count)],
        "poly_yes_token_id": [f"yes{n}" for n in range(count)],
        "poly_no_token_id": [f"no{n}" for n in range(count)],
        "kalshi_close_time": pd.to_datetime([kalshi_close] * count, utc=True),
    })


class NoInternet:
    """A session that fails the test if it is used: tests must never reach the real internet."""

    def get(self, url, *args, **kwargs):
        raise AssertionError(f"a test tried to use the internet: {url}")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Every test starts without a stop request and without real connections."""
    snapshot.STOP.clear()
    for name in list(snapshot.SESSIONS):
        monkeypatch.setitem(snapshot.SESSIONS, name, NoInternet())
    yield
    snapshot.STOP.clear()


@pytest.fixture
def pauses(monkeypatch):
    """Replace time.sleep while a test runs: record the pauses instead of waiting."""
    recorded = []
    monkeypatch.setattr(snapshot.time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """Point snapshot.py's files at a temporary folder, never at the real project data."""
    monkeypatch.setattr(snapshot, "PAIRS_FILE", tmp_path / "pairs.parquet")
    monkeypatch.setattr(snapshot, "POLY_MARKETS_FILE", tmp_path / "poly_markets.parquet")
    monkeypatch.setattr(snapshot, "SNAPSHOT_FOLDER", tmp_path / "snapshots")
    monkeypatch.setattr(snapshot, "PID_FILE", tmp_path / "snapshot.pid")
    monkeypatch.setattr(snapshot, "LOG_FOLDER", tmp_path)
    monkeypatch.setattr(snapshot, "PAIR_SPACING_SECONDS", 0)
    return tmp_path


# ---------------------------------------------------------------------------
# fetch(): one request, its tries and what is recorded
# ---------------------------------------------------------------------------

def use_session(monkeypatch, answers, name="kalshi"):
    session = FakeSession(answers)
    monkeypatch.setitem(snapshot.SESSIONS, name, session)
    return session


def test_fetch_ok_keeps_the_answer_exactly(monkeypatch, pauses):
    text = '{"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"]]}}'
    use_session(monkeypatch, [FakeResponse(200, text)])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "ok"
    assert result["text"] == text  # the raw text, unchanged
    assert result["data"] == json.loads(text)
    assert result["error"] is None and result["tries"] == 1 and not result["offline"]
    assert result["sent_time"] <= result["received_time"]
    assert str(result["sent_time"].tz) == "UTC"
    assert pauses == []


def test_fetch_retries_after_429_and_respects_retry_after(monkeypatch, pauses):
    use_session(monkeypatch, [FakeResponse(429, "slow down", {"Retry-After": "3"}), FakeResponse(200, "{}")])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "ok" and result["tries"] == 2
    assert pauses == [3]


def test_fetch_gives_up_after_three_server_errors(monkeypatch, pauses):
    session = use_session(monkeypatch, [FakeResponse(500), FakeResponse(502), FakeResponse(503)])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "error" and result["error"] == "HTTP 503"
    assert result["tries"] == 3 and len(session.calls) == 3
    assert pauses == [1, 2]
    assert not result["offline"]  # the server answered: the internet works


def test_fetch_without_internet_is_marked_offline(monkeypatch, pauses):
    use_session(monkeypatch, [requests.exceptions.ConnectionError("no route")] * 3)
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "error" and result["offline"]
    assert result["error"] == "no connection (ConnectionError)"
    assert result["text"] is None and result["tries"] == 3


def test_fetch_recovers_after_a_timeout(monkeypatch, pauses):
    use_session(monkeypatch, [requests.exceptions.ReadTimeout("slow"), FakeResponse(200, "{}")])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "ok" and result["tries"] == 2 and not result["offline"]


def test_fetch_retries_an_answer_cut_off_on_the_way(monkeypatch, pauses):
    use_session(monkeypatch, [requests.exceptions.ChunkedEncodingError("connection broken"),
                              FakeResponse(200, "{}")])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "ok" and result["tries"] == 2 and not result["offline"]


def test_fetch_retries_a_broken_json_answer(monkeypatch, pauses):
    use_session(monkeypatch, [FakeResponse(200, '{"cut off'), FakeResponse(200, '{"a": 1}')])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "ok" and result["data"] == {"a": 1} and result["tries"] == 2


def test_fetch_404_means_no_book_and_is_not_retried(monkeypatch, pauses):
    session = use_session(monkeypatch, [FakeResponse(404, "not found")], name="poly_yes")
    result = snapshot.fetch("poly_yes", "https://example.com/book")
    assert result["status"] == "no_book" and len(session.calls) == 1 and pauses == []


def test_fetch_other_client_errors_are_not_retried(monkeypatch, pauses):
    session = use_session(monkeypatch, [FakeResponse(400, "bad ticker")])
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "error" and "HTTP 400" in result["error"] and "bad ticker" in result["error"]
    assert len(session.calls) == 1


def test_fetch_names_the_blocked_domain(monkeypatch, pauses):
    blocked = requests.exceptions.ProxyError("Tunnel connection failed: 403 Forbidden")
    session = use_session(monkeypatch, [blocked])
    result = snapshot.fetch("kalshi", "https://external-api.kalshi.com/trade-api/v2/markets/X/orderbook")
    assert "external-api.kalshi.com" in result["error"] and "allowed domains" in result["error"]
    assert len(session.calls) == 1 and not result["offline"]


def test_fetch_stops_retrying_when_a_stop_is_requested(monkeypatch, pauses):
    session = use_session(monkeypatch, [FakeResponse(500), FakeResponse(200)])
    snapshot.STOP.set()
    result = snapshot.fetch("kalshi", "https://example.com/book")
    assert result["status"] == "error" and len(session.calls) == 1 and pauses == []


# ---------------------------------------------------------------------------
# Which pairs are open?
# ---------------------------------------------------------------------------

def test_select_open_pairs():
    pairs = make_pairs(4)
    pairs.loc[0, "kalshi_close_time"] = T("2026-11-05 11:59", tz="UTC")  # Kalshi already closed
    closed_flags = {"100": False, "101": False, "102": True}             # "103" unknown
    now = T("2026-11-05 12:00", tz="UTC")
    open_pairs = snapshot.select_open_pairs(pairs, now, closed_flags)
    # 100: Kalshi closed; 101: open on both; 102: Polymarket closed; 103: flag unknown -> closed
    assert list(open_pairs["poly_market_id"]) == ["101"]


def test_check_polymarket_closed_reads_the_closed_flag(monkeypatch):
    asked = []

    def fake_fetch(session_name, url, params=None):
        asked.append((session_name, url, params))
        # Gamma, asked with closed=false, lists only the markets that are still open
        return {"status": "ok", "data": {"markets": [{"id": "1", "closed": False}, {"id": 2, "closed": False}]}}

    monkeypatch.setattr(snapshot, "fetch", fake_fetch)
    flags = {"3": False}  # an old value that Gamma's answer must overwrite
    assert snapshot.check_polymarket_closed(["1", "2", "3"], flags)
    assert flags == {"1": False, "2": False, "3": True}
    session_name, url, params = asked[0]
    assert session_name == "gamma" and url.endswith("/markets/keyset")
    assert ("closed", "false") in params and ("version", "v1") in params and ("version", "v2") in params
    assert [value for key, value in params if key == "id"] == ["1", "2", "3"]


def test_check_polymarket_closed_asks_in_chunks_of_50(monkeypatch):
    chunks = []

    def fake_fetch(session_name, url, params=None):
        ids = [value for key, value in params if key == "id"]
        chunks.append(len(ids))
        return {"status": "ok", "data": {"markets": [{"id": i, "closed": False} for i in ids]}}

    monkeypatch.setattr(snapshot, "fetch", fake_fetch)
    flags = {}
    snapshot.check_polymarket_closed([str(n) for n in range(120)], flags)
    assert chunks == [50, 50, 20]
    assert len(flags) == 120 and not any(flags.values())


def test_check_polymarket_closed_keeps_old_flags_when_gamma_fails(monkeypatch):
    monkeypatch.setattr(snapshot, "fetch", lambda *args, **kwargs: failed_result())
    flags = {"1": False}
    assert not snapshot.check_polymarket_closed(["1", "2"], flags)
    assert flags == {"1": False}


def test_load_known_closed_flags(folders):
    pd.DataFrame({"market_id": ["1", "2", "3"], "closed": pd.array([True, False, None], dtype="boolean")}
                 ).to_parquet(snapshot.POLY_MARKETS_FILE)
    assert snapshot.load_known_closed_flags() == {"1": True, "2": False}


def test_load_known_closed_flags_without_the_file(folders):
    assert snapshot.load_known_closed_flags() == {}


# ---------------------------------------------------------------------------
# One pair: parallel requests, time gap, the row
# ---------------------------------------------------------------------------

def test_fetch_pair_books_asks_the_right_books_at_the_same_moment(monkeypatch):
    sessions = {
        "kalshi": FakeSession([FakeResponse(200, json.dumps(KALSHI_BOOK))], delay=0.3),
        "poly_yes": FakeSession([FakeResponse(200, json.dumps(POLY_YES_BOOK))], delay=0.3),
        "poly_no": FakeSession([FakeResponse(200, json.dumps(POLY_NO_BOOK))], delay=0.3),
    }
    for name, session in sessions.items():
        monkeypatch.setitem(snapshot.SESSIONS, name, session)
    pair = make_pairs(1).to_dict("records")[0]

    with ThreadPoolExecutor(max_workers=3) as pool:
        started = time.monotonic()
        results = snapshot.fetch_pair_books(pair, pool)
        seconds = time.monotonic() - started

    assert {book: result["status"] for book, result in results.items()} == \
        {"kalshi": "ok", "poly_yes": "ok", "poly_no": "ok"}
    assert sessions["kalshi"].calls[0]["url"].endswith("/markets/KX-0/orderbook")
    assert sessions["poly_yes"].calls[0]["params"] == {"token_id": "yes0"}
    assert sessions["poly_no"].calls[0]["params"] == {"token_id": "no0"}
    # In parallel: three answers of 0.3 s each take about 0.3 s, not 0.9 s,
    # and the three requests leave (almost) together.
    assert seconds < 0.7
    sent = [session.calls[0]["time"] for session in sessions.values()]
    assert max(sent) - min(sent) < 0.1


def test_book_time_is_the_middle_of_the_request():
    result = ok_result(KALSHI_BOOK, "2026-11-05 12:00:00.000", seconds=0.1)
    assert snapshot.book_time(result) == T("2026-11-05 12:00:00.050", tz="UTC")
    assert snapshot.book_time(failed_result()) is None


def test_time_gap_seconds():
    # Kalshi:     sent 0.000, received 0.100 -> book time 0.050
    # Polymarket: sent 0.002, received 0.202 -> book time 0.102
    kalshi_result = ok_result(KALSHI_BOOK, "2026-11-05 12:00:00.000", seconds=0.1)
    poly_result = ok_result(POLY_YES_BOOK, "2026-11-05 12:00:00.002", seconds=0.2)
    assert snapshot.time_gap_seconds(kalshi_result, poly_result) == pytest.approx(0.052)
    # negative when Kalshi's book is the later one
    assert snapshot.time_gap_seconds(poly_result, kalshi_result) == pytest.approx(-0.052)


def test_time_gap_is_missing_if_a_book_is_missing():
    kalshi_result = ok_result(KALSHI_BOOK, "2026-11-05 12:00:00", seconds=0.1)
    assert pd.isna(snapshot.time_gap_seconds(kalshi_result, failed_result()))
    assert pd.isna(snapshot.time_gap_seconds(failed_result(), kalshi_result))


def test_polymarket_server_time():
    result = ok_result(POLY_YES_BOOK, "2026-11-05 12:00")
    assert snapshot.polymarket_server_time(result) == T("2026-11-04 23:31:39.062", tz="UTC")
    assert snapshot.polymarket_server_time(failed_result()) is None
    assert snapshot.polymarket_server_time(ok_result({"bids": []}, "2026-11-05 12:00")) is None


def test_make_row_keeps_the_raw_books():
    pair = make_pairs(1).to_dict("records")[0]
    results = good_results()
    row = snapshot.make_row(T("2026-11-05 12:00", tz="UTC"), pair, results)
    assert list(row) == snapshot.SNAPSHOT_COLUMNS
    assert row["pair_id"] == "KX-0 / 100"
    for book in snapshot.BOOKS:
        assert row[f"{book}_book"] == results[book]["text"]
        assert row[f"{book}_status"] == "ok" and row[f"{book}_error"] is None
    assert row["time_gap_seconds"] == pytest.approx(0.052)


def test_make_row_for_failed_books():
    pair = make_pairs(1).to_dict("records")[0]
    results = {"kalshi": failed_result(), "poly_yes": failed_result("HTTP 404", False, "no_book"),
               "poly_no": failed_result("HTTP 404", False, "no_book")}
    row = snapshot.make_row(T("2026-11-05 12:00", tz="UTC"), pair, results)
    assert row["kalshi_status"] == "error" and row["kalshi_book"] is None
    assert row["poly_yes_status"] == "no_book" and row["poly_yes_error"] == "HTTP 404"
    assert pd.isna(row["time_gap_seconds"]) and row["poly_yes_server_time"] is None


def test_rows_to_table_has_fixed_column_types():
    pair = make_pairs(1).to_dict("records")[0]
    cycle = T("2026-11-05 12:00", tz="UTC")
    failed = {book: failed_result() for book in snapshot.BOOKS}
    table = snapshot.rows_to_table([snapshot.make_row(cycle, pair, good_results()),
                                    snapshot.make_row(cycle, pair, failed)])
    assert list(table.columns) == snapshot.SNAPSHOT_COLUMNS
    for column in snapshot.TIME_COLUMNS:
        assert str(table[column].dtype) == "datetime64[us, UTC]", column
    assert str(table["kalshi_tries"].dtype) == "Int64"
    assert table["time_gap_seconds"].dtype == "float64"
    assert table.loc[1, "kalshi_book"] is pd.NA or pd.isna(table.loc[1, "kalshi_book"])


# ---------------------------------------------------------------------------
# Saving: one file per UTC day
# ---------------------------------------------------------------------------

def test_day_file_uses_the_utc_day(folders):
    assert snapshot.day_file(T("2026-11-04 23:55", tz="UTC")).name == "snapshots_2026-11-04.parquet"
    assert snapshot.day_file(T("2026-11-05 00:00", tz="UTC")).name == "snapshots_2026-11-05.parquet"


def one_cycle_table(cycle_time, count=2, results=None):
    pairs = make_pairs(count).to_dict("records")
    return snapshot.rows_to_table([snapshot.make_row(T(cycle_time, tz="UTC"), pair, results or good_results())
                                   for pair in pairs])


def test_append_adds_rows_below_the_old_ones(folders):
    path = snapshot.day_file(T("2026-11-05", tz="UTC"))
    first = one_cycle_table("2026-11-05 12:00")
    failed = {book: failed_result() for book in snapshot.BOOKS}
    second = one_cycle_table("2026-11-05 12:05", results=failed)  # a cycle with only failed books
    snapshot.append_to_day_file(first, path)
    size_mb = snapshot.append_to_day_file(second, path)

    saved = pd.read_parquet(path)
    assert len(saved) == 4
    assert list(saved["cycle_time"].dt.strftime("%H:%M")) == ["12:00", "12:00", "12:05", "12:05"]
    assert list(saved.dtypes) == list(first.dtypes)  # the types survive the round trip
    assert 0 < size_mb < 1
    assert not list(path.parent.glob("*.tmp"))  # the temporary file is gone


def test_append_moves_an_unreadable_file_aside(folders):
    path = snapshot.day_file(T("2026-11-05", tz="UTC"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a parquet file")
    snapshot.append_to_day_file(one_cycle_table("2026-11-05 12:00"), path)
    assert len(pd.read_parquet(path)) == 2
    moved = list(path.parent.glob("*.unreadable-*"))
    assert len(moved) == 1 and moved[0].read_bytes() == b"this is not a parquet file"


def test_stored_books_can_be_read_with_the_existing_parsers(folders):
    path = snapshot.day_file(T("2026-11-05", tz="UTC"))
    snapshot.append_to_day_file(one_cycle_table("2026-11-05 12:00", count=1), path)
    row = pd.read_parquet(path).iloc[0]

    top, _ = kalshi.parse_orderbook(json.loads(row["kalshi_book"]), row["kalshi_ticker"])
    assert top["best_yes_bid"] == 0.42 and top["best_yes_ask"] == 0.44  # 1 - 0.56

    top, levels = polymarket.parse_orderbook(json.loads(row["poly_yes_book"]),
                                             json.loads(row["poly_no_book"]), row["poly_market_id"])
    assert top["best_yes_bid"] == 0.41 and top["best_yes_ask"] == 0.44
    assert len(levels) == 8  # all levels of both books are kept
    assert polymarket.books_are_mirrored(json.loads(row["poly_yes_book"]), json.loads(row["poly_no_book"]))


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------

def fake_venues(gamma_open_ids, book_result=None):
    """A fake fetch(): Gamma lists gamma_open_ids as open; every book gets book_result (or a good book)."""
    calls = []

    def fake_fetch(session_name, url, params=None):
        calls.append(session_name)
        if session_name == "gamma":
            return {"status": "ok", "data": {"markets": [{"id": i, "closed": False} for i in gamma_open_ids]}}
        if book_result is not None:
            return book_result
        return good_results()[session_name]

    return fake_fetch, calls


def test_run_cycle_saves_only_pairs_open_on_both_venues(folders, monkeypatch):
    pairs = make_pairs(4)
    pairs.loc[0, "kalshi_close_time"] = T("2020-01-01", tz="UTC")  # Kalshi closed long ago
    pairs.to_parquet(snapshot.PAIRS_FILE)
    fake_fetch, calls = fake_venues(gamma_open_ids=["100", "101", "102"])  # Polymarket closed 103
    monkeypatch.setattr(snapshot, "fetch", fake_fetch)

    flags = {}
    cycle = T("2026-11-05 12:00", tz="UTC")
    with ThreadPoolExecutor(max_workers=3) as pool:
        saved = snapshot.run_cycle(cycle, flags, pool)

    assert saved == 2
    table = pd.read_parquet(snapshot.day_file(cycle))
    assert list(table["poly_market_id"]) == ["101", "102"]
    assert (table["cycle_time"] == cycle).all()
    assert flags == {"101": False, "102": False, "103": True}  # Gamma was asked only about Kalshi-open pairs
    assert calls.count("gamma") == 1 and calls.count("kalshi") == 2


def test_run_cycle_skips_the_rest_when_the_internet_is_down(folders, monkeypatch):
    make_pairs(6).to_parquet(snapshot.PAIRS_FILE)
    fake_fetch, calls = fake_venues(gamma_open_ids=[], book_result=failed_result())
    # Gamma cannot be reached either: the last known flags are used
    monkeypatch.setattr(snapshot, "fetch", lambda name, url, params=None:
                        failed_result() if name == "gamma" else fake_fetch(name, url, params))
    flags = {str(n): False for n in range(100, 106)}
    cycle = T("2026-11-05 12:00", tz="UTC")
    with ThreadPoolExecutor(max_workers=3) as pool:
        saved = snapshot.run_cycle(cycle, flags, pool)

    assert saved == snapshot.OFFLINE_PAIRS_LIMIT  # 3 pairs tried, then the cycle stopped
    table = pd.read_parquet(snapshot.day_file(cycle))
    assert (table["kalshi_status"] == "error").all() and table["kalshi_book"].isna().all()


def test_run_cycle_does_not_stop_for_one_failed_venue(folders, monkeypatch):
    make_pairs(5).to_parquet(snapshot.PAIRS_FILE)

    def fake_fetch(session_name, url, params=None):
        if session_name == "gamma":
            return {"status": "ok", "data": {"markets": [{"id": str(n), "closed": False} for n in range(100, 105)]}}
        if session_name == "kalshi":
            return failed_result()  # Kalshi unreachable, Polymarket fine
        return good_results()[session_name]

    monkeypatch.setattr(snapshot, "fetch", fake_fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert snapshot.run_cycle(T("2026-11-05 12:00", tz="UTC"), {}, pool) == 5


def test_run_cycle_keeps_going_after_an_unexpected_error_in_one_pair(folders, monkeypatch):
    make_pairs(3).to_parquet(snapshot.PAIRS_FILE)
    fake_fetch, calls = fake_venues(gamma_open_ids=["100", "101", "102"])

    def fetch_with_a_bug(session_name, url, params=None):
        if session_name == "kalshi" and "KX-1/" in url:
            raise RuntimeError("something unexpected")
        return fake_fetch(session_name, url, params)

    monkeypatch.setattr(snapshot, "fetch", fetch_with_a_bug)
    cycle = T("2026-11-05 12:00", tz="UTC")
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert snapshot.run_cycle(cycle, {}, pool) == 2
    assert list(pd.read_parquet(snapshot.day_file(cycle))["poly_market_id"]) == ["100", "102"]


def test_run_cycle_stops_after_the_current_pair_when_asked(folders, monkeypatch):
    make_pairs(5).to_parquet(snapshot.PAIRS_FILE)
    fake_fetch, calls = fake_venues(gamma_open_ids=[str(n) for n in range(100, 105)])

    def fetch_then_stop(session_name, url, params=None):
        if session_name == "kalshi":
            snapshot.STOP.set()  # the user presses Ctrl+C during the first pair
        return fake_fetch(session_name, url, params)

    monkeypatch.setattr(snapshot, "fetch", fetch_then_stop)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert snapshot.run_cycle(T("2026-11-05 12:00", tz="UTC"), {}, pool) == 1


def test_run_cycle_with_no_open_pairs_writes_nothing(folders, monkeypatch):
    make_pairs(2, kalshi_close="2020-01-01").to_parquet(snapshot.PAIRS_FILE)
    fake_fetch, calls = fake_venues(gamma_open_ids=[])
    monkeypatch.setattr(snapshot, "fetch", fake_fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert snapshot.run_cycle(T("2026-11-05 12:00", tz="UTC"), {}, pool) == 0
    assert not snapshot.SNAPSHOT_FOLDER.exists()


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------

def test_next_mark():
    assert snapshot.next_mark(T("2026-11-05 12:03:10", tz="UTC")) == T("2026-11-05 12:05", tz="UTC")
    assert snapshot.next_mark(T("2026-11-05 12:05:00", tz="UTC")) == T("2026-11-05 12:10", tz="UTC")
    assert snapshot.next_mark(T("2026-11-05 23:58", tz="UTC")) == T("2026-11-06 00:00", tz="UTC")
    assert snapshot.next_mark(T("2026-11-05 12:03:10", tz="UTC"), minutes=1) == T("2026-11-05 12:04", tz="UTC")


def test_run_forever_keeps_the_schedule_through_sleep_and_errors(monkeypatch):
    # The clock, as run_forever reads it: start, then before/after each cycle.
    clock = iter(T(moment, tz="UTC") for moment in [
        "2026-11-05 12:03:10",                          # start: first cycle at 12:05
        "2026-11-05 12:05:00.5", "2026-11-05 12:05:20",  # 12:05 runs, but fails
        "2026-11-05 12:12:00", "2026-11-05 12:12:00",    # woke up at 12:12: 12:10 is missed
        "2026-11-05 12:15:01", "2026-11-05 12:15:20",    # 12:15 runs; then we stop
    ])
    monkeypatch.setattr(snapshot, "now_utc", lambda: next(clock))
    monkeypatch.setattr(snapshot, "wait_until", lambda moment: None)
    monkeypatch.setattr(snapshot, "load_known_closed_flags", lambda: {})
    cycles = []

    def fake_cycle(cycle_time, closed_flags, pool):
        cycles.append(cycle_time)
        if len(cycles) == 1:
            raise RuntimeError("something unexpected")  # must not stop the program
        snapshot.STOP.set()
        return 0

    monkeypatch.setattr(snapshot, "run_cycle", fake_cycle)
    snapshot.run_forever()
    assert cycles == [T("2026-11-05 12:05", tz="UTC"), T("2026-11-05 12:15", tz="UTC")]


def test_wait_until_returns_when_a_stop_is_requested():
    snapshot.STOP.set()
    started = time.monotonic()
    snapshot.wait_until(snapshot.now_utc() + pd.Timedelta(hours=1))
    assert time.monotonic() - started < 1


# ---------------------------------------------------------------------------
# Only one copy at a time
# ---------------------------------------------------------------------------

def test_pid_lock_taken_and_released(folders):
    assert snapshot.take_pid_lock() == (True, None)
    assert snapshot.PID_FILE.read_text() == str(snapshot.os.getpid())
    snapshot.release_pid_lock()
    assert not snapshot.PID_FILE.exists()


def test_pid_lock_refused_while_another_copy_runs(folders):
    other = snapshot.os.getppid()  # a process that certainly runs: the one that started pytest
    snapshot.PID_FILE.write_text(str(other))
    assert snapshot.take_pid_lock() == (False, other)
    snapshot.release_pid_lock()  # not ours: must not be deleted
    assert snapshot.PID_FILE.read_text() == str(other)


def test_pid_lock_taken_over_from_a_copy_that_has_ended(folders):
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              capture_output=True, text=True, check=True)
    snapshot.PID_FILE.write_text(finished.stdout.strip())  # that process has ended
    assert snapshot.take_pid_lock() == (True, None)
    snapshot.PID_FILE.write_text("not a number")
    assert snapshot.take_pid_lock() == (True, None)
