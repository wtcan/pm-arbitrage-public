"""
download_poly_trades.py - Download every trade of the Polymarket markets in
data/processed/pairs.parquet and aggregate them into 1-hour bars.

How to run it (from the project folder):
    python src/download_poly_trades.py

What it does:
  1. Reads the Polymarket markets of the verified pairs (pairs.parquet), and
     from poly_markets.parquet when each market opened and closed and the
     volume Polymarket reports for it.
  2. Downloads all trades of each market from Polymarket's public Data API
     (GET /v2/trades, see polymarket.get_trades). The trader identity fields
     (wallet, name, pseudonym, bio, profile pictures, transaction hash) are
     dropped from each page the moment it arrives, before anything is saved.
  3. Labels each trade "yes" or "no" from its token id, and checks the data
     (known tokens, prices between 0 and 1, positive sizes).
  4. Saves three tables:
       data/raw/poly_trades.parquet                one row per trade
                                                   (data/raw/ is NOT committed)
       data/processed/poly_trade_bars.parquet      one row per market, outcome
                                                   and hour with trades
       data/processed/poly_trade_coverage.parquet  one row per market: first
                                                   and last trade, gaps
  5. Prints a summary, any problems found, and example rows.

Every page of trades is cached in data/raw/polymarket/, so if the script
stops halfway, running it again in the same session continues where it
stopped instead of downloading everything again.
"""

import sys
import time

import pandas as pd

import polymarket

PAIRS_FILE = polymarket.PROCESSED_FOLDER / "pairs.parquet"
MARKETS_FILE = polymarket.PROCESSED_FOLDER / "poly_markets.parquet"
TRADES_FILE = polymarket.PROJECT_ROOT / "data" / "raw" / "poly_trades.parquet"  # not committed

LONG_GAP_HOURS = 24  # coverage: count the gaps between trades longer than this

# Columns of the trade-level file (no trader identity fields).
TRADE_FILE_COLUMNS = ["market_id", "condition_id", "token_id", "outcome", "side", "price", "size",
                      "timestamp"]

COVERAGE_COLUMNS = [
    "market_id", "topic", "month", "question", "start_date", "closed_time",
    "first_trade", "last_trade", "n_trades", "n_yes_trades", "n_no_trades",
    "hours_with_trades", "hours_in_span", "share_hours_with_trades",
    "longest_gap_hours", "longest_gap_from", f"gaps_over_{LONG_GAP_HOURS}h",
    "volume", "notional", "gamma_volume", "volume_vs_gamma",
]
COVERAGE_TIME_COLUMNS = ["start_date", "closed_time", "first_trade", "last_trade", "longest_gap_from"]


def markets_to_download():
    """One row per Polymarket market of the pairs: ids, token ids, dates and Gamma's volume."""
    pairs = pd.read_parquet(PAIRS_FILE)
    markets = pairs.rename(columns={
        "poly_market_id": "market_id", "poly_condition_id": "condition_id",
        "poly_yes_token_id": "yes_token_id", "poly_no_token_id": "no_token_id",
        "poly_question": "question",
    })
    markets = markets[["market_id", "condition_id", "yes_token_id", "no_token_id", "topic", "month",
                       "question"]].drop_duplicates("market_id", ignore_index=True)
    info = pd.read_parquet(MARKETS_FILE, columns=["market_id", "start_date", "closed_time", "closed",
                                                  "volume"])
    info = info.rename(columns={"volume": "gamma_volume"})
    return markets.merge(info, on="market_id", how="left", validate="one_to_one")


def label_outcomes(trades, yes_token_id, no_token_id):
    """Set outcome to "yes" or "no" from each trade's token id (empty if the token is neither).

    The token id is the safest label: it is the same id as in pairs.parquet.
    Returns (table, number of trades whose token is unknown, number of trades
    whose label disagrees with the API's own "Yes"/"No" label).
    """
    trades = trades.copy()
    api_label = trades["outcome"].str.lower()
    trades["outcome"] = trades["token_id"].map({yes_token_id: "yes", no_token_id: "no"})
    unknown = int(trades["outcome"].isna().sum())
    disagree = int((trades["outcome"].notna() & (trades["outcome"] != api_label)).sum())
    return trades, unknown, disagree


