"""
kalshi.py - Read public market data from Kalshi (no account, no API keys).

Kalshi is a regulated US prediction-market exchange. Its "Trade API" has
public, read-only GET endpoints for market data, and this file uses only
those: it never logs in and never places orders.
Official documentation: https://docs.kalshi.com

How Kalshi organises its markets:
    series  - a recurring topic,           e.g. KXFED  "Fed funds rate"
    event   - one occurrence of the topic, e.g. KXFED-25DEC  (Dec 2025 meeting)
    market  - one yes/no question,         e.g. KXFED-25DEC-T3.75
              ("Will the upper bound be above 3.75%?")
A YES contract pays $1 if the answer is yes, a NO contract pays $1 if it is
no, so prices are between $0 and $1 and can be read as probabilities.

The functions, from low level to high level:
    polite_get()            one GET request: throttled, retried, cached
    get_all_pages()         follow the "cursor" through every page of a list
    list_series(), get_series(), search_series()
    list_events()
    list_markets(), list_historical_markets(), list_all_markets()
    get_historical_cutoff(), is_historical()
    get_orderbook(), parse_orderbook(), orderbook_snapshot()
    candle_windows(), get_candles(), parse_candles(), blank_no_quote_markers()
    markets_table(), save_table()

Facts about the API that this code relies on (checked on 2026-11-04):
  * Lists come in pages. Each answer carries a "cursor"; sending it back
    returns the next page, until the cursor is empty.
    https://docs.kalshi.com/getting_started/pagination
    (GET /series is an exception: it sends all series at once, no cursor.)
  * Kalshi moves old data to a separate "historical" store. Markets that
    settled before the cutoff time given by GET /historical/cutoff, and their
    candlesticks, are only available from the /historical/... endpoints.
    https://docs.kalshi.com/getting_started/historical_data
  * Historical candlesticks use slightly different field names than live
    ones ("close" instead of "close_dollars", "volume" instead of
    "volume_fp"). parse_candles() accepts both.
  * One candlestick request may cover at most 5,000 candles, so long periods
    are downloaded in several pieces.
  * Kalshi only returns a candle for a period in which something happened
    (a trade, or a change in the best bid or ask). Quiet periods have no
    candle: the prices of the previous candle still hold. A candle with quote
    changes but no trade has empty trade-price fields.
  * In candlesticks Kalshi writes a YES bid of 0 when nobody bids and a YES
    ask of 1 when nobody offers. These "no quote" markers are turned into
    NaN (see blank_no_quote_markers), like an empty side of an order book.
  * Prices are strings in dollars ("0.4200" = $0.42), contract counts are
    strings too ("13.00" = 13 contracts).
    https://docs.kalshi.com/getting_started/fixed_point_migration
  * When it is busy, Kalshi answers HTTP 429 "too many requests". There is no
    penalty: wait and try again.
    https://docs.kalshi.com/getting_started/rate_limits

Typical use (from the project folder, in Python or a notebook):
    import sys; sys.path.append("src")
    import kalshi
    markets = kalshi.list_all_markets("KXFEDDECISION")
    top, depth = kalshi.parse_orderbook(kalshi.get_orderbook(markets[0]["ticker"]))
"""

import hashlib
import json
import math
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
# Kalshi's other official host serves the same data. To use it, set
#     kalshi.BASE_URL = FALLBACK_BASE_URL
FALLBACK_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder above src/
RAW_FOLDER = PROJECT_ROOT / "data" / "raw" / "kalshi"  # cache of raw answers (not committed)
PROCESSED_FOLDER = PROJECT_ROOT / "data" / "processed"  # clean Parquet tables (committed)

MIN_SECONDS_BETWEEN_REQUESTS = 0.25  # at most 4 requests per second
MAX_ATTEMPTS = 10                    # the first try + up to 9 retries
MAX_PAUSE_SECONDS = 30               # never wait longer than this between tries
TIMEOUT_SECONDS = 60                 # the full list of series is about 20 MB

