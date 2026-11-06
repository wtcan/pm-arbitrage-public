"""
polymarket.py - Read public market data from Polymarket (no account, no API keys).

Polymarket is a prediction market. Its market data is served by three public,
read-only web APIs, and this file uses only their GET endpoints: it never logs
in, never signs anything and never places orders.
Official documentation: https://docs.polymarket.com (index: https://docs.polymarket.com/llms.txt)

    Gamma API  https://gamma-api.polymarket.com   events, markets, tags, search
    CLOB API   https://clob.polymarket.com        order books
    Data API   https://data-api.polymarket.com    price history and trades

How Polymarket organises its markets:
    series  - a recurring topic,              e.g. "fomc" (one event per Fed meeting)
    event   - one question shown to users,    e.g. "Fed Decision in October?"
    market  - one yes/no question inside it,  e.g. "Will there be no change in Fed
              interest rates after the October 2026 meeting?"
Each market has two outcome tokens, YES and NO. A YES token pays $1 if the
answer is yes, a NO token pays $1 if it is no, so prices are between $0 and $1.
https://docs.polymarket.com/concepts/markets-events.md

The functions, from low level to high level:
    polite_get()                         one GET request: throttled, retried, cached
    get_keyset_pages(), get_data_api_pages()   follow the pages of a list
    list_events(), get_events_by_id(), get_event(), search_events()
    list_markets(), get_market(), get_tag()
    token_ids(), resolved_outcome(), category_from_tags()
    get_orderbook(), parse_orderbook(), books_are_mirrored(), orderbook_snapshot()
    get_price_history(), parse_price_history()
    keep_trade_fields(), get_trades(), trades_table(), trade_bars()
    markets_table(), save_table()

Facts about the APIs that this code relies on (checked on 2026-11-04):
  * Gamma sends long lists in "keyset" pages of at most 100 items. Each answer
    has a "next_cursor"; sending it back as "after_cursor" gives the next page.
    https://docs.polymarket.com/market-data/discover-markets.md
  * Gamma's event lists contain open AND closed events when "closed" is left
    out, and each event carries its markets. (Gamma's market lists would
    return only open markets by default.) Gamma returns only markets of the
    older "CTF" protocol (version "v1") unless both versions are asked for,
    so we always ask for "v1" and "v2".
  * A market's YES/NO token ids are in "clobTokenIds" (a JSON text) for v1
    markets and in the list "positionIds" for v2 markets, in the order of
    "outcomes": index 0 = YES, index 1 = NO. The full resolution rules are in
    the market's "description".
    https://docs.polymarket.com/market-data/market-details.md
  * Gamma's "category" field is empty for the Fed and CPI markets. What a
    market charges is in its own "feeSchedule" (rate, exponent, takerOnly,
    rebateRate), and Polymarket says fees must be calculated from it.
    https://docs.polymarket.com/trading/fees.md
    https://docs.polymarket.com/changelog/predictions.md (Mar 31, 2026)
  * Each outcome token has its own order book (GET /book). Bids are listed
    from the lowest price up and asks from the highest price down, so the best
    prices come LAST. A closed market has no book: the CLOB answers HTTP 404.
    https://docs.polymarket.com/market-data/prices-order-books.md
  * Price history moved from the CLOB to the Data API on 2026-09-04:
    GET /v2/prices-history?token_id=<outcome token id>. The old CLOB route
    returns an empty history for old markets.
    https://docs.polymarket.com/changelog/predictions.md (Sep 4, 2026)
  * Polymarket keeps 3-hour and 12-hour prices forever, finer ones only for a
    while (hourly: about the last 30 days). Before about spring 2026 the
    3-hour series has only one point every 12 hours.
  * Trades: GET /v2/trades?condition=<condition id>, newest first, covering
    the last three years. https://data-api.polymarket.com/v2/openapi.json

Typical use (from the project folder, in Python or a notebook):
    import sys; sys.path.append("src")
    import polymarket
    events = polymarket.list_events(tag_slug="fed-rates")
    table = polymarket.markets_table(events)
    yes_book = polymarket.get_orderbook(table.loc[0, "yes_token_id"])
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

GAMMA_URL = "https://gamma-api.polymarket.com"  # events, markets, tags, search
CLOB_URL = "https://clob.polymarket.com"        # order books
DATA_URL = "https://data-api.polymarket.com"    # price history, trades

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder above src/
RAW_FOLDER = PROJECT_ROOT / "data" / "raw" / "polymarket"  # cache of raw answers (not committed)
PROCESSED_FOLDER = PROJECT_ROOT / "data" / "processed"     # clean Parquet tables (committed)

MIN_SECONDS_BETWEEN_REQUESTS = 0.25  # at most 4 requests per second, all three APIs together
MAX_ATTEMPTS = 8                     # the first try + up to 7 retries
MAX_PAUSE_SECONDS = 30               # never wait longer than this between tries
TIMEOUT_SECONDS = 60

GAMMA_PAGE_SIZE = 100                # the largest page Gamma's keyset lists allow
PRICE_PAGE_SIZE = 10000              # the largest page of /v2/prices-history
TRADE_PAGE_SIZE = 1000               # the largest page of /v2/trades
PROTOCOL_VERSIONS = ["v1", "v2"]     # ask Gamma for markets of both protocols
MAX_FILE_MB = 50                     # project rule: every Parquet file under 50 MB

# Widths of the price-history buckets, in seconds.
FULL_LIFE_BUCKET_SECONDS = 3 * 3600  # 3 hours: kept for a market's whole life
RECENT_BUCKET_SECONDS = 3600         # 1 hour: kept for about the last 30 days

# Tell the servers what is calling them (polite, and some servers require it).
HEADERS = {"User-Agent": "pm-arbitrage academic research script (read-only)"}

# Counters, so a long download can report what happened.
STATS = {"requests": 0, "retries": 0, "from_cache": 0}


class PolymarketError(Exception):
    """Polymarket answered with an error that retrying will not fix."""


class NotFoundError(PolymarketError):
    """HTTP 404: what was asked for does not exist (e.g. no order book)."""


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


def cache_path(base_url, path, params):
    """The file in data/raw/polymarket/ where the answer to one request is saved.

    Example: GAMMA_URL, "/events/keyset", {"tag_slug": "cpi", ...} is saved as
    data/raw/polymarket/gamma-api/events/keyset/limit=100_tag_slug=cpi_....json
    Lists in params (e.g. version=["v1", "v2"]) become repeated keys.
    """
    host = base_url.split("/")[2].split(".")[0]  # "gamma-api", "clob" or "data-api"
    query = urlencode(sorted(params.items()), doseq=True)
    name = re.sub(r"[^A-Za-z0-9.=-]+", "_", query) or "index"
    if len(name) > 120:  # very long queries (token ids, cursors): shorten, keep unique with a hash
        name = name[:80] + "_" + hashlib.sha1(query.encode()).hexdigest()[:12]
    folder = RAW_FOLDER / host / re.sub(r"[^A-Za-z0-9._/-]+", "_", path.strip("/"))
    return folder / f"{name}.json"


def seconds_to_wait(retry_after):
    """The number in a "Retry-After" header (seconds), or None if there is none."""
    try:
        return max(0.0, float(retry_after))
    except (TypeError, ValueError):
        return None


def polite_get(base_url, path, params=None, use_cache=True, clean=None):
    """Send GET base_url + path and return the JSON answer (dicts and lists).

    * Parameters that are None are left out, so callers can pass optional
      filters without checking them first. A list becomes a repeated key.
    * Cleaned: if `clean` (a function) is given, the answer is passed through
      it BEFORE it is saved in the cache or returned, so the cache only ever
      holds what clean() keeps. get_trades() uses this so that the traders'
      identities are never written to disk.
    * Throttled: at most 4 requests per second.
    * Retried: on HTTP 429 ("too many requests"), HTTP 5xx (server error) or a
      broken connection we wait 1, 2, 4, 8, 16, 30 ... seconds (or longer if
      the server asks for it with "Retry-After") and try again, up to
      MAX_ATTEMPTS times. HTTP 404 raises NotFoundError and other errors
      (e.g. 400 "bad request") raise PolymarketError at once: trying again
      would not help.
    * Cached: every answer is saved in data/raw/polymarket/. If the same
      request was already answered in this session, the saved answer is used
      and Polymarket is not asked again (unless use_cache=False, used for
      order books, which change every second).
    """
    params = {key: value for key, value in (params or {}).items() if value is not None}
    cache_file = cache_path(base_url, path, params)
    if use_cache and cache_file.exists():
        STATS["from_cache"] += 1
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        # Clean again: harmless if the file is already clean, and safe if it
        # was saved earlier by a call without clean().
        return clean(data) if clean is not None else data

    url = base_url + path
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_for_turn()
        STATS["requests"] += 1
        server_pause = None
        try:
            response = SESSION.get(url, params=params, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.ProxyError as error:
            # In Claude Code cloud sessions all traffic passes through a proxy.
            # A 403 from the proxy means the domain is not on the allowed list.
            if "403" in str(error):
                domain = base_url.split("/")[2]
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
                    text = response.text
                    if clean is not None:
                        data = clean(data)
                        text = json.dumps(data)  # save the cleaned answer, not the original
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(text, encoding="utf-8")
                    return data
            elif response.status_code == 429 or response.status_code >= 500:
                problem = f"HTTP {response.status_code}"
                server_pause = seconds_to_wait(response.headers.get("Retry-After"))
            elif response.status_code == 404:
                raise NotFoundError(f"HTTP 404 for {url} {params}: {response.text[:300]}")
            else:
                raise PolymarketError(
                    f"HTTP {response.status_code} for {url} {params}: {response.text[:300]}")

        if attempt == MAX_ATTEMPTS:
            raise PolymarketError(f"Gave up after {MAX_ATTEMPTS} attempts ({problem}): {url} {params}")
        pause = min(2 ** (attempt - 1), MAX_PAUSE_SECONDS)  # 1, 2, 4, 8, 16, 30, 30 ...
        if server_pause is not None:
            pause = min(max(pause, server_pause), MAX_PAUSE_SECONDS)
        STATS["retries"] += 1
        if pause >= 8:
            print(f"      {problem} from Polymarket, attempt {attempt}: waiting {pause:.0f} s ...")
        time.sleep(pause)


def get_keyset_pages(path, list_key, params=None):
    """Download every page of a Gamma list and return all items in one list.

    Gamma's keyset lists answer {list_key: [...], "next_cursor": "..."}.
    Sending next_cursor back as "after_cursor" gives the next page; an empty
    or missing next_cursor means this was the last page.
    https://docs.polymarket.com/market-data/discover-markets.md
    """
    params = dict(params or {})
    items = []
    cursors_seen = set()
    cursor = None
    while True:
        page = polite_get(GAMMA_URL, path, {**params, "after_cursor": cursor})
        items.extend(page.get(list_key) or [])
        cursor = page.get("next_cursor")
        if not cursor:
            return items
        if cursor in cursors_seen:  # safety net: never loop forever
            raise PolymarketError(f"Gamma sent the same cursor twice for {path}")
        cursors_seen.add(cursor)


def get_data_api_pages(path, params=None, max_pages=None, clean=None):
    """Download every page of a Data API list and return all rows in one list.

    Data API answers look like {"data": [...], "pagination": {"has_more": ...,
    "next_cursor": "..."}}. Sending next_cursor back as "cursor" gives the
    next page; a null next_cursor means this was the last page.
    max_pages: stop after this many pages (None = all).
    clean:     optional function applied to each page before it is cached
               (see polite_get); it must keep "pagination".
    https://data-api.polymarket.com/v2/openapi.json (schema "Pagination")
    """
    params = dict(params or {})
    rows = []
    cursors_seen = set()
    cursor = None
    pages = 0
    while True:
        page = polite_get(DATA_URL, path, {**params, "cursor": cursor}, clean=clean)
        rows.extend(page.get("data") or [])
        pages += 1
        cursor = (page.get("pagination") or {}).get("next_cursor")
        if not cursor or (max_pages is not None and pages >= max_pages):
            return rows
        if cursor in cursors_seen:  # safety net: never loop forever
            raise PolymarketError(f"The Data API sent the same cursor twice for {path}")
        cursors_seen.add(cursor)


# ---------------------------------------------------------------------------
# Small conversion helpers
# ---------------------------------------------------------------------------

def to_float(value):
    """A Polymarket number ("0.45", 0.45 or a Decimal) as a float; missing -> NaN."""
    if value is None or value == "":
        return math.nan
    return float(value)


def to_utc(value):
    """A Polymarket time ("2026-10-29T03:59:00Z", "2026-10-28 20:01:40+00") in UTC; missing -> NaT."""
    if not value:
        return pd.NaT
    return pd.to_datetime(value, utc=True, format="ISO8601")


def parse_json_list(value):
    """Gamma sends some lists as JSON text ('["Yes", "No"]'). Return a real list ([] if missing)."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