def download_all(markets):
    """Step 2-3: the trades of every market in one table, plus a list of problems found."""
    tables, problems = [], []
    for number, market in enumerate(markets.itertuples(index=False), start=1):
        try:
            rows = polymarket.get_trades(market.condition_id)
        except polymarket.PolymarketError as error:  # e.g. still failing after all retries
            problems.append(f"market {market.market_id}: download failed: {error}")
            continue
        table = polymarket.trades_table(rows)
        table, unknown, disagree = label_outcomes(table, market.yes_token_id, market.no_token_id)
        if unknown:
            problems.append(f"market {market.market_id}: {unknown} trades of an unknown token (left out of the bars)")
        if disagree:
            problems.append(f"market {market.market_id}: {disagree} trades where the API's Yes/No label "
                            f"disagrees with the token id")
        table.insert(0, "market_id", market.market_id)
        tables.append(table[TRADE_FILE_COLUMNS])
        print(f"   {number:>3}/{len(markets)}  market {market.market_id:<8} {len(table):>9,} trades"
              f"   ({polymarket.STATS['requests']:,} requests, {polymarket.STATS['from_cache']:,} from cache)")
    if not tables:
        return pd.DataFrame(columns=TRADE_FILE_COLUMNS), problems
    return pd.concat(tables, ignore_index=True), problems


def check_trades(trades):
    """Problems in the trade data itself: prices outside 0..1, sizes that are not positive."""
    problems = []
    bad_price = ~trades["price"].between(0, 1)
    bad_size = ~(trades["size"] > 0)
    if bad_price.any():
        problems.append(f"{int(bad_price.sum())} trades with a price outside 0..1")
    if bad_size.any():
        problems.append(f"{int(bad_size.sum())} trades with a size that is not positive")
    return problems


def coverage_table(trades, markets, long_gap_hours=LONG_GAP_HOURS):
    """One row per market: when its trades start and end, how many there are, and the gaps.

    A gap is the time between two consecutive trades of a market, YES and NO
    trades together (both tell us about the same market).
    Columns:
        market_id, topic, month, question   which market (from pairs.parquet)
        start_date, closed_time       when Polymarket opened and closed the market
                                      (from poly_markets.parquet), for comparison
        first_trade, last_trade       time of the first and the last trade (UTC)
        n_trades, n_yes_trades, n_no_trades
        hours_with_trades             1-hour bars with at least one trade (YES or NO)
        hours_in_span                 hours from the hour of the first trade to the
                                      hour of the last trade, both counted
        share_hours_with_trades       hours_with_trades / hours_in_span
        longest_gap_hours             the longest time without any trade, in hours
        longest_gap_from              time of the trade just before that gap
        gaps_over_24h                 number of gaps longer than 24 hours
        volume                        shares traded: sum of size
        notional                      dollars traded: sum of price x size
        gamma_volume                  the market's volume as Polymarket's Gamma API
                                      reports it (poly_markets.parquet). It counts
                                      SHARES, not dollars: it matches our volume.
        volume_vs_gamma               volume / gamma_volume: about 1 if we have every
                                      trade (above 1 for open markets that traded
                                      after poly_markets.parquet was downloaded)
    A market without any trade has empty times and zero counts.
    """
    by_market = {market_id: group for market_id, group in trades.groupby("market_id")}
    rows = []
    for market in markets.itertuples(index=False):
        row = {"market_id": market.market_id, "topic": market.topic, "month": market.month,
               "question": market.question, "start_date": market.start_date,
               "closed_time": market.closed_time, "gamma_volume": market.gamma_volume}
        group = by_market.get(market.market_id)
        if group is None or group.empty:
            row.update(n_trades=0, n_yes_trades=0, n_no_trades=0, hours_with_trades=0, hours_in_span=0,
                       volume=0.0, notional=0.0, volume_vs_gamma=0.0, **{f"gaps_over_{long_gap_hours}h": 0})
            rows.append(row)
            continue

        times = group["timestamp"].sort_values().reset_index(drop=True)
        hours = times.dt.floor("1h")
        gaps = times.diff().dt.total_seconds() / 3600  # gaps[i] = hours between trade i-1 and trade i
        row["first_trade"], row["last_trade"] = times.iloc[0], times.iloc[-1]
        row["n_trades"] = len(group)
        row["n_yes_trades"] = int((group["outcome"] == "yes").sum())
        row["n_no_trades"] = int((group["outcome"] == "no").sum())
        row["hours_with_trades"] = hours.nunique()
        row["hours_in_span"] = int((hours.iloc[-1] - hours.iloc[0]) / pd.Timedelta("1h")) + 1
        row["share_hours_with_trades"] = row["hours_with_trades"] / row["hours_in_span"]
        if len(times) > 1:
            longest = gaps.idxmax()  # skips the empty first gap
            row["longest_gap_hours"] = gaps[longest]
            row["longest_gap_from"] = times[longest - 1]
        row[f"gaps_over_{long_gap_hours}h"] = int((gaps > long_gap_hours).sum())
        row["volume"] = float(group["size"].sum())
        row["notional"] = float((group["price"] * group["size"]).sum())
        row["volume_vs_gamma"] = row["volume"] / market.gamma_volume  # NaN if Gamma has no volume
        rows.append(row)

    table = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
    for column in COVERAGE_TIME_COLUMNS:
        table[column] = pd.to_datetime(table[column], utc=True)
    for column in ["n_trades", "n_yes_trades", "n_no_trades", "hours_with_trades", "hours_in_span",
                   f"gaps_over_{long_gap_hours}h"]:
        table[column] = table[column].astype("int64")
    return table


