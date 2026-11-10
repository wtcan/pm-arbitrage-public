"""
download_kalshi.py - Download Kalshi's Fed-decision and US CPI markets of the
last 24 months, and save them as tables in data/processed/.

How to run it (from the project folder):
    python src/download_kalshi.py

What it does:
  1. Searches Kalshi's list of series for Fed and CPI series and prints the
     candidates, then checks the chosen ones (SERIES below).
  2. Downloads every market of those series that closes on or after the start
     date (24 months ago), including the markets that are still open.
  3. Takes one snapshot of the order book of every market that is open now.
  4. Downloads hourly candlesticks over each market's whole life.
  5. Saves four tables in data/processed/ and prints 5 example rows of each:
       kalshi_markets.parquet           one row per market
       kalshi_orderbooks.parquet        one row per open market: best bids/asks
       kalshi_orderbook_levels.parquet  one row per price level (full depth)
       kalshi_candles.parquet           one row per market and hour with activity
                                        (Kalshi skips hours in which nothing changed)

Kalshi often answers "too many requests", so this takes a while: the script
waits and tries again. Every answer is cached in data/raw/kalshi/, so if the
script stops halfway, running it again in the same session skips everything
it already downloaded.
"""

import sys
import time

import pandas as pd

import kalshi

# The series to download, chosen from the search results of step 1. Each "KX"
# series holds its whole history: Kalshi merged the old tickers without "KX"
# (e.g. FED-21DEC) into them, and the old series (FED, CPI, ...) are now empty.
SERIES = {
    "KXFEDDECISION": "Fed decision at each FOMC meeting: hike / hold / cut, by size",
    "KXFED": "Upper bound of the Fed funds target range after each meeting",
    "KXCPI": "US CPI inflation, change from the previous month",
    "KXCPIYOY": "US CPI inflation, change from a year earlier",
}
MONTHS_BACK = 24            # keep markets that close on or after this many months ago
CANDLE_PERIOD_MINUTES = 60  # hourly candles (1 = one minute, 1440 = one day)


def show_candidates():
    """Step 1: find Fed and CPI series through the API and print them."""
    print("\nSTEP 1: Fed and CPI series in Kalshi's 'Economics' category")
    for label, words in [("Fed", ["fed", "fomc"]), ("CPI / inflation", ["cpi", "inflation"])]:
        hits = kalshi.search_series(words, category="Economics")
        print(f"\n   {len(hits)} series match {words} ({label}):")
        for row in hits.itertuples():
            print(f"      {row.ticker:<22} {row.frequency:<10} {row.title}")
    print("\n   Chosen:")
    chosen = []
    for ticker, meaning in SERIES.items():
        series = kalshi.get_series(ticker)
        chosen.append(series)
        print(f"      {ticker:<14} title '{series['title']}', category {series['category']}: {meaning}")
    return chosen


def download_markets(start):
    """Step 2: all markets of the chosen series that close on or after `start`."""
    print(f"\nSTEP 2: markets that close on or after {start:%Y-%m-%d}")
    markets, events = [], []
    for ticker in SERIES:
        events += kalshi.list_events(series_ticker=ticker)
        series_markets = kalshi.list_all_markets(ticker)
        recent = [m for m in series_markets if kalshi.to_utc(m["close_time"]) >= start]
        markets += recent
        open_now = sum(m["status"] == "active" for m in recent)
        print(f"   {ticker:<14} {len(series_markets):>5} markets in total, {len(recent):>5} recent "
              f"({open_now} open now)")
    return markets, events