# ---------------------------------------------------------------------------
# Events, markets, tags and search (Gamma API)
# ---------------------------------------------------------------------------

def list_events(tag_slug=None, tag_id=None, series_id=None, title_search=None, closed=None):
    """Events, each with its markets, open and closed, from GET /events/keyset.

    Filters (all optional, combine as you like):
        tag_slug      e.g. "fed-rates"; tag_id the tag's number instead (see get_tag)
        series_id     the number of a series, e.g. 35 = "fomc" (one event per Fed meeting)
        title_search  a keyword that must appear in the event title, e.g. "inflation"
        closed        None = open and closed events, True = closed only, False = open only
    Both protocol versions are asked for (Gamma's default is "v1" only).
    https://docs.polymarket.com/market-data/discover-markets.md#list-events
    """
    params = {
        "tag_slug": tag_slug,
        "tag_id": tag_id,
        "series_id": series_id,
        "title_search": title_search,
        "closed": None if closed is None else str(bool(closed)).lower(),
        "version": PROTOCOL_VERSIONS,
        "limit": GAMMA_PAGE_SIZE,
    }
    return get_keyset_pages("/events/keyset", "events", params)


def get_events_by_id(event_ids):
    """The full records (with markets) of the events in event_ids, asked 50 at a time."""
    event_ids = list(dict.fromkeys(str(event_id) for event_id in event_ids))  # unique, same order
    events = []
    for start in range(0, len(event_ids), 50):
        params = {"id": event_ids[start:start + 50], "version": PROTOCOL_VERSIONS, "limit": GAMMA_PAGE_SIZE}
        events += get_keyset_pages("/events/keyset", "events", params)
    return events