def coverage_markdown(coverage):
    """The coverage table as a Markdown table, for docs/poly_trades.md (one line per market)."""
    lines = ["| market | topic | month | first trade | last trade | trades | hours with trades "
             "| longest gap (h) | gap starts | gaps > 24 h |",
             "|---|---|---|---|---|---:|---:|---:|---|---:|"]

    def when(value):
        return "" if pd.isna(value) else value.strftime("%Y-%m-%d %H:%M")

    for row in coverage.sort_values(["topic", "month", "market_id"]).itertuples(index=False):
        share = "" if pd.isna(row.share_hours_with_trades) else f"{row.share_hours_with_trades:.0%}"
        gap = "" if pd.isna(row.longest_gap_hours) else f"{row.longest_gap_hours:,.1f}"
        lines.append(f"| {row.market_id} | {row.topic} | {row.month} | {when(row.first_trade)} "
                     f"| {when(row.last_trade)} | {row.n_trades:,} | {share} | {gap} "
                     f"| {when(row.longest_gap_from)} | {getattr(row, f'gaps_over_{LONG_GAP_HOURS}h')} |")
    return "\n".join(lines)


def main():
    started = time.time()
    markets = markets_to_download()
    print(f"1. {len(markets)} Polymarket markets in {PAIRS_FILE.name}: "
          f"{int(markets['closed'].sum())} closed, {int((~markets['closed']).sum())} still open")

    print("\n2. Downloading their trades (trader identity fields are dropped on arrival) ...")
    try:
        trades, problems = download_all(markets)
    except polymarket.NetworkBlockedError as error:
        print(f"\nSTOPPED: {error}")
        sys.exit(1)
    problems += check_trades(trades)

    print("\n3. Saving ...")
    TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
    trades.to_parquet(TRADES_FILE, index=False, compression="zstd")
    print(f"   {TRADES_FILE.relative_to(polymarket.PROJECT_ROOT)}: {len(trades):,} trades "
          f"({TRADES_FILE.stat().st_size / 1e6:.1f} MB, not committed)")
    bars = polymarket.trade_bars(trades)
    path = polymarket.save_table(bars, "poly_trade_bars")
    print(f"   {path.relative_to(polymarket.PROJECT_ROOT)}: {len(bars):,} hourly bars "
          f"({path.stat().st_size / 1e6:.1f} MB)")
    coverage = coverage_table(trades, markets)
    path = polymarket.save_table(coverage, "poly_trade_coverage")
    print(f"   {path.relative_to(polymarket.PROJECT_ROOT)}: {len(coverage)} markets")

    print("\n4. Summary")
    traded = coverage[coverage["n_trades"] > 0]
    print(f"   markets with trades: {len(traded)} of {len(coverage)}")
    if len(traded):
        print(f"   trades per market: median {traded['n_trades'].median():,.0f}, "
              f"min {traded['n_trades'].min():,}, max {traded['n_trades'].max():,}")
        print(f"   first trade overall {traded['first_trade'].min()}, last {traded['last_trade'].max()}")
        print(f"   share of hours with trades (first to last trade): median "
              f"{traded['share_hours_with_trades'].median():.0%}")
        print(f"   longest gap: median {traded['longest_gap_hours'].median():.1f} h, "
              f"max {traded['longest_gap_hours'].max():.1f} h")
        ratio = traded["volume_vs_gamma"]
        print(f"   our volume (shares) / Gamma's volume: median {ratio.median():.3f}, "
              f"min {ratio.min():.3f}, max {ratio.max():.3f}; within 0.5% for "
              f"{int(((ratio - 1).abs() <= 0.005).sum())} of {int(ratio.notna().sum())} markets")
    print(f"   problems found: {len(problems)}")
    for problem in problems:
        print(f"      {problem}")
    print(f"\n   Example bars:\n{bars.head(5).to_string()}")
    print(f"\n   Done in {(time.time() - started) / 60:.1f} minutes; {polymarket.STATS}")


if __name__ == "__main__":
    main()
