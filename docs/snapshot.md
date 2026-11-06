# Order-book snapshots: `src/snapshot.py`

`snapshot.py` runs in the background, for days if you like. Every 5 minutes it
downloads both venues' **full order books** for every open pair and saves
them, exactly as the venues sent them. It does **not** compute prices or
arbitrage: `src/arb.py` will do that later from the saved files.

## What one cycle does

Cycles start on the clock's 5-minute marks: 00:00, 00:05, 00:10 … UTC.

1. **Open pairs.** It reads `data/processed/pairs.parquet` (again every
   cycle, so a rebuilt pairs file is picked up without a restart) and keeps
   the pairs that are open on both venues:
   * Kalshi: the market has not closed yet (`kalshi_close_time` is in the future);
   * Polymarket: the market's `closed` flag is `False`. The flag is asked
     live from Polymarket's Gamma API, in one request per cycle. If Gamma
     cannot be reached, the last known flags are used (at the start: the
     ones in `data/processed/poly_markets.parquet`).

   A pair drops out by itself once Kalshi closes it: after that there is
   nothing left to compare.
2. **Three books at the same moment.** For each open pair, three requests go
   out together, in parallel: the Kalshi book, the Polymarket YES book and
   the Polymarket NO book. The pairs are spaced 0.6 s apart, so the program
   never sends more than 5 requests per second. With 22 open pairs a cycle
   takes about 14 seconds.
3. **Save.** One row per pair is added to the file of the day (UTC):
   `data/processed/snapshots/snapshots_YYYY-MM-DD.parquet`. Git ignores this
   folder, so the files stay on your computer only. **Back them up**,
   for example by copying the folder to a cloud drive now and then.

## Before the first run (once, on your Mac)

Open the Terminal app and go to the project folder (change the path to where
your copy is):

```bash
cd ~/pm-arbitrage
```

Get the latest code and pairs table, and install the packages. The pinned
package versions (numpy 2.5) need Python 3.12 or newer: check with
`python3 --version`. If yours is older, install a new Python from python.org.

```bash
git pull
```

```bash
pip3 install -r requirements.txt
```

Test one cycle (takes about 15 seconds):

```bash
python3 src/snapshot.py --once
```

It should end with a line like
`Cycle ...: 22 of 22 open pairs saved; books ok: kalshi 22/22, poly_yes 22/22, poly_no 22/22; ...`.
These test rows are saved like any other cycle; their `cycle_time` is simply
not on a 5-minute mark.

## Start

**Option A: in a Terminal window** (simplest). The window must stay open.

```bash
caffeinate -is python3 src/snapshot.py
```

**Option B: in the background.** You can close the Terminal window afterwards.

```bash
nohup caffeinate -is python3 src/snapshot.py > logs/snapshot_console.log 2>&1 &
```

`caffeinate` is a macOS tool that keeps the Mac awake for as long as the
program runs (`-i`: no sleep when idle, `-s`: no sleep while on the power
adapter). `nohup … &` keeps the program running after the window is closed.

The first cycle runs at the next 5-minute mark, not right away. Only one copy
can run at a time: a second one stops at once with
"Another copy is already running".

## Is it running?

Watch the log (press Ctrl+C to stop *watching*; the program keeps running):

```bash
tail -f logs/snapshot.log
```

There is one line per cycle, plus a warning for every book that could not
be downloaded. Check that the process is alive (it prints the process if
yes, nothing if no):

```bash
ps -p $(cat logs/snapshot.pid)
```

## Stop

* Option A: press **Ctrl+C** in its window.
* Option B:

  ```bash
  kill $(cat logs/snapshot.pid)
  ```

Either way the program finishes the pair it is working on, saves what this
cycle has downloaded so far, writes "Stopped." to the log and ends
(`caffeinate` ends with it). Pressing Ctrl+C twice stops it at once.

If it says "Another copy is already running" but you are sure none is (for
example after the Mac crashed), delete the leftover marker file:

```bash
rm logs/snapshot.pid
```

## Running overnight on macOS

1. **Plug in the power adapter.**
2. **Keep the lid open.** A MacBook sleeps when its lid is closed, even with
   `caffeinate`. The screen may turn off; that is fine.
3. Start it with `caffeinate` (above).
4. Optional: in System Settings → General → Software Update, make sure the
   Mac will not restart for an update that night. After a restart the
   program does not start again by itself.

What happens when something goes wrong:

| Problem | What the program does |
|---|---|
| A venue answers "too many requests" (429) or a server error (5xx) | tries again after 1 s, then 2 s (3 tries in total) |
| A book cannot be downloaded | saves the row anyway, with the reason in `<book>_error`, and logs a warning |
| Wi-Fi is down | after 3 pairs in a row without any connection, skips the rest of the cycle; tries again at the next mark |
| The Mac slept anyway | logs "Missed the cycle of … was the computer asleep?" and goes on at the next mark |
| Any other unexpected error | writes it to the log (with details) and goes on at the next mark |

**The next morning**, see how many cycles ran and whether there were problems:

```bash
grep -c "Cycle" logs/snapshot.log
```

```bash
grep -E "WARNING|ERROR" logs/snapshot.log
```

A new log file starts every day at midnight UTC; the older ones are named
`logs/snapshot.log.YYYY-MM-DD` and kept for 30 days.

## What is saved

One row per pair and cycle. All times are UTC.