def get_event(event_id):
    """One event, with its markets. https://docs.polymarket.com/market-data/discover-markets.md#fetch-an-event"""
    return polite_get(GAMMA_URL, f"/events/{event_id}")


def search_events(query, max_pages=20):
    """Events whose text matches `query` (Polymarket's own search), open and closed.

    GET /public-search answers in numbered pages ("page" = 1, 2, ...) and says
    in pagination.hasMore whether another page exists. Search results can be
    shortened: use get_events_by_id() on the ids for the full records.
    https://docs.polymarket.com/market-data/discover-markets.md#search
    """
    events = {}
    for page_number in range(1, max_pages + 1):
        params = {"q": query, "page": page_number, "limit_per_type": 50, "keep_closed_markets": 1}
        answer = polite_get(GAMMA_URL, "/public-search", params)
        for event in answer.get("events") or []:
            events.setdefault(event["id"], event)
        if not (answer.get("pagination") or {}).get("hasMore"):
            break
    return list(events.values())


def list_markets(tag_id=None, closed=None):
    """Markets (without their event's details), from GET /markets/keyset.

    closed: None = open and closed markets, True = closed only, False = open only.
    Careful: Gamma's market list returns only OPEN markets unless told
    otherwise, so for None we ask twice (closed=false, then closed=true).
    For most uses list_events() is better: it also gives each market's event.
    https://docs.polymarket.com/market-data/discover-markets.md#list-markets
    """
    states = [False, True] if closed is None else [bool(closed)]
    markets = []
    for state in states:
        params = {"tag_id": tag_id, "closed": str(state).lower(), "version": PROTOCOL_VERSIONS,
                  "limit": GAMMA_PAGE_SIZE}
        markets += get_keyset_pages("/markets/keyset", "markets", params)
    return markets