MAX_CANDLES_PER_REQUEST = 5000       # Kalshi's limit (it answers HTTP 400 above it)
CANDLE_PERIODS = (1, 60, 1440)       # allowed candle lengths in minutes: minute, hour, day
MAX_FILE_MB = 50                     # project rule: every Parquet file under 50 MB

# Tell the server what is calling it (polite, and some servers require it).
HEADERS = {"User-Agent": "pm-arbitrage academic research script (read-only)"}

# Counters, so a long download can report what happened.
STATS = {"requests": 0, "retries": 0, "from_cache": 0}


class KalshiError(Exception):
    """Kalshi answered with an error that retrying will not fix."""


class NetworkBlockedError(Exception):
    """The cloud environment's network does not allow this domain."""


# ---------------------------------------------------------------------------
# Polite requests: throttling, retries and the raw-answer cache
# ---------------------------------------------------------------------------

SESSION = requests.Session()  # re-uses the connection: faster than separate requests
last_request_time = 0.0       # clock time at which the previous request was sent


def wait_for_turn():
    """Pause just long enough to send at most 4 requests per second."""
    global last_request_time
    seconds_since_last = time.monotonic() - last_request_time
    if seconds_since_last < MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - seconds_since_last)
    last_request_time = time.monotonic()


def cache_path(path, params):
    """The file in data/raw/kalshi/ where the answer to one request is saved.

    Example: path "/historical/markets/ABC/candlesticks" with
    params {"period_interval": 60, ...} is saved as
    data/raw/kalshi/historical/markets/ABC/candlesticks/end_ts=..._period_interval=60_....json
    """
    query = urlencode(sorted(params.items()))
    name = re.sub(r"[^A-Za-z0-9.=-]+", "_", query) or "index"
    if len(name) > 120:  # very long queries: shorten, keep it unique with a hash
        name = name[:80] + "_" + hashlib.sha1(query.encode()).hexdigest()[:12]
    folder = RAW_FOLDER / re.sub(r"[^A-Za-z0-9._/-]+", "_", path.strip("/"))
    return folder / f"{name}.json"


def polite_get(path, params=None, use_cache=True):
    """Send GET BASE_URL + path and return Kalshi's JSON answer (dicts and lists).

    * Parameters that are None are left out, so callers can pass optional
      filters without checking them first.
    * Throttled: at most 4 requests per second.
    * Retried: on HTTP 429 ("too many requests"), HTTP 5xx (server error) or a
      broken connection we wait 1, 2, 4, 8, 16, 30, 30 ... seconds and try
      again, up to MAX_ATTEMPTS times. Other errors (e.g. 400 "bad request",
      404 "not found") raise KalshiError at once: trying again would not help.
    * Cached: every answer is saved in data/raw/kalshi/. If the same request
      was already answered in this session, the saved answer is used and
      Kalshi is not asked again (unless use_cache=False, used for order
      books, which change every second).
    """
    params = {key: value for key, value in (params or {}).items() if value is not None}
    cache_file = cache_path(path, params)
    if use_cache and cache_file.exists():
        STATS["from_cache"] += 1
        return json.loads(cache_file.read_text(encoding="utf-8"))

    url = BASE_URL + path
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_for_turn()
        STATS["requests"] += 1
        try:
            response = SESSION.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.ProxyError as error:
            # In Claude Code cloud sessions all traffic passes through a proxy.
            # A 403 from the proxy means the domain is not on the allowed list.
            if "403" in str(error):
                domain = BASE_URL.split("/")[2]
                raise NetworkBlockedError(
                    f"The network blocked {domain}. Add this domain to your cloud "
                    f"environment's allowed domains: {domain}") from error
            problem = "network proxy error"
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
            problem = f"connection problem ({type(error).__name__})"
        else:
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    problem = "answer was not valid JSON"  # e.g. cut off on the way
                else:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(response.text, encoding="utf-8")
                    return data
            elif response.status_code == 429 or response.status_code >= 500:
                problem = f"HTTP {response.status_code}"
            else:
                raise KalshiError(
                    f"HTTP {response.status_code} for {url} {params}: {response.text[:300]}")

        if attempt == MAX_ATTEMPTS:
            raise KalshiError(f"Gave up after {MAX_ATTEMPTS} attempts ({problem}): {url} {params}")
        pause = min(2 ** (attempt - 1), MAX_PAUSE_SECONDS)  # 1, 2, 4, 8, 16, 30, 30 ...
        STATS["retries"] += 1
        if pause >= 8:  # short waits are routine (about half of all requests get a 429)
            print(f"      {problem} from Kalshi, attempt {attempt}: waiting {pause} s ...")
        time.sleep(pause)


