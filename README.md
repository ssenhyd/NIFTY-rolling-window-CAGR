# NSE Index Data Toolkit

Four standalone Python scripts that take NIFTY index history from NSE Indices Ltd and
turn it into an auditable time series: **price + total return (TRI) history → official
USD/INR decoration → rolling N-year CAGR in INR and USD → an interactive HTML dashboard.**

Everything is built around one rule: **no number is ever guessed.** Every gap, fallback,
carry-forward and dropped row is written to disk and reported.

---

## Contents

| Script | What it does | Depends on |
|---|---|---|
| `nse_index_downloader.py` | Resumable, polite, self-checking downloader of NIFTY index PRICE and TRI history from niftyindices.com, in 3-month chunks | `requests`, `pandas` |
| `nse_fx_decorator.py` | Adds an official USD/INR rate column (RBI / FBIL / Fed H.10) to any CSV with a date column | `requests` |
| `nse_rolling_cagr.py` | Rolling N-year CAGR of the TRI series, in INR and USD, with a self-describing header block | none (stdlib) |
| `build_dashboard.py` | Builds an interactive HTML dashboard for a rolling-CAGR file | `numpy`, `pandas` |

Python 3.9 or later for all four.

---

## The pipeline

The scripts are a chain — each one's output is the next one's input. Run them in this
order:

```
nse_index_downloader.py
        |   merged/NIFTY_50_price_and_tri.csv
        v
nse_fx_decorator.py
        |   merged/NIFTY_50_price_and_tri_fx.csv
        v
nse_rolling_cagr.py  -w 7
        |   merged/NIFTY_50_price_and_tri_fx_cagr_7y.csv
        v
build_dashboard.py
            dashboard/NIFTY_50_7y_cagr_dashboard.html
```

You do not have to run the whole chain. Each script takes a plain CSV and is useful on
its own — for example, `nse_fx_decorator.py` will decorate any dated series, not just
NSE output.

---

## Requirements

```bash
python3 --version          # need 3.9+
pip install requests pandas numpy
```

`nse_rolling_cagr.py` needs nothing beyond the standard library.
`build_dashboard.py` embeds Plotly from a CDN by default (or downloads it with
`--inline`), so the generated page needs no Python-side plotting package.

---

## Quick start

Full run, end to end, for NIFTY 50 and NIFTY 500:

```bash
# 1. Download price + TRI history (takes roughly an hour — it is deliberately slow)
python3 nse_index_downloader.py --out-dir nse_index_data --end-date 2026-09-28

# 2. Add the official USD/INR rate to each merged file
python3 nse_fx_decorator.py nse_index_data/merged/NIFTY_50_price_and_tri.csv \
                            nse_index_data/merged/NIFTY_500_price_and_tri.csv

# 3. Rolling 7-year CAGR of the TRI series, in INR and USD
python3 nse_rolling_cagr.py -w 7 nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv \
                                   nse_index_data/merged/NIFTY_500_price_and_tri_fx.csv

# 4. Interactive dashboard
python3 build_dashboard.py --data nse_index_data/merged/NIFTY_50_price_and_tri_fx_cagr_7y.csv
```

Every script also prints its full documentation: `--help` is long and worth reading
before a first run.

---

## 1. `nse_index_downloader.py`

Downloads NIFTY index **PRICE** (OHLC) and **TOTAL RETURN** (TRI / Net TRI) history from
NSE Indices Ltd in polite, resumable 3-month chunks, sanity-checks each chunk, and merges
the good ones.

### What it does, step by step

1. Discovers the earliest date the site has data for, per index × series.
2. Splits `[earliest .. --end-date]` into calendar quarters (Jan–Mar, Apr–Jun, Jul–Sep,
   Oct–Dec); the first and last chunk are clipped.
3. Downloads one chunk at a time with random waits in between.
4. Retries a failing chunk with exponential back-off and jitter; a fresh HTTP session per
   retry.
5. Sanity-checks every chunk (structure, coverage, boundaries, gaps).
6. Records every outcome — log file, attempt ledger, state file, status CSV.
7. At the end, **offers** to retry chunks that failed sanity checks and chunks abandoned
   after all retries.
8. Merges good chunks into one CSV per index/series, plus a price+TRI combined file.

### Common commands

