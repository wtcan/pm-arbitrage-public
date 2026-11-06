# Code walkthrough

A guided tour of every file in `src/`, written for the thesis author (basic
Python, economics background). For each file it covers what the file does,
its key functions, the design choices and the reasons for them, and one
interview question with a model answer. At the end there is a one-page
summary of the whole pipeline and twelve likely interview questions about the
project.

The walkthrough describes the code as it was on 2026-11-04 and changes
nothing. Numbers come from the committed tables in `data/processed/`, from
[`docs/matching.md`](matching.md), which has the full detail on matching and
on the resolution risks, and from [`docs/arbitrage.md`](arbitrage.md), which
has the full detail on fees and the worked example.

**Contents**

0. [Ideas used in several files](#0-ideas-used-in-several-files)
1. [`check_connection.py`](#1-check_connectionpy): can we reach the APIs?
2. [`kalshi.py`](#2-kalshipy): Kalshi data client
3. [`download_kalshi.py`](#3-download_kalshipy): download the Kalshi sample
4. [`polymarket.py`](#4-polymarketpy): Polymarket data client
5. [`download_polymarket.py`](#5-download_polymarketpy): download the Polymarket sample
6. [`match.py`](#6-matchpy): find markets with the same outcome on both venues
7. [`make_review_sheet.py`](#7-make_review_sheetpy): Excel sheet for checking pairs by hand
8. [`read_review_sheet.py`](#8-read_review_sheetpy): read your verdicts back
9. [`apply_independent_review.py`](#9-apply_independent_reviewpy): combine the two review passes
10. [`build_pairs.py`](#10-build_pairspy): the final table of verified pairs
11. [`arb.py`](#11-arbpy): fee-adjusted arbitrage, with order-book depth and return on capital
12. [`gaps.py` and the Step 5 statistics](#12-gapspy-and-the-step-5-statistics): historical gaps, persistence, determinants, price discovery, resolution risk
13. [One-page summary of the pipeline](#13-one-page-summary-of-the-pipeline)
14. [Twelve likely interview questions about the project](#14-twelve-likely-interview-questions-about-the-project)

---

## 0. Ideas used in several files

Read this section once. The file sections below refer back to it instead of
repeating it.

### 0.1 The one economic fact everything rests on

Every market in this project is a **binary contract**. A YES contract pays $1
if the event happens and $0 otherwise; a NO contract pays the opposite. So

> **one YES + one NO of the same market always pay exactly $1 together.**

Three things follow, and the code uses all three:

* **Prices are between $0 and $1** and can be read as probabilities: a YES
  price of $0.42 means the market puts roughly a 42% chance on the event.
* **A bid to buy NO at price p works as an offer to sell YES at 1 − p.** If
  you buy YES at 1 − p while that person buys NO at p, the two of you pay
  (1 − p) + p = $1 for one YES and one NO, which is exactly what the pair is
  worth. The exchange can match the two orders. Kalshi uses this to publish
  only bids, and Polymarket shows the same order in both books (sections 2
  and 4).
* **Two legs that together always pay $1 are a riskless position.** If the
  same outcome trades on Kalshi and Polymarket, buying YES on one venue and
  NO on the other pays exactly $1 whatever happens. If the two prices add up
  to less than $1 after all costs, that is an arbitrage. The whole project is
  about finding such pairs and checking whether they really pay exactly once.

### 0.2 Python patterns you will see everywhere

| Pattern | What it means | Why it is used here |
|---|---|---|
| Long text in `"""..."""` at the top of a file | the *module docstring*: how to run the file, what it does, and the API facts it relies on, with documentation links | anyone (a supervisor, an examiner, you in six months) can read the purpose and the assumptions without reading the code |
| Names in CAPITALS at the top (`MONTHS_BACK = 24`) | *constants*: settings | change a setting in one place |
| `if __name__ == "__main__": main()` | runs `main()` only when the file is started as a script (`python src/x.py`), not when another file or a test imports it | tests can import a file's functions without starting a long download |
| Plain functions, almost no classes | each function takes inputs and returns outputs | short functions are easier to read and to unit-test one at a time |
| `market.get("title")` | reads a field of a dict and gives `None` if it is missing (`market["title"]` would crash) | APIs leave fields out; missing data should become an empty value, not a crash |
| `re.search(r"...", text)` | a *regular expression*: a pattern for text. `r"more than (\d+(?:\.\d+)?)%"` finds "more than 0.2%" and captures `0.2` | market titles and rules are written in fixed templates, so patterns can read them reliably |
| `pd.DataFrame` | a pandas table (like a spreadsheet) | all cleaned data are tables |
| `.parquet` files | a compressed table format | much smaller than CSV, and it keeps data types: a UTC timestamp stays a timestamp, a number stays a number |
| `NaN` / `NaT` / `None` | "missing" for numbers / times / anything | an empty side of an order book is *missing*, not zero; a zero would be a (wrong) price |

### 0.3 Polite API requests: throttle, retry, cache

Project rule 2 asks for polite API use. Every file that downloads data does
three things in its `polite_get()` function:

1. **Throttle.** `wait_for_turn()` remembers when the last request was sent
   and sleeps so requests are at least 0.2 s apart (`check_connection.py`, at
   most 5 per second) or 0.25 s apart (`kalshi.py` and `polymarket.py`, at
   most 4 per second, a little below the limit because Kalshi often answers
   "too many requests"). `time.monotonic()` is used instead of the wall
   clock because it never jumps, even if the computer's clock is reset.
2. **Retry with exponential backoff.** On HTTP 429 ("too many requests"), a
   5xx server error or a broken connection, wait and try again. The wait
   doubles each time (1, 2, 4, 8, 16, then at most 30 seconds). Errors that
   retrying cannot fix, such as 400 "bad request" or 404 "not found", stop at
   once with a clear message.
3. **Cache.** Each answer is saved as a JSON file in `data/raw/<venue>/`, in a
   folder and file name built from the request (`cache_path()`). If the same
   request is made again in the same session, the saved file is read and the
   API is not asked again. So a script that stops halfway can be re-run and
   skips what it already has. Order books and Kalshi's "historical cutoff"
   change all the time, so they are never taken from the cache
   (`use_cache=False`).

If the cloud environment's network blocks a domain, the proxy answers 403.
The code turns that into `NetworkBlockedError` with the message "Add this
domain to your cloud environment's allowed domains: ...", as rule 8 asks.

### 0.4 Pages and cursors

APIs send long lists in pages. Each answer carries a **cursor**, a bookmark
you send back to get the next page. An empty cursor means the last page.
`get_all_pages()` (Kalshi), `get_keyset_pages()` and `get_data_api_pages()`
(Polymarket) loop until the cursor is empty. They also remember every cursor
they have seen and stop with an error if one repeats, so a faulty API can
never trap the script in an endless loop.

### 0.5 Exact prices with `Decimal`

Computers store ordinary decimal numbers (floats) in binary, so
`1 - 0.56` gives `0.43999999999999995` instead of `0.44`. Order-book
arithmetic is therefore done with Python's `Decimal` type, which keeps the
exact digits. Only the final results are turned into floats for the tables.
`match.py` goes one step further and counts everything in whole numbers (see
section 6).

### 0.6 Time: UTC in storage, US Eastern for calendar days

All stored times are in UTC (rule 3). Both venues state their deadlines in US
Eastern time ("8:29 AM ET"), so `match.py` converts to Eastern only when it
needs a *calendar day* (an FOMC meeting day or a CPI release day).

### 0.7 Saving tables safely

`save_table()` (in `kalshi.py` and `polymarket.py`) and
`save_review_file()` (in `match.py`) work in three steps:

1. write the table to a temporary file (`name.parquet.tmp`);
2. check its size. At 50 MB or more (rule 3), delete the temporary file,
   keep the old file and stop with an error;
3. otherwise rename the temporary file to the real name.

Renaming is instant, so a crash never leaves a half-written file behind, and
a file that is too large never replaces a good one. The later scripts
(review sheets, `match_verified.csv`, `pairs.parquet`) write their small
files directly and then check the 50 MB limit, deleting the file if it is
too large.

---

## 1. `check_connection.py`

### What it does

A quick check, run at the start of a session: can we reach every public API
the project uses? It sends one small GET request to each of:

* Kalshi (main host; the fallback host only if the main one fails),
* Polymarket Gamma API (market information),
* Polymarket CLOB API (order books),
* Polymarket Data API (price history and trades).

For each one it prints OK, FAILED or BLOCKED with details and saves the raw
answer in `data/raw/check_connection/`. It ends with a summary table and exits
with code 0 if everything worked and 1 if not.

```bash
python src/check_connection.py
```

### Key functions

| Function | What it does |
|---|---|
| `wait_for_turn()` | sleeps so requests are at least 0.2 s apart (at most 5 per second) |
| `polite_get(url)` | GET with up to 4 attempts; on 429 or 5xx it waits 2, 4, then 8 s |
| `find_market_list(data)` | finds the list of markets (or trades) inside each API's answer. Each API packs it differently: Kalshi under `"markets"`, Gamma as the bare list, CLOB and Data API under `"data"` |
| `describe_market(market)` | a short label for printing: Kalshi says `title` and has a `ticker`, Polymarket says `question` |
| `check_endpoint(name, url, save_as, what)` | one check: sends the request, sorts out what went wrong if anything, saves the answer, and returns (worked?, one-line summary) |
| `main()` | runs all the checks, prints the summary, returns the exit code |

### Design choices and why

* **Self-contained.** It imports only `requests` and none of the project's
  own files, so it still works when other code is broken or half written.
  It is the first thing to run in a fresh cloud session.
* **Always asks the live API, never the cache.** A saved answer would not
  test the connection. It still *saves* each answer, so you can open the
  file and see what the data look like.
* **It tells failures apart, because each has a different fix.**
  * Blocked by the network (a proxy error containing 403): the fix is to
    allow a domain, and the message names that domain (rule 8).
  * Could not connect (no internet, unknown host, timeout).
  * The server answered with an HTTP error.
  * The server answered 200 but the text was not valid JSON.
* **Fallback host only when needed.** Kalshi serves the same data from two
  hosts. Trying the second one only after the first fails avoids needless
  requests.
* **Exit code 0 or 1.** Other programs (or a person writing a shell script)
  can tell from it whether the check passed.

### Interview question

**"Your scripts retry failed requests with exponential backoff. Why back off
at all, why double the wait, and why not retry every error?"**

*Model answer.* HTTP 429 means "too many requests" and 5xx means a temporary
server problem. Both are about the *server's state*, so they may clear up if
we wait. Retrying at once would add load exactly when the server is
struggling, and would likely fail again. Doubling the wait (1, 2, 4, 8 s...)
gets out of the way quickly in a real overload but still recovers fast from
a brief glitch. A cap of 30 seconds keeps a single wait from growing without
limit. Errors such as 400 "bad request" or 404 "not found" are about the
*request itself*: the same request gives the same answer every time, so
retrying only wastes time and hides the real problem. Those stop at once
with a clear message. Polymarket's client also follows the server's own
`Retry-After` header when it sends one.

---

## 2. `kalshi.py`

### What it does

A *library*: a toolbox of functions that other scripts import (it has no
`main()`). It reads Kalshi's public market data, turns it into tidy tables
and saves them. It never logs in and never places orders.

Kalshi organises markets in three levels:

| Level | Meaning | Example |
|---|---|---|
| series | a recurring topic | `KXFED`, "Fed funds rate" |
| event | one occurrence of it | `KXFED-25DEC`, the December 2025 meeting |
| market | one yes/no question | `KXFED-25DEC-T3.75`, "upper bound above 3.75%?" |

### Key functions

| Group | Function | What it does |
|---|---|---|
| Requests | `polite_get(path, params, use_cache)` | one GET: throttled (4 per second), retried (up to 10 attempts), cached (section 0.3) |
| | `cache_path(path, params)` | the cache file for a request; very long names are shortened with a hash so they stay unique |
| | `get_all_pages(path, list_key, params)` | follows the cursor through every page (section 0.4) |
| Converting | `to_float(value)`, `to_utc(value)` | Kalshi's text numbers ("0.4200") to floats, and its times to UTC timestamps; missing values become NaN / NaT |
| Finding markets | `list_series()`, `get_series()`, `search_series(words)` | all series, one series, or the series whose ticker or title contains a word (used to find the Fed and CPI series) |
| | `list_events()`, `list_markets()`, `list_historical_markets()` | events and markets, from the live and the historical store |
| | `list_all_markets(series_ticker)` | every market of a series from both stores, each found once, with two added columns: `series_ticker` and `source` (live or historical) |
| Live or historical | `get_historical_cutoff()`, `is_historical(market, cutoff)` | Kalshi moves markets that settled before a cutoff time to a separate "historical" store with its own endpoints. These two functions say which store to ask |
| Order books | `get_orderbook(ticker)` | the current order book, always fresh |
| | `read_bids(pairs)` | the list of `[price, count]` as exact `Decimal`s, best (highest) price first |
| | `parse_orderbook(raw, ticker, snapshot_time)` | **the key formula:** turns Kalshi's bids-only book into best bids, best asks, spread and the full depth (see below) |
| | `orderbook_snapshot(tickers)` | downloads and parses the books of many markets; one row per market and one row per price level |
| Price history | `candle_windows(start, end, period)` | splits a long time range into pieces of at most 5,000 candles, Kalshi's limit per request |
| | `get_candles(market, period_minutes, cutoff)` | a market's whole price history as candles, from the right store |
| | `parse_candles(...)` | one tidy row per candle: open/high/low/close of the best YES bid, the best YES ask and the traded price, plus volume and open interest |
| | `blank_no_quote_markers(table)` | turns Kalshi's "nobody is bidding" (0) and "nobody is offering" (1) markers into NaN |
| Tables | `markets_table(markets, events, series)` | one tidy row per market: ids, titles, full rules, status, result, times, strikes, prices |
| | `save_table(table, name)` | saves as Parquet with the 50 MB check (section 0.7) |

**The order-book formula.** Kalshi lists only *bids*: bids to buy YES and
bids to buy NO. Because YES + NO = $1 (section 0.1), a NO bid at p is a YES
offer at 1 − p. From https://docs.kalshi.com/getting_started/orderbook_responses:

```
best YES bid = highest YES bid
best YES ask = 1 − highest NO bid      (size: the size of that NO bid)
best NO bid  = highest NO bid
best NO ask  = 1 − highest YES bid     (size: the size of that YES bid)
spread       = best YES ask − best YES bid
```

*Worked example.* YES bids: 13 contracts at $0.42. NO bids: 20 contracts at
$0.56. Then best YES bid = $0.42 (13), best YES ask = 1 − 0.56 = $0.44 (20),
spread = $0.02, best NO bid = $0.56, best NO ask = 1 − 0.42 = $0.58. If one
side is empty, the prices that depend on it are NaN, not 0 or 1.

**Candles** are summaries of one period (here one hour): the first, highest,
lowest and last value of the best bid, the best ask and the traded price.
Kalshi only sends a candle for a period in which *something changed* (a trade,
or a new best bid or ask). A missing hour means "nothing changed", so the
previous candle's closing quotes still hold.

### Design choices and why

* **Live store first, then the historical store.** Markets only ever move
  from live to historical. If a market moves while we read, reading live
  first means we see it in one store or the other. A market found in both is
  kept once.
* **Facts about the API are written down with links and a date** ("checked
  on 2026-11-04") in the docstring. APIs change; the reader can see what was
  true when the data were downloaded.
* **We sort the bids ourselves.** One Kalshi docs page says the list is
  ascending, another says best to worst. Sorting ourselves makes the result
  right either way, and a unit test (`test_order_of_the_lists_does_not_matter`)
  checks it.
* **`Decimal` in the order book** so that 1 − 0.56 is exactly 0.44
  (`test_prices_are_exact_not_rounded_floats`).
* **"No quote" markers become NaN.** In candles Kalshi writes a YES bid of 0
  when nobody bids and an ask of 1 when nobody offers. Left in, they would
  look like real prices and create fake huge spreads (and fake arbitrage).
  Kalshi's docs do not spell this out, so it was checked in the data: all 36
  open markets with no YES bids showed a bid of 0.0000, all 74 with no YES
  offers showed an ask of 1.0000, and in 1.8 million hourly candles no trade
  ever happened at 0 or 1. This evidence is in the docstring.
* **Both field names are accepted.** Historical candles say `close` and
  `volume`, live ones say `close_dollars` and `volume_fp`. `parse_candles()`
  reads either, so the same table comes out
  (`test_live_and_historical_candles_give_the_same_row`).
* **Only finished candles.** "Now" is rounded down to a whole period, so a
  half-finished hour is never requested. Otherwise the cache would keep a
  candle that later changes.
* **Windows touch but do not overlap.** Each request window starts one
  second after the previous one ends, so no candle is downloaded twice.
* **Errors are specific.** `KalshiError` (an error retrying will not fix)
  and `NetworkBlockedError` (allow a domain) let the download scripts react
  differently: skip one market and carry on, or stop.
* **Counters in `STATS`** (requests, retries, answers from the cache) let a
  long download report what happened.

### Interview question

**"Kalshi's order book only shows bids. How do you know the price at which you
could buy YES?"**

*Model answer.* A YES and a NO contract of the same market together always
pay exactly $1. So someone bidding p for NO is, in effect, offering to sell
YES at 1 − p: if I buy YES at 1 − p, the exchange can pair my order with
their NO bid, and together we pay exactly $1 for one YES and one NO. The best YES ask is
therefore one minus the highest NO bid, and the quantity available is the
size of that NO bid. The best NO ask is one minus the highest YES bid. This
is Kalshi's own documented rule (I cite the docs page in the code), and the
unit tests check it against the documentation example and a real Kalshi
answer. It is a no-arbitrage identity between complementary claims, like
put-call parity in options. I do the arithmetic with exact decimals and leave
an empty side as missing, so a market with no NO bids has no YES ask, rather
than an ask of $1.

---

## 3. `download_kalshi.py`

### What it does

The script that uses `kalshi.py` to build the Kalshi half of the data set:
the Fed-decision and US CPI markets of the last 24 months.

```bash
python src/download_kalshi.py
```

| Step | What happens |
|---|---|
| 1 | searches Kalshi's "Economics" series for Fed and CPI words and prints the hits, then prints the four chosen series |
| 2 | downloads every market of those series that closes on or after the start date (24 months ago), including markets still open |
| 3 | takes one order-book snapshot of every open market |
| 4 | downloads hourly candles over each market's whole life |
| 5 | checks the data, saves four tables and prints five example rows of each |

The four series:

| Series | What it measures |
|---|---|
| `KXFEDDECISION` | the Fed's decision at each FOMC meeting: hike / hold / cut, by size |
| `KXFED` | the upper bound of the Fed funds target range after each meeting |
| `KXCPI` | US CPI inflation, change from the previous month |
| `KXCPIYOY` | US CPI inflation, change from a year earlier |

The four tables in `data/processed/`:

| File | One row per | Rows (2026-11-04) |
|---|---|---|
| `kalshi_markets.parquet` | market | 1,150 (KXFED 419, KXCPIYOY 366, KXCPI 230, KXFEDDECISION 135) |
| `kalshi_orderbooks.parquet` | open market: best bids, asks, sizes, spread | 358 |
| `kalshi_orderbook_levels.parquet` | price level of the order books (full depth) | 14,528 |
| `kalshi_candles.parquet` | market and hour in which something happened | 1,837,894 |

### Key functions

| Function | What it does |
|---|---|
| `show_candidates()` | step 1: prints the series found by keyword and returns the chosen ones (with their category) |
| `download_markets(start)` | step 2: events and markets of each series; keeps markets that close on or after `start`; prints counts |
| `download_candles(markets, cutoff)` | step 4: candles of every market that has opened; a market that fails is listed and skipped |
| `check_prices(name, table, columns)` | rule 3: every price must be between 0 and 1, otherwise the script stops |
| `main()` | runs steps 1 to 5, checks, saves, prints a summary |

### Design choices and why

* **The series were chosen by searching, and the search is printed.** The
  choice is documented and can be repeated: an examiner can see which other
  series existed and why these four were taken. The "KX" series hold the
  whole history, because Kalshi merged the old tickers (such as `FED-21DEC`)
  into them.
* **The sample is defined by the close date** ("closes on or after 24 months
  ago") and includes both settled and still-open markets. Taking only
  markets that are open today would leave out everything already settled,
  a survivorship-style selection.
* **One failing market does not stop the run.** It is listed at the end.
  Because every answer is cached, running the script again in the same
  session fetches only what is missing.
* **Checks before saving:** prices between 0 and 1, tickers unique, no
  duplicate candles. If a check fails the script stops (`sys.exit` or
  `assert`), so bad data are never saved.
* **Five example rows with a fixed random seed** (`random_state=1`) are
  printed for every table, so you can eyeball the data and see the same rows
  each run.
* **Progress every 25 markets** with request, retry and cache counts. The
  download takes a while because Kalshi often answers "too many requests".

### Interview question

**"Kalshi only sends a candle for an hour in which something changed. How do
you handle the missing hours when you line up Kalshi and Polymarket prices?"**

*Model answer.* A missing hour is not missing data. It means the best bid,
the best ask and the last trade did not change, so the previous candle's
closing quotes were still standing. To build a regular hourly series I
carry the last close forward (forward-fill) into the gap. I do that only
while the market is open, and I keep a column for how old the quote is,
because a quote that has not moved for days in a thin market may not be
tradable at size. Two things I would *not* do: fill a value that is NaN
inside an existing candle (a NaN bid means nobody was bidding, a genuine
absence), and interpolate between two candles (that uses the future price,
a look-ahead bias).

---

## 4. `polymarket.py`

### What it does

The Polymarket counterpart of `kalshi.py`: a library that reads public data
from Polymarket's three APIs and turns it into tidy tables in the same format
as Kalshi's where possible.

| API | Address | Used for |
|---|---|---|
| Gamma | `gamma-api.polymarket.com` | events, markets, tags, search |
| CLOB | `clob.polymarket.com` | order books |
| Data | `data-api.polymarket.com` | price history and trades |

Polymarket also has series → events → markets. Each market has two **outcome
tokens**, YES and NO, each with its own id, order book and price history.

### Key functions

| Group | Function | What it does |
|---|---|---|
| Requests | `polite_get(base_url, path, params, use_cache)` | like Kalshi's, plus: follows the `Retry-After` header, and HTTP 404 raises `NotFoundError` |
| | `get_keyset_pages()`, `get_data_api_pages()` | page through Gamma lists and Data API lists (the two use different page formats) |
| Finding events | `list_events(tag_slug, series_id, ...)` | events with their markets, open **and** closed, from both protocol versions |
| | `get_events_by_id()`, `get_event()`, `search_events()`, `list_markets()`, `get_market()`, `get_tag()` | other ways to look things up |
| Reading a market | `yes_no_order(market)` | which position in the lists is YES and which is NO (normally 0 and 1, but read from the labels) |
| | `token_ids(market)` | the YES and NO token ids; v2 markets keep them in `positionIds`, v1 markets in `clobTokenIds` |
| | `resolved_outcome(market)` | "yes", "no", "split" (a rare 50-50) or None, and the payout of one YES token |
| | `category_from_tags(tags)` | a category label read from the event's tags, because Gamma's own category field is empty for these markets |
| Order books | `get_orderbook(token_id)` | one token's book, or None if it has none (closed market, HTTP 404) |
| | `read_levels(levels, highest_first)` | price levels as exact `Decimal`s, best first |
| | `parse_orderbook(yes_book, no_book, ...)` | best bids and asks of YES and NO, spread and full depth, **in the same format as Kalshi's** |
| | `books_are_mirrored(yes_book, no_book)` | True if the NO book is the mirror image of the YES book |
| | `orderbook_snapshot(markets)` | books of many markets; prints how many were mirrored |
| Price history | `get_price_history(token_id, bucket_seconds, interval)` | price points of one token |
| | `parse_price_history(points, ...)` | one row per point, labelled `bucket`, `last_tick` or `settlement` |
| Trades | `get_trades(condition_id)`, `trades_table(trades)` | trades of a market, without the traders' profile fields |
| Tables | `markets_table(events, labels)` | one tidy row per market, including its own fee schedule (`fee_rate`, `fee_exponent`, `fee_taker_only`, `fee_rebate_rate`) |
| | `save_table(table, name)` | Parquet with the 50 MB check |

### Design choices and why

* **One throttle for all three APIs together** (at most 4 requests per
  second in total), the simplest way to stay polite to one company.
* **Always ask for protocol versions "v1" and "v2".** Gamma returns only v1
  markets unless told otherwise. Forgetting this would *silently* drop all
  newer markets, the most dangerous kind of data error because nothing
  crashes.
* **`list_events()` rather than `list_markets()`.** Event lists include open
  and closed events by default, and each event carries its markets and tags.
  The market list returns only open markets by default, so
  `list_markets()` asks twice (open, then closed).
* **The `version` field decides where the token ids are**, not which field
  happens to be filled, because both fields can be present
  (`test_token_ids_v2_market_uses_position_ids`).
* **A missing order book is normal, not an error.** Closed markets have no
  book, so `get_orderbook()` turns HTTP 404 into `None` and the snapshot
  skips the market.
* **Same output format as Kalshi** (`test_same_format_as_kalshi`). The later
  price comparison can use one piece of code for both venues.
* **Never add YES and NO depth together.** Polymarket shows every order in
  both books (see the interview question). `books_are_mirrored()` checks
  this, and the snapshot prints in how many markets it held.
* **Every price point is labelled.** The last point of a resolved market is
  the *payout* (1 or 0), not a traded price. Labelling it `settlement` stops
  it from being mistaken for a price of 1 or 0 at that moment.
* **Fees are kept per market, not guessed per category.** Polymarket's docs
  say fees must be calculated from each market's own `feeSchedule`, so those
  fields are saved in `poly_markets`, and `arb.py` computes fees from them
  (section 11). The category from tags is only a label for reading and
  filtering.
* **Privacy: traders' profile fields are dropped.** Trade records include
  public names, bios and wallets. The research does not need them, so
  `trades_table()` keeps only the trade itself.
* **Some code is duplicated from `kalshi.py`** (`polite_get`, `save_table`,
  `to_float`). This is deliberate: each venue file reads on its own from top
  to bottom. The cost is that a fix to one must be copied to the other.

### Interview question

**"Polymarket shows a YES order book and a NO order book. Why must you never
add the depth of the two together?"**

*Model answer.* Because they are the same orders seen from two sides. A YES
and a NO token together pay exactly $1, so an order to buy NO at p is also
an offer to sell YES at 1 − p, and Polymarket shows it in both books: as a
NO bid at p and as a YES ask at 1 − p, with the same size. Adding the two
books would count every order twice and overstate the liquidity, and with
it how much arbitrage could actually be traded. The code checks this with
`books_are_mirrored()`: every NO bid at p must match a YES ask at 1 − p of
the same size, and the other way round. The two books are downloaded a
fraction of a second apart, so a book that changed in between can fail the
check. That is why the code reports how often the mirror holds instead of
assuming it.

---

## 5. `download_polymarket.py`

### What it does

The script that builds the Polymarket half of the data set: Fed
rate-decision and US CPI markets that end on or after 24 months ago.

```bash
python src/download_polymarket.py
```

| Step | What happens |
|---|---|
| 1 | collects candidate events from five tags and six series, sorts them by title rules into Fed and CPI types, and prints what it keeps **and what it leaves out** |
| 2 | turns the kept events into one row per market; leaves out markets that ended before the start date or have no token ids yet |
| 3 | one snapshot of the YES and NO order books of every open market |
| 4 | price history of the YES and NO token of every market: 3-hour points over the whole life, plus hourly points for the last 30 days of open markets |
| 5 | checks, saves four tables and prints examples |

The four tables in `data/processed/`:

| File | One row per | Rows (2026-11-04) |
|---|---|---|
| `poly_markets.parquet` | market | 736 (Fed 303, CPI 433) |
| `poly_orderbooks.parquet` | open market: best bids, asks, sizes, spread | 142 |
| `poly_orderbook_levels.parquet` | price level of the YES and NO books | 15,272 |
| `poly_prices.parquet` | market, token (YES/NO), series and price point | 607,341 |

### Key functions

| Function | What it does |
|---|---|
| `classify_event(title)` | (topic, market type) of an event title, or None to leave it out |
| `title_pattern(title)` | replaces month names with `<month>` and numbers with `#`, to group similar titles in the printout of what was left out |
| `collect_candidates()` | step 1a: every event with one of the source tags or in one of the source series, each once |
| `choose_events(events, start)` | step 1b: keeps events that end on or after the start date and match a title rule; prints the counts |
| `download_prices(markets)` | step 4; a series that fails is listed and skipped |
| `check_prices()` | prices between 0 and 1, or stop |
| `check_settlements(markets, prices)` | the final `settlement` point of each resolved market must equal the payout in `poly_markets` |
| `main()` | runs the steps, checks and saves |

The title rules, in order (the first match wins):

* `EXCLUDE_RULE` first: titles about dissent votes, or "derivative" markets
  that bet on Polymarket's own odds, are left out even if they mention the
  Fed.
* `FED_RULES`: `decision` ("Fed decision in December?"), `rate_level`,
  `cuts_count`, `other`.
* `CPI_RULES`: `cpi_mom`, `cpi_yoy`, `core_cpi_mom`, `core_cpi_yoy`, `other`.
  The patterns match US titles only; other countries are named in their
  titles ("Inflation China").

### Design choices and why

* **Cast a wide net, then filter by explicit rules.** Several tags and series
  are combined (each event kept once, by id), so few relevant events are
  missed. Then written rules decide what stays. Recall first, then
  precision.
* **The left-out events are printed, grouped by pattern.** You can see what
  was excluded and spot a rule that is too strict. Sample selection is
  transparent rather than hidden.
* **Rules are tested on real titles** (`test_events_we_keep`,
  `test_events_we_leave_out` in `tests/test_polymarket.py`).
* **Two price series.** Polymarket keeps 3-hour prices forever but hourly
  prices only for about 30 days. The 3-hour series covers the whole sample;
  the hourly series adds detail where it exists. Column `bucket_seconds`
  tells them apart.
* **A cross-check between two APIs.** `check_settlements()` compares the
  final payout in the price history (Data API) with the payout in the market
  record (Gamma API). Agreement is evidence that both were read correctly.
* **Missing information is handled openly.** A market whose `closed` field
  is unknown is treated as open (so its book is fetched); markets without
  token ids are left out *with a printed warning*, never silently.

### Interview question

**"How did you select the Polymarket markets in your sample, and how do you
know the selection is right?"**

*Model answer.* Polymarket has no clean "economics" category for these
markets, so I first collected every event carrying one of five Fed or
inflation tags or belonging to one of six recurring series. Then I applied
written title rules: an exclusion rule for markets that only mention the Fed
(dissent votes, bets on Polymarket's own odds), then patterns for each market
type, US only. The rules are in one place in the code and unit-tested on real
titles. The script prints how many events of each type it kept and the 40
most common patterns it left out, so the selection can be inspected and
repeated. The sample includes closed and open markets ending in the last 24
months, so it is not limited to survivors. Finally, the matching step checks
each market again, field by field, against Kalshi, so a wrongly included
market would not become a pair.

---

## 6. `match.py`

### What it does

The heart of the project. It finds pairs of **individual binary markets**, one
on Kalshi and one on Polymarket, whose YES pays in exactly the same
situations (or in exactly the opposite ones). Then it compares their rules
texts, flags every difference and suggests a verdict.

```bash
python src/match.py
```

It reads `kalshi_markets.parquet` and `poly_markets.parquet` (plus four small
cached requests for Kalshi's settlement sources) and writes:

* `match_candidates.csv`: the 201 candidate pairs, for review;
* `match_all_pairs.parquet`: all 16,713 pairs that were compared, each with
  the reason it is or is not a candidate (an audit trail).

### The main idea: compare "key fields", not titles

For every market, simple text rules read the **key fields**:

| Key field | Example |
|---|---|
| measure: what is measured | `cpi_mom` (CPI, change from the previous month), `fed_change` (change of the Fed's upper bound at one meeting), `fed_level`, `cpi_yoy` ... |
| month: the meeting or data month | Kalshi `KXCPI-26OCT` → `2026-10` |
| event date: the scheduled meeting or release day | from the close time (Kalshi) or the rules text (Polymarket) |
| outcome: the values for which YES wins, as an interval [low, high] | Kalshi "more than 0.2%" → [3, no limit] |

**Values are whole numbers of steps**, so comparisons are exact:

* CPI in tenths of a percent, because the BLS publishes CPI changes with
  **one decimal** and both venues settle on that figure. So Kalshi's "more
  than 0.2%" means 0.3% or more: [3, no limit].
* The Fed in basis points, because it moves in 25 bp steps. Kalshi's "cut
  >25bps" means a cut of 50 bp or more: [no limit, −50].

Two outcomes are then compared with `relation()`:

| Relation | Meaning | Example |
|---|---|---|
| `same` | both YES win for exactly the same values | Polymarket "0.9% or more" and Kalshi "more than 0.8%" |
| `opposite` | one YES wins exactly when the other loses | Polymarket "0.1% or less" and Kalshi "more than 0.1%" |
| `overlap` | they share some values, not all | "= 0.3%" and "≥ 0.3%" |
| `no overlap` | never win together | "= 0.3%" and "≥ 0.4%" |

Only `same` and `opposite` pairs can form a riskless position, so only those
become candidates.

### The seven steps

| Step | Functions | What happens |
|---|---|---|
| 1 | `kalshi_table()`, `poly_table()` with `kalshi_outcome()`, `poly_outcome()`, `kalshi_month()`, `poly_month()`, `kalshi_rules_date()`, `poly_scheduled_date()`, `kalshi_rule_features()`, `poly_rule_features()` | read the key fields and the edge-case rules of every market |
| 2 | `build_pairs()` | pair markets of the same topic whose dates are within 7 days *or* whose key fields say same measure and month |
| 3 | `title_similarity()` | a 0–100 similarity score of the titles (rapidfuzz `token_set_ratio`), kept for information |
| 4 | `classify_pair()`, `relation()` | how the two outcomes relate; is the pair a candidate, and if not, why not |
| 5 | `pair_flags()` = `settlement_flags()` + `data_flags()` + `fed_rule_flags()` or `cpi_rule_flags()` + `timing_flags()` | every difference between the two rules texts and every data problem, each with points |
| 6 | `suggest()` | suggested verdict `same` / `unsure` / `different` with a one-line reason |
| 7 | `keep_earlier_verdicts()`, `save_review_file()`, `print_summary()`, `print_trickiest()` | save, keeping any verdicts typed earlier; print a summary and the 10 trickiest pairs |

Other important helpers:

* `to_steps()`, `describe_outcome()`, `format_value()`: convert between
  numbers in titles, whole steps and readable text ("CPI MoM ≥ 0.3%").
* `rate_before_each_meeting()`: the Fed's upper bound before each meeting,
  read from Kalshi's *settled* rate-level markets of the previous meeting.
  It is needed for `level_vs_change` pairs (Kalshi asks about the *level*
  after a meeting, Polymarket about the *change* at it).
* `settled_values()` and `describe_values()`: which values of the figure are
  consistent with how *all* markets of an event settled. If Kalshi's markets
  for October 2025 paid as if CPI was 0.2% and Polymarket's as if it was
  0.3%, the venues settled the month on different values, even if one
  particular pair happened to pay the same.
* `duplicate_events()`: Polymarket events that list exactly the same outcome
  as another event (this found the duplicate September 2026 Fed event).
* `trickiest()`: picks 10 informative pairs with high scores and as many
  different problems as possible, one per event.

**Flags and points.** Each flag adds points to a `difference_score`, so the
riskiest pairs can be sorted to the top. For example: `paid_differently` 10
(the pair actually paid differently), `settled_on_different_values` 6,
`poly_closed_early` 5, `level_vs_change` 4, `cancelled_meeting` 1 to 4,
`postponed_release` 2, `rounding` 1, and notes with 0 points such as
`revisions` (neither venue's text covers later data revisions). The full
list is in `docs/matching.md`.

**Suggested verdict.**

* `different`: the pair actually paid differently.
* `unsure`: the same outcome on paper, but it rests on an assumption
  (`level_vs_change`) or the data show a problem (closed early, dates far
  apart, the venues settled the month or meeting on different values).
* `same`: the same outcome; rare edge cases are listed but do not change the
  verdict.

Result on 2026-11-04: 201 candidates, 155 `same`, 43 `unsure`, 3 `different`.

### Design choices and why

* **Key fields decide, titles do not.** Title similarity cannot separate true
  from false pairs (see the interview question), so it is only a fallback
  (`title_only`, needs a score of 80 or more, and was never needed).
* **Dates are a sanity check, not the key.** Some Polymarket end dates are a
  month off, so the date check uses the end date *or* the actual close time,
  and a pair about the same measure and month is compared even if its dates
  look far apart (it is then flagged).
* **Whole-number steps** avoid rounding errors, and they build the
  one-decimal publication rule into the comparison itself.
* **Rules-based text reading (regular expressions) rather than machine
  learning.** The markets are written in a few fixed templates, so rules read
  them reliably. Every rule is visible, explainable to an examiner and
  unit-tested (77 test cases in `tests/test_match.py`, many on real titles and
  rules texts).
* **Single markets, not whole events.** A Polymarket bucket such as "exactly
  0.3%" has no single twin on Kalshi, which lists only "more than X%"
  thresholds. It equals the *difference* of two Kalshi markets, so it is not
  paired one-to-one (a possible extension).
* **Cautious by design.** Whenever a match depends on an assumption, the
  suggestion is `unsure`. Calling a false pair `same` would create fake
  arbitrage in the results; calling a true pair `unsure` only loses one
  observation.
* **Settled results are used as evidence.** For pairs that have already
  settled, the code checks whether both venues actually paid the same. This
  turns history into a test of the matching.
* **Nothing typed by hand is lost.** Re-running `match.py` copies earlier
  verdicts and notes into the new file by pair, and keeps typed rows that are
  no longer candidates at the end, marked "NO LONGER FOUND".
* **Excel-friendly CSV.** It is saved in UTF-8 with a byte-order mark so
  Excel shows ≤ and ≥ correctly, and read back whether Excel saved it with
  "," or ";" and in UTF-8 or Windows encoding.
* **An audit trail.** `match_all_pairs.parquet` keeps every compared pair
  with `why_not_candidate`, so any rejection can be traced.

### Interview question

**"Why didn't you simply match markets by how similar their titles are?"**

*Model answer.* Because similar titles can describe different contracts, and
the difference is often one symbol. In my data, among pairs about the same
question, the 165 true candidates have a median title score of 68 and the
4,489 rejected pairs a median of 67: the score cannot tell them apart.
"Cut 25bps" and "decrease 25 bps" score 78 and are the same outcome; "Cut
>25bps" and "decrease 25 bps" also score 78 but are not. Kalshi's headline
CPI "more than 0.3%" and Polymarket's *core* CPI "0.3%" score 80, higher than
most true pairs. So I read the economic content instead: what is measured,
for which month or meeting, and the exact set of values for which YES pays,
in whole steps of the published precision. Two markets are a candidate only
if those sets are identical or exact complements. Dates and titles are kept
as sanity checks, and the settled results confirm the method: of 177
candidate pairs settled on both venues, 174 paid the same, and the 3 that did
not are explained by documented events (the 2025 government shutdown and a
duplicate Polymarket event).

---

## 7. `make_review_sheet.py`

### What it does

Turns `match_candidates.csv` into an Excel file, `match_review.xlsx`, for
checking the pairs by hand.

```bash
python src/make_review_sheet.py              # refuses to overwrite typed work
python src/make_review_sheet.py --overwrite  # replace it anyway
```

The sheet has one row per pair and 11 columns: `pair_id`, `kalshi_title`,
`polymarket_title`, `side`, `suggested_verdict`, `reason`, `differences` (one
flag per line), `kalshi_rules`, `polymarket_rules`, and two empty columns for
you: `verdict` (a dropdown: same / different / unsure) and `note`.

### Key functions

| Function | What it does |
|---|---|
| `pair_id(kalshi_ticker, poly_market_id)` | the pair's id, e.g. `KXCPI-25OCT-T0.4 / 655625` |
| `one_flag_per_line(differences)` | splits the flags onto separate lines, only where a new `[points]` starts, so a "\|" inside an explanation stays |
| `review_table(candidates)` | the 11 columns, sorted by `difference_score`, highest first |
| `save_review_sheet(table, path)` | writes the Excel layout: wrapped text, borders, bold frozen header, colours, column widths, dropdown, filter arrows |
| `count_typed_rows(path)` | how many rows of an existing sheet already have a verdict or note |
| `make_sheet(...)` | puts it together; stops if typed work would be overwritten |

### Design choices and why

* **The trickiest pairs come first** (sorted by `difference_score`), so your
  attention goes where errors are most likely. Pairs with equal scores keep
  their date order ("stable" sort).
* **Colours by suggested verdict** (light red = different, light yellow =
  unsure, white = same) for a quick overview.
* **A dropdown** in `verdict`: Excel refuses anything other than the three
  words, so typos cannot happen.
* **`pair_id` is a stable key.** It is built from the two market ids, not
  from the row number, so verdicts can be matched to the right pair even
  after a re-run or after you sort the sheet.
* **It refuses to overwrite your work.** If the sheet already has verdicts or
  notes, the script stops unless you add `--overwrite`. Hours of manual
  review cannot be lost by one careless command.
* **Text stays text.** openpyxl treats any text starting with "=" as a
  formula; the code switches such cells back to plain text, so a rules text
  can never turn into a formula.
* **Reuses `match.py`'s file reader**, so the CSV is read in exactly the same
  way everywhere.

### Interview question

**"Why include a manual review step at all, if the code already suggests a
verdict?"**

*Model answer.* Because the main risk in cross-venue arbitrage is not prices
but *contract specification*: two contracts that look the same can resolve
differently in edge cases, and that turns a "riskless" position into a
losing one. The code reads the rules with text patterns, which is
transparent but can miss wording it was not written for. A reviewer reading
both full rules texts side by side is an independent check. The sheet is
designed to make that review fast and hard to get wrong: riskiest pairs
first, the code's reasons and flags next to the rules, a dropdown that only
allows valid verdicts, and a stable pair id so every verdict lands on the
right pair.

---

## 8. `read_review_sheet.py`

### What it does

Reads your filled-in `match_review.xlsx` and saves your verdicts as
`match_verified.csv`: `pair_id` first, then all columns of
`match_candidates.csv`, with your `verdict` and `note` filled in.

```bash
python src/read_review_sheet.py                      # the default file
python src/read_review_sheet.py path/to/my_review.xlsx
```

### Key functions

| Function | What it does |
|---|---|
| `read_verdicts(path)` | reads `pair_id`, `verdict`, `note`; tidies them ("Same " → "same"); stops with a list of **Excel row numbers** to fix if a verdict is invalid or a `pair_id` is empty or repeated |
| `verified_table(review, candidates)` | puts your verdicts on the rows of `match_candidates.csv` by `pair_id`; keeps typed pairs that are no longer candidates; lists candidates that are not in your sheet |
| `save_verified(table, path)` | writes the CSV (UTF-8 with byte-order mark, 50 MB check) |
| `main()` | runs it and prints your counts, and how many of your verdicts differ from `match.py`'s suggestion |

### Design choices and why

* **Errors name Excel row numbers** (row 1 is the header, so table row 0 is
  Excel row 2). You can go straight to the cell to fix.
* **Matching by `pair_id`, not by position.** Sorting or filtering the sheet
  in Excel cannot mix up verdicts.
* **Nothing is silently dropped.** A pair you reviewed that `match.py` no
  longer finds is kept at the end, marked "NO LONGER FOUND". Pairs found
  after the sheet was made are listed, with an empty verdict.
* **It reports disagreement with the code.** The number of your verdicts
  that differ from `match.py`'s suggestion is printed: a quick measure of how
  far the human and the automatic check agree.
* **Same output format as `apply_independent_review.py`**, so the next step
  (`build_pairs.py`) works with either way of reviewing.

### Interview question

**"Your verdicts come from an Excel file that a person edited. What can go
wrong when reading them back, and how does the code protect against it?"**

*Model answer.* Several things: typos or stray spaces in a verdict
("Same "), an invalid word, a row copied twice, an emptied id, rows re-sorted
or filtered in Excel, and pairs that changed because the matching was re-run
in the meantime. The code tidies case and spaces, then validates: any
verdict other than same, different, unsure or empty, or a missing or
duplicated pair id, stops the script with the exact Excel rows to fix. It
never writes half-checked data. Verdicts are joined by a stable pair id
built from the two market ids, so row order does not matter. Pairs that
disappeared after a re-run are kept and marked, and new pairs are listed, so
no reviewed work is lost and no unreviewed pair is mistaken for a reviewed
one.

---

## 9. `apply_independent_review.py`

### What it does

Produces the verified pairs through an **automated two-pass review**, the
method actually used for the 201 candidates on 2026-11-04 (see "Verification
method" in `docs/matching.md`).

* **Pass 1** (`data/processed/independent_review.csv`): a second reviewer, an
  AI assistant, read both full rules texts of every pair *without* seeing
  `match.py`'s suggestion, reason, score or flags, and wrote its own verdict
  and a one-line reason. These were saved before any comparison.
* **Pass 2** (this script) combines the two verdicts cautiously:

| Pass 1 | `match.py` | Combined |
|---|---|---|
| same | same | **same** |
| either says unsure | | **unsure** |
| otherwise (both say "not the same") | | **different** |

It writes `match_verified.csv` (the same format as `read_review_sheet.py`,
plus a column `review_method` = "automated two-pass") and `spot_check.xlsx`:
10 random `same` pairs (fixed seed 20261104) with an empty `my_check` column
(agree / disagree) and a second sheet with instructions, for a human check.

```bash
python src/apply_independent_review.py
```

### Key functions

| Function | What it does |
|---|---|
| `combine(independent, suggested)` | the rule in the table above |
| `read_independent_review(path)` | reads and validates pass 1 (columns present, ids unique, verdicts valid) |
| `combined_review(independent, candidates)` | checks that both files hold exactly the same pairs, then combines; note = pass-1 reason, plus " (disagreement)" where the two differ |
| `verified_table(...)` | builds `match_verified.csv` by reusing `read_review_sheet.verified_table()`, and adds `review_method` |
| `disagreements(...)` | the pairs where pass 1 and `match.py` differ, with both reasons, for printing |
| `made_by_this_script(path)` | True if `match_verified.csv` does not exist or was written by this script |
| `spot_check_rows()`, `row_height()`, `save_spot_check()`, `count_typed_checks()` | the spot-check sheet: random rows, tall enough to read both rules texts without clicking |

### Design choices and why

* **Blind first pass.** The second reviewer did not see the code's answer,
  so it could not simply agree with it. Its verdicts were frozen in a file
  before the comparison.
* **Asymmetric combination.** A pair is `same` only if both say so. A false
  `same` would put a non-identical pair into the arbitrage analysis and could
  create fake profits; a false `unsure` only loses an observation. So the
  rule minimises the costly error.
* **Disagreements are marked and printed** with both reasons, so each one can
  be explained. The passes agreed on 192 of 201 pairs. All 9 disagreements
  are pairs that paid consistently but whose venues settled the month or
  meeting on different values (the 2025 shutdown CPI months and the
  duplicate September 2026 Polymarket event). Pass 1 said `different`,
  `match.py` said `unsure`, so they end up `unsure`. Final: 155 same, 43
  unsure, 3 different.
* **Provenance is recorded.** `review_method` says how each verdict was made,
  so a reader of `pairs.parquet` knows.
* **A reproducible human spot check.** A fixed random seed means the same 10
  pairs come out every time, so the check can be repeated and reported. In
  the committed `spot_check.xlsx`, `my_check` is "agree" for all 10 pairs.
* **Safety before writing.** Both possible blocks (a `match_verified.csv`
  made by hand, a spot-check sheet already filled in) are checked *before*
  either file is written, so a stop never leaves one file updated and the
  other not.
* **Reuse.** It calls `read_review_sheet.verified_table()` rather than
  copying it, so both review routes produce identical files.

### Interview question

**"One of your two reviewers is an AI. How do you defend the verification as
credible?"**

*Model answer.* I treat it as one input, not as the truth, and I designed
around its weaknesses. First, it was blind: it saw the full rules texts but
not the code's verdict, and its answers were saved before the comparison.
Second, the combination is conservative: a pair counts as the same only if
both the rule-based matcher and the reviewer agree, so either one can veto.
Third, every disagreement is listed and explained; all nine trace back to
two documented events. Fourth, there is external evidence: of the 177
candidate pairs that settled on both venues, 174 paid the same way, and the
three that did not were already flagged. Fifth, I checked 10 randomly drawn
`same` pairs by hand (fixed seed, so it can be reproduced) and agreed with
all 10. The limitation I would state openly is that the two automated checks
could share blind spots, so their agreement is not fully independent, and 10
pairs is a small sample: agreeing on all 10 rules out a high error rate but
not a low one. The spot check could be extended, or reported with an
agreement statistic such as Cohen's kappa.

---

## 10. `build_pairs.py`

### What it does

The last step of matching: turns the `same` pairs of `match_verified.csv`
into `data/processed/pairs.parquet`, the input for `arb.py` (section 11). It
adds the ids needed to fetch prices (Kalshi event ticker, Polymarket
condition id and YES/NO token ids) and prints counts per topic and month.

```bash
python src/build_pairs.py
```

Result: **155 pairs**: 76 Fed-decision pairs (21 meetings, November 2024 to
April 2027), 37 CPI month-on-month pairs and 42 CPI year-on-year pairs. 133
have settled on both venues; 39 are opposite-side pairs.

### Key functions

| Function | What it does |
|---|---|
| `build_pairs(verified, kalshi_markets, poly_markets)` | keeps verdict `same`; checks every pair; builds the table with the ids, times (UTC) and results |
| `counts_per_month(pairs)` | a cross-table of pairs per month and measure, with totals |
| `save_pairs(pairs, path)` | writes Parquet with the 50 MB check |

The checks in `build_pairs()` (any failure stops with a list of problems):
both markets must measure the same thing, the measure must be known
(`fed_change`, `cpi_mom`, `cpi_yoy`), and both markets must exist in their
market tables. So a `level_vs_change` pair can never slip into the final
table by mistake.

### Design choices and why

* **One row per verified pair, with everything the next step needs:** the
  outcome in words, which side matches which (`poly_yes_equals`), the token
  ids for Polymarket prices, and the times that limit the comparison.
* **Event date from Kalshi.** Kalshi's markets close on the day of the
  meeting or release; Polymarket's rules have date typos (risk 4 in
  `docs/matching.md`).
* **`kalshi_close_time` is kept to limit the comparison.** Kalshi stops
  trading just before the announcement; after that, prices on the two venues
  are no longer comparable.
* **Works with either review route.** If `match_verified.csv` came from your
  own sheet, `review_method` is set to "manual review sheet".
* **Empty text becomes "missing"**, so an unsettled result is a missing
  value and not the empty string "", which pandas would count as a real
  value.

### Interview question

**"What does `poly_yes_equals` mean, and which two positions form a riskless
hedge for a pair?"**

*Model answer.* It says how the two YES contracts relate. "Kalshi YES" means
Polymarket's YES wins exactly when Kalshi's YES wins. "Kalshi NO" means
Polymarket's YES wins exactly when Kalshi's YES loses, for example
Polymarket "0.1% or less" against Kalshi "more than 0.1%". The hedge is any
two legs that together pay $1 in every state:

| `poly_yes_equals` | Hedge 1 | Hedge 2 |
|---|---|---|
| Kalshi YES | Kalshi YES + Polymarket NO | Kalshi NO + Polymarket YES |
| Kalshi NO | Kalshi YES + Polymarket YES | Kalshi NO + Polymarket NO |

In each case exactly one leg pays $1. Buying both legs at their ask prices
is an arbitrage if the two asks plus all costs add up to less than $1.
`arb.py` checks both directions of every pair it is given and reports the
better one (section 11).

---

## 11. `arb.py`

### What it does

Step 4 of the project: the arbitrage calculation itself. Given one verified
pair and one order-book snapshot from each venue (taken at almost the same
moment), it answers four questions:

1. At the two best asks, how much of the price gap is left after **both
   venues' taker fees**?
2. Walking down both order books, **how many contracts** is it worth buying,
   and what is the **total profit**?
3. What is the **return on the capital** that stays locked until the markets
   pay out, and what is that per year?
4. Which **direction** (which two legs) is the better one?

It reads `pairs.parquet` and `poly_markets.parquet` (for each Polymarket
market's own fee fields). Like everything else, it only reads public order
books and never places orders.

```bash
python src/arb.py --download   # fresh books of the 3 example pairs -> data/processed/arb_examples.parquet
python src/arb.py              # print the rows again from the saved books
python src/arb.py --explain "KXFEDDECISION-26DEC-H25 / 3215008"   # every step of one pair
```

In a notebook, `arb.snapshot_row(pair, kalshi_levels, poly_levels, poly_market)`
does the same for any two order-book snapshots of a pair.

### The two fee formulas

We always buy offers that are already in the book. That makes us the
**taker** on both venues, and takers pay the fee.

| | Kalshi | Polymarket |
|---|---|---|
| taker fee | round up(M × 0.07 × C × P × (1 − P)) | C × feeRate × (p × (1 − p))^exponent |
| letters | C = contracts, P = price, M = the series' multiplier | C = shares, p = price; feeRate and exponent from the market's own record |
| source | fee schedule PDF (https://kalshi.com/docs/kalshi-fee-schedule.pdf, version effective 7 July 2026), read by hand because the cloud session could not download it; rounding from https://docs.kalshi.com/getting_started/fee_rounding.md | https://docs.polymarket.com/trading/fees.md; the exponent from https://docs.polymarket.com/market-data/market-details.md#trading-fees |
| in our data | M = 1 for all six series, checked with Kalshi's public API (no series or event overrides) | of the 155 paired markets: 86 have no fee schedule, 60 have rate 0.05, 4 have rate 0.03, 5 have rate 0; the exponent is always 1 |
| rounding | each fill's fee up to $0.000001, then cost + fee up to a centicent ($0.0001), **once per order** | to 5 decimal places |
| makers | a maker fee exists but never applies (we only take) | "Makers are never charged fees" |
| dates | | no fee on Fed and CPI markets before 2026-03-30 ("Fee Structure V2"). **Assumption:** a market's current schedule applies from that day on |
| largest fee | 1.75 cents per contract, at P = $0.50 | 1.25 cents per share at p = $0.50 (rate 0.05) |

**What P × (1 − P) means.** Both fees grow with P × (1 − P), which is largest
at a price of 50 cents and falls to zero towards 0 and 1. (It is also the
variance of the payoff of a $1 binary bet that wins with probability P.) So
trading costs are highest exactly when the market is most uncertain, and the
price gap needed for an arbitrage depends on the price level.

**One rounding question was decided openly.** The schedule's rule (round to
a centicent) gives a fee of $1.7325 for 100 contracts at $0.55, but its own
example table shows $1.74, the fee rounded up to a whole cent. The code uses
the centicent rule by default; `precision=arb.CENT` reproduces the table. Both
values are unit tests, and the difference is at most $0.01 per order.

### The depth walk

The best ask only offers a limited number of contracts. To buy more you must
pay the next, higher prices. `walk_books()`:

1. buys the cheapest offers first, the **same number of contracts on both
   venues** (each Kalshi contract needs its Polymarket hedge);
2. moves on in steps: a step ends when an offer on either venue is used up;
3. computes each step's profit per contract,
   1 − Kalshi price − Polymarket price − both fees per contract;
4. **stops at the first step that would lose money.**

Stopping there is optimal. It is the familiar rule of producing until
marginal revenue equals marginal cost. Each further contract pays $1
(marginal revenue). Its cost is price + fee, and that cost only rises down
the book, because price + 0.07 × P × (1 − P) always increases with P (its
slope, 1 + 0.07 × (1 − 2P), is at least 0.93). So once one step loses money,
every later step loses more. Because rounding can make the last few
contracts not worth buying, the exact totals are recomputed after every step
and the best one is kept.

### Return on capital

* **Capital locked** = everything paid on both venues, prices plus fees.
  Neither venue lends: you pay the full price up front, and the money stays
  locked until both markets pay out.
* **Return on capital** = total profit / capital locked.
* **Annualised** = return × 365 / days until resolution (simple, not
  compounded). Resolution is taken as the later of Kalshi's close time and
  Polymarket's end date.

The annual figure makes trades of different lengths comparable, and it can
be compared with a safe interest rate, the opportunity cost of the capital.

### Worked example: `KXFEDDECISION-26DEC-H25 / 3215008`

Kalshi "Will the Federal Reserve Hike rates by 25bps at their December 2026
meeting?" against Polymarket "Will the Fed increase interest rates by 25 bps
after the December 2026 meeting?" (side `Kalshi YES`: the same outcome).
Order books of 2026-11-04 at 23:37:35 UTC, the two venues 0.72 s apart.

| Step | Calculation | Result |
|---|---|---|
| direction | Kalshi YES 0.81 + Polymarket NO 0.23 = 1.04 loses 4 cents before fees, so buy **Kalshi NO + Polymarket YES**: if the Fed hikes 25 bp, Polymarket YES pays $1; if not, Kalshi NO pays $1 | |
| best asks | Kalshi NO $0.20 (8,265.49 contracts; Kalshi's YES bid of $0.80 seen from the other side), Polymarket YES $0.78 (151,465.13 shares) | gross gap = 1 − 0.20 − 0.78 = **2 cents** |
| Kalshi fee | 0.07 × 8,265.49 × 0.20 × 0.80 = 92.573488; cost 1,653.098 + fee, rounded up to $0.0001 | **$92.5735** |
| Polymarket fee | 8,265.49 × 0.05 × (0.78 × 0.22) = 70.9179042, rounded to 5 decimals | **$70.9179** |
| fees per contract | (92.5735 + 70.9179) / 8,265.49, i.e. 1.12 + 0.858 cents | **1.978 cents** |
| net per contract | 2 − 1.978 cents | **0.022 cents** |
| depth | the next Kalshi level, $0.21, would give 1 − 0.21 − 0.78 − fees = −1.02 cents per contract: stop | max size **8,265.49** on each venue |
| capital | 1,653.098 + 92.5735 + 6,447.0822 + 70.9179 | **$8,263.67** |
| profit | 8,265.49 × $1 at resolution − 8,263.67 | **$1.82** |
| return | 1.8184 / 8,263.67 = 0.022%, over 35.01 days (to Polymarket's end date, 2026-12-09 23:59 UTC) | **0.23% a year** |

So: **$1.82 profit on $8,264 locked for 35 days.** The other two example
pairs lose money after fees, so their `max_size` is 0:

| pair | better direction | gross gap | fees per contract | net per contract |
|---|---|---|---|---|
| `KXCPI-26OCT-T0.1 / 5691223` | Kalshi NO + Polymarket NO | −1 cent | 1.135 cents | −2.135 cents |
| `KXFEDDECISION-26DEC-H0 / 3215007` | Kalshi YES + Polymarket NO | 0 | 1.991 cents | −1.991 cents |
| `KXFEDDECISION-26DEC-H25 / 3215008` | Kalshi NO + Polymarket YES | +2 cents | 1.978 cents | +0.022 cents |

### Key functions

| Group | Function | What it does |
|---|---|---|
| Kalshi fee | `kalshi_fee_per_contract(price, multiplier)` | M × 0.07 × P × (1 − P), before rounding |
| | `kalshi_fee_for_fills(fills, multiplier, precision)` | the fee of ONE order that fills at several prices, with Kalshi's rounding applied once |
| | `kalshi_fee(price, contracts, ...)`, `kalshi_multiplier(ticker)` | one price; the series' multiplier |
| Polymarket fee | `polymarket_fee_per_share(market, price, timestamp)` | feeRate × (p × (1 − p))^exponent; zero without a schedule or before 2026-03-30 |
| | `polymarket_fee(market, price, contracts, timestamp, taker)` | the fee rounded to 5 decimals; makers pay nothing |
| | `has_fee_schedule(market)` | does the market charge fees at all? |
| Books | `ask_ladder(levels, outcome)` | the offers to sell us YES or NO, cheapest first |
| | `poly_outcome_to_buy(kalshi_outcome, side)` | which Polymarket token hedges the Kalshi contract we buy |
| Profit | `top_of_book(...)` | gross gap, fees, net profit and capital per contract at the best asks |
| | `walk_books(...)`, `depth_totals(...)` | the depth walk, and the exact totals with all rounding |
| Return | `return_on_capital()`, `annualised_return()`, `resolution_time()`, `days_until_resolution()` | see above |
| One row | `check_direction()`, `snapshot_row(pair, kalshi_levels, poly_levels, poly_market)` | both directions of a pair; the row of the better one |
| Example | `download_example_books()`, `example_rows()`, `print_worked_example()`, `main()` | fresh books of the three example pairs, their rows, and every step printed |

### Design choices and why

* **Money in `Decimal`.** With floats, 0.07 × 100 × 0.5 × 0.5 is
  1.7500000000000002, and "round up to the next cent" would charge $1.76
  instead of $1.75 (`test_kalshi_fee_has_no_float_errors`).
* **Every formula cites its source and is tested against the venue's own
  examples** (rule 4): Kalshi's table (100 contracts at $0.55 → $1.74) and
  rounding example, and Polymarket's Economics table (100 shares at $0.50 →
  $1.25, at $0.10 → $0.45). `tests/test_arb.py` has 34 test cases.
* **What could not be verified automatically is said openly.** The Kalshi
  PDF was read by hand, and the assumption about when Polymarket's current fee
  schedules started is written in the code and in `docs/arbitrage.md`.
* **Fees come from each Polymarket market's own fields,** as Polymarket asks,
  and depend on the trade time, so the same functions will work for
  historical prices (no Polymarket fee before 30 March 2026).
* **Only the ask price counts.** We buy at the ask, never at the midpoint or
  the last trade, because only the ask is a price you can actually trade at.
* **Both directions are checked, and only one can be positive.** The two
  gross gaps add up to minus the two venues' bid-ask spreads. In the example,
  −4 + 2 = −2 cents = −(1 cent Kalshi spread + 1 cent Polymarket spread)
  (`test_the_two_gaps_add_up_to_minus_both_spreads`).
* **Rounding as a real order would see it.** Top-of-book fees are computed
  for a real order of all contracts on offer at both best asks, then divided
  per contract. A Kalshi order that sweeps several price levels is rounded
  once, as Kalshi's "fee accumulator" does (`test_kalshi_fee_is_rounded_once_per_order`).
* **Polymarket depth only from the token's own book.** The NO book mirrors
  the YES book, so adding them would count every order twice
  (`test_ask_ladder_never_adds_polymarket_yes_and_no_depth`).
* **Snapshots must be simultaneous.** The example books of both venues were
  taken within a second of each other, and every row reports
  `seconds_apart`. The order books saved by the two download scripts are
  about 3.5 hours apart and must not be combined.
* **It reuses the order-book format** of `kalshi.parse_orderbook()` and
  `polymarket.parse_orderbook()`, and `kalshi.save_table()` for the 50 MB check.

### Interview question

**"Your best example still makes a profit after fees. Why do you conclude it
is not an attractive arbitrage?"**

*Model answer.* Because "positive after fees" is not the same as "worth
doing". The gap was 2 cents, but the two taker fees took 1.978 cents, leaving
0.022 cents per contract: $1.82 on $8,264 locked for 35 days. That is 0.23%
a year. After its October 2026 meeting the Fed's target range was 3.75–4.00%
(Kalshi's settled KXFED markets), so safe short-term money earned roughly 4%
a year, about 17 times more. To match 4% over those 35 days, the gap would
have needed to be about 2.4 cents. Three more things make it worse. It is
fragile: the next Kalshi level was one cent higher, and there each extra
contract loses about a cent. It has execution risk: the two books were seen
0.72 seconds apart, and two orders on two venues cannot be sent at exactly
the same moment, so one leg can fill without the other. And it carries
resolution risk: if the venues settle differently you can lose both legs,
about $1 per contract, so even a 1-in-4,500 chance of that would cost as much
as the expected profit. Among my 177 candidate pairs settled on both venues,
3 paid differently. So the example is evidence that the venues' prices are
*close to* no-arbitrage once fees are counted, not evidence of free money.

---

## 12. `gaps.py` and the Step 5 statistics

### What it does

Step 5 of the project: the **historical comparison**. Where `arb.py` looks at
two order-book snapshots at one moment, Step 5 looks at the whole history of
all 155 verified pairs, hour by hour, from November 2024 to 4 November 2026.
It is split in two:

* `src/gaps.py` holds the arithmetic that is worth testing on its own:
  lining up the two venues, the gap formulas, the AR(1), the Granger test and
  the settlement payoffs (25 test functions in `tests/test_gaps.py`). It reads
  saved tables only and never calls an API.
* `notebooks/01_historical_gaps.ipynb` uses these functions, explains every
  choice in plain language, runs the statistics, draws the three figures in
  `figures/` and writes [`docs/results.md`](results.md).

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/01_historical_gaps.ipynb
```

It reads `pairs.parquet`, `kalshi_candles.parquet`, `poly_trade_bars.parquet`,
`poly_prices.parquet` (cross-check and part 7), `poly_orderbooks.parquet` (the
spread), `poly_markets.parquet` (fee fields) and `match_verified.csv` (part 7),
and writes `hourly_panel.parquet` (220,609 pair-hours).

### Key functions

| Notebook part | Function | What it does |
|---|---|---|
| 1. One grid | `hourly_grid(start, end)` | all bar ends (UTC), one hour apart |
| | `kalshi_hourly(candles, grid)` | Kalshi's last candle at or before each bar end; an empty bid stays empty (it was no longer there) |
| | `poly_in_kalshi_yes_terms(price, outcome, side)` | a Polymarket trade price as the price of "Kalshi YES happens": p or 1 − p |
| | `poly_hourly(bars, side, grid)` | Polymarket's latest trade of either token, carried forward, with its age in hours |
| | `pair_window()`, `align_pair()`, `build_panel()` | one pair's common hours (until the last full hour before Kalshi closes), then all pairs stacked |
| | `poly_prices_cross_check()` | Polymarket's own price series next to our trade prices |
| 2. Gaps | `raw_gap(poly_price, kalshi_mid)`, `kalshi_mid(bid, ask)` | Polymarket last trade − Kalshi mid |
| | `typical_spread()`, `poly_buy_price()` | the median Polymarket spread (1 cent), and ask ≈ last trade + half of it |
| | `kalshi_fees()`, `poly_fees()` | the fees of `arb.py`, per contract, in force at each hour |
| | `executable_gaps(panel, pairs, poly_markets, half_spread)` | both hedged directions after fees; keeps the better one (`exec_net`) |
| 4. Persistence | `previous_hour(table, column)` | the value exactly one hour earlier in the same pair, else NaN |
| | `ar1(gap, gap_before)`, `half_life(rho)` | the AR(1) by OLS, and ln(0.5) / ln(ρ) |
| 6. Price discovery | `granger_test(y, own_lags, other_lags)` | does the other venue's past help to predict y? (robust F-test) |
| 7. Resolution risk | `direction_payoffs(kalshi_result, poly_result, side)` | what each direction paid: $1 and $1, or $2 and $0 |

### The Step 5 statistics, one by one

**Shares and medians (part 3).** The headline numbers are *shares* of
pair-hours ("26% of hours have a positive gap after fees") and *medians*
("0.87 cents when positive"), not means. A few odd single trades create gaps
of 50 cents or more; they would move a mean a lot and a median hardly at
all. Fed pairs live for months and CPI pairs for about a month, so pooled
shares mostly describe the Fed. The column "median pair's share" gives every
pair the same weight instead.

**Sensitivity table (part 2).** The Polymarket ask is estimated, so the
share is recomputed for half spreads of 0, 0.25, 0.5 and 1 cent and for
Polymarket prices at most 1, 6 or 24 hours old. Result: the share depends on
the spread (41% down to 17%), hardly on the age (25% to 26% at 0.5 cents).
That tells the reader which assumption matters.

**AR(1) and half-life (part 4).** For each pair, OLS of
gap(t) = a + ρ · gap(t − 1) + noise. ρ is the share of a deviation still
there after one hour. After h hours ρ^h is left, so half is gone when
ρ^h = 0.5, i.e. h = ln(0.5) / ln(ρ). Example: ρ = 0.88 (the median) gives
about 5.3 hours. Only pairs with at least 100 pairs of neighbouring hours
count (136 pairs). `previous_hour()` returns NaN when the hour before is
missing, so two hours a day apart are never treated as neighbours.

**Pooled AR(1) with pair fixed effects (part 4).** One ρ for all pairs:
subtract each pair's own mean from the gap and from the lagged gap (this is
what "pair fixed effects" means), then run one OLS. Result: ρ = 0.892,
half-life 6.1 hours, close to the median of the pair-by-pair estimates.

**Clustered standard errors (parts 4 and 5).** The hours of one pair are not
independent: a gap that lasts 5 hours appears in 5 rows. Ordinary standard
errors would treat 162,937 rows as 162,937 independent pieces of evidence
and be far too small. Clustering by pair allows any correlation *within* a
pair and treats the 153 pairs as the independent units. In statsmodels:
`.fit(cov_type="cluster", cov_kwds={"groups": pair_codes})`.

**The determinants regression (part 5).** OLS of |raw gap| in cents on log
24-hour volume on each venue, log(1 + days to resolution), the distance from
0.5 in cents, and topic dummies (the Fed is the base). Logs because volume
runs from 0 to millions (the "1 +" keeps zero-volume hours). Three columns:
(1) main, (2) with two measurement controls (Polymarket price age, Kalshi
spread), (3) with the dependent variable capped at its 99th percentile, so
the odd trades cannot drive it. A coefficient is "cents of gap per unit",
e.g. −0.050 per cent of distance from 0.5. It is a description, not a causal
estimate.

**Granger causality (part 6).** For each pair and each direction, OLS of one
venue's hourly price change on its own last 3 changes and the other venue's
last 3 changes, then an F-test that the other venue's 3 coefficients are all
zero. HC1 ("heteroskedasticity-robust") standard errors, because changes are
much larger around news than in quiet hours. If Kalshi's past predicts
Polymarket but not the reverse (5% level), "Kalshi leads". A pair needs at
least 20 hours in which both prices moved (`MIN_PRICE_MOVES`), otherwise "no
result". With two tests per pair at 5%, about 1 pair in 20 shows a lead by
chance in each direction: compare the shares with 5%.

**Resolution risk (part 7).** `direction_payoffs()` turns the two venues'
results into what each direction paid. For all 177 settled candidate pairs,
the notebook takes the last moment both venues had prices, buys the better
direction (as in part 2) and computes profit = payout − cost. Two extra
statistics:
* **The confidence interval** of the divergence rate (3 of 177 = 1.7%;
  0.4% to 4.9%) is the exact Clopper-Pearson interval, from beta quantiles
  (`scipy.stats.beta.ppf`). With only 3 events, the usual "rate ± 2 standard
  errors" would be poor and could even go below 0.
* **Break-even:** with divergence rate d and average loss L when it hits,
  normal trades must earn d × L / (1 − d) each: 0.0169 × $0.765 / 0.983 ≈
  1.32 cents, against a median positive gap of 0.87 cents.

### Design choices and why

* **Only past information.** Every value at a bar end is the last one known
  at that moment (`merge_asof(..., direction="backward")`), and the panel stops
  at the last full hour before Kalshi closes, before the news. A gap measured
  after the announcement would compare a frozen price with one that already
  knows the answer.
* **An empty Kalshi bid stays empty.** If the last candle has no bid, nobody
  was bidding; filling in an older bid would invent a price no one offered.
* **The Polymarket price carries its age** (`poly_age_hours`), so the cut-off
  (24 hours) is a choice the sensitivity table can vary, not something baked
  into the data.
* **Executable, not mid-to-mid.** The Kalshi leg uses its real ask (or 1 −
  bid for NO); only the Polymarket ask is estimated. Fees use exactly the
  functions of `arb.py`, with the trade time, so Polymarket's fee is zero
  before 30 March 2026.
* **The result is called an upper bound,** because choosing the better of two
  directions also picks up any error in the estimated Polymarket ask.
* **A cross-check, not a second source.** Polymarket's own price series is
  used only to check the trade prices (correlation 0.999), because the API
  does not say what exactly its price is.
* **Part 7 puts the rejected pairs back.** The verified pairs were chosen
  using how they settled, so parts 1-6 alone would hide the risk (see Q11).
* **Speed without loops over rows:** fees are computed once per distinct
  (price, market, before/after 30 March) and merged back, so 220,609 rows take
  seconds.

### Interview question

**"Your median half-life is 5.3 hours. How do you know that is not just
stale Polymarket prices?"**

*Model answer.* I checked it in two ways. First, I re-estimated the AR(1)
using only hours in which the Polymarket price was less than one hour old.
The median half-life falls to 2.6 hours, so part of the 5.3 hours is indeed
staleness: a price that has not been updated looks like a gap that has not
closed. But it does not fall to zero, so gaps are not only an artefact.
Second, the sensitivity table shows that the share of profitable hours
barely changes when I require fresher prices (25% to 26%), so stale prices do
not drive the headline result either. There are opposite biases too: bid-ask
bounce in Kalshi's mid and in Polymarket's last trade makes gaps look *less*
persistent. That is why I report the fresh-price version next to the main
one, and why the live five-minute order-book snapshots matter: they measure
both venues within a fraction of a second.

---

## 13. One-page summary of the pipeline

**Research question.** When the same event trades on Kalshi and Polymarket,
do the prices differ, and are the differences real arbitrage opportunities
after fees and other frictions?

**Sample.** US Fed rate decisions (FOMC meetings) and US CPI inflation
(month-on-month and year-on-year): every market that closes on or after the
date 24 months before the download, both settled and still open. These events are scheduled, settled on official statistics
(Federal Reserve, BLS) and listed on both venues as families of yes/no
thresholds, which makes exact matching possible.

```
 check_connection.py          can we reach the 4 public APIs?
          |
 kalshi.py  ------------>  download_kalshi.py  ------> kalshi_markets, kalshi_orderbooks,
 (Kalshi data client)      (4 series, 24 months)        kalshi_orderbook_levels, kalshi_candles
                                                                         \
 polymarket.py ---------> download_polymarket.py ----> poly_markets, poly_orderbooks,      \
 (Polymarket data client)  (Fed/CPI events, 24 months)  poly_orderbook_levels, poly_prices  |
                                                                          |                |
                                                                       match.py <----------+
                                     (key fields, outcome intervals, rule flags, suggestion)
                                                   |                     |
                                  match_candidates.csv (201)   match_all_pairs.parquet (16,713)
                                         |                                |
           +-----------------------------+-------------------+            (audit trail)
           |                                                 |
 make_review_sheet.py -> match_review.xlsx      apply_independent_review.py
   -> (you fill it in) -> read_review_sheet.py    (blind pass 1 + match.py, cautious combination,
           |                                       spot_check.xlsx for a human check)
           +--------------> match_verified.csv <-----+
                                   |
                            build_pairs.py
                                   |
                     pairs.parquet  (155 verified pairs)
                                   |
      fresh order books -->     arb.py     <-- poly_markets (each market's fee fields)
      (both venues, < 1 s apart)   |
                (taker fees on both venues, depth walk, return on capital)
                                   |
             arb_examples.parquet + one row per pair (3 example pairs, 2026-11-04)

 pairs.parquet --> download_poly_trades.py --> poly_trade_bars (Polymarket trades, hourly)
                                                     |
 kalshi_candles + poly_trade_bars --> gaps.py + notebooks/01_historical_gaps.ipynb
     (hourly grid, raw and fee-adjusted gaps, AR(1), regression, Granger, resolution risk)
                                                     |
                     docs/results.md + figures/ (3 figures) + hourly_panel.parquet

 snapshot.py: every 5 minutes, both venues' full order books (live, until December 2026)
```

**What each stage contributes.**

1. **Access** (`check_connection.py`): public, unauthenticated GET requests
   only; no keys, no accounts, no orders (rule 1).
2. **Data clients** (`kalshi.py`, `polymarket.py`): throttled, retried,
   cached requests; exact order-book arithmetic built on YES + NO = $1;
   tidy tables in a common format; every API fact cited with a link and
   unit-tested.
3. **Downloads** (`download_*.py`): a documented, reproducible sample
   selection; checks before saving (prices in [0, 1], unique ids, payouts
   consistent across APIs).
4. **Matching** (`match.py`): pairs individual markets by their *economic
   content* (what is measured, which month or meeting, the exact set of
   values for which YES pays, at the published precision), not by title
   similarity. Each candidate's rules are compared, differences flagged and
   scored, and a cautious verdict suggested.
5. **Verification** (`make_review_sheet.py`, `read_review_sheet.py`,
   `apply_independent_review.py`): a blind second review, a conservative
   combination, a reproducible human spot check, and safeguards so typed
   work is never lost.
6. **Final table** (`build_pairs.py`): 155 pairs with the ids, sides and
   time limits needed for the price comparison.
7. **Arbitrage** (`arb.py`): for two simultaneous order-book snapshots of a
   pair, both directions at the best asks, both venues' taker fees from their
   official formulas (each cited and unit-tested), the profit-maximizing size
   from walking down both books, and the return on the capital locked until
   resolution, per year.
8. **Historical gaps** (`gaps.py`, notebook 01): both venues on one hourly
   grid, executable-style gaps after fees, persistence, determinants, price
   discovery, and resolution risk on all 177 settled candidates (section 12).

**Main findings so far.**

* Title similarity cannot identify identical contracts; reading the key
  fields can.
* "Identical" contracts can still pay differently. In the 2025 government
  shutdown the BLS published no October 2025 CPI, and the two venues
  settled October (and November month-on-month) on different values, so one
  matched pair paid on neither leg (or on both). A duplicate Polymarket
  event for the September 2026 meeting closed four months early and
  resolved differently. Resolution risk is therefore a real limit to
  arbitrage, not a theoretical one.
* Smaller rule differences (cancelled FOMC meetings, Fed moves that are not
  multiples of 25 bp, postponed releases) are flagged for every pair.
* Fees eat most of a small gap. Both venues charge takers in proportion to
  P × (1 − P), up to 1.75 cents (Kalshi) plus 1.25 cents (Polymarket, rate
  0.05) per contract at a price of 50 cents. In the order books of
  2026-11-04, 23:37 UTC, one of three open pairs had a positive gap: 2 cents,
  of which fees took 1.978. What was left, $1.82 on $8,264 for 35 days
  (0.23% a year), is far below a safe interest rate of roughly 4%. The other
  two pairs lost about 2 cents per contract after fees.
* Historically (`docs/results.md`), 26% of usable pair-hours show a positive
  gap after fees, typically 0.87 cents (an upper bound); gaps halve in about
  5 hours; Kalshi leads in Fed markets; and 3 of 177 settled pairs paid
  differently, enough to turn a promised $1.25 into $0.25.

**What is not built yet.**

* **The live analysis.** `snapshot.py` collects simultaneous order books of
  all open pairs every 5 minutes until December 2026; applying `arb.py` to
  them will measure executable gaps with real asks and depth.
* **Costs not yet counted:** the interest the locked capital could earn
  elsewhere, deposit and withdrawal costs, minimum order sizes, Polymarket's
  rebate programmes, and the extra cent per order for Kalshi accounts held
  through a broker.

**Quality practices throughout.** Pinned package versions; raw answers
cached; clean tables committed as Parquet under 50 MB; all times in UTC;
fixed random seeds; 358 unit tests (`pytest`, all passing); assumptions and API facts
written into the docstrings with dates and links; an audit trail of every
compared pair.

---

## 14. Twelve likely interview questions about the project

### Q1. What is your research question, and why doesn't a price difference automatically mean arbitrage?

*Model answer.* I ask whether identical event contracts on Kalshi and
Polymarket trade at different prices, and whether those differences could be
exploited after costs. A price gap is only an arbitrage if four conditions
hold. (1) The contracts really are identical, including in edge cases; my
data show they sometimes are not. (2) The prices are executable: a best ask
with enough size, not a last trade or a midpoint. (3) The prices are
simultaneous. (4) The gap survives all costs: trading fees on both venues,
the cost of moving money onto each venue (Polymarket is funded in crypto,
Kalshi in dollars), and the capital tied up until the event
resolves. On top of that, not every trader can legally use both venues. So I
first establish which pairs are truly identical, and only then measure gaps
net of these frictions.

### Q2. How do you build a riskless position from two binary contracts?

*Model answer.* Each contract pays $1 or $0. If Polymarket's YES wins exactly
when Kalshi's YES wins, then Kalshi YES plus Polymarket NO pays exactly $1 in
every state of the world: one leg wins, the other loses. If the outcomes are
complements (Polymarket "0.1% or less", Kalshi "more than 0.1%"), the hedge
is YES on both. The cost is the sum of the two ask prices. If ask₁ + ask₂ +
fees < $1, the difference is a riskless profit, earned at resolution. If it
is above $1, the opposite pair of legs may be cheap instead, so I check both
directions. The column `poly_yes_equals` in `pairs.parquet` tells me which
legs to combine. To trade more than the best ask offers, I buy the same
number of contracts on both venues, walking down both order books, and stop
at the first contract that would lose money after fees.

### Q3. How did you make sure two markets are about the same event?

*Model answer.* I read each market's key fields with explicit text rules:
the quantity measured (headline CPI month-on-month, the Fed's upper bound
change, and so on), the month or meeting, and the exact set of values for
which YES pays, counted in whole steps of the published precision (tenths of
a percent for CPI, 25 bp for the Fed). A pair is a candidate only if these
sets are identical or exact complements. Then the rules texts are compared
for edge cases and every difference is flagged. The candidates went through
a blind second review and a conservative combination of the two verdicts,
plus a random human spot check (I agreed with all 10 pairs drawn). Out of
16,713 compared pairs, 201 were candidates and 155 were verified as the
same. The settled results back this up: of 177 candidates settled on both
venues, 174 paid the same.

### Q4. Tell me about a case where "identical" contracts paid differently.

*Model answer.* The 2025 US government shutdown. The BLS could not collect
October 2025 prices and never published an October CPI. Both venues' rules
assumed the figure would be published; when it was not, each fell back on
its own procedure. Polymarket resolved October using the September figures;
Kalshi settled at different values. For the pair Kalshi "CPI year-on-year
above 3.1%" against Polymarket "≥ 3.2%", identical on paper, Kalshi's YES
paid and Polymarket's did not. A hedge meant to be riskless lost on both
legs, and its mirror image paid twice. A second case is a duplicate
Polymarket event for the September 2026 Fed meeting that closed four months
early and resolved "no change", while the Fed hiked. The lesson for the
thesis is that resolution risk is a real limit to arbitrage: part of an
observed price gap may be compensation for it.

### Q5. What data do you have, and what are its limitations?

*Model answer.* For each venue: a market table with the full rules, hourly
price history, and one order-book snapshot with full depth for the markets
open at download time. Kalshi's history is hourly candles with the best bid,
best ask and trade prices, but only for hours in which something changed.
Polymarket's history is one price per bucket, not a bid and an ask: 3-hour
points over the whole life (only 12-hourly before about spring 2026) and
hourly points only for the last 30 days. The main limitations are that
Polymarket's history is not an executable quote, that the two series are not
sampled at the same moments, and that full order-book depth exists only in
snapshots. The two download scripts' snapshots are about 3.5 hours apart, so
they cannot be combined. For the arbitrage calculation I therefore took
separate snapshots of both venues less than a second apart, so far for three
open pairs. So historical gaps measured from the price histories are an upper
bound on what could have been traded, and the depth analysis rests on
simultaneous snapshots.

### Q6. When are the two venues' prices comparable in time?

*Model answer.* Only while both are trading and before any information is
released. Kalshi closes its markets a minute before the CPI release or a few
minutes before the Fed statement. Polymarket's markets often keep trading
until they are resolved, hours later, and its "end date" field is sometimes
wrong (in one case a month off). After the announcement, Polymarket's price
reflects the known result and Kalshi's is frozen, so a "gap" then is not an
arbitrage. I therefore compare prices only before Kalshi's close time, which
is stored for every pair, and I align observations on a common time grid
without using any future information.

### Q7. How do fees enter your analysis?

*Model answer.* An arbitrageur buys offers that are already in the book, so
I charge the **taker fee on both legs**, each from the venue's official
formula:

* **Kalshi:** fee = round up(M × 0.07 × C × P × (1 − P)), from Kalshi's fee
  schedule (version effective 7 July 2026), with C contracts at price P. The
  multiplier M is 1 for all my series; I checked that with Kalshi's public
  API. Cost plus fee is rounded up to a hundredth of a cent, once per order.
* **Polymarket:** fee = C × feeRate × (p × (1 − p))^exponent, from
  Polymarket's fee documentation, using each market's own fee fields, as
  Polymarket requires. It is rounded to 5 decimals, and makers pay nothing.
  Fees on Fed and CPI markets only started on 30 March 2026: of my 155
  paired Polymarket markets, 86 have no fee schedule, 60 have rate 0.05, 4
  have 0.03 and 5 have 0, all with exponent 1.

Each formula cites its source in the code and is unit-tested against the
venue's own examples: Kalshi's table (100 contracts at 55 cents → $1.74) and
Polymarket's Economics table (100 shares at 50 cents → $1.25).

Three points matter economically. First, both fees are proportional to
P × (1 − P), so they are largest at 50 cents, up to 1.75 + 1.25 = 3 cents
for a hedged pair whose legs are both near 50 cents, and they fall towards
zero near 0 or 1. The gap needed for an arbitrage therefore depends on the
price level, so I compare each gap with its own fees, not with one fixed
threshold. Second, fees are large compared with the gaps I see. In my worked
example the gap was 2 cents and the fees took 1.978 cents (1.12 on Kalshi,
0.858 on Polymarket), leaving $1.82 on $8,264 for 35 days, 0.23% a year. The
other two open pairs went from gaps of 0 and −1 cent to about −2 cents after
fees. Third, fees change over time: before 30 March 2026 Polymarket charged
nothing on these markets, so my fee function takes the trade time as an
input, which matters for the historical comparison.

Two things I state openly. Kalshi's fee schedule could not be downloaded in
my environment, so I read the PDF by hand and recorded its version. And the
schedule's rounding rule and its own example table differ by up to a cent
per order; I use the rule, keep the table's version as an option, and test
both. Not yet included: deposit and withdrawal costs, and the interest the
locked capital could earn elsewhere, which I treat as the required return.

### Q8. How do you make your results reproducible?

*Model answer.* Every package version is pinned. Raw API answers are cached
during a session, and the cleaned tables are committed as Parquet files, so
anyone can rerun the analysis on exactly my data. All times are in UTC. The
sample selection is rule-based and printed, including what was excluded.
Random draws use fixed seeds. Every assumption about the APIs is written in
the code with its documentation link and the date it was checked. There are
358 unit tests, many on real API answers and real rules texts. The matching
keeps an audit trail of all 16,713 compared pairs with the reason each was
accepted or rejected, and the verification records how each verdict was
made. The order books behind the arbitrage example are saved too
(`arb_examples.parquet`), so `python src/arb.py --explain` reprints every
step of the calculation without downloading anything.

### Q9. Why only Fed and CPI markets?

*Model answer.* Because they make exact matching possible. They are
scheduled in advance, settled on official statistics (the Fed's statement
and the BLS release), listed on both venues for every meeting or month, and
structured as families of numeric thresholds, so the payoff of each market
can be written as an interval and compared exactly. They also recur, which
gives a panel: 21 Fed meetings and about two years of CPI months. The
trade-off is external validity. Sports or election markets may behave
differently, and their resolution criteria are harder to match exactly.
Narrow but clean was the better choice for measuring *true* arbitrage.

### Q10. If you find persistent price gaps, how would you explain them economically?

*Model answer.* Through the limits-to-arbitrage literature (Shleifer and
Vishny, 1997): mispricing can persist when exploiting it is costly or risky.
Here the candidates are: (1) trading fees, which depend on the price and,
in my worked example, took 1.978 cents of a 2-cent gap;
(2) capital lock-up, since the money is tied up until resolution, sometimes
for months, so a small gap may earn less than a risk-free rate;
(3) resolution risk, since contracts identical on paper can pay differently,
as in the 2025 shutdown, so part of the gap is a risk premium; (4) market
segmentation, since the venues serve different clienteles with different
access rules and funding methods (dollars against crypto), and few traders
are active on both; (5) limited depth, since the gap may exist only for a
few contracts at the best price. A useful test is whether gaps shrink as
resolution approaches (lock-up costs fall) and whether they are larger in
thinly traded markets.

### Q11. Your verified pairs were chosen with hindsight. Doesn't that bias your results?

*Model answer.* Yes, and I say so in the paper. The matching review used the
realised settlements: a pair whose two markets paid differently was rejected
as "different". A trader in real time could not have done that, so my 155
verified pairs are the pairs that *turned out* to be identical. It is a form
of survivorship bias. The direction of the bias is clear: the gap statistics
of parts 1 to 6 describe positions that, by construction, never suffered a
divergence, so they **understate the risk** a trader faced. They do not
overstate how often gaps appear, but they make those gaps look safer than
they were. I correct for it in part 7 by putting the rejected pairs back: I
take all 177 candidate pairs settled on both venues, whatever their verdict,
and ask what a trader who entered every positive gap would have earned. That
is the honest real-time experiment. The three divergent pairs all showed a
positive gap at the end, so they would have been traded, and they turn a
promised $1.25 into $0.25. One more subtlety: the trade data cover only the
verified pairs, so part 7 uses Polymarket's own price series for all 177
pairs, which I checked agrees with the trade prices to about half a cent.

### Q12. How large is resolution risk, and how would you price it?

*Model answer.* In my sample 3 of 177 settled pairs paid differently, 1.7%,
with an exact 95% confidence interval of 0.4% to 4.9%, because three events
are very few. They come from two episodes: the October 2025 government
shutdown, when the BLS published no October CPI and each venue fell back on
its own rule (Polymarket used September's figures, Kalshi settled on other
values), and a duplicate Polymarket event for the September 2026 Fed meeting
that closed four months early and resolved "no change" while the Fed hiked.
When it hits, the hedge becomes a bet: one direction pays $2, the other $0,
and the trader holds whichever looked cheaper. A simple way to price it is a
break-even condition. With divergence probability d and loss L, normal
trades must earn d × L / (1 − d). With d = 1.7% and L = $0.76, that is about
1.3 cents per contract, more than the median positive gap of 0.87 cents. So
resolution risk alone can explain why the remaining gaps are not
arbitraged away. Two refinements: the risk is not uniform (it concentrates
in unusual events and rule differences, which my matching flags), and the
gap itself is informative: at the last common moment the October 2025 pair
showed a 44-cent gap, because the market was already pricing the different
fallback rules. A large gap in an "identical" pair is more likely a warning
than an opportunity.