def get_market(market_id):
    """One market by its Gamma id. https://docs.polymarket.com/market-data/discover-markets.md#fetch-a-market"""
    return polite_get(GAMMA_URL, f"/markets/{market_id}")


def get_tag(slug):
    """One tag by its slug, e.g. get_tag("fed-rates") -> {"id": "100196", "label": "Fed Rates", ...}."""
    return polite_get(GAMMA_URL, f"/tags/slug/{slug}")


# ---------------------------------------------------------------------------
# Reading a market: token ids, resolution, category
# ---------------------------------------------------------------------------

def yes_no_order(market):
    """(index of YES, index of NO) in the market's outcome lists.

    For Yes/No markets index 0 is YES and 1 is NO; we still read the labels in
    "outcomes" in case a market lists "No" first. Markets with other labels
    (e.g. "Hike"/"Cut") keep the API's order: the first outcome counts as YES.
    https://docs.polymarket.com/market-data/market-details.md#market-outcomes
    """
    labels = [str(label).strip().lower() for label in parse_json_list(market.get("outcomes"))]
    if labels == ["no", "yes"]:
        return 1, 0
    return 0, 1


def token_ids(market):
    """(YES token id, NO token id) of a market, as text; (None, None) if not available yet.

    These are the ids the order books and the price history use. v2 markets
    keep them in "positionIds" (a list), v1 ("CTF") markets in "clobTokenIds"
    (a JSON text). The "version" field says which one to use; both fields can
    be filled, so the presence of a field is not enough.
    https://docs.polymarket.com/market-data/market-details.md#market-outcomes
    """
    if market.get("version") == "v2":
        ids = parse_json_list(market.get("positionIds"))
    else:
        ids = parse_json_list(market.get("clobTokenIds"))
    if len(ids) != 2:
        return None, None
    yes_index, no_index = yes_no_order(market)
    return str(ids[yes_index]), str(ids[no_index])


def resolved_outcome(market):
    """How a market resolved: ("yes", "no", "split" or None, payout of one YES token in $).

    A market is resolved when it is closed and its resolution status says
    "resolved" ("umaResolutionStatus" for markets settled by the UMA oracle,
    "resolutionStatus" for v2 markets). Its "outcomePrices" then hold the
    final payout of each outcome: ["1", "0"] means YES won and pays $1.
    "split" is a rare 50-50 resolution in which each token pays e.g. $0.50.
    Markets not resolved yet give (None, NaN).
    https://docs.polymarket.com/concepts/resolution.md
    """
    status = market.get("umaResolutionStatus") or market.get("resolutionStatus")
    prices = parse_json_list(market.get("outcomePrices"))
    if not market.get("closed") or status != "resolved" or len(prices) != 2:
        return None, math.nan
    yes_index, _ = yes_no_order(market)
    yes_payout = float(prices[yes_index])
    if yes_payout == 1:
        return "yes", 1.0
    if yes_payout == 0:
        return "no", 0.0
    return "split", yes_payout


# The categories of Polymarket's fee table (https://docs.polymarket.com/trading/fees.md)
# and the tag (slug) that marks each of them on Polymarket's website.
FEE_CATEGORY_TAGS = {
    "crypto": "Crypto",
    "sports": "Sports",
    "finance": "Finance",
    "politics": "Politics",
    "economy": "Economics",
    "economics": "Economics",
    "pop-culture": "Culture",
    "culture": "Culture",
    "weather": "Weather",
    "mention-markets": "Mentions",
    "mentions": "Mentions",
    "tech": "Tech",
    "geopolitics": "Geopolitics",
}


def category_from_tags(tags):
    """The fee-table categories found among an event's tags, e.g. "Economics|Politics".

    Gamma leaves its own "category" field empty for these markets, so the
    category is read from the event's tags instead: tag "economy" means
    "Economics", "politics" means "Politics", and so on. An event can carry
    several of these tags; all are listed, in alphabetical order. None if
    the event has none of them.
    This is a label for reading and filtering. Fees should be calculated from
    the market's own fee schedule (fee_rate, ... in markets_table()), as
    Polymarket's docs say: https://docs.polymarket.com/trading/fees.md
    """
    found = {FEE_CATEGORY_TAGS[tag.get("slug")] for tag in tags or []
             if tag.get("slug") in FEE_CATEGORY_TAGS}
    return "|".join(sorted(found)) or None


# ---------------------------------------------------------------------------
# Order books (CLOB API)
# ---------------------------------------------------------------------------

ONE_DOLLAR = Decimal("1")

ORDERBOOK_TOP_COLUMNS = [
    "snapshot_time", "market_id",
    "best_yes_bid", "best_yes_bid_size", "best_yes_ask", "best_yes_ask_size",
    "best_no_bid", "best_no_bid_size", "best_no_ask", "best_no_ask_size",
    "spread", "yes_bid_levels", "no_bid_levels", "yes_ask_levels", "no_ask_levels",
]
ORDERBOOK_LEVEL_COLUMNS = ["snapshot_time", "market_id", "outcome", "side", "level", "price", "quantity"]