def get_all_pages(path, list_key, params=None):
    """Download every page of a list and return all items in one Python list.

    Kalshi sends long lists in pages. Each answer has the items under
    `list_key` (e.g. "markets") and a "cursor"; sending the cursor back gives
    the next page. An empty or missing cursor means this was the last page.
    https://docs.kalshi.com/getting_started/pagination
    """
    params = dict(params or {})
    items = []
    cursors_seen = set()
    cursor = None
    while True:
        page = polite_get(path, {**params, "cursor": cursor})
        items.extend(page.get(list_key) or [])
        cursor = page.get("cursor")
        if not cursor:
            return items
        if cursor in cursors_seen:  # safety net: never loop forever
            raise KalshiError(f"Kalshi sent the same cursor twice for {path}")
        cursors_seen.add(cursor)


# ---------------------------------------------------------------------------
# Small conversion helpers
# ---------------------------------------------------------------------------

def to_float(value):
    """A Kalshi number ("0.4200", "13.00", 4.25 or a Decimal) as a float; missing -> NaN."""
    if value is None or value == "":
        return math.nan
    return float(value)


def to_utc(value):
    """A Kalshi time ("2025-12-10T18:59:00Z") as a pandas Timestamp in UTC; missing -> NaT."""
    if not value:
        return pd.NaT
    return pd.to_datetime(value, utc=True)


# ---------------------------------------------------------------------------
# Series, events and markets
# ---------------------------------------------------------------------------

def list_series(category=None, tags=None):
    """All series, optionally only those of one category (e.g. "Economics").

    Kalshi's GET /series has no status or series_ticker filter; to look up one
    series use get_series(). It also does not split its answer into pages
    today, but get_all_pages() would follow a cursor if Kalshi ever sends one.
    https://docs.kalshi.com/api-reference/market/get-series-list
    """
    return get_all_pages("/series", "series", {"category": category, "tags": tags})


def get_series(series_ticker):
    """One series (title, category, frequency, fee type, ...).
    https://docs.kalshi.com/api-reference/market/get-series
    """
    return polite_get(f"/series/{series_ticker}")["series"]


def search_series(words, category=None):
    """Find series whose ticker or title contains any of `words` (not case-sensitive).

    Returns a table with one row per matching series, to look through by eye.
    Example: search_series(["fed", "fomc"], category="Economics")
    """
    words = [word.lower() for word in words]
    hits = []
    for series in list_series(category=category):
        text = f"{series.get('ticker', '')} {series.get('title', '')}".lower()
        if any(word in text for word in words):
            hits.append({key: series.get(key) for key in ["ticker", "title", "category", "frequency"]})
    return pd.DataFrame(hits, columns=["ticker", "title", "category", "frequency"]).sort_values(
        "ticker", ignore_index=True)


