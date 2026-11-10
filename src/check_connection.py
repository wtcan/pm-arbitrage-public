"""
check_connection.py - Can we reach the Kalshi and Polymarket APIs?

This script calls one public, read-only market-data endpoint on each API we
use (Kalshi, and Polymarket's Gamma, CLOB and Data APIs) and prints whether
it worked. It uses no API keys and no account: these are plain GET requests
that anyone could also open in a web browser.

How to run it (from the project folder):
    python src/check_connection.py

Following the project rules in CLAUDE.md, every request is:
  * throttled - at most 5 requests per second;
  * retried   - if the server answers 429 ("too many requests") or 5xx
                ("server error"), we wait 2, then 4, then 8 seconds and try
                again;
  * saved     - the raw answer goes to data/raw/check_connection/ so you can
                open it and see what the data looks like. (The script always
                asks the live API, though: re-using a saved answer would not
                test the connection.)
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# The public endpoints we test. Each one answers with a list of markets,
# except the Data API, which answers with a list of trades.
KALSHI_MAIN = "https://external-api.kalshi.com/trade-api/v2/markets?limit=1"
KALSHI_FALLBACK = "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com/markets?limit=1"
POLYMARKET_CLOB = "https://clob.polymarket.com/markets"
POLYMARKET_DATA = "https://data-api.polymarket.com/v2/trades?limit=1"

# Where the raw answers are saved. Git ignores data/raw/, so these files only
# live for the current cloud session.
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder above src/
RAW_FOLDER = PROJECT_ROOT / "data" / "raw" / "check_connection"

MIN_SECONDS_BETWEEN_REQUESTS = 0.2  # 1 / 0.2 s = at most 5 requests per second
MAX_ATTEMPTS = 4                    # the first try + up to 3 retries
TIMEOUT_SECONDS = 30                # stop waiting for an answer after 30 s

# Tell the servers what is calling them (polite, and some servers require it).
HEADERS = {"User-Agent": "pm-arbitrage academic research script (read-only)"}


# ---------------------------------------------------------------------------
# Polite requests: throttling and retries
# ---------------------------------------------------------------------------

last_request_time = 0.0  # clock time at which the previous request was sent


def wait_for_turn():
    """Pause just long enough to stay under 5 requests per second."""
    global last_request_time  # we update the variable defined just above
    seconds_since_last = time.monotonic() - last_request_time
    if seconds_since_last < MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - seconds_since_last)
    last_request_time = time.monotonic()


def polite_get(url):
    """Send a GET request with throttling, and retry on 429 and 5xx answers.

    Returns the server's answer (a `response` object), which can still be an
    error after the last try. If the request cannot be sent at all (blocked
    domain, no internet, timeout), `requests` raises an exception instead,
    and the caller deals with it.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait_for_turn()
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)

        # 429 = "too many requests", 5xx = server error: worth trying again.
        temporary_error = response.status_code == 429 or response.status_code >= 500
        if not temporary_error or attempt == MAX_ATTEMPTS:
            return response

        pause = 2 ** attempt  # 2, 4, 8 seconds: "exponential backoff"
        print(f"   HTTP {response.status_code}: trying again in {pause} s ...")
        time.sleep(pause)


# ---------------------------------------------------------------------------
# Reading the answers
# ---------------------------------------------------------------------------