| Column | Meaning |
|---|---|
| `cycle_time` | the 5-minute mark this cycle belongs to |
| `pair_id`, `kalshi_ticker`, `poly_market_id`, `poly_yes_token_id`, `poly_no_token_id` | which pair (join with `pairs.parquet` on `pair_id` for everything else) |
| `time_gap_seconds` | Polymarket YES book's time − Kalshi book's time (see below); positive = Polymarket's book was taken later; empty if either book is missing |
| `poly_yes_server_time`, `poly_no_server_time` | the `timestamp` Polymarket writes into its answer. It is **not** the time of the answer: in our test it was 0.2 to 68 s earlier, so it seems to be the time the book last changed |
| `<book>_sent_time`, `<book>_received_time` | when the request was sent and when its answer arrived (of the last try) |
| `<book>_status` | `ok`, `no_book` (HTTP 404: no book, e.g. the market has closed) or `error` |
| `<book>_tries` | number of tries (1 = it worked at once) |
| `<book>_error` | what went wrong; empty if `ok` |
| `<book>_book` | the book exactly as the venue sent it (JSON text); empty if not `ok` |

`<book>` is `kalshi`, `poly_yes` or `poly_no`.

* **Kalshi** sends bids only: `orderbook_fp.yes_dollars` and
  `orderbook_fp.no_dollars`, each a list of `[price, contracts]`; all price
  levels (`depth` = 0, the default).
  https://docs.kalshi.com/api-reference/market/get-market-orderbook
* **Polymarket** sends one book per outcome token, with `bids`, `asks`
  (lists of `{price, size}`), `timestamp`, `hash`, `tick_size`,
  `last_trade_price` and more. The NO book is normally the mirror image of
  the YES book (it was in all 22 markets of our test); it is kept so that
  `arb.py` can check this.
  https://docs.polymarket.com/market-data/prices-order-books.md

### How the time difference is measured

A book is taken somewhere between the moment its request was sent and the
moment its answer arrived. The program uses the middle of that interval as
the book's time:

    book time        = sent_time + (received_time - sent_time) / 2
    time_gap_seconds = Polymarket YES book time - Kalshi book time

Both times come from the same computer's clock, so an error in that clock
cancels out. When a request needs a second try (`<book>_tries` = 2, e.g.
after a "too many requests" answer), its book is taken a second or more
after the other venue's, and `time_gap_seconds` shows it (in the live test:
2 rows of 132, both in the same cycle, gap −1.0 s). Filter on
`time_gap_seconds` in `arb.py`, for example keep only rows within 0.5 s. The three requests of a pair leave within a few thousandths of
a second of each other; the gap that remains comes from the venues' different
answer times (Kalshi about 0.05 s, Polymarket about 0.13 s from the cloud
test machine; it will be different from your home internet).

## Reading the files (for `arb.py` and notebooks)

```python
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.append("src")
import kalshi
import polymarket

# All days together. (Read the snapshots_*.parquet files, not the whole folder:
# the folder can briefly contain a temporary file while a cycle is saving.)
files = sorted(Path("data/processed/snapshots").glob("snapshots_*.parquet"))
snaps = pd.concat([pd.read_parquet(file) for file in files], ignore_index=True)

# The books of one row, with the parse functions the project already has:
row = snaps[snaps["kalshi_status"].eq("ok") & snaps["poly_yes_status"].eq("ok")].iloc[0]
kalshi_top, kalshi_depth = kalshi.parse_orderbook(
    json.loads(row["kalshi_book"]), row["kalshi_ticker"], row["kalshi_received_time"])
poly_top, poly_depth = polymarket.parse_orderbook(
    json.loads(row["poly_yes_book"]), json.loads(row["poly_no_book"]),
    row["poly_market_id"], row["poly_yes_received_time"])
```

Remember `poly_yes_equals` in `pairs.parquet`: for "Kalshi NO" pairs,
Polymarket's YES is Kalshi's NO.

## Settings

At the top of `src/snapshot.py`:

| Setting | Value | Meaning |
|---|---|---|
| `CYCLE_MINUTES` | 5 | minutes between cycles (or `--minutes N` on the command line) |
| `PAIR_SPACING_SECONDS` | 0.6 | at most one pair per 0.6 s = at most 5 requests per second |
| `TIMEOUT_SECONDS` | (5, 15) | give up on a try after 5 s without a connection or 15 s without an answer |
| `MAX_TRIES` | 3 | tries per request |
| `OFFLINE_PAIRS_LIMIT` | 3 | pairs in a row without connection before a cycle is skipped |
| `MAX_LATE_SECONDS` | 60 | a cycle more than 60 s late is skipped (the computer was asleep) |

## Live test (cloud test machine, 2026-11-04/05)

* The loop was started with `nohup` at 23:48 UTC and ran its cycles at
  23:50, 23:55 and 00:00 UTC: each saved 22 of 22 open pairs with all 66
  books, in about 14 seconds. The 00:00 cycle went into a new file
  (`snapshots_2026-11-05.parquet`) and a new log file. `kill` stopped it
  within a second, with "Stopped." in the log.
* Time gap between the Kalshi and Polymarket YES books, over all 132 rows
  with all three books: median 0.045 s, 95% of rows within 0.064 s, largest
  1.0 s (the two rows where Kalshi needed a second try). The three requests
  of a pair left within about 3 thousandths of a second of each other.
* Without internet (simulated with `--once`), the cycle tried 3 pairs,
  saved them as rows with `status = error`, logged "the internet seems to
  be down", and ended normally.

## File sizes

With 22 open pairs, one cycle's books are about 155 KB of text, and one
cycle saved on its own is a 69 KB Parquet file. So a day (288 cycles) is at
most about 20 MB; less in practice, because a larger file compresses better.

The day's file is rewritten after every cycle (Parquet files cannot be added
to in place): it is read, the new rows are put under the old ones, and the
result replaces the old file in a single step, so a program stopped halfway
never leaves a damaged file. The project's 50 MB limit is for committed
files; these are not committed, and the program only logs a warning if a
day's file ever passes 50 MB.