```bash
# Full history of NIFTY 50 and NIFTY 500 (price + TRI)
python3 nse_index_downloader.py --out-dir nse_index_data

# More cautious and unattended: auto-retry anything that fails
python3 nse_index_downloader.py --out-dir nse_index_data --min-wait 8 --max-wait 20 --yes

# Resume an interrupted run — just repeat the command. Or retry only failures:
python3 nse_index_downloader.py --out-dir nse_index_data --retry-only

# Exact trading-day verification with your own holiday list
python3 nse_index_downloader.py --out-dir nse_index_data --holidays-csv nse_holidays.csv

# Show progress and failures without touching the network
python3 nse_index_downloader.py --out-dir nse_index_data --status

# Accept a chunk you inspected and judged to be genuinely as published
python3 nse_index_downloader.py --out-dir nse_index_data --waive-sanity NIFTY_500.tri.2004Q3
```

`--indices`, `--series`, `--end-date` and `--out-dir` are the arguments you will change
most often; `--min-wait` / `--max-wait` / `--long-pause-every` control how hard the script
leans on NSE's servers.

### Sanity checks (per chunk — any failure ⇒ `SANITY_FAILED`, data kept for review)

- **Structure** — non-empty; every date parses; no duplicate dates; no rows outside the
  chunk window; `CLOSE` (price) / `TRI` present and > 0.
- **Coverage**, strongest check first:
  1. `--holidays-csv` — expected days = Mon–Fri minus listed holidays, and *any* missing
     day fails the chunk. This is the only exact check.
  2. **Cross-series reconciliation** — all series of the same quarter trade on the same
     days, so a date present in the majority of other series but missing here fails the
     chunk. Needs ≥ 2 series.
  3. **Heuristics** — more than `--max-missing-weekdays-pct` % of weekdays missing, a
     boundary further than `--boundary-tolerance-days` from the window edge, or a gap
     larger than `--max-gap-days`.
- **Warnings** (do not fail a chunk) — weekend rows (special sessions), rows on a listed
  holiday (Muhurat trading), blank `OPEN`/`HIGH`/`LOW`/`NTR` columns, `HIGH < LOW`.

### Chunk statuses

| Status | Meaning |
|---|---|
| `PENDING` | Not attempted yet (or reset by `--force` / a changed window) |
| `OK` | Downloaded and passed every check |
| `SANITY_FAILED` | Downloaded, but a check failed. CSV kept, reasons recorded |
| `ABANDONED` | Still failing after all retries; no data for this chunk |
| `WAIVED` | A `SANITY_FAILED` chunk you accepted via `--waive-sanity` |

Only `OK` and `WAIVED` chunks are merged.

### Output layout (under `--out-dir`)

```
state.json               machine-readable state; enables resume
chunk_status.csv         one row per chunk: status, rows, issues, errors
attempt_ledger.csv       every attempt and sanity outcome, timestamped
logs/run_<time>.log      full DEBUG log of each run
chunks/<INDEX>/<series>/ one CSV per chunk
raw/<INDEX>/<series>/    raw JSON exactly as received (audit trail)
merged/<INDEX>_price.csv, <INDEX>_tri.csv, <INDEX>_price_and_tri.csv
                         built from OK/WAIVED chunks only; named *_PARTIAL.csv
                         while any chunk of that index is still unresolved
```

### Exit codes

`0` every chunk OK/WAIVED · `1` some chunks failed, abandoned or pending ·
`2` fatal error (state saved) · `130` interrupted with Ctrl+C (state saved)

### Data source and honest limits

The script calls the same back end the "Historical Data" page uses in a browser:

```
POST <base>/BackPage/getHistoricaldatatabletoString   (price: OHLC)
POST <base>/BackPage/getTotalReturnIndexString        (TRI and Net TRI)
```

Both return a bare JSON array of records (older builds wrapped them in `{"d": "<json
string>"}`; both layouts are accepted). The site allows at most one year per request,
which is why the chunks are three months wide. Endpoints as of the Aug-2025 site rewrite —
the older `/Backpage.aspx/...` paths now redirect to a login page.

These are **not** a documented public API. NSE may change or restrict them. If the script
reports an unrecognised layout, open the page's Network tab and adapt `normalise()`. Check
the site's terms of use before bulk downloading, keep the waits generous, and do not run
several copies in parallel.

**Runtime:** roughly 450 requests for two indices × two series; at the default 4–9 s waits
plus long pauses, expect about an hour.

**Honest limitation on coverage:** no free machine-readable calendar of NSE trading days
back to the 1990s exists, so without `--holidays-csv` the coverage checks are strong
evidence of completeness, not proof — a day missing from *all* series at the source would
pass. Supply `--holidays-csv` (one date per line, or a `date` column) for exact
verification.

---

## 2. `nse_fx_decorator.py`