def list_events(series_ticker=None, status=None, with_nested_markets=False):
    """Events, optionally filtered by series and status.

    status: "unopened", "open", "closed" or "settled" (None = any). An event
    matches a status if any of its markets has it.
    All events stay available here, also when their markets are historical.
    https://docs.kalshi.com/api-reference/events/get-events
    """
    params = {
        "series_ticker": series_ticker,
        "status": status,
        "with_nested_markets": "true" if with_nested_markets else None,
        "limit": 200,  # the largest page Kalshi allows for events
    }
    return get_all_pages("/events", "events", params)


def list_markets(series_ticker=None, event_ticker=None, status=None):
    """Markets in Kalshi's live data, optionally filtered by series, event and status.

    status: "unopened", "open", "paused", "closed" or "settled" (None = any).
    Markets that settled before the historical cutoff are NOT included; use
    list_historical_markets() or list_all_markets() for those.
    https://docs.kalshi.com/api-reference/market/get-markets
    """
    params = {
        "series_ticker": series_ticker,
        "event_ticker": event_ticker,
        "status": status,
        "limit": 1000,  # the largest page Kalshi allows for markets
    }
    return get_all_pages("/markets", "markets", params)


def list_historical_markets(series_ticker=None, event_ticker=None):
    """Markets that Kalshi has moved to its historical store (all of them settled).

    Kalshi accepts only one filter at a time here (series OR event).
    https://docs.kalshi.com/api-reference/historical/get-historical-markets
    """
    if series_ticker and event_ticker:
        raise ValueError("Kalshi's historical markets endpoint takes series_ticker OR event_ticker, not both")
    params = {"series_ticker": series_ticker, "event_ticker": event_ticker, "limit": 1000}
    return get_all_pages("/historical/markets", "markets", params)


def list_all_markets(series_ticker):
    """Every market of a series, from the live store and the historical store.

    Each market dict gets two extra keys added by this code:
        "series_ticker"  - the series it belongs to (Kalshi's market objects lack it)
        "source"         - "live" or "historical": where we found it
    The live store is read first: markets only move from live to historical,
    so this order avoids missing one that moves while we read. A market found
    in both stores is kept once.
    """
    combined = {}
    for source, markets in [("live", list_markets(series_ticker=series_ticker)),
                            ("historical", list_historical_markets(series_ticker=series_ticker))]:
        for market in markets:
            if market["ticker"] not in combined:
                combined[market["ticker"]] = {**market, "series_ticker": series_ticker, "source": source}
    return list(combined.values())


# ---------------------------------------------------------------------------
# Live versus historical data
# ---------------------------------------------------------------------------

def get_historical_cutoff():
    """The time before which settled markets live only in the historical store.

    Asked fresh every time (not from the cache), because Kalshi moves it forward.
    https://docs.kalshi.com/api-reference/historical/get-historical-cutoff-timestamps
    """
    answer = polite_get("/historical/cutoff", use_cache=False)
    return to_utc(answer["market_settled_ts"])


def is_historical(market, cutoff):
    """True if the market settled before the cutoff.

    Kalshi's rule: markets that settled before `market_settled_ts`, and their
    candlesticks, are only available from the /historical/... endpoints.
    https://docs.kalshi.com/getting_started/historical_data
    Markets that have not settled yet (no settlement time) are always live.
    """
    settled = market.get("settlement_ts")
    return bool(settled) and to_utc(settled) < cutoff


# ---------------------------------------------------------------------------
# Order books
# ---------------------------------------------------------------------------

ONE_DOLLAR = Decimal("1")

ORDERBOOK_TOP_COLUMNS = [
    "snapshot_time", "ticker",
    "best_yes_bid", "best_yes_bid_size", "best_yes_ask", "best_yes_ask_size",
    "best_no_bid", "best_no_bid_size", "best_no_ask", "best_no_ask_size",
    "spread", "yes_bid_levels", "no_bid_levels",
]
ORDERBOOK_LEVEL_COLUMNS = ["snapshot_time", "ticker", "outcome", "side", "level", "price", "quantity"]