def download_candles(markets, cutoff):
    """Step 4: candlesticks for every market that has opened already."""
    started = [m for m in markets if kalshi.to_utc(m["open_time"]) < pd.Timestamp.now(tz="UTC")]
    print(f"\nSTEP 4: {CANDLE_PERIOD_MINUTES}-minute candles for {len(started)} markets")
    tables, failed = [], []
    t0 = time.monotonic()
    for number, market in enumerate(started, start=1):
        try:
            table = kalshi.get_candles(market, CANDLE_PERIOD_MINUTES, cutoff)
        except kalshi.KalshiError as error:
            print(f"   WARNING: no candles for {market['ticker']}: {error}")
            failed.append(market["ticker"])
            continue
        if len(table):
            tables.append(table)
        if number % 25 == 0 or number == len(started):
            minutes = (time.monotonic() - t0) / 60
            print(f"   {number}/{len(started)} markets done after {minutes:.0f} min "
                  f"({sum(len(t) for t in tables):,} candles; Kalshi requests {kalshi.STATS['requests']}, "
                  f"retries {kalshi.STATS['retries']}, from cache {kalshi.STATS['from_cache']})")
    candles = pd.concat(tables, ignore_index=True) if tables else pd.DataFrame(columns=kalshi.CANDLE_COLUMNS)
    return candles, failed


def check_prices(name, table, columns):
    """Project rule: prices are dollars between 0 and 1. Stop if one is not."""
    for column in columns:
        values = table[column].dropna()
        if not values.between(0, 1).all():
            sys.exit(f"ERROR: {name}.{column} has prices outside 0-1: {values[~values.between(0, 1)].head()}")


def main():
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", 45)

    now = pd.Timestamp.now(tz="UTC")
    start = now.normalize() - pd.DateOffset(months=MONTHS_BACK)
    print(f"Kalshi download started {now:%Y-%m-%d %H:%M} UTC")
    cutoff = kalshi.get_historical_cutoff()
    print(f"Historical cutoff: markets that settled before {cutoff:%Y-%m-%d %H:%M} UTC are "
          f"read from Kalshi's /historical/... endpoints")

    series = show_candidates()
    markets, events = download_markets(start)
    markets_table = kalshi.markets_table(markets, events, series)

    open_tickers = sorted(m["ticker"] for m in markets if m["status"] == "active")
    print(f"\nSTEP 3: order-book snapshot of the {len(open_tickers)} open markets")
    orderbooks, orderbook_levels = kalshi.orderbook_snapshot(open_tickers)

    candles, failed = download_candles(markets, cutoff)

    # Checks before saving.
    check_prices("kalshi_orderbooks", orderbooks,
                 ["best_yes_bid", "best_yes_ask", "best_no_bid", "best_no_ask"])
    check_prices("kalshi_orderbook_levels", orderbook_levels, ["price"])
    check_prices("kalshi_candles", candles, [c for c in kalshi.CANDLE_COLUMNS
                                             if c.startswith(("yes_bid_", "yes_ask_", "price_"))])
    assert markets_table["ticker"].is_unique
    assert not candles.duplicated(["ticker", "period_end"]).any()

    print("\nSTEP 5: saving and showing 5 example rows of each table")
    for name, table in [("kalshi_markets", markets_table), ("kalshi_orderbooks", orderbooks),
                        ("kalshi_orderbook_levels", orderbook_levels), ("kalshi_candles", candles)]:
        path = kalshi.save_table(table, name)
        size_mb = path.stat().st_size / 1_000_000
        print(f"\n--- {path.relative_to(kalshi.PROJECT_ROOT)}: {len(table):,} rows, {size_mb:.1f} MB")
        print(table.sample(min(5, len(table)), random_state=1).to_string() if len(table) else "(empty)")

    print(f"\nDone in {(pd.Timestamp.now(tz='UTC') - now).total_seconds() / 60:.0f} min. "
          f"Kalshi requests: {kalshi.STATS['requests']}, retries: {kalshi.STATS['retries']}, "
          f"answers from cache: {kalshi.STATS['from_cache']}")
    if failed:
        print(f"WARNING: candles missing for {len(failed)} markets: {failed}. "
              f"Run the script again: it re-uses everything already downloaded.")


if __name__ == "__main__":
    main()