Adds an official USD/INR rate to every date of one or more time-series CSVs. For each
input, a **new** file is written with four extra columns, so nothing about the decoration
is implicit:

| Column | Meaning |
|---|---|
| `usdinr` | The USD/INR rate applied to that row |
| `fx_date` | The date the rate was actually published for (`== date` when exact) |
| `fx_source` | `rbi` \| `fbil` \| `h10` |
| `fx_status` | `exact` \| `carried_forward` \| `missing` |

### Sources — all official, never an aggregator

| Key | Source |
|---|---|
| `rbi` | RBI reference rate, from RBI's own Reference Rate Archive. Published every business day from 25-Aug-1998 to 09-Jul-2018, and again from 2023 onward. RBI's archive currently returns an **empty table for 2019–2022** and for 10-Jul-2018..2018-12-31; those dates are filled from FBIL, which was the publisher for that era anyway. Every such window is reported explicitly. |
| `fbil` | FBIL USD/INR reference rate, straight from the benchmark administrator, published 13:30 IST on business days from 10-Jul-2018 onward. |
| `h10` | US Federal Reserve H.10 "noon buying rate" for India (series `DEXINUS`, via FRED). Covers 1973-01-02 to the present; blank on US holidays. |

`--source ORDER` sets priority (default `auto` = `rbi fbil h10`). A date is taken from the
first source that publishes it, and `fx_source` always records which one. With `auto`, a
source is only queried for dates earlier sources did not cover — so the default run is a
single pass over RBI.

Note the differing User-Agent handling: RBI's ASP.NET front end answers HTTP 502 to
unfamiliar User-Agents on POST, so the two Indian sources send a browser string. FRED is
the opposite — it holds browser-style User-Agents from non-browser clients — so the Fed
source sends no User-Agent override at all.

### Fill rule

A date with no published rate is filled with the last rate used for the previous date
(carry-forward), marked `fx_status=carried_forward`, with `fx_date` naming the observation
it came from. Dates before the first published rate of every source cannot be filled at
all: `fx_status=missing`, left blank, reported as **ERRORS**. Carry-forwards older than
`--stale-warn-days` are **WARNINGS**.

### Common commands

```bash
# Decorate both merged NSE files, writing *_fx.csv next to each input
python3 nse_fx_decorator.py nse_index_data/merged/NIFTY_500_price_and_tri.csv \
                            nse_index_data/merged/NIFTY_50_price_and_tri.csv

# Write elsewhere, and use RBI's own series only (no fallback sources)
python3 nse_fx_decorator.py merged/*.csv --out-dir decorated --source rbi

# Cross-check the three official sources against each other
python3 nse_fx_decorator.py merged/NIFTY_50_price_and_tri.csv --cross-check

# Re-run later (uses the cache) / re-download everything / stay offline
python3 nse_fx_decorator.py merged/*.csv
python3 nse_fx_decorator.py merged/*.csv --refresh
python3 nse_fx_decorator.py merged/*.csv --offline

# Status only — no network, no writing
python3 nse_fx_decorator.py --status

# Accept a window that failed a sanity check (ids from chunk_status.csv)
python3 nse_fx_decorator.py merged/*.csv --waive-sanity rbi.2019
```

### Auditing — every failure is written down and reported

| File | Contents |
|---|---|
| `chunk_status.csv` | One row per downloaded window: status, rows, issues, warnings, last error. Statuses: `OK`, `EMPTY` (source does not cover that window), `SANITY_FAILED` (data kept but **not used** unless waived), `ABANDONED`, `PENDING` |
| `attempt_ledger.csv` | Every HTTP attempt, its outcome and its error message |
| `fx_audit.csv` | One row per event: unfillable dates (ERROR), carry-forwards and stale carry-forwards (WARNING), unparseable/duplicate input dates, implausible rates, cross-check deviations, per-file failures |
| console | Final SUMMARY block: abandoned windows, every failure, and the first `--report-limit` warnings — the complete list is always in `fx_audit.csv` |

Raw responses and parsed tables are cached under `<work-dir>/cache/`, so re-runs,
`--offline` runs and `--status` never touch the network.

### Output files

```
<--out-dir, or the input's own directory>/<name><--suffix>.csv   decorated data
<work-dir>/{state.json, chunk_status.csv, attempt_ledger.csv,
            fx_audit.csv, cache/, logs/run_*.log}                run bookkeeping
```

### Exit codes

`0` every row decorated, no abandoned window · `1` some rows unfillable / abandoned
windows / an input file failed · `2` fatal error · `130` Ctrl+C