def get_orderbook(token_id):
    """The current order book of one outcome token, exactly as the CLOB sends it.

    Returns None if the token has no order book (HTTP 404: e.g. the market is
    closed). Always asked fresh (never from the cache): order books change
    constantly. https://docs.polymarket.com/market-data/prices-order-books.md#order-book
    """
    try:
        return polite_get(CLOB_URL, "/book", {"token_id": token_id}, use_cache=False)
    except NotFoundError:
        return None


def read_levels(levels, highest_first):
    """[{"price": "0.45", "size": "100"}, ...] -> [(Decimal("0.45"), Decimal("100")), ...].

    Sorted best price first: for bids the best is the HIGHEST price
    (highest_first=True), for asks the LOWEST (highest_first=False). We sort
    ourselves instead of relying on the API's order (bids lowest-first, asks
    highest-first). Decimal keeps the exact digits, so 0.46 - 0.45 is exactly
    0.01 (with ordinary floats it would be 0.010000000000000009).
    """
    pairs = [(Decimal(str(level["price"])), Decimal(str(level["size"]))) for level in levels or []]
    return sorted(pairs, key=lambda pair: pair[0], reverse=highest_first)


def parse_orderbook(yes_book, no_book, market_id=None, snapshot_time=None):
    """Turn a market's YES and NO order books into best bids/asks and the full depth.

    yes_book, no_book:        answers of get_orderbook() (None = no book)
    market_id, snapshot_time: copied into the output, to label the rows

    The output has the same format as kalshi.parse_orderbook(), with market_id
    in place of ticker and two extra counts (yes_ask_levels, no_ask_levels).
    Unlike Kalshi, Polymarket lists bids AND asks for each outcome, so every
    price is read directly from that outcome's own book. Formulas, from
    https://docs.polymarket.com/market-data/prices-order-books.md and
    https://docs.polymarket.com/concepts/prices-orderbook.md
        best YES bid = highest bid in the YES book   (size: its quantity)
        best YES ask = lowest ask in the YES book
        best NO bid  = highest bid in the NO book
        best NO ask  = lowest ask in the NO book
        spread       = best YES ask - best YES bid
    If a side of a book is empty, the prices that depend on it are NaN.
    The NO book is normally the mirror image of the YES book: it shows the
    SAME orders seen from the other outcome (see books_are_mirrored()), so
    never add YES and NO depth together.

    Returns two things:
      top    - a dict: one row of the poly_orderbooks table
      levels - a DataFrame: every price level of all four ladders (YES bids,
               YES asks, NO bids, NO asks), best price first (level 1)
    """
    ladders = {
        ("yes", "bid"): read_levels((yes_book or {}).get("bids"), highest_first=True),
        ("yes", "ask"): read_levels((yes_book or {}).get("asks"), highest_first=False),
        ("no", "bid"): read_levels((no_book or {}).get("bids"), highest_first=True),
        ("no", "ask"): read_levels((no_book or {}).get("asks"), highest_first=False),
    }

    # Best prices: the first level of each ladder.
    top = {"snapshot_time": snapshot_time, "market_id": market_id}
    for (outcome, side), ladder in ladders.items():
        best_price, best_size = ladder[0] if ladder else (None, None)
        top[f"best_{outcome}_{side}"] = to_float(best_price)
        top[f"best_{outcome}_{side}_size"] = to_float(best_size)
    yes_bids, yes_asks = ladders[("yes", "bid")], ladders[("yes", "ask")]
    if yes_bids and yes_asks:
        top["spread"] = float(yes_asks[0][0] - yes_bids[0][0])
    else:
        top["spread"] = math.nan
    for outcome in ["yes", "no"]:
        top[f"{outcome}_bid_levels"] = len(ladders[(outcome, "bid")])
    for outcome in ["yes", "no"]:
        top[f"{outcome}_ask_levels"] = len(ladders[(outcome, "ask")])
    top = {column: top[column] for column in ORDERBOOK_TOP_COLUMNS}  # fixed column order

    # Full depth: one row per price level.
    rows = []
    for (outcome, side), ladder in ladders.items():
        for level, (price, size) in enumerate(ladder, start=1):
            rows.append({"snapshot_time": snapshot_time, "market_id": market_id, "outcome": outcome,
                         "side": side, "level": level, "price": float(price), "quantity": float(size)})
    levels = pd.DataFrame(rows, columns=ORDERBOOK_LEVEL_COLUMNS)
    return top, levels


def books_are_mirrored(yes_book, no_book):
    """True if the NO book is the exact mirror image of the YES book.

    A YES and a NO token together always pay exactly $1, so an order to buy
    NO at price p is also an offer to sell YES at 1 - p, and Polymarket shows
    it in both books. Mirror image means: every NO bid at p matches a YES ask
    at 1 - p of the same size, and every NO ask at p a YES bid at 1 - p.
    (See the "Fetch Multiple Order Books" example in
    https://docs.polymarket.com/market-data/prices-order-books.md)
    The two books are downloaded a fraction of a second apart, so a book that
    changed in between can make this False.
    """
    def mirrored(levels):
        return sorted((ONE_DOLLAR - price, size) for price, size in levels)

    yes_bids = read_levels((yes_book or {}).get("bids"), highest_first=True)
    yes_asks = read_levels((yes_book or {}).get("asks"), highest_first=False)
    no_bids = read_levels((no_book or {}).get("bids"), highest_first=True)
    no_asks = read_levels((no_book or {}).get("asks"), highest_first=False)
    return sorted(no_bids) == mirrored(yes_asks) and sorted(no_asks) == mirrored(yes_bids)