def get_orderbook(ticker):
    """A market's current order book, exactly as Kalshi sends it (all price levels).

    Always asked fresh (never from the cache): order books change constantly.
    https://docs.kalshi.com/api-reference/market/get-market-orderbook
    """
    return polite_get(f"/markets/{ticker}/orderbook", use_cache=False)


def read_bids(pairs):
    """[["0.4200", "13.00"], ...] -> [(Decimal("0.4200"), Decimal("13.00")), ...], best price first.

    The best bid is the HIGHEST price. We sort ourselves instead of trusting
    Kalshi's order: one docs page says the list is ascending, another says
    best to worst. Decimal keeps the exact digits, so 1 - 0.56 is exactly
    0.44 (with ordinary floats it would be 0.43999999999999995).
    """
    bids = [(Decimal(price), Decimal(count)) for price, count in (pairs or [])]
    return sorted(bids, reverse=True)


def parse_orderbook(raw, ticker=None, snapshot_time=None):
    """Turn Kalshi's bids-only order book into best bids/asks and the full depth.

    raw:            the answer of get_orderbook()
    ticker, snapshot_time: copied into the output, to label the rows

    Kalshi lists only bids: orderbook_fp.yes_dollars (bids to buy YES) and
    orderbook_fp.no_dollars (bids to buy NO), each a list of
    [price_dollars, count]. A YES and a NO contract together always pay
    exactly $1, so a bid to buy NO at price p is also an offer to sell YES at
    1 - p, for the same number of contracts (and the other way round).
    Formulas, from https://docs.kalshi.com/getting_started/orderbook_responses
        best YES bid = highest YES bid
        best YES ask = 1 - highest NO bid     (size: the size of that NO bid)
        best NO bid  = highest NO bid
        best NO ask  = 1 - highest YES bid    (size: the size of that YES bid)
        spread       = best YES ask - best YES bid  (the NO spread is the same number)
    If one side of the book is empty, the prices that depend on it are NaN.

    Returns two things:
      top    - a dict: one row of the kalshi_orderbooks table
      levels - a DataFrame: every price level of all four ladders (YES bids,
               YES asks, NO bids, NO asks), best price first (level 1)
    """
    book = (raw or {}).get("orderbook_fp") or {}
    yes_bids = read_bids(book.get("yes_dollars"))
    no_bids = read_bids(book.get("no_dollars"))

    # The four ladders. The asks are the other side's bids seen from the
    # opposite outcome: price 1 - p, same quantity. Since the bids are sorted
    # highest first, the asks come out lowest (= best) first.
    ladders = {
        ("yes", "bid"): yes_bids,
        ("yes", "ask"): [(ONE_DOLLAR - price, count) for price, count in no_bids],
        ("no", "bid"): no_bids,
        ("no", "ask"): [(ONE_DOLLAR - price, count) for price, count in yes_bids],
    }

    # Best prices: the first level of each ladder.
    top = {"snapshot_time": snapshot_time, "ticker": ticker}
    for (outcome, side), ladder in ladders.items():
        best_price, best_size = ladder[0] if ladder else (None, None)
        top[f"best_{outcome}_{side}"] = to_float(best_price)
        top[f"best_{outcome}_{side}_size"] = to_float(best_size)
    if yes_bids and no_bids:
        top["spread"] = float((ONE_DOLLAR - no_bids[0][0]) - yes_bids[0][0])
    else:
        top["spread"] = math.nan
    top["yes_bid_levels"] = len(yes_bids)
    top["no_bid_levels"] = len(no_bids)

    # Full depth: one row per price level.
    rows = []
    for (outcome, side), ladder in ladders.items():
        for level, (price, count) in enumerate(ladder, start=1):
            rows.append({"snapshot_time": snapshot_time, "ticker": ticker, "outcome": outcome,
                         "side": side, "level": level, "price": float(price), "quantity": float(count)})
    levels = pd.DataFrame(rows, columns=ORDERBOOK_LEVEL_COLUMNS)
    return top, levels