**Runtime:** roughly 3–5 minutes for a 27-year RBI history — one request per year plus a
single page fetch, spaced 2–7 s apart on purpose. Re-runs reuse the cache and take seconds.

---

## 3. `nse_rolling_cagr.py`

Computes the rolling N-year CAGR of an NSE index total-return series, in INR and USD. One
output row per input row:

| Column | Meaning |
|---|---|
| `date` | The observation date |
| `tri_inr` | TRI as published in the input (INR) |
| `tri_usd` | `tri_inr / usdinr` for that row (USD) |
| `usdinr` | The USD/INR rate the input carries for that row |
| `cagr_inr_pct` | Annualised TRI growth over the trailing window, INR, percent |
| `cagr_usd_pct` | Annualised TRI growth over the trailing window, USD, percent |

Rows whose window does not yet fit inside the file keep their TRI values and get blank
CAGR cells — so the output holds exactly one row per input row, and no row ever uses a
later value.

### Window convention

The window is **trailing**: the CAGR on a row is the growth over the N years *ending* on
that row's date. The window starts on the anniversary date (row date minus N calendar
years); when that day is not a trading day, the start comes from `--anchor`:

- `on_or_before` — last trading day on or before the anniversary (default: window is N
  years or slightly longer)
- `on_or_after` — first trading day on or after it (N years or slightly shorter)
- `nearest` — whichever is closer; ties go to the earlier day

`--years-mode` then decides the exponent:

- `actual` — the real elapsed span in years (`days / 365.2425`); the honest annualised
  rate for the window actually used (**default**)
- `nominal` — exactly N, whatever the anchor slipped to; the "N-year CAGR" of common
  practice, comparable across dates but slightly off when the anniversary is a holiday

29-Feb anniversaries land on 28 Feb in non-leap years. Whichever anchor mode is used, the
start found must lie within `--max-slip-days` (default 10) of the anniversary — the NSE
calendar never shuts for longer than a long weekend, so anything further away is missing
coverage, not a holiday. Those rows are reported as having **no history** rather than
being handed a window of the wrong length. This is what stops the earliest rows of a file
being given the file's own first row as their "N-year" start.

Formula, applied twice per row (once on `tri_inr`, once on `tri_usd`, same window dates):

```
CAGR = (end_value / start_value) ** (1 / years) - 1
```

### Currency

`tri_inr` is the published INR TRI. `tri_usd = tri_inr / usdinr`, using the official rate
the input file already carries for that same date — so the USD series is what an unhedged
USD investor in the index would have seen, and both CAGRs share the same start and end
dates. The rate is repeated in the output so every row is self-contained.

### Header block — the file explains itself to a program

The output starts with a comment block; every line begins with `#`, so a CSV reader only
has to skip those lines. It carries two kinds of line:

```
# key: value                                file-level metadata
# column: name | unit | description         one per column, in order
```

Metadata covers the generator and version, timestamp, source file and which of its columns
were used, row count, how many rows have a CAGR and how many have no full window, the
window definition (length, trailing direction, anchor, exponent), the CAGR unit and
formula, what a blank cell means, and the rounding. The column lines give each column's
name exactly as it appears in the CSV header, its unit, and what it holds — so a program
can map and scale columns from the block alone, with no human or hard-coded schema.

Format is stable: `format_version 1`, `key: value` with one space after the colon, column
lines split on ` | `.

```python
# pandas
df = pd.read_csv(path, comment='#')

# stdlib csv
rows = [r for r in csv.reader(fh) if r and not r[0].startswith('#')]
```

A parser that cannot skip comments at all: `--header-block off` gives a bare CSV.

### Common commands

```bash
# Rolling 5-year CAGR of NIFTY 50, written next to the input as *_cagr_5y.csv
python3 nse_rolling_cagr.py -w 5 nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv

# Both indices, 10 years, into a separate directory
python3 nse_rolling_cagr.py -w 10 --out-dir cagr_out \
        nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv \
        nse_index_data/merged/NIFTY_500_price_and_tri_fx.csv

# Fractional output, nominal exponent, nearest trading day
python3 nse_rolling_cagr.py -w 3 --cagr-unit fraction --years-mode nominal \
        --anchor nearest nse_index_data/merged/NIFTY_500_price_and_tri_fx.csv

# Show the arithmetic behind specific dates (accepts 'latest')
python3 nse_rolling_cagr.py -w 5 --explain latest --explain 2026-01-01 \
        nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv

# Bare CSV (no comment block)
python3 nse_rolling_cagr.py -w 5 --header-block off \
        nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv
```