def orderbook_snapshot(markets):
    """Download and convert the current YES and NO order books of every market.

    markets: markets_table() or some of its rows (each needs market_id,
             yes_token_id and no_token_id)
    Returns two tables: (one row per market, one row per price level).
    The snapshot time (UTC) is taken when both books of a market have arrived.
    A market without any book (both tokens answer "no order book") is skipped.
    Prints how many markets had a NO book that mirrors the YES book.
    """
    tops, depth_tables, mirrored, skipped = [], [], 0, []
    if isinstance(markets, pd.DataFrame):
        markets = markets.to_dict("records")  # one dict per row
    markets = list(markets)
    for number, market in enumerate(markets, start=1):
        yes_book = get_orderbook(market["yes_token_id"])
        no_book = get_orderbook(market["no_token_id"])
        if yes_book is None and no_book is None:
            skipped.append(market["market_id"])
            continue
        top, levels = parse_orderbook(yes_book, no_book, market["market_id"], pd.Timestamp.now(tz="UTC"))
        tops.append(top)
        if len(levels):
            depth_tables.append(levels)
        mirrored += books_are_mirrored(yes_book, no_book)
        if number % 25 == 0:
            print(f"   order books: {number}/{len(markets)} markets done")
    print(f"   {len(tops)} markets with an order book; the NO book mirrors the YES book in {mirrored} of them")
    if skipped:
        print(f"   no order book at all for {len(skipped)} markets: {skipped}")
    top_table = pd.DataFrame(tops, columns=ORDERBOOK_TOP_COLUMNS)
    if depth_tables:
        depth_table = pd.concat(depth_tables, ignore_index=True)
    else:
        depth_table = pd.DataFrame(columns=ORDERBOOK_LEVEL_COLUMNS)
    return top_table, depth_table


# ---------------------------------------------------------------------------
# Price history (Data API)
# ---------------------------------------------------------------------------

PRICE_COLUMNS = ["market_id", "outcome", "bucket_seconds", "timestamp", "price",
                 "resolution_seconds", "point_type"]


def get_price_history(token_id, bucket_seconds=FULL_LIFE_BUCKET_SECONDS, interval="max"):
    """The price points of one outcome token, oldest first, as the Data API sends them.

    token_id:        the YES or NO token id (see token_ids()): price history is
                     kept per outcome token, not per market
    bucket_seconds:  width of each point's time bucket, 60 to 86400 seconds.
                     10800 (3 hours) is kept for a market's whole life; 3600
                     (1 hour) only for about the last 30 days. Asking for a
                     width Polymarket no longer has gives an empty list.
    interval:        "max" = the token's whole life; "1m" = the last 30 days
                     (also "1w", "1d", "6h", "1h")
    Each point is {"timestamp": <Unix seconds>, "price": 0..1,
    "resolution_seconds": width of the window it describes}.
    https://docs.polymarket.com/market-data/prices-order-books.md#price-history
    """
    params = {"token_id": token_id, "interval": interval, "bucket_seconds": bucket_seconds,
              "limit": PRICE_PAGE_SIZE}
    return get_data_api_pages("/v2/prices-history", params)


def parse_price_history(points, market_id, outcome, bucket_seconds, resolved=False):
    """Turn the points of get_price_history() into a tidy table: one row per point.

    Columns:
        market_id, outcome   which market and which token ("yes" or "no")
        bucket_seconds       the bucket width that was asked for (10800 = the
                             3-hour series, 3600 = the hourly series): use it
                             to pick one series
        timestamp            UTC; for a bucket, the START of the bucket
        price                in dollars, 0 to 1
        resolution_seconds   width of the window the price describes; 0 means
                             one exact moment (see point_type)
        point_type           "bucket": a price for the bucket
                             "last_tick": the latest observed price, which can
                                 fall between bucket edges (the series' last point)
                             "settlement": the final payout of a resolved market
                                 (1 = this outcome won, 0 = lost): not a traded price
    Polymarket ends every series with one exact-moment point: the latest
    observation, or for a resolved market the on-chain payout of the token.
    Pass resolved=True for resolved markets so that last point is labelled
    "settlement". Points are not evenly spaced: quiet or old periods have gaps.
    https://data-api.polymarket.com/v2/openapi.json (operation get_prices_history)
    """
    rows = [{"market_id": market_id, "outcome": outcome, "bucket_seconds": bucket_seconds,
             "timestamp": point["timestamp"], "price": to_float(point.get("price")),
             "resolution_seconds": point.get("resolution_seconds")} for point in points]
    table = pd.DataFrame(rows, columns=PRICE_COLUMNS)
    table["timestamp"] = pd.to_datetime(table["timestamp"], unit="s", utc=True)
    table["resolution_seconds"] = table["resolution_seconds"].astype("int64")
    table = table.sort_values("timestamp", kind="stable", ignore_index=True)
    exact = table["resolution_seconds"] == 0
    table["point_type"] = exact.map({True: "last_tick", False: "bucket"})
    if resolved and len(table) and exact.iloc[-1]:
        table.loc[table.index[-1], "point_type"] = "settlement"
    return table.drop_duplicates(ignore_index=True)


# ---------------------------------------------------------------------------
# Trades (Data API)
# ---------------------------------------------------------------------------

