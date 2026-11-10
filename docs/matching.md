# Matching Kalshi and Polymarket markets

`src/match.py` finds pairs of Kalshi and Polymarket markets that refer to the
same outcome, compares their rules, and writes a list for manual review. This
page explains the method, every column of the review file, and the
**resolution risks found** so far. All numbers are from the run of
2026-11-04 on the committed tables.

## How to run it

```bash
python src/match.py
```

It reads `data/processed/kalshi_markets.parquet` and
`data/processed/poly_markets.parquet` (made by the two download scripts) and
asks Kalshi's public API for the settlement source of each Kalshi series
(4 cached GET requests). It writes:

| File | Contents |
|---|---|
| `data/processed/match_candidates.csv` | the candidate pairs, for your review (201 rows) |
| `data/processed/match_all_pairs.parquet` | every pair that was compared (16,713 rows), with the reason it is or is not a candidate |

**Reviewing the CSV.** Type `same`, `different` or `unsure` in `verdict`
and anything you like in `note`. In Excel, save with "CSV UTF-8" (a `;`
separator is fine too). Running `match.py` again keeps what you typed. If a
pair you reviewed is no longer found, its row is kept at the end and its
`suggested_reason` starts with "NO LONGER FOUND". Your `verdict` is final;
`suggested_verdict` is only a suggestion.

In pandas, read the file with
`pd.read_csv("data/processed/match_candidates.csv", dtype=str, keep_default_na=False)`.

**Reviewing in Excel (easier).** Run

```bash
python src/make_review_sheet.py
```

It writes `data/processed/match_review.xlsx`: one row per pair, the trickiest
first (highest `difference_score`), coloured by the suggested verdict (light
red = different, light yellow = unsure, white = same). Its columns are
`pair_id`, `kalshi_title`, `polymarket_title`, `side` (= `poly_yes_equals`),
`suggested_verdict`, `reason` (= `suggested_reason`), `differences` (one
flag per line), `kalshi_rules`, `polymarket_rules`, and two empty columns
for you: `verdict` (a dropdown: same / different / unsure) and `note`.
`pair_id` is "Kalshi ticker / Polymarket market id", e.g.
`KXCPI-25OCT-T0.4 / 655625`; it stays the same when `match.py` is run again.
Every row has the same height: click a cell to read all of its text. When you
are done, save the file under the same name and run

```bash
python src/read_review_sheet.py
```

It writes `data/processed/match_verified.csv`: `pair_id`, then all the
columns of `match_candidates.csv` in the same order, with your `verdict` and
`note`. A verdict other than same, different, unsure or empty stops the
script with the Excel rows to fix. The sheet and the CSV are two separate
ways to review: verdicts typed in the sheet go to `match_verified.csv`, not
into `match_candidates.csv`. `make_review_sheet.py` refuses to overwrite a
sheet that already has verdicts or notes in it (`--overwrite` replaces it
anyway).

## Method

### 1. Key fields of every market

Simple text rules read from each market's title and rules:

* **what is measured** (`measure`):

  | measure | meaning | Kalshi series | Polymarket |
  |---|---|---|---|
  | `fed_change` | change of the upper bound of the Fed's target range at one meeting | KXFEDDECISION | "Fed decision" events |
  | `fed_level` | the upper bound right after one meeting | KXFED | (none) |
  | `cpi_mom` | all-items CPI-U, % change from the previous month | KXCPI | "... Inflation US - Monthly" |
  | `cpi_yoy` | all-items CPI-U, % change from 12 months earlier | KXCPIYOY | "... Inflation US - Annual" |
  | `core_cpi_mom`, `core_cpi_yoy` | CPI without food and energy | (not downloaded) | "Core CPI MoM/YoY" |
  | `fed_other`, `fed_cuts_count`, `fed_level_other`, `cpi_other` | kinds that exist only on Polymarket in our data, e.g. "Fed rate cut by July meeting?" (any cut over several months) | (none) | |