`--explain DATE` is the fastest way to check a surprising number: it prints the start row,
the end row and the arithmetic behind that row's CAGR.

### Input layout

Any CSV with a date column, a TRI column and a USD/INR rate column. The merged files
produced by `nse_fx_decorator.py` match out of the box:

```
date,open,high,low,close,tri,ntr,usdinr,fx_date,fx_source,fx_status
```

### Auditing

Rows with a missing or unparseable date, TRI or rate are dropped and written to the audit
CSV; duplicate dates keep the first occurrence; an input not in date order is sorted (and
noted). The audit CSV is written **only** when there is something to record, and is named
`<output>_audit.csv` or `--audit PATH`. Rows with no full window behind them are counted
in the console report (NO HIST line) rather than audited one by one — at the start of a
file there are hundreds of them, and they are expected.

### Exit codes

`0` every usable row processed, nothing dropped · `1` rows dropped or a file failed ·
`2` fatal error (no usable input) · `130` Ctrl+C

---

## 4. `build_dashboard.py`

Builds the interactive dashboard HTML for a rolling-CAGR file:

```bash
python3 build_dashboard.py --data merged/NIFTY_500_price_and_tri_fx_cagr_7y.csv
python3 build_dashboard.py --data merged/NIFTY_50_price_and_tri_fx_cagr_4y.csv
python3 build_dashboard.py --root NIFTY_50 --window 7          # shorthand
python3 build_dashboard.py --data <file> --window 10 --inline  # explicit window, offline page
```

- `--data` is the CAGR csv to plot; the window width comes from the file name
  (`_cagr_<N>y`) unless `--window` says otherwise.
- `--root` is shorthand for `merged/<root>_price_and_tri_fx_cagr_<window>y.csv`.
- `--inline` downloads and embeds Plotly and the Inter font, producing a page that works
  offline and can be shared as a single file (output name gains a `_share` suffix).
  `--plotly cartesian` (the default) embeds a bundle ~2 MB smaller that still carries
  every feature the page uses.

Output: `dashboard/<stem>_<window>y_cagr_dashboard[_share].html` — a self-contained page
apart from the Plotly and Google Fonts CDN includes (none at all with `--inline`), which
opens straight from the file system.

**Every number the commentary quotes is measured from the input file at build time**, so
the page cannot drift from the data.

Note: `--data` takes any path and works from anywhere. The `--root` shorthand and the
default output directory assume the `~/Documents/nse_index_data/` layout
(`merged/` and `dashboard/` subdirectories), so pass `--data` and `--out` explicitly if
your files live elsewhere.

---

## Repository layout

```
nse_index_downloader.py   stage 1: fetch price + TRI history
nse_fx_decorator.py       stage 2: add official USD/INR rates
nse_rolling_cagr.py       stage 3: rolling N-year CAGR, INR and USD
build_dashboard.py        stage 4: interactive HTML dashboard
README.md                 this file
```

The scripts are independent files, not a package — there is no installer and no imports
between them. Copy the four files anywhere and run them with `python3`.

---

## Troubleshooting

**The downloader reports an unfamiliar response layout.** NSE changed the endpoints once
already (Aug-2025 rewrite). Open the Historical Data page's Network tab, compare the
request payload, and adapt `normalise()` in `nse_index_downloader.py`.

**A chunk is stuck on `SANITY_FAILED`.** Open its CSV under `chunks/` and look. If the
source really is incomplete for that window, accept it with `--waive-sanity <CHUNK_ID>`
(ids are in `chunk_status.csv`, and look like `NIFTY_50.price.2010Q1`). Only OK/WAIVED
chunks are merged.

**A run was interrupted.** Just repeat the same command — `state.json` resumes it. Nothing
is re-downloaded.

**FX run reports unfillable dates.** Those dates precede the first published rate of every
source (`fx_status=missing`). Check the date range against each source's coverage in the
sources table above.

**Everything was slow.** That is intentional. Both NSE and RBI are third-party sites not
built for bulk traffic; the waits are the price of not being blocked.

---

## Data provenance

- **Index history** — NSE Indices Ltd (niftyindices.com), the same back end the public
  Historical Data page uses.
- **USD/INR** — Reserve Bank of India reference rate, FBIL reference rate, and the US
  Federal Reserve H.10 noon buying rate (series `DEXINUS` via FRED). All three are the
  publishing institutions themselves; no aggregator or scraped rate site is ever used.

Add your own verification to the chain if any number matters commercially — the audit
files exist precisely so that a number can be traced back to the request that produced it.