def orderbook_snapshot(tickers):
    """Download and convert the current order book of every market in `tickers`.

    Returns two tables: (one row per market, one row per price level).
    The snapshot time (UTC) is taken when each answer arrives. A market whose
    order book cannot be downloaded is skipped with a warning.
    """
    tops, depth_tables = [], []
    for number, ticker in enumerate(tickers, start=1):
        try:
            raw = get_orderbook(ticker)
        except KalshiError as error:
            print(f"   WARNING: no order book for {ticker}: {error}")
            continue
        top, levels = parse_orderbook(raw, ticker, pd.Timestamp.now(tz="UTC"))
        tops.append(top)
        depth_tables.append(levels)
        if number % 25 == 0:
            print(f"   order books: {number}/{len(tickers)} done")
    top_table = pd.DataFrame(tops, columns=ORDERBOOK_TOP_COLUMNS)
    depth_tables = [table for table in depth_tables if len(table)]
    if depth_tables:
        depth_table = pd.concat(depth_tables, ignore_index=True)
    else:
        depth_table = pd.DataFrame(columns=ORDERBOOK_LEVEL_COLUMNS)
    return top_table, depth_table


# ---------------------------------------------------------------------------
# Candlesticks (price history)
# ---------------------------------------------------------------------------

CANDLE_COLUMNS = [
    "ticker", "period_minutes", "period_end",
    "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
    "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
    "price_open", "price_high", "price_low", "price_close", "price_mean", "price_previous",
    "volume", "open_interest", "source",
]


def candle_windows(start_ts, end_ts, period_minutes):
    """Split the time range [start_ts, end_ts] into pieces of at most 5,000 candles.

    Times are Unix seconds. Kalshi returns the candles that END inside a
    window (both ends included), so the windows touch but do not overlap:
    each one starts one second after the previous one ends.
    """
    step = (MAX_CANDLES_PER_REQUEST - 1) * period_minutes * 60
    windows = []
    start = start_ts
    while start < end_ts:
        end = min(start + step, end_ts)
        windows.append((start, end))
        start = end + 1
    return windows


def parse_candles(candles, ticker, period_minutes, source):
    """Turn Kalshi's candlesticks into a tidy table: one row per period.

    Each row describes the period that ENDS at `period_end` (UTC), e.g. the
    hourly candle with period_end 14:00 covers 13:00-14:00. Columns:
        yes_bid_*  best bid to buy YES: open/high/low/close in the period
        yes_ask_*  best offer to sell YES: open/high/low/close
        price_*    traded YES prices: open/high/low/close/mean, and the last
                   trade before the period (NaN when nothing traded)
        volume     contracts traded in the period
        open_interest  contracts outstanding at the end of the period
        source     "live" or "historical": which endpoint we used
    Kalshi skips quiet periods (no trade and no change in the best bid or
    ask), so the table has gaps; during a gap the previous candle's close
    prices still hold.
    A yes_bid_* or yes_ask_* value is NaN when there was no quote: Kalshi's
    "no quote" markers (bid 0, ask 1) are blanked, see blank_no_quote_markers().
    Live answers name fields "close_dollars", "volume_fp", "open_interest_fp";
    historical ones "close", "volume", "open_interest". Both are accepted.
    https://docs.kalshi.com/api-reference/market/get-market-candlesticks
    https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks
    """
    rows = []
    for candle in candles:
        row = {"ticker": ticker, "period_minutes": period_minutes, "period_end": candle["end_period_ts"]}
        for group, fields in [("yes_bid", ["open", "high", "low", "close"]),
                              ("yes_ask", ["open", "high", "low", "close"]),
                              ("price", ["open", "high", "low", "close", "mean", "previous"])]:
            values = candle.get(group) or {}
            for field in fields:
                row[f"{group}_{field}"] = to_float(values.get(f"{field}_dollars", values.get(field)))
        row["volume"] = to_float(candle.get("volume_fp", candle.get("volume")))
        row["open_interest"] = to_float(candle.get("open_interest_fp", candle.get("open_interest")))
        row["source"] = source
        rows.append(row)
    table = pd.DataFrame(rows, columns=CANDLE_COLUMNS)
    table["period_end"] = pd.to_datetime(table["period_end"], unit="s", utc=True)
    table = table.drop_duplicates(subset=["ticker", "period_end"])
    table = blank_no_quote_markers(table)
    return table.sort_values("period_end", ignore_index=True)


