"""
download_polymarket.py - Download Polymarket's Fed rate-decision and US CPI
markets of the last 24 months, and save them as tables in data/processed/.

How to run it (from the project folder):
    python src/download_polymarket.py

What it does:
  1. Collects candidate events from Polymarket tags and series about the Fed
     and inflation (SOURCE_TAGS, SOURCE_SERIES), sorts them with the title
     rules below (FED_RULES, CPI_RULES) and prints what it keeps and what it
     leaves out.
  2. Keeps every market of the chosen events that ends on or after the start
     date (24 months ago), including the markets that are still open.
  3. Takes one snapshot of the YES and NO order books of every open market.
  4. Downloads price history for the YES and NO token of every market:
     3-hour points over the market's whole life, plus hourly points for the
     last 30 days of the markets that are still open.
  5. Saves four tables in data/processed/ and prints 5 example rows of each:
       poly_markets.parquet           one row per market
       poly_orderbooks.parquet        one row per open market: best bids/asks
       poly_orderbook_levels.parquet  one row per price level (full depth)
       poly_prices.parquet            one row per market, token and price point
                                      (column bucket_seconds: 10800 = 3-hour
                                      series, 3600 = hourly series)

Every answer is cached in data/raw/polymarket/, so if the script stops
halfway, running it again in the same session skips what it already has
(order books are always downloaded fresh).
"""

import re
import sys
import time

import pandas as pd

import polymarket

# Where candidate events come from. Tags are Polymarket's topic labels; a
# series is a recurring topic (one event per Fed meeting or CPI release).
SOURCE_TAGS = ["fed-rates", "fed", "cpi", "us-cpi", "inflation"]
SOURCE_SERIES = {
    35: "fomc (Fed decision at each FOMC meeting)",
    10008: "us-monthly-inflation (US CPI, change from the previous month)",
    10007: "us-annual-inflation (US CPI, change from a year earlier)",
    5: "cpi (older US CPI events)",
    11905: "core-cpi (US core CPI, change from a year earlier)",
    12054: "core-cpi-mom (US core CPI, change from the previous month)",
}

# Which events we keep, by title. The rules are checked in order and the first
# match decides the event's market_type. Titles matching none are left out.
# Not about the rate decision itself, even if the title mentions it: dissent
# counts, and "derivative" markets that bet on Polymarket's own odds.
EXCLUDE_RULE = r"dissent|derivative|odds|favou?red|flips"
FED_RULES = [
    ("decision", r"^fed (decision|interest rates)\b"),            # one meeting: "Fed decision in December?"
    ("rate_level", r"^what will (the )?fed rate (be|hit)\b"),     # "What will the Fed rate be at the end of 2026?"
    ("cuts_count", r"^how many fed rate cuts\b"),                 # "How many Fed rate cuts in 2026?"
    ("other", r"^(another )?fed (emergency )?rate (cut|hike)\b"),  # "Fed rate cut by...?", "Fed rate hike in 2026?"
    ("other", r"^fed decisions \("),                              # 3-meeting combos: "Fed decisions (Sep-Dec)"
    ("other", r"^what will be the next fed rate change\b"),
]
# US only: Polymarket names other countries in the title ("Inflation China",
# "Argentina Monthly Inflation"), so these patterns do not match them.
CPI_RULES = [
    ("cpi_mom", r"^[a-z]+ inflation( us)? - monthly\b"),          # "October Inflation US - Monthly"
    ("cpi_yoy", r"^[a-z]+ inflation( us)? - annual\b"),           # also "(Higher Brackets)", "(Lower Brackets)"
    ("cpi_yoy", r"^annual inflation above\b"),                    # "Annual inflation above 2.4% in October?"
    ("core_cpi_mom", r"^core cpi\b.*\bmom\b"),                    # "Core CPI MoM - July 2026"
    ("core_cpi_yoy", r"^core cpi\b.*\byoy\b"),                    # "Core CPI YoY - July 2026"
    ("other", r"^how high will inflation get in \d{4}\b"),        # highest CPI inflation of a year
]
MONTHS_BACK = 24  # keep markets that end on or after this many months ago

MONTH_NAMES = ("January|February|March|April|May|June|July|August|September|October|"
               "November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")