def find_market_list(data):
    """Return the list of markets (or trades) inside an API answer.

    Each API packs its list differently:
      Kalshi:           {"markets": [...], "cursor": "..."}
      Polymarket Gamma: [...]   (the answer is the list itself)
      Polymarket CLOB:  {"data": [...], "next_cursor": "...", ...}
      Polymarket Data:  {"data": [...], "pagination": {...}}
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ["markets", "data"]:
            if isinstance(data.get(key), list):
                return data[key]
    return []  # unknown format: no markets found


def describe_market(market):
    """Make a short, readable label for one market."""
    # Kalshi calls the market's text "title", Polymarket calls it "question".
    title = market.get("title") or market.get("question") or "(no title)"
    ticker = market.get("ticker")  # only Kalshi markets have a ticker
    if ticker:
        title = f"[{ticker}] {title}"
    return title[:100]  # cut very long titles


# ---------------------------------------------------------------------------
# One check = one endpoint
# ---------------------------------------------------------------------------

def check_endpoint(name, url, save_as, what="market"):
    """Call one endpoint and print what happened.

    `what` names the items in the answer ("market", or "trade" for the
    Polymarket Data API). Returns two things: whether it worked (True/False),
    and a one-line summary for the table at the end.
    """
    domain = urlparse(url).netloc  # e.g. "clob.polymarket.com"
    print(f"\n--- {name} ---")
    print(f"   GET {url}")

    try:
        response = polite_get(url)
    except requests.exceptions.ProxyError as error:
        # In Claude Code cloud sessions, all traffic goes through a proxy.
        # If the proxy answers 403, this domain is not on the allowed list.
        if "403" in str(error):
            print("   BLOCKED: the cloud environment's network refused the connection.")
            print(f"   Add this domain to your environment's allowed domains: {domain}")
            return False, f"BLOCKED by the network: allow {domain}"
        print(f"   FAILED: network proxy error: {error}")
        return False, "FAILED: network proxy error"
    except requests.exceptions.RequestException as error:
        # Any other network problem: no internet, unknown domain, timeout ...
        print(f"   FAILED: could not connect: {error}")
        return False, f"FAILED: could not connect ({type(error).__name__})"

    if response.status_code != 200:
        print(f"   FAILED: the server answered HTTP {response.status_code}")
        print(f"   Start of the answer: {response.text[:300]}")
        return False, f"FAILED: HTTP {response.status_code}"

    try:
        data = response.json()  # turn the JSON text into Python lists/dicts
    except ValueError:
        print("   FAILED: HTTP 200, but the answer is not valid JSON")
        print(f"   Start of the answer: {response.text[:300]}")
        return False, "FAILED: answer is not JSON"

    # Save the raw answer exactly as we received it, for later inspection.
    RAW_FOLDER.mkdir(parents=True, exist_ok=True)
    saved_file = RAW_FOLDER / save_as
    saved_file.write_text(response.text, encoding="utf-8")

    markets = find_market_list(data)
    print(f"   OK: HTTP 200, {len(markets)} {what}(s) in the answer")
    if markets:
        print(f"   First {what}: {describe_market(markets[0])}")
    print(f"   Raw answer saved to {saved_file.relative_to(PROJECT_ROOT)}")
    return True, f"OK ({len(markets)} {what}(s) received)"


# ---------------------------------------------------------------------------
# Main program
# ---------------------------------------------------------------------------

def main():
    """Run all checks, print a summary, and return 0 if every venue worked."""
    started = datetime.now(timezone.utc)  # project rule: timestamps in UTC
    print(f"Connection check, {started:%Y-%m-%d %H:%M:%S} UTC")

    results = []  # one (name, worked, summary) entry per check

    # 1) Kalshi: try the main host; only if that fails, try the fallback host.
    for name, url in [("Kalshi (main host)", KALSHI_MAIN),
                      ("Kalshi (fallback host)", KALSHI_FALLBACK)]:
        kalshi_worked, summary = check_endpoint(name, url, "kalshi_markets.json")
        results.append((name, kalshi_worked, summary))
        if kalshi_worked:
            break  # no need to try the fallback host

    # 2) Polymarket Gamma API: market information (questions, dates, outcomes).
    gamma_worked, summary = check_endpoint(
        "Polymarket Gamma API", POLYMARKET_GAMMA, "polymarket_gamma_markets.json")
    results.append(("Polymarket Gamma API", gamma_worked, summary))

    # 3) Polymarket CLOB API: the order-book side of Polymarket. This URL has
    #    no "limit", so it answers with a whole page of markets.
    clob_worked, summary = check_endpoint(
        "Polymarket CLOB API", POLYMARKET_CLOB, "polymarket_clob_markets.json")
    results.append(("Polymarket CLOB API", clob_worked, summary))

    # 4) Polymarket Data API: price history and trades. This URL asks for the
    #    single most recent trade on Polymarket.
    data_worked, summary = check_endpoint(
        "Polymarket Data API", POLYMARKET_DATA, "polymarket_data_trades.json", what="trade")
    results.append(("Polymarket Data API", data_worked, summary))

    # Summary table
    print("\n" + "=" * 72)
    print("SUMMARY")
    for name, worked, summary in results:
        print(f"   {name:<24} {summary}")
    print("=" * 72)

    if kalshi_worked and gamma_worked and clob_worked and data_worked:
        print("All venues reachable.")
        return 0
    print("Some checks failed: see the details above.")
    return 1


# This block runs only when the file is started as a script
# ("python src/check_connection.py"), not when another file imports it.
if __name__ == "__main__":
    sys.exit(main())  # exit code 0 = everything worked, 1 = something failed