# The ONLY trade fields we keep. Everything else in the Data API's answer is
# thrown away the moment it arrives, before it is cached. That removes all
# fields that identify a trader:
#     proxy_wallet, name, pseudonym, bio, profile_image, profile_image_optimized
#     transaction_hash (it leads straight to the wallets on the public blockchain)
# and also the market labels (title, slug, event_slug, icon), which
# markets_table() already has. Keeping a list of allowed fields, instead of a
# list of forbidden ones, means a profile field that Polymarket adds later is
# dropped automatically. Field meanings: https://data-api.polymarket.com/v2/openapi.json (schema Trade)
TRADE_COLUMNS = ["condition_id", "token_id", "outcome", "outcome_index", "side", "price", "size",
                 "timestamp"]


def keep_trade_fields(page):
    """A Data API trades page with each trade cut down to the fields in TRADE_COLUMNS.

    page: one answer of GET /v2/trades, {"data": [...], "pagination": {...}}.
    The pagination part is kept unchanged (it is needed to ask for the next page).
    """
    trades = [{field: trade.get(field) for field in TRADE_COLUMNS} for trade in page.get("data") or []]
    return {**page, "data": trades}


def get_trades(condition_id, max_pages=None):
    """The trades of one market (newest first), without any trader identity fields.

    condition_id: the market's condition id (column condition_id of markets_table())
    max_pages:    stop after this many pages of 1,000 trades (None = all)
    Each fill is listed once, from the taker's side (the API's default
    taker_only=true), so YES and NO trades never count the same fill twice.
    Fills smaller than 0.01 shares are left out (the API's default
    filter_amount). The market feed covers the last three years. Rows come
    newest first, sorted by block time and then by the API's sequence number
    (the "(block_timestamp, sequence_id)" anchor of its pages), so trades of
    the same second are in a fixed order. Each page is passed through
    keep_trade_fields() before it is cached, so names, wallets and
    transaction hashes never reach the disk.
    https://data-api.polymarket.com/v2/openapi.json (operation get_trades)
    """
    params = {"condition": condition_id, "limit": TRADE_PAGE_SIZE}
    return get_data_api_pages("/v2/trades", params, max_pages=max_pages, clean=keep_trade_fields)


def trades_table(trades):
    """One tidy row per trade, oldest first, without the traders' identity fields.

    trades: rows of get_trades(), newest first, as the API sends them
    Columns: condition_id; token_id, outcome ("Yes"/"No") and outcome_index
    (0, 1; 999 = unknown) of the token traded; side ("BUY"/"SELL", what the
    taker did); price in dollars per share; size in shares (tokens);
    timestamp of the fill in UTC (block time, to the second).
    Several trades often share the same second. The API lists them newest
    first, so we turn the whole list around before sorting by time: trades of
    the same second then keep their true order, and the last row of a second
    really is the last trade (this matters for "last price").
    """
    rows = [{column: trade.get(column) for column in TRADE_COLUMNS} for trade in reversed(trades)]
    table = pd.DataFrame(rows, columns=TRADE_COLUMNS)
    table["price"] = table["price"].astype("float64")
    table["size"] = table["size"].astype("float64")
    table["timestamp"] = pd.to_datetime(table["timestamp"].astype("float64"), unit="s", utc=True)
    return table.sort_values("timestamp", kind="stable", ignore_index=True)


BAR_COLUMNS = ["market_id", "outcome", "bar_start", "bar_end", "last_price", "vwap", "volume",
               "notional", "n_trades", "last_trade_time"]


def trade_bars(trades, bar="1h"):
    """Aggregate trades into time bars: one row per market, outcome and bar with trades.

    trades: one row per trade with columns market_id, outcome, timestamp (UTC),
            price and size; trades of the same second must be in their true
            order (trades_table() does this)
    bar:    bar width as pandas text ("1h" = one hour, "15min", "1D", ...)

    Columns:
        market_id, outcome  which market and which token ("yes" or "no")
        bar_start, bar_end  the bar holds trades with bar_start <= time < bar_end
                            (UTC). Kalshi's candles are labelled by their END:
                            join on bar_end = Kalshi's period_end.
        last_price          price of the last trade in the bar
        vwap                volume-weighted average price = notional / volume
        volume              shares traded = sum of size
        notional            dollars traded = sum of price x size
        n_trades            number of trades
        last_trade_time     time of the last trade (shows how old last_price is)
    Bars without any trade get NO row: nothing is filled in for quiet hours.
    YES and NO get separate bars; never add their prices together.

    Formulas. Polymarket's Data API defines a trade's "price" as the execution
    price per share in USDC (= dollars) and "size" as the filled quantity in
    shares, never dollars: https://data-api.polymarket.com/v2/openapi.json (schema Trade)
        notional = sum(price x size)                 (dollars)
        volume   = sum(size)                         (shares)
        vwap     = notional / volume
    """
    trades = trades.sort_values("timestamp", kind="stable")  # stable: same-second order is kept
    trades = trades.assign(bar_start=trades["timestamp"].dt.floor(bar),
                           notional=trades["price"] * trades["size"])
    groups = trades.groupby(["market_id", "outcome", "bar_start"], sort=True)
    bars = groups.agg(last_price=("price", "last"),
                      volume=("size", "sum"),
                      notional=("notional", "sum"),
                      n_trades=("price", "size"),
                      last_trade_time=("timestamp", "last")).reset_index()
    bars["vwap"] = bars["notional"] / bars["volume"]
    bars["bar_end"] = bars["bar_start"] + pd.Timedelta(bar)
    bars["n_trades"] = bars["n_trades"].astype("int64")
    for column in ["bar_start", "bar_end", "last_trade_time"]:
        # Same time unit (milliseconds) as the project's other tables, e.g. Kalshi's candles.
        bars[column] = bars[column].astype("datetime64[ms, UTC]")
    return bars[BAR_COLUMNS]