def classify_event(title):
    """(topic, market_type) of an event title, or None if the event is left out.

    topic is "fed" or "cpi". market_type is decision / rate_level /
    cuts_count / other for the Fed, and cpi_mom / cpi_yoy / core_cpi_mom /
    core_cpi_yoy / other for CPI. Upper or lower case does not matter.
    """
    title = " ".join((title or "").split())  # tidy spaces
    if re.search(EXCLUDE_RULE, title, re.IGNORECASE):
        return None
    for topic, rules in [("fed", FED_RULES), ("cpi", CPI_RULES)]:
        for market_type, pattern in rules:
            if re.search(pattern, title, re.IGNORECASE):
                return topic, market_type
    return None


def title_pattern(title):
    """A title with month names and numbers replaced, to group similar titles in the printout."""
    title = re.sub(rf"\b({MONTH_NAMES})\b", "<month>", title)
    return re.sub(r"\d+(\.\d+)?", "#", title)


def collect_candidates():
    """Step 1a: every event carrying one of the source tags or in one of the source series."""
    events = {}
    for tag in SOURCE_TAGS:
        found = polymarket.list_events(tag_slug=tag)
        print(f"   tag {tag:<10} {len(found):>5} events")
        for event in found:
            events[str(event["id"])] = event
    for series_id, meaning in SOURCE_SERIES.items():
        found = polymarket.list_events(series_id=series_id)
        print(f"   series {series_id:<6} {len(found):>5} events  {meaning}")
        for event in found:
            events[str(event["id"])] = event
    return events


def choose_events(events, start):
    """Step 1b: keep the Fed rate-decision and US CPI events that end on or after `start`."""
    chosen, labels, left_out = [], {}, {}
    for event in events.values():
        end = polymarket.to_utc(event.get("endDate"))
        if pd.notna(end) and end < start:
            continue  # ended before the start date
        label = classify_event(event.get("title"))
        if label is None:
            pattern = title_pattern(event.get("title") or "")
            left_out[pattern] = left_out.get(pattern, 0) + 1
            continue
        chosen.append(event)
        labels[str(event["id"])] = label
    counts = pd.Series([f"{topic} / {kind}" for topic, kind in labels.values()]).value_counts().sort_index()
    print(f"\n   Kept {len(chosen)} events:")
    for name, count in counts.items():
        print(f"      {name:<22} {count:>4} events")
    print(f"\n   Left out {sum(left_out.values())} events in the date range, e.g. (# = a number):")
    for pattern, count in sorted(left_out.items(), key=lambda item: -item[1])[:40]:
        print(f"      {count:>4} x {pattern[:95]}")
    return chosen, labels


def download_prices(markets):
    """Step 4: price history of the YES and NO token of every market.

    3-hour points over each market's whole life; for markets still open also
    hourly points for the last 30 days. Returns (table, list of failures).
    """
    tables, failed = [], []
    t0 = time.monotonic()
    for number, market in enumerate(markets.itertuples(), start=1):
        resolved = pd.notna(market.resolved_outcome)
        series = [(polymarket.FULL_LIFE_BUCKET_SECONDS, "max")]
        if not market.closed:
            series.append((polymarket.RECENT_BUCKET_SECONDS, "1m"))  # "1m" = the last 30 days
        for outcome, token in [("yes", market.yes_token_id), ("no", market.no_token_id)]:
            for bucket_seconds, interval in series:
                try:
                    points = polymarket.get_price_history(token, bucket_seconds, interval)
                except polymarket.PolymarketError as error:
                    print(f"   WARNING: no prices for {market.market_id} {outcome} ({bucket_seconds} s): {error}")
                    failed.append((market.market_id, outcome, bucket_seconds))
                    continue
                table = polymarket.parse_price_history(points, market.market_id, outcome, bucket_seconds, resolved)
                if len(table):
                    tables.append(table)
        if number % 50 == 0 or number == len(markets):
            minutes = (time.monotonic() - t0) / 60
            print(f"   {number}/{len(markets)} markets done after {minutes:.1f} min "
                  f"({sum(len(t) for t in tables):,} price points; requests {polymarket.STATS['requests']}, "
                  f"retries {polymarket.STATS['retries']}, from cache {polymarket.STATS['from_cache']})")
    prices = pd.concat(tables, ignore_index=True) if tables else pd.DataFrame(columns=polymarket.PRICE_COLUMNS)
    return prices, failed


def check_prices(name, table, columns):
    """Project rule: prices are dollars between 0 and 1. Stop if one is not."""
    for column in columns:
        values = table[column].dropna()
        if not values.between(0, 1).all():
            sys.exit(f"ERROR: {name}.{column} has prices outside 0-1: {values[~values.between(0, 1)].head()}")