* **month**: the meeting month or the data month. Kalshi gives it in the
  event ticker (`KXCPI-26OCT` → 2026-10). For Polymarket it is the "Month
  YYYY" in the question or else in the rules.
* **event date**: the scheduled meeting or release day. Kalshi closes these
  markets minutes before the announcement, on that day. Polymarket gives the
  date in its rules ("currently scheduled to be released on November 10,
  2026"; "meeting scheduled for September 15-16, 2026").
* **threshold** and **direction**, as written in the title, e.g. `0.2`
  with `more than`, or `25` with `cut, exactly`.
* **outcome**: the values for which YES wins, written in one common form.
  Values are counted in whole steps:
  * CPI in tenths of a percent, because both venues use the BLS figure with
    one decimal. So Kalshi's "more than 0.2%" is "CPI MoM ≥ 0.3%".
  * The Fed in basis points (bp), because it moves in 25 bp steps. So
    Kalshi's "Cut >25bps" is "cut of 50 bp or more".

### 2. Which pairs are compared

A Kalshi and a Polymarket market of the same topic (Fed or CPI) are compared
in either of two cases:

* their dates are within 7 days: Kalshi's close time against Polymarket's
  end date **or** its actual close time. Some Polymarket end dates are a
  month off (see risk 4 below);
* their key fields say they are about the same question: same measure and
  same month. Kalshi's rate *level* against Polymarket's rate *change* also
  counts.

The key fields are the main key; the dates are a sanity check (flag
`dates_far_apart`). On 2026-11-04 this gives 16,713 pairs. 5,963 of them are
about the same question, and all of those also pass the 7-day check.

### 3. Candidates

A pair is a **candidate** if its two YES outcomes are the same outcome, in
one of two ways:

* `poly_yes_equals = Kalshi YES`: both YES win for exactly the same values.
  Example: Polymarket "0.9% or more" and Kalshi "more than 0.8%".
* `poly_yes_equals = Kalshi NO`: Polymarket's YES wins exactly when Kalshi's
  YES loses. Example: Polymarket "0.1% or less" and Kalshi "more than 0.1%".
  For an arbitrage, buy Polymarket YES and Kalshi YES: exactly one of them
  pays $1.

`pair_type` says how the pair was found:

* `direct`: both markets measure the same thing.
* `level_vs_change`: Kalshi's rate level after the meeting (KXFED, "upper
  bound above 3.75%") against Polymarket's change at the meeting (a bucket).
  They are the same outcome only given the rate before the meeting. That rate
  is read from Kalshi's settled level markets of the previous meeting, so it
  is known only once the previous meeting is over. These pairs are always
  `unsure`; drop them with one filter on `pair_type`.
* `title_only`: a fallback when the key fields cannot be read but the titles
  score 80 or more. None on 2026-11-04: the fields were read for every market.

Individual binary markets are matched, not whole events. A single Polymarket
bucket such as "exactly 0.3%" has no single-market twin on Kalshi, which only
lists "more than X%" thresholds. Such a bucket equals the difference of two
Kalshi markets ("more than 0.2%" minus "more than 0.3%"), so it is not paired.

**Why the title score cannot decide.** The `title_similarity` column
(rapidfuzz `token_set_ratio`, 0-100) is kept, but it cannot tell right pairs
from wrong ones:

| pairs about the same question | number | median score | middle half |
|---|---|---|---|
| candidates (same outcome) | 165 | 68 | 65-75 |
| rejected (outcomes differ) | 4,489 | 67 | 63-80 |

For example, "Cut 25bps" and "decrease 25 bps" score 78 and are the same
outcome. "Cut >25bps" and "decrease 25 bps" also score 78 but are not.
Kalshi's headline CPI "more than 0.3%" and Polymarket's *core* CPI "0.3%"
score 80, higher than most true pairs.

### 4. Flags and suggested verdict

For each candidate, `match.py` compares the two rules texts and the data and
lists the differences in the column `differences`. Each entry has the form
`[points] code: explanation`. The points add up to `difference_score`, which
you can sort by to find the trickiest pairs.

| code | points | what it means |
|---|---|---|
| `paid_differently` | 10 | both settled, and Polymarket's YES did not win exactly when it should have |
| `settled_on_different_values` | 6 | taking all settled markets of each venue's event, the two venues paid as if the figure had different values (also possible when this pair paid the same) |
| `poly_closed_early` | 5 | the Polymarket market closed more than a day before the meeting or release |
| `level_vs_change` | 4 | `level_vs_change` pair: needs the rate before the meeting, and no move between meetings |
| `cancelled_meeting` | 1, 2 or 4 | the venues handle a cancelled or moved FOMC meeting differently (4 = opposite payoffs) |
| `dates_far_apart` | 3 | the sanity check failed: dates more than 7 days apart |
| `postponed_release` | 2 | the venues handle a postponed CPI release differently |
| `release_was_postponed` | 2 | this CPI release actually was delayed (dates moved, or settlement days late) |
| `poly_duplicate` | 2 | Polymarket lists the same outcome in another event too |
| `poly_source_unusual` | 2 | Polymarket's resolution-source field is not the BLS or the Fed |
| `scheduled_dates_differ` | 2 | the two venues give different meeting or release dates |
| `rounding` | 1 | how a Fed move that is not a multiple of 25 bp is counted |
| `seasonal_adjustment` | 1 | only Polymarket's text says whether the CPI figure is seasonally adjusted |
| `precision` | 1 | one text does not state the one-decimal rounding of CPI |
| `kalshi_text_lost_symbol` | 1 | Kalshi's rules text lost a ">" sign |
| `poly_end_date_off`, `poly_rules_date_typo` | 1 | Polymarket's end date, or the year in its rules, is wrong |
| `revisions`, `emergency_meeting`, `trading_end` | 0 | notes: revisions and emergency meetings are covered by neither text; when each venue stops trading |

`suggested_verdict`:

* **different**: the pair settled differently.
* **unsure**: same outcome on paper, but the comparison rests on an
  assumption (`level_vs_change`), or the data shows a problem
  (`poly_closed_early`, `dates_far_apart`, `settled_on_different_values`),
  or the key fields could not be read.
* **same**: same outcome. Rule differences that only matter in rare edge
  cases do not change this; they are all listed in `differences`, and the
  most important one is named in `suggested_reason`.

`match.py` also prints the 10 trickiest candidates. It goes down the
`difference_score` and prefers pairs that show a problem not yet shown, at
most one pair per event.

## Columns of `match_candidates.csv`

| column | meaning |
|---|---|
| `kalshi_ticker`, `poly_market_id` | the two market ids (Kalshi ticker, Polymarket Gamma id) |
| `poly_url` | the Polymarket event page |
| `pair_type` | `direct`, `level_vs_change` or `title_only` (see above) |
| `poly_yes_equals` | `Kalshi YES` (same side) or `Kalshi NO` (opposite sides) |
| `suggested_verdict`, `suggested_reason` | the suggestion and its one-line reason |
| `verdict`, `note` | **yours**: empty, to fill in |
| `difference_score`, `differences` | flag points and the full list of flags |
| `kalshi_title`, `poly_question`, `title_similarity` | the two titles and their score (0-100) |
| `kalshi_measure`, `poly_measure` | what is measured |
| `kalshi_month`, `poly_month` | meeting month or data month |
| `kalshi_event_date`, `poly_event_date` | scheduled meeting or release day (Kalshi: day of its close; Polymarket: from its rules) |
| `kalshi_threshold`, `kalshi_direction`, `kalshi_outcome` | Kalshi's number, how it is used, and its YES outcome in common form |
| `poly_threshold`, `poly_direction`, `poly_outcome` | the same for Polymarket |
| `rate_before_meeting` | `level_vs_change` pairs: the upper bound before the meeting and where it comes from |
| `kalshi_close_time`, `poly_end_date`, `poly_closed_time` | close and end times (UTC) |
| `days_apart`, `dates_within_7_days` | the date sanity check |
| `kalshi_result`, `poly_result`, `results_agree` | how each YES settled (`yes`/`no`, empty if not yet), and whether they agree for this pair |
| `kalshi_settled_value`, `poly_settled_value` | the value of the figure each venue's settled event implies, e.g. `= 0.2%` or `≥ 3.2%` |
| `kalshi_source`, `poly_source` | the official resolution sources (Kalshi: from its series record) |
| `kalshi_contract_terms` | link to Kalshi's full contract terms (PDF) |
| `kalshi_rules`, `poly_rules` | the full rules texts, side by side |

## Results on 2026-11-04

| pair type | measure | same | unsure | different | total |
|---|---|---|---|---|---|
| direct | Fed decision (`fed_change`) | 76 | 3 | 2 | 81 |
| direct | CPI MoM | 37 | 3 | 0 | 40 |
| direct | CPI YoY | 42 | 1 | 1 | 44 |
| level_vs_change | Fed level vs change | 0 | 36 | 0 | 36 |
| **all** | | **155** | **43** | **3** | **201** |

* The candidates cover 21 Fed meetings, 22 months of CPI MoM and 23 of CPI
  YoY. 60 of the 201 pairs are opposite-side pairs.
* In 177 pairs both markets have settled. 174 of them paid the same, and
  3 paid differently (risks 1 and 2 below).
* Of the 16,512 pairs that are not candidates:
  * 10,651 measure different things;
  * 5,313 are about the same question but have different outcomes (e.g.
    "exactly 0.3%" vs "more than 0.3%");
  * 429 are level-vs-change pairs whose rate before the meeting is not known
    (the previous meeting has not settled yet, or is not in our data);
  * 99 are about different months;
  * 20 involve Polymarket's old "Other" market.
* 557 of the 736 Polymarket markets have no candidate:
  * 229 single buckets ("exactly ...") of headline CPI and the Fed;
  * 98 core-CPI markets (Kalshi's core-CPI series are not downloaded yet);
  * 215 Polymarket-only kinds;
  * 2 "Other" markets;
  * 13 open-ended buckets without a twin. For 11 CPI buckets Kalshi listed
    no threshold at that level that month (e.g. "-0.7% or less" in July
    2026). Two of them are October 2026 YoY, for which Kalshi has no event
    at all (checked with Kalshi's API on 2026-11-04). For 2 November 2024
    Fed buckets the rate before the meeting is not in our data.

## Verification method

The 201 candidates were verified on 2026-11-04 in an **automated two-pass
review**. In *pass 1* a second reviewer (Claude, an AI assistant) read both
full rules texts and the titles of every pair, and checked each venue's
settled results in the market tables. It did this *without* seeing
`suggested_verdict`, `suggested_reason`, `difference_score` or `differences`.
It called a pair `same` only if buying YES on one venue and the matching side
on the other always pays exactly once: same measured quantity (headline CPI-U,
not core), same month or meeting, same threshold at CPI's one-decimal
precision, and the correct side. Rare edge cases (a cancelled or moved FOMC
meeting, a delayed BLS release) were allowed but noted. A realised different
settlement or a data problem meant `different`, and every `level_vs_change`
pair was `unsure`. Its verdicts and one-line reasons were frozen before any
comparison, in `data/processed/independent_review.csv`. In *pass 2*,
`src/apply_independent_review.py` combines them cautiously with `match.py`'s
suggestion: `same` only if both say same, otherwise `unsure` if either says
unsure, otherwise `different`. It writes `data/processed/match_verified.csv`
(`review_method` = "automated two-pass"; `note` = the pass-1 reason, plus
"(disagreement)" where the two differ). The passes agreed on 192 of the 201
pairs. All 9 disagreements are pairs that paid consistently but whose venues
settled the month or meeting on different values (risks 1 and 2 below: four
October/November 2025 CPI pairs and five pairs with the faulty September 2026
Polymarket event). Pass 1 said `different` and `match.py` said `unsure`, so
they end up `unsure`. As a human check, `data/processed/spot_check.xlsx` holds
10 random `same` pairs (fixed seed 20261104) with both titles and rules texts
and an empty `my_check` column (agree / disagree). `src/build_pairs.py` turns
the `same` pairs into `data/processed/pairs.parquet`, the input for the price
comparison.

| | same | unsure | different |
|---|---|---|---|
| pass 1: independent review | 155 | 34 | 12 |
| `match.py`'s suggestion | 155 | 43 | 3 |
| **final verdict** (`match_verified.csv`) | **155** | **43** | **3** |

The 155 pairs in `pairs.parquet` are 76 Fed decision pairs (21 meetings,
November 2024 to April 2027), 37 CPI MoM pairs (20 months) and 42 CPI YoY
pairs (22 months). 133 have settled on both venues, and 39 are opposite-side
pairs. To redo the last steps:

```bash
python src/apply_independent_review.py   # match_verified.csv and spot_check.xlsx
python src/build_pairs.py                # pairs.parquet, with counts per topic and month
```

Running `src/read_review_sheet.py` replaces `match_verified.csv` with the
verdicts typed in your own review sheet; `build_pairs.py` works with either.
`apply_independent_review.py` will not overwrite such a file, or a spot-check
sheet you have started to fill in, unless you add `--overwrite`.

## Resolution risks found

These are cases in the 24 months of data (Nov 2024 to Nov 2026) in which the
two venues' rules or data differ in a way that can make "the same" market
pay differently. Ids are Kalshi tickers and Polymarket Gamma market or event
ids. All times are UTC unless marked ET.

### 1. Government shutdown 2025: the venues settled October and November CPI on different values

**What happened at the source.** During the lapse in federal appropriations
from 1 October to 12 November 2025, the BLS could not collect October 2025
price data. It issued **no October 2025 CPI release**. It published the
November 2025 CPI on 18 December 2025, **without one-month percent changes**
for November where the October data were missing. (The September 2025 CPI
was delayed too: both venues' markets for it settled on 24 October 2025,
nine days after the scheduled 15 October.) Sources: BLS,
[2025 shutdown impact on the CPI](https://www.bls.gov/cpi/additional-resources/2025-federal-government-shutdown-impact-cpi.htm)
and [FAQ](https://www.bls.gov/cpi/additional-resources/2025-federal-government-shutdown-impact-cpi-faq.htm).

**How each venue settled.** The value each venue's settled markets imply:

| data month | figure | BLS | Kalshi settled as | Polymarket settled as |
|---|---|---|---|---|
| Sep 2025 | MoM / YoY | published late (24 Oct) | 0.3% / 3.0% (24 Oct) | 0.3% / 3.0% (24 Oct) |
| Oct 2025 | MoM | not published | **0.2%** ("more than 0.1%" Yes, "more than 0.2%" No; 22 Nov 02:06) | **0.3%** ("0.3%" bucket Yes; 22 Nov 11:22) |
| Oct 2025 | YoY | not published | **≥ 3.2%** (all strikes 2.4%-3.1% Yes; 22 Nov 02:04) | **3.0%** ("3.0%" bucket Yes; 22 Nov 14:25) |
| Nov 2025 | MoM | not published (one-month change missing) | **0.2%** (18 Dec 19:23) | **0.3%** (13 Jan 2026 17:30) |
| Nov 2025 | YoY | published (18 Dec) | 2.7% | "≤ 2.8%" Yes (consistent) |

Polymarket's October values are exactly its September values. Kalshi's are
not, and the market data does not show what Kalshi based them on.

**What the rules say.**

* *Kalshi* (all 596 CPI markets): "In the case of a delay in data caused by
  a federal government shutdown impacting the reliability of the Source
  Agency, the market's latest Expiration Date will be extended to the sooner
  of the release of the Underlying or six months after the end of the
  government shutdown." The market text does not say what happens if the
  figure is never published. Kalshi's full contract terms might, but they
  could not be read here (see "Not checked yet" below).
* *Polymarket*, every CPI event from the November 2025 data on (and none
  before): "If the BLS does not release the relevant figures on the
  scheduled date, this market may remain open up until the scheduled release
  time of the next CPI report. If the information is not released by that
  time, this market will resolve according to the figures of the most recent
  previous month with available data." The September and October 2025
  events had no such clause, yet October resolved to the September figures.
  For November MoM, Polymarket waited until the next report (13 January 2026),
  26 days after Kalshi had settled.

**Consequence for matched pairs.** Kalshi `KXCPIYOY-25OCT-T3.1` ("above
3.1%") and Polymarket `654401` ("≥ 3.2%", October 2025) are the same outcome
on paper. Kalshi's YES paid and Polymarket's YES did not. A position meant to
be riskless lost both legs: YES on Polymarket and NO on Kalshi. Its mirror
image paid twice. In the other matched pairs of October and November 2025
(e.g. `KXCPI-25OCT-T0.4` / `655625`, `KXCPI-25NOV-T0.1` / `680561`) the two
values happened to fall on the same side of the threshold, so they paid
consistently. The single buckets did not: Polymarket's October "0.3%" paid
Yes while Kalshi's "more than 0.2%" paid No.

**Takeaway.** The definitions agree only as long as the statistic is
published on schedule. When a release is cancelled or incomplete, each venue
falls back on its own rule and the payoffs can split. Flags:
`paid_differently`, `settled_on_different_values`, `release_was_postponed`,
`postponed_release`.

### 2. A duplicate Polymarket event for the September 2026 FOMC meeting

Polymarket has two events titled "Fed Decision in September?". They have the
same five questions ("50+ bps decrease" ... "50+ bps increase"), the same
rules text and the same end date (2026-09-16 00:00):

| | event 432066 (`fed-decision-in-september-568`) | event 481717 (`fed-decision-in-september-762`) |
|---|---|---|
| series | none | `fomc` |
| created | 2026-04-29 | 2026-05-13 |
| volume per market | $8,326 to $12,776 | $19.7 million to $56.4 million |
| closed | 2026-05-19 02:58 to 2026-05-20 14:37 (four months before the meeting) | 2026-09-16 20:02 to 21:40 |
| resolved | "No change" = Yes (`umaResolutionStatus` "resolved", `outcomePrices` ["1", "0"]) | "25 bps increase" = Yes |

At the meeting the Fed raised the target range by 25 bp. Kalshi
`KXFEDDECISION-26SEP-H25` ("Hike 25bps") settled Yes on 2026-09-16 18:08,
and Kalshi's level markets imply an upper bound of 4.00% after the meeting
(3.75% before). Against the duplicate event, Kalshi's "Fed maintains rate"
(`KXFEDDECISION-26SEP-H0` / `2118744`) and "Hike 25bps"
(`KXFEDDECISION-26SEP-H25` / `2118745`) settled the opposite way from
Polymarket.

**Why it matters.** Titles, questions, rules and end dates are identical, so a
matcher based on text and dates pairs Kalshi with both events. Only the
close time, the volume and the missing series give the duplicate away.
Prices from event 432066 after May 2026 are not tradable prices. Flags:
`poly_closed_early`, `poly_duplicate`, `paid_differently`.

### 3. A cancelled or moved FOMC meeting is handled differently

* *Kalshi, Fed decision markets of the six meetings from November 2024 to
  June 2025*: "Clarification 10:10 AM ET 8/5/24: If a scheduled FOMC meeting
  is canceled and does not occur on its scheduled date, then all strikes in
  this market resolve to No."
* *Kalshi, from the July 2025 meeting on*: "If the scheduled FOMC meeting is
  canceled and does not occur on its scheduled date, "Fed maintains rate"
  resolves to Yes and all other outcomes resolve to No."
* *Kalshi, rate-level markets (KXFED)*: "This market will expire the first
  2:05 PM ET following the release of a Federal Reserve statement for their
  [...] meeting or one week following the last day of that meeting."
* *Polymarket, every Fed decision event*: "If no statement is released by
  the end date of the next scheduled meeting, this market will resolve to the
  "No change" bracket." (November 2024 event: "by December 18, 2024, 11:59 PM
  ET".)

**Consequences.**

* Under Kalshi's older rule, the "No change" pair would pay **opposite ways**
  if the meeting were cancelled: Kalshi No, Polymarket Yes. This affects the
  six "No change" pairs from November 2024 to June 2025 (flag
  `cancelled_meeting` with 4 points).
* Under the newer rule both end at "no change", but at different times.
  Kalshi decides as soon as the meeting does not take place on its scheduled
  date; Polymarket waits for a statement until the next scheduled meeting. A
  meeting postponed by a few days that then cuts would pay "Fed maintains
  rate" on Kalshi and the cut on Polymarket.
* No meeting in the sample was cancelled or moved: all 135 Kalshi decision
  markets closed on the meeting day named in their rules.

### 4. Wrong dates on Polymarket

| Polymarket event | end date in the data | actual release / meeting | effect |
|---|---|---|---|
| `january-inflation-annual` (Jan 2025 CPI YoY, event 16808) | 2025-01-12 | release 2025-02-12 (closed that day) | its end date is 3 days from Kalshi's **December 2024** YoY markets (2025-01-15), so a date-only matcher pairs it with the wrong month |
| `february-inflation-monthly` and `-annual` (Feb 2025, events 17894, 17892) | 2025-03-31 | release 2025-03-12 | end date 19 days late |
| `december-inflation-us-monthly` (Dec 2025 CPI MoM, event 97910) | 2025-12-13 | release 2026-01-13 (closed 16:43) | end date 31 days early; its rules also give the release as "January 13, **2025**". With a strict end-date window it is compared with Kalshi's November 2025 markets (5 days) and misses its true December twin (31 days) |
| `fed-interest-rates-january-2025` (event 14220) | 2025-01-29 (correct) | meeting 28-29 January 2025 | its rules say the "meeting scheduled for January **30 - 31**, 2025" |

Polymarket's end date is also not the decision time. 8 of the 22 Fed
decision events end at 00:00 UTC on the decision day, which is 8 PM ET the
evening *before* the statement; 9 end at 12:00 UTC. A Polymarket market
closes only when it is resolved (`poly_closed_time`), usually hours after the
announcement. Kalshi closes 1 minute before the CPI release (8:29 AM ET) and
a few minutes before the 2 PM ET Fed statement. Compare prices only before
Kalshi's close.

`match.py` therefore uses the end date *or* the actual close time for the
date check, makes the key fields (measure and month) the main key, and flags
`poly_end_date_off`, `poly_rules_date_typo` and `scheduled_dates_differ`.

### 5. A YouTube video as Polymarket's resolution source

The four markets of Polymarket's June 2025 Fed decision event
(`fed-decision-in-june`, event 17792, markets 521834-521837) give
`https://www.youtube.com/watch?v=aLI1V4kKFME` as their resolution source. That
is not an official Fed page. Their rules text still names the FOMC statement
and federalreserve.gov. The event resolved "No change" = Yes, the same as
Kalshi (`KXFEDDECISION-25JUN-H0`), so it caused no divergence. But the
structured field cannot be relied on for an automatic source check. Of the
521 Polymarket Fed decision and CPI markets:

* 335 have no resolution-source field at all;
* 152 point to the BLS CPI release page;
* 30 point to the Fed's FOMC calendar;
* 4 point to this video.

Kalshi keeps its sources on the series record: "Federal Reserve" and
"Federal Reserve Board of Governors" (federalreserve.gov) for the Fed, and
"Bureau of Labor Statistics" (bls.gov/cpi) for CPI. Flag:
`poly_source_unusual`.

### 6. Smaller differences (also flagged)

* **Rate level vs rate change** (36 pairs, `level_vs_change`). Kalshi's
  KXFED markets ask about the level after the meeting, which also counts
  moves between meetings. Polymarket asks about the change at the meeting.
  They only match once the previous meeting has fixed the starting level.
* **Fed moves that are not a multiple of 25 bp.** Polymarket's rules have
  three versions:
  * from the May 2025 meeting on: "the change will be rounded up to the
    nearest 25";
  * January and March 2025: ranges, e.g. "decreased by between 26
    (inclusive) and 50 (inclusive)", which does the same;
  * November and December 2024: exact sizes ("decreased by exactly 50 basis
    points") and a separate "Other" market for moves in between.

  Kalshi's market texts say nothing. In `level_vs_change` pairs the round-up
  can split the payoffs: a 37.5 bp cut counts as "50+ bps" on Polymarket but
  leaves the level above Kalshi's matching threshold.
* **Seasonal adjustment.** Polymarket states it (MoM "seasonally adjusted",
  YoY "before seasonal adjustment"); none of Kalshi's 596 CPI market texts
  do.
* **Not covered by any market text:** data revisions and emergency
  (unscheduled) FOMC meetings. 0 of 1,150 Kalshi and 0 of 521 Polymarket
  rules texts mention them.
* **Kalshi's text lost the ">" sign** in 31 decision markets (16 events),
  e.g. `KXFEDDECISION-28JAN-C26`:
  `If the Federal Reserve does a Cut of   25bps on January 26, 2028`. The
  title and ticker (C26) show it means *more than* 25 bp.

### Not checked yet

* Kalshi's full contract terms (`kalshi_contract_terms`, e.g.
  `https://assets.kalshi.com/contract_terms/CPI.pdf`) may define what happens
  if a figure is never published, and what "seasonally adjusted" and
  "revisions" mean for Kalshi. The cloud environment's network blocks
  `assets.kalshi.com`. Add that domain to the environment's allowed domains
  to read them.
* Why Kalshi settled October 2025 at 0.2% and ≥ 3.2%: Kalshi's settlement
  notices are not in the API data.

## Sources

* BLS, 2025 federal government shutdown impact on the CPI:
  https://www.bls.gov/cpi/additional-resources/2025-federal-government-shutdown-impact-cpi.htm
  and FAQ: https://www.bls.gov/cpi/additional-resources/2025-federal-government-shutdown-impact-cpi-faq.htm
* BLS CPI release page (named in Polymarket's rules): https://www.bls.gov/bls/news-release/cpi.htm
* FOMC meeting calendar: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  and target range history: https://www.federalreserve.gov/monetarypolicy/openmarket.htm
* Kalshi API (markets, series): https://docs.kalshi.com/api-reference/market/get-markets,
  https://docs.kalshi.com/api-reference/market/get-series
* Polymarket API (market fields, resolution): https://docs.polymarket.com/market-data/market-details.md,
  https://docs.polymarket.com/concepts/resolution.md
* rapidfuzz `token_set_ratio`: https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html
* Rules texts quoted above: `kalshi_rules` and `poly_rules` in
  `data/processed/match_candidates.csv`, as sent by the two APIs on
  2026-11-04.