# ---------------------------------------------------------------------------
# Tidy tables and saving
# ---------------------------------------------------------------------------

MARKET_COLUMNS = [
    "market_id", "condition_id", "event_id", "event_slug", "event_title", "series_slug",
    "topic", "market_type", "question", "outcome_label", "rules", "resolution_source",
    "category", "tags", "fees_enabled", "fee_rate", "fee_exponent", "fee_taker_only",
    "fee_rebate_rate", "start_date", "end_date", "closed", "closed_time", "resolved_outcome",
    "yes_payout", "yes_token_id", "no_token_id", "outcome_labels", "protocol_version",
    "neg_risk", "tick_size", "volume",
]
MARKET_TIME_COLUMNS = ["start_date", "end_date", "closed_time"]
MARKET_BOOLEAN_COLUMNS = ["fees_enabled", "fee_taker_only", "closed", "neg_risk"]


def markets_table(events, labels=None):
    """One tidy row per market, from events of list_events() / get_events_by_id().

    labels: optional {event_id: (topic, market_type)} to fill those two
            columns, e.g. {"606422": ("fed", "decision")}

    Columns:
      ids          market_id (Gamma), condition_id (on-chain; used by trades),
                   event_id, event_slug (the event's web page is
                   polymarket.com/event/<event_slug>), series_slug
      texts        event_title; question (the market's yes/no question);
                   outcome_label (its short label in the event, e.g. "25 bps
                   decrease"); rules (the full resolution rules: the market's
                   description, or the event's if the market has none);
                   resolution_source (usually a web address)
      category     fee-table categories read from the event's tags (see
                   category_from_tags()), and tags: all the event's tag slugs
      fees         the market's own fee schedule, as Polymarket reports it
                   today: fees_enabled, fee_rate, fee_exponent, fee_taker_only,
                   fee_rebate_rate (empty when the market has no schedule)
                   https://docs.polymarket.com/market-data/market-details.md#trading-fees
      times (UTC)  start_date, end_date (scheduled end), closed_time (when it
                   actually closed)
      result       closed (True/False), resolved_outcome ("yes"/"no"/"split",
                   empty while unresolved), yes_payout (dollars per YES token)
      tokens       yes_token_id, no_token_id (for order books and prices),
                   outcome_labels (normally "Yes|No"), protocol_version
      other        neg_risk (True if exactly one market of the event can
                   resolve YES), tick_size (smallest price step), volume
                   (total traded, in dollars, as reported by Gamma)
    """
    labels = labels or {}
    rows = []
    for event in events:
        event_id = str(event.get("id"))
        topic, market_type = labels.get(event_id, (None, None))
        tags = event.get("tags") or []
        series_slugs = "|".join(series.get("slug") or "" for series in event.get("series") or []) or None
        for market in event.get("markets") or []:
            yes_token, no_token = token_ids(market)
            outcome, yes_payout = resolved_outcome(market)
            fees = market.get("feeSchedule") or {}
            volume = market.get("volumeNum")
            rows.append({
                "market_id": str(market.get("id")),
                "condition_id": market.get("conditionId"),
                "event_id": event_id,
                "event_slug": event.get("slug"),
                "event_title": event.get("title"),
                "series_slug": series_slugs,
                "topic": topic,
                "market_type": market_type,
                "question": market.get("question"),
                "outcome_label": market.get("groupItemTitle") or None,
                "rules": market.get("description") or event.get("description") or None,
                "resolution_source": market.get("resolutionSource") or event.get("resolutionSource") or None,
                "category": market.get("category") or event.get("category") or category_from_tags(tags),
                "tags": "|".join(tag.get("slug") or "" for tag in tags) or None,
                "fees_enabled": market.get("feesEnabled"),
                "fee_rate": to_float(fees.get("rate")),
                "fee_exponent": to_float(fees.get("exponent")),
                "fee_taker_only": fees.get("takerOnly"),
                "fee_rebate_rate": to_float(fees.get("rebateRate")),
                "start_date": market.get("startDate") or event.get("startDate"),
                "end_date": market.get("endDate") or event.get("endDate"),
                "closed": market.get("closed"),
                "closed_time": market.get("closedTime"),
                "resolved_outcome": outcome,
                "yes_payout": yes_payout,
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "outcome_labels": "|".join(parse_json_list(market.get("outcomes"))) or None,
                "protocol_version": market.get("version"),
                "neg_risk": market.get("negRisk"),
                "tick_size": to_float(market.get("orderPriceMinTickSize")),
                "volume": to_float(volume if volume is not None else market.get("volume")),
            })
    table = pd.DataFrame(rows, columns=MARKET_COLUMNS)
    for column in MARKET_TIME_COLUMNS:
        table[column] = pd.to_datetime(table[column], utc=True, format="ISO8601")
    for column in MARKET_BOOLEAN_COLUMNS:
        table[column] = table[column].astype("boolean")  # True/False, or <NA> if unknown
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