CANDLE_BID_COLUMNS = ["yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close"]
CANDLE_ASK_COLUMNS = ["yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close"]


def blank_no_quote_markers(table):
    """Replace Kalshi's "no quote" markers in a candle table with NaN.

    In candlesticks Kalshi writes a YES bid of 0 when nobody bids and a YES
    ask of 1 when nobody offers. These are markers, not prices, so they
    become NaN: the same way parse_orderbook() leaves an empty side empty
    (https://docs.kalshi.com/getting_started/orderbook_responses).
    Field meanings: https://docs.kalshi.com/api-reference/market/get-market-candlesticks

    Kalshi's documentation does not spell this convention out; we checked it
    ourselves on 2026-11-04: all 36 open markets whose order book had no YES
    bids showed a YES bid of 0.0000 on Kalshi's market object, all 74 without
    NO bids (that is, without YES offers) showed a YES ask of 1.0000, and in
    1.8 million hourly candles every other quote was between 0.01 and 0.99
    and no trade happened at 0 or 1.

    Within one period only the marker values change: if bids appear during
    the hour, yes_bid_open and yes_bid_low become NaN, but yes_bid_high and
    yes_bid_close keep the real prices. Trade prices (price_*) are left
    alone. Returns a new table; the input table is not changed.
    """
    table = table.copy()
    table[CANDLE_BID_COLUMNS] = table[CANDLE_BID_COLUMNS].mask(table[CANDLE_BID_COLUMNS] == 0)
    table[CANDLE_ASK_COLUMNS] = table[CANDLE_ASK_COLUMNS].mask(table[CANDLE_ASK_COLUMNS] == 1)
    return table


def get_candles(market, period_minutes=60, cutoff=None):
    """Download one market's price history as candlesticks (see parse_candles).

    market:          a market dict from list_all_markets() (it needs ticker,
                     series_ticker, open_time, close_time and settlement_ts)
    period_minutes:  1, 60 or 1440 (one minute, one hour or one day)
    cutoff:          the result of get_historical_cutoff() (asked if missing)

    Covers the market's whole life: from its open time to its close time, or
    to now if it is still open. "Now" is rounded down to a whole period, so
    only finished candles are requested. Markets that settled before the
    cutoff are read from the historical endpoint, all others from the live one.
    """
    if period_minutes not in CANDLE_PERIODS:
        raise ValueError(f"period_minutes must be one of {CANDLE_PERIODS}")
    if cutoff is None:
        cutoff = get_historical_cutoff()
    ticker = market["ticker"]

    period_seconds = period_minutes * 60
    now_ts = int(time.time()) // period_seconds * period_seconds
    start_ts = int(to_utc(market["open_time"]).timestamp())
    end_ts = min(int(to_utc(market["close_time"]).timestamp()), now_ts)

    if is_historical(market, cutoff):
        source, path = "historical", f"/historical/markets/{ticker}/candlesticks"
    else:
        source, path = "live", f"/series/{market['series_ticker']}/markets/{ticker}/candlesticks"

    candles = []
    for window_start, window_end in candle_windows(start_ts, end_ts, period_minutes):
        params = {"start_ts": window_start, "end_ts": window_end, "period_interval": period_minutes}
        candles.extend(polite_get(path, params).get("candlesticks") or [])
    return parse_candles(candles, ticker, period_minutes, source)