def check_settlements(markets, prices):
    """The last point of a resolved market's series must equal the token's payout."""
    settled = prices[prices["point_type"] == "settlement"].merge(
        markets[["market_id", "yes_payout"]], on="market_id")
    expected = settled["yes_payout"].where(settled["outcome"] == "yes", 1 - settled["yes_payout"])
    wrong = settled[(settled["price"] - expected).abs() > 1e-9]
    print(f"   {len(settled):,} settlement points; {len(wrong)} differ from the payout in poly_markets")
    if len(wrong):
        print(wrong.head(10).to_string())


def main():
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_colwidth", 40)

    now = pd.Timestamp.now(tz="UTC")
    start = now.normalize() - pd.DateOffset(months=MONTHS_BACK)
    print(f"Polymarket download started {now:%Y-%m-%d %H:%M} UTC; keeping markets that end on or "
          f"after {start:%Y-%m-%d}")

    print("\nSTEP 1: Fed and inflation events from Polymarket tags and series")
    events, labels = choose_events(collect_candidates(), start)

    print(f"\nSTEP 2: the markets of these events that end on or after {start:%Y-%m-%d}")
    all_markets = polymarket.markets_table(events, labels)
    all_markets["closed"] = all_markets["closed"].fillna(False)  # unknown: treat as open
    markets = all_markets[all_markets["end_date"].isna() | (all_markets["end_date"] >= start)]
    no_tokens = markets[markets["yes_token_id"].isna() | markets["no_token_id"].isna()]
    if len(no_tokens):
        print(f"   WARNING: {len(no_tokens)} markets have no token ids yet and are left out: "
              f"{no_tokens['market_id'].tolist()}")
    markets = markets.drop(no_tokens.index).sort_values(["topic", "end_date", "event_id", "market_id"],
                                                        ignore_index=True)
    summary = markets.groupby(["topic", "market_type"]).agg(
        events=("event_id", "nunique"), markets=("market_id", "size"),
        open_now=("closed", lambda closed: int((~closed).sum())))
    print(summary.to_string())
    print(f"   total: {len(markets)} markets, {int((~markets['closed']).sum())} open now; "
          f"protocol versions: {markets['protocol_version'].value_counts().to_dict()}")

    open_markets = markets[~markets["closed"]]
    print(f"\nSTEP 3: order-book snapshot (YES and NO book) of the {len(open_markets)} open markets")
    orderbooks, orderbook_levels = polymarket.orderbook_snapshot(open_markets)

    print(f"\nSTEP 4: price history of {len(markets)} markets (YES and NO token): 3-hour points over "
          f"each market's life, plus hourly points for the last 30 days of the {len(open_markets)} open markets")
    prices, failed = download_prices(markets)

    # Checks before saving.
    check_prices("poly_orderbooks", orderbooks, ["best_yes_bid", "best_yes_ask", "best_no_bid", "best_no_ask"])
    check_prices("poly_orderbook_levels", orderbook_levels, ["price"])
    check_prices("poly_prices", prices, ["price"])
    assert markets["market_id"].is_unique
    assert not prices.duplicated(["market_id", "outcome", "bucket_seconds", "timestamp", "point_type"]).any()
    check_settlements(markets, prices)

    print("\nSTEP 5: saving and showing 5 example rows of each table")
    for name, table in [("poly_markets", markets), ("poly_orderbooks", orderbooks),
                        ("poly_orderbook_levels", orderbook_levels), ("poly_prices", prices)]:
        path = polymarket.save_table(table, name)
        size_mb = path.stat().st_size / 1_000_000
        print(f"\n--- {path.relative_to(polymarket.PROJECT_ROOT)}: {len(table):,} rows, {size_mb:.1f} MB")
        print(table.sample(min(5, len(table)), random_state=1).to_string() if len(table) else "(empty)")

    print(f"\nDone in {(pd.Timestamp.now(tz='UTC') - now).total_seconds() / 60:.0f} min. "
          f"Polymarket requests: {polymarket.STATS['requests']}, retries: {polymarket.STATS['retries']}, "
          f"answers from cache: {polymarket.STATS['from_cache']}")
    if failed:
        print(f"WARNING: prices missing for {len(failed)} series: {failed}. "
              f"Run the script again: it re-uses everything already downloaded.")


if __name__ == "__main__":
    main()