# ---------------------------------------------------------------------------
# Tidy tables and saving
# ---------------------------------------------------------------------------

MARKET_COLUMNS = [
    "ticker", "event_ticker", "series_ticker", "title", "yes_sub_title", "event_title",
    "category", "status", "result", "open_time", "close_time", "settlement_time",
    "rules_primary", "rules_secondary", "market_type", "strike_type", "floor_strike",
    "cap_strike", "last_price", "volume", "open_interest", "settlement_value", "source",
]
MARKET_TIME_COLUMNS = ["open_time", "close_time", "settlement_time"]


def markets_table(markets, events=(), series=()):
    """One tidy row per market.

    markets:  market dicts from list_all_markets()
    events:   event dicts from list_events()  -> adds the event title
    series:   series dicts from get_series()  -> adds the category (Kalshi
              keeps the category on the series; the event-level one is deprecated)

    Columns: identifiers (ticker, event_ticker, series_ticker); texts (title =
    the market's question, yes_sub_title = the outcome's short label,
    event_title, rules_primary + rules_secondary = the full rules); category;
    status (e.g. "active", "finalized") and result ("yes"/"no", empty if not
    known yet); times in UTC; strikes (the numbers in the question, e.g. 3.75
    for "above 3.75%"); last traded price in dollars; volume and open
    interest in contracts; settlement value of YES in dollars; and source
    ("live"/"historical").
    """
    event_titles = {event["event_ticker"]: event.get("title") for event in events}
    categories = {one["ticker"]: one.get("category") for one in series}
    rows = []
    for market in markets:
        rows.append({
            "ticker": market["ticker"],
            "event_ticker": market.get("event_ticker"),
            "series_ticker": market.get("series_ticker"),
            "title": market.get("title"),
            "yes_sub_title": market.get("yes_sub_title"),
            "event_title": event_titles.get(market.get("event_ticker")),
            "category": categories.get(market.get("series_ticker")),
            "status": market.get("status"),
            "result": market.get("result") or None,  # Kalshi sends "" while unknown
            "open_time": market.get("open_time"),
            "close_time": market.get("close_time"),
            "settlement_time": market.get("settlement_ts"),
            "rules_primary": market.get("rules_primary"),
            "rules_secondary": market.get("rules_secondary"),
            "market_type": market.get("market_type"),
            "strike_type": market.get("strike_type"),
            "floor_strike": to_float(market.get("floor_strike")),
            "cap_strike": to_float(market.get("cap_strike")),
            "last_price": to_float(market.get("last_price_dollars")),
            "volume": to_float(market.get("volume_fp")),
            "open_interest": to_float(market.get("open_interest_fp")),
            "settlement_value": to_float(market.get("settlement_value_dollars")),
            "source": market.get("source"),
        })
    table = pd.DataFrame(rows, columns=MARKET_COLUMNS)
    for column in MARKET_TIME_COLUMNS:
        table[column] = pd.to_datetime(table[column], utc=True, format="ISO8601")
    return table


def save_table(table, name):
    """Save a table as data/processed/<name>.parquet and return the file path.

    Refuses (and keeps any older file) if the new file would be 50 MB or
    more, the project's limit for committed files.
    """
    PROCESSED_FOLDER.mkdir(parents=True, exist_ok=True)
    final_path = PROCESSED_FOLDER / f"{name}.parquet"
    temporary_path = PROCESSED_FOLDER / f"{name}.parquet.tmp"
    table.to_parquet(temporary_path, index=False, compression="zstd")
    size_mb = temporary_path.stat().st_size / 1_000_000
    if size_mb >= MAX_FILE_MB:
        temporary_path.unlink()
        raise ValueError(f"{final_path.name} would be {size_mb:.1f} MB, over the {MAX_FILE_MB} MB "
                         f"limit. Save fewer rows, or split it into several files.")
    temporary_path.replace(final_path)
    return final_path
