#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# MIT License
#
# Copyright (c) 2026 Suvamoy Sen <suvamoy.sen@pm.me>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
nse_rolling_cagr.py -- rolling N-year CAGR of an NSE index TRI series, in INR and USD.

Reads a merged NSE index CSV that already carries an official USD/INR column (the
output of nse_fx_decorator.py, e.g. merged/NIFTY_50_price_and_tri_fx.csv) and writes
a NEW csv holding, for every date, the trailing N-year compound annual growth rate of
the total-return index (TRI) measured both in INR and in USD:

  date            the observation date
  tri_inr         the TRI as published (INR)
  tri_usd         the same TRI converted at that row's rate (tri_inr / usdinr)
  usdinr          the official USD/INR rate the input carries for that date
  cagr_inr_pct    annualised TRI growth over the trailing N-year window, in INR
  cagr_usd_pct    annualised TRI growth over the trailing N-year window, in USD

The window is TRAILING: the CAGR printed on a row measures the N years ENDING on that
date, so no row uses information from the future.  Rows that do not yet have a full
N-year history (the start of the file) keep their TRI values and get blank CAGR cells.

The file opens with a '#'-prefixed comment block that states the window rules and
describes every column (format_version 1: `key: value` lines plus one
`column: name | unit | description` line per column) -- a program can skip the '#' lines
and interpret the rest with no human help.  --header-block off writes a bare CSV.

Run with --help for the window, anchor and unit conventions.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "1.1.0"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
MIN_WINDOW, MAX_WINDOW = 1, 10
# Gregorian mean year length: used only when the window is annualised over the ACTUAL
# elapsed span (which is N years plus or minus the trading-calendar slip).
DAYS_PER_YEAR = 365.2425

DEFAULT_TRI_COL = "tri"
DEFAULT_FX_COL = "usdinr"
DEFAULT_DATE_COL = "date"

# How the window's start is located when the anniversary date is not a trading day.
ON_OR_BEFORE, ON_OR_AFTER, NEAREST = "on_or_before", "on_or_after", "nearest"
ANCHOR_MODES = (ON_OR_BEFORE, ON_OR_AFTER, NEAREST)

# What goes in the exponent.
ACTUAL, NOMINAL = "actual", "nominal"      # actual elapsed years | exactly N
YEARS_MODES = (ACTUAL, NOMINAL)

# How the CAGR itself is printed.
PCT, FRACTION = "pct", "fraction"

# How the output file is introduced: a readable comment block, or nothing but the CSV.
HEADER_MULTILINE, HEADER_OFF = "multiline", "off"
HEADER_MODES = (HEADER_MULTILINE, HEADER_OFF)

# Why a row has no CAGR (reported as counts, not errors).
NO_BEFORE = "window start is before the file's first row"
NO_SLIP = "nearest trading day is too far from the window start"
ANCHOR_BAD = "anchor row has no usable TRI"
EMPTY_WINDOW = "anchor row is the row itself (zero-length window)"
NO_HISTORY = "no full window behind this row"
# The NSE calendar shuts for a long weekend at most (5-6 days in the merged files), so
# a window start further than this from the anniversary means the file does not cover
# the window -- it is never a holiday in the way.  Without this guard an anchor mode
# that may look forward would hand an early row the file's first row and print a
# multi-year CAGR measured over a few days.
DEFAULT_MAX_SLIP_DAYS = 10

AUDIT_COLS = ["severity", "file", "row", "date", "message"]

# date patterns: ISO first, then "10 Mar 2026", then day-first "10/03/2026"
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_TEXT_RE = re.compile(r"^\s*(\d{1,2})[\s\-/,.]+([A-Za-z]{3,9})[\s\-/,.]+(\d{4})")
_NUM_RE = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_LOOKUP = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class FatalError(Exception):
    """Unrecoverable problem with one input file; that file is skipped."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def parse_any_date(value) -> Optional[date]:
    """'2026-09-28' | '28 Sep 2026' | '28/09/2026' (day-first, Indian style)."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("nan", "nat", "none", "-"):
        return None
    try:
        m = _ISO_RE.match(s)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = _TEXT_RE.match(s)
        if m:
            mon = _MONTH_LOOKUP.get(m[2][:3].lower())
            return date(int(m[3]), mon, int(m[1])) if mon else None
        m = _NUM_RE.match(s)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def to_num(value) -> Optional[float]:
    """'1688.44' -> 1688.44; blanks, dashes and junk -> None."""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None", "null", "&nbsp;"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def shift_years(d: date, delta: int) -> date:
    """The same calendar date `delta` years away; 29 Feb lands on 28 Feb."""
    try:
        return d.replace(year=d.year + delta)
    except ValueError:                      # 29 Feb in a non-leap target year
        return d.replace(year=d.year + delta, day=28)


def find_anchor(dates: Sequence[date], target: date, mode: str,
                max_slip_days: int) -> Tuple[Optional[int], str]:
    """The observation that starts the window whose anniversary is `target`.

    `target` is the anniversary date (the row's date minus N calendar years).  Trading
    calendars have holidays there, so the start is taken from the trading day on/before
    it, on/after it, or nearest to it -- --anchor picks which.  The start must also lie
    within `max_slip_days` of the anniversary: anything further away means the file does
    not cover the window's start (or has a hole), and accepting it would silently turn
    an N-year window into a much shorter or longer one.  Returns (index, reason); the
    index is None when there is no usable start.
    """
    pos = bisect.bisect_left(dates, target)
    if pos < len(dates) and dates[pos] == target:   # exact anniversary is a trading day
        return pos, ""
    before = pos - 1 if pos > 0 else None
    after = pos if pos < len(dates) else None
    if mode == ON_OR_BEFORE:
        j = before
    elif mode == ON_OR_AFTER:
        j = after
    elif before is None:
        j = after
    elif after is None:
        j = before
    else:
        j = before if (target - dates[before]) <= (dates[after] - target) else after
    if j is None:
        return None, NO_BEFORE
    if abs((dates[j] - target).days) > max_slip_days:
        return None, NO_SLIP
    return j, ""


def fmt_cagr(value: Optional[float], unit: str, precision: int) -> str:
    """Blank for None, otherwise a fixed-precision number (percent or fraction)."""
    if value is None:
        return ""
    if unit == PCT:
        return f"{value * 100.0:.{precision}f}"
    return f"{value:.{precision}f}"


# --------------------------------------------------------------------------- #
# Input / window model
# --------------------------------------------------------------------------- #
@dataclass
class Observation:
    """One usable row of the input: a date with a TRI in INR and the rate to convert it."""
    date: date
    tri_inr: float
    tri_usd: float
    fx: float
    row_no: int


@dataclass
class Window:
    """How a trailing window is located and how its growth is annualised."""
    n_years: int
    anchor_mode: str
    years_mode: str
    max_slip_days: int = DEFAULT_MAX_SLIP_DAYS

    def start_of(self, dates: Sequence[date], i: int) -> Tuple[Optional[int], str]:
        """Start row of the window ending at row `i`, and why there is none if so."""
        target = shift_years(dates[i], -self.n_years)
        j, why = find_anchor(dates, target, self.anchor_mode, self.max_slip_days)
        if j is None:
            return None, why
        if j >= i:                          # only reachable with duplicate dates
            return None, NO_BEFORE
        return j, ""

    def years(self, span_days: int) -> float:
        """Exponent: the actual elapsed span, or exactly N when --years-mode nominal."""
        if self.years_mode == NOMINAL:
            return float(self.n_years)
        return span_days / DAYS_PER_YEAR


def cagr(start_value: float, end_value: float, years: float) -> Optional[float]:
    """(end/start)^(1/years) - 1; None when the inputs cannot produce a growth rate."""
    if years <= 0 or start_value <= 0 or end_value <= 0:
        return None
    return (end_value / start_value) ** (1.0 / years) - 1.0


@dataclass
class CagrCell:
    """The window behind one row's CAGR -- kept so --explain can show its working."""
    anchor_idx: int
    years: float
    cagr_inr: Optional[float]
    cagr_usd: Optional[float]
    reason: str = ""                    # set when either CAGR is None


# --------------------------------------------------------------------------- #
# Reading the input
# --------------------------------------------------------------------------- #
@dataclass
class InputFile:
    path: Path
    header: List[str]
    rows: List[List[str]]
    date_idx: int = -1
    tri_idx: int = -1
    fx_idx: int = -1


def find_column(header: List[str], wanted: str, what: str) -> int:
    """Exact (case-insensitive) match first, then a 'contains' match, then fail."""
    for i, name in enumerate(header):
        if name.strip().lower() == wanted.lower():
            return i
    for i, name in enumerate(header):
        if wanted.lower() in name.lower():
            return i
    raise FatalError(f"no {what} column {wanted!r} in this file "
                     f"(columns: {', '.join(header)})")


def read_input(path: Path, a, log) -> InputFile:
    if not path.exists():
        raise FatalError(f"{path}: no such file")
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        try:
            header = next(rd)
        except StopIteration:
            raise FatalError(f"{path}: file is empty")
        rows = [r for r in rd if r]
    if not rows:
        raise FatalError(f"{path}: header only, no data rows")
    date_idx = find_column(header, a.date_col, "date")
    tri_idx = find_column(header, a.tri_col, "TRI")
    fx_idx = find_column(header, a.fx_col, "USD/INR")
    log.info("%s: %d data row(s); date=%r tri=%r fx=%r", path.name, len(rows),
             header[date_idx], header[tri_idx], header[fx_idx])
    return InputFile(path=path, header=header, rows=rows,
                     date_idx=date_idx, tri_idx=tri_idx, fx_idx=fx_idx)


def build_series(inp: InputFile, path_name: str) -> Tuple[List[Observation], List[dict]]:
    """Parse the usable rows, sorted by date; every rejected row is audited."""
    events: List[dict] = []
    parsed: List[Tuple[date, float, float, int]] = []
    bad_date = bad_tri = bad_fx = 0
    for n, row in enumerate(inp.rows, start=2):        # line 1 is the header
        d = parse_any_date(row[inp.date_idx]) if inp.date_idx < len(row) else None
        if d is None:
            bad_date += 1
            events.append({"severity": "ERROR", "file": path_name, "row": n, "date": "",
                           "message": "date is missing or unparseable -- row dropped"})
            continue
        tri = to_num(row[inp.tri_idx]) if inp.tri_idx < len(row) else None
        if tri is None or tri <= 0:
            bad_tri += 1
            events.append({"severity": "ERROR", "file": path_name, "row": n,
                           "date": d.isoformat(),
                           "message": f"no usable TRI value ({row[inp.tri_idx]!r}) -- row dropped"})
            continue
        fx = to_num(row[inp.fx_idx]) if inp.fx_idx < len(row) else None
        if fx is None or fx <= 0:
            bad_fx += 1
            events.append({"severity": "ERROR", "file": path_name, "row": n,
                           "date": d.isoformat(),
                           "message": f"no usable USD/INR rate ({row[inp.fx_idx]!r}) -- row dropped"})
            continue
        parsed.append((d, tri, tri / fx, fx, n))

    unsorted = sum(1 for x, y in zip(parsed, parsed[1:]) if y[0] < x[0])
    parsed.sort(key=lambda t: t[0])
    obs, seen, dupes = [], set(), 0
    for d, tri, tri_usd, fx, n in parsed:
        if d in seen:
            dupes += 1
            events.append({"severity": "WARNING", "file": path_name, "row": n,
                           "date": d.isoformat(),
                           "message": "duplicate date -- the first occurrence is kept"})
            continue
        seen.add(d)
        obs.append(Observation(date=d, tri_inr=tri, tri_usd=tri_usd, fx=fx, row_no=n))
    if unsorted:
        events.append({"severity": "WARNING", "file": path_name, "row": "", "date": "",
                       "message": f"input is not in date order ({unsorted} step(s) go "
                                  f"backwards); rows were sorted before computing"})
    skipped = bad_date + bad_tri + bad_fx
    if skipped:
        events.append({"severity": "INFO", "file": path_name, "row": "", "date": "",
                       "message": f"{skipped} row(s) dropped: {bad_date} bad date, "
                                  f"{bad_tri} bad TRI, {bad_fx} bad rate"})
    return obs, events


# --------------------------------------------------------------------------- #
# The rolling calculation
# --------------------------------------------------------------------------- #
def compute_cells(obs: List[Observation], w: Window, events: List[dict],
                  path_name: str) -> Tuple[List[Optional[CagrCell]], Dict[str, int]]:
    """One CagrCell per observation, plus a tally of why the others have none."""
    dates = [o.date for o in obs]
    cells: List[Optional[CagrCell]] = []
    reasons: Dict[str, int] = {}
    for i, row in enumerate(obs):
        j, why = w.start_of(dates, i)
        if j is None:
            cells.append(None)
            reasons[why] = reasons.get(why, 0) + 1
            continue
        years = w.years((row.date - obs[j].date).days)
        g_inr = cagr(obs[j].tri_inr, row.tri_inr, years)
        g_usd = cagr(obs[j].tri_usd, row.tri_usd, years)
        cell = CagrCell(anchor_idx=j, years=years, cagr_inr=g_inr, cagr_usd=g_usd)
        if g_inr is None or g_usd is None:
            cell.reason = ANCHOR_BAD if obs[j].tri_inr <= 0 else EMPTY_WINDOW
            reasons[cell.reason] = reasons.get(cell.reason, 0) + 1
            events.append({"severity": "WARNING", "file": path_name, "row": row.row_no,
                           "date": row.date.isoformat(),
                           "message": f"no CAGR here: {cell.reason} "
                                      f"(window {obs[j].date.isoformat()} .. "
                                      f"{row.date.isoformat()}, {years:.2f} y)"})
        cells.append(cell)
    return cells, reasons


def count_missing(cells: Sequence[Optional[CagrCell]]) -> Dict[str, int]:
    out = {"no_history": 0, "computed": 0, "flagged": 0}
    for c in cells:
        if c is None:
            out["no_history"] += 1
        elif c.cagr_inr is None or c.cagr_usd is None:
            out["flagged"] += 1
        else:
            out["computed"] += 1
    return out


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, a, log: logging.Logger, inputs: List[InputFile]):
        self.a = a
        self.log = log
        self.inputs = inputs
        self.window = Window(n_years=a.window_years, anchor_mode=a.anchor,
                             years_mode=a.years_mode, max_slip_days=a.max_slip_days)
        self.cagr_cols = (f"cagr_inr{'_pct' if a.cagr_unit == PCT else ''}",
                          f"cagr_usd{'_pct' if a.cagr_unit == PCT else ''}")

    # ---- paths ------------------------------------------------------------
    def suffix(self) -> str:
        return self.a.suffix if self.a.suffix is not None else f"_cagr_{self.a.window_years}y"

    def output_path(self, inp: InputFile) -> Path:
        base = Path(self.a.out_dir) if self.a.out_dir else inp.path.parent
        return base / f"{inp.path.stem}{self.suffix()}{inp.path.suffix or '.csv'}"

    def audit_path(self, inp: InputFile) -> Path:
        if self.a.audit:
            return Path(self.a.audit)
        out = self.output_path(inp)
        return out.with_name(f"{out.stem}_audit.csv")

    # ---- per file ---------------------------------------------------------
    def process(self, inp: InputFile) -> int:
        out = self.output_path(inp)
        if out.resolve() == inp.path.resolve():
            raise FatalError(f"refusing to overwrite the input file {inp.path} "
                             f"(change --out-dir or --suffix)")
        obs, events = build_series(inp, inp.path.name)
        if not obs:
            raise FatalError(f"{inp.path}: no usable row (every row was dropped)")
        cells, reasons = compute_cells(obs, self.window, events, inp.path.name)
        stats = count_missing(cells)
        self.decorate(inp, obs, cells, stats, out)
        if self.a.explain:
            self.explain(obs, cells, inp)
        audit = self.write_audit(inp, events)
        self.log.info("%s -> %s", inp.path.name, out)
        self.report(inp, obs, cells, stats, reasons, out, audit)
        hard = [e for e in events if e["severity"] == "ERROR"]
        return 1 if hard else 0

    # ---- the comment header a program can read -----------------------------
    def header_block(self, inp: InputFile, obs: List[Observation],
                     stats: Dict[str, int]) -> List[str]:
        """A machine-readable preamble describing this file, its columns and its rules.

        Every line starts with '#'; the metadata lines are `key: value` and the column
        lines are `column: name | unit | description`, in the order the columns appear.
        A reader needs no human help: skip the '#' lines, then read the CSV header.
        """
        a, w = self.a, self.window
        unit = ("percent per year" if a.cagr_unit == PCT else "fraction per year")
        exponent = ("actual elapsed years (window days / %.4f)" % DAYS_PER_YEAR
                    if w.years_mode == ACTUAL else f"exactly {w.n_years} year(s)")
        start = (f"{w.anchor_mode} -- the last trading day at or before the anniversary "
                 f"date" if w.anchor_mode == ON_OR_BEFORE else
                 f"{w.anchor_mode} -- see nse_rolling_cagr.py --help")
        cols = [
            ("date", "YYYY-MM-DD (ISO 8601)", "observation date of the row"),
            ("tri_inr", "index points, INR",
             f"total return index as published in the source file, rounded to {a.precision} dp"),
            ("tri_usd", "index points, USD",
             f"tri_inr / {a.fx_col} for the same date, rounded to {a.precision} dp"),
            (a.fx_col, "INR per USD",
             f"the official USD/INR rate the source file carries for this date, "
             f"rounded to {a.precision} dp"),
            (self.cagr_cols[0], unit,
             f"annualised growth of tri_inr over the window, rounded to "
             f"{a.cagr_precision} dp; blank when the row has no full window"),
            (self.cagr_cols[1], unit,
             f"annualised growth of tri_usd over the same window dates, rounded to "
             f"{a.cagr_precision} dp; blank when the row has no full window"),
        ]
        lines = [
            "# nse_rolling_cagr -- rolling N-year CAGR of an NSE index TRI series, INR and USD",
            "# format_version: 1",
            f"# generator: nse_rolling_cagr.py {__version__}",
            f"# generated: {datetime.now().astimezone().isoformat(timespec='seconds')}",
            f"# source_file: {inp.path.resolve()}",
            f"# source_columns: date <- '{inp.header[inp.date_idx]}', tri_inr <- "
            f"'{inp.header[inp.tri_idx]}', {a.fx_col} <- '{inp.header[inp.fx_idx]}'",
            f"# rows: {len(obs)} (one per source observation, ascending by date, unique dates)",
            f"# rows_with_cagr: {stats['computed']}   rows_without_a_full_window: "
            f"{stats['no_history']}   rows_flagged: {stats['flagged']}",
            f"# window: {w.n_years} year(s), trailing -- each row's CAGR covers the "
            f"{w.n_years} year(s) ENDING on that row's date",
            f"# window_start: {start}; a start more than {w.max_slip_days} day(s) from the "
            f"anniversary counts as no window at all",
            f"# window_exponent: {exponent}",
            f"# cagr_unit: {unit}",
            "# cagr_formula: (end_value / start_value) ** (1 / years) - 1",
            "# blank_means: undefined -- the row has no full window behind it; never zero",
            f"# rounding: tri_inr, tri_usd and {a.fx_col} to {a.precision} dp; CAGR to "
            f"{a.cagr_precision} dp; the CAGRs are computed from the unrounded values",
            "# parsing: ignore every line whose first character is '#', then read the CSV "
            "header (python csv: filter the rows; pandas: read_csv(path, comment='#'))",
            "# column_format: column: name | unit | description   (one line per column, "
            "in the order they appear below)",
        ]
        lines += [f"# column: {n} | {u} | {d}" for n, u, d in cols]
        return lines

    def decorate(self, inp: InputFile, obs: List[Observation],
                 cells: List[Optional[CagrCell]], stats: Dict[str, int],
                 out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        vp, cp = self.a.precision, self.a.cagr_precision
        unit = self.a.cagr_unit
        tmp = out.with_name(out.name + ".part")       # never leave a half-written output
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            if self.a.header_block != HEADER_OFF:
                for line in self.header_block(inp, obs, stats):
                    fh.write(line + "\n")
            w = csv.writer(fh)
            w.writerow(["date", "tri_inr", "tri_usd", self.a.fx_col,
                        self.cagr_cols[0], self.cagr_cols[1]])
            for row, cell in zip(obs, cells):
                w.writerow([row.date.isoformat(), f"{row.tri_inr:.{vp}f}",
                            f"{row.tri_usd:.{vp}f}", f"{row.fx:.{vp}f}",
                            "" if cell is None else fmt_cagr(cell.cagr_inr, unit, cp),
                            "" if cell is None else fmt_cagr(cell.cagr_usd, unit, cp)])
        os.replace(tmp, out)

    def write_audit(self, inp: InputFile, events: List[dict]) -> Optional[Path]:
        """Written only when there is something to record, so clean runs stay clean."""
        if not events:
            return None
        path = self.audit_path(inp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=AUDIT_COLS)
            w.writeheader()
            for e in events:
                w.writerow(e)
        return path

    # ---- verification -----------------------------------------------------
    def explain(self, obs: List[Observation], cells: List[Optional[CagrCell]],
                inp: InputFile) -> None:
        """Print the arithmetic behind chosen dates, so a result can be checked by hand."""
        dates = [o.date for o in obs]
        for wanted in self.a.explain:
            if wanted.lower() == "latest":
                idx = len(obs) - 1
            else:
                d = parse_any_date(wanted)
                if d is None:
                    self.log.error("--explain %r: not a date", wanted)
                    continue
                pos = bisect.bisect_left(dates, d)
                if pos >= len(dates):
                    self.log.error("--explain %s: later than the last row (%s)",
                                   d.isoformat(), dates[-1].isoformat())
                    continue
                idx = pos
            row, cell = obs[idx], cells[idx]
            self.log.info("---- explain %s (%s) : window=%dy anchor=%s exponent=%s ----",
                          row.date.isoformat(), inp.path.name, self.window.n_years,
                          self.window.anchor_mode, self.window.years_mode)
            self.log.info("   end    row %d: date=%s tri_inr=%.4f tri_usd=%.4f",
                          row.row_no, row.date.isoformat(), row.tri_inr, row.tri_usd)
            if cell is None:
                self.log.info("   no window: %s", NO_HISTORY)
                continue
            j = cell.anchor_idx
            start = obs[j]
            self.log.info("   start  row %d: date=%s tri_inr=%.4f tri_usd=%.4f "
                          "(anniversary %s)", start.row_no, start.date.isoformat(),
                          start.tri_inr, start.tri_usd,
                          shift_years(row.date, -self.window.n_years).isoformat())
            self.log.info("   span   %d day(s) = %.6f year(s)",
                          (row.date - start.date).days, cell.years)
            self.log.info("   cagr   inr: (%.4f / %.4f)^(1/%.6f) - 1 = %s%%   "
                          "usd: (%.4f / %.4f)^(1/%.6f) - 1 = %s%%",
                          row.tri_inr, start.tri_inr, cell.years,
                          fmt_cagr(cell.cagr_inr, PCT, self.a.cagr_precision),
                          row.tri_usd, start.tri_usd, cell.years,
                          fmt_cagr(cell.cagr_usd, PCT, self.a.cagr_precision))

    def report(self, inp: InputFile, obs: List[Observation], cells: List[Optional[CagrCell]],
               stats: Dict[str, int], reasons: Dict[str, int], out: Path,
               audit: Optional[Path]) -> None:
        a, unit = self.a, self.a.cagr_unit
        scale = 100.0 if unit == PCT else 1.0
        tag = "%" if unit == PCT else ""
        inr = [c.cagr_inr * scale for c in cells if c and c.cagr_inr is not None]
        usd = [c.cagr_usd * scale for c in cells if c and c.cagr_usd is not None]
        self.log.info("=" * 72)
        self.log.info("SUMMARY  %s | window=%d year(s) trailing | anchor=%s | exponent=%s "
                      "| CAGR in %s", inp.path.name, self.window.n_years,
                      self.window.anchor_mode,
                      "actual elapsed years" if self.window.years_mode == ACTUAL else "exactly N",
                      "percent" if unit == PCT else "fraction")
        self.log.info("ROWS     %d usable | %d with a CAGR | %d without history "
                      "(file starts %s) | %d flagged",
                      len(obs), stats["computed"], stats["no_history"],
                      obs[0].date.isoformat(), stats["flagged"])
        if stats["no_history"]:
            self.log.info("NO HIST  %d row(s) have no window: %d start before the file "
                          "begins, %d have their nearest trading day more than %d day(s) "
                          "from the window start", stats["no_history"],
                          reasons.get(NO_BEFORE, 0), reasons.get(NO_SLIP, 0),
                          self.window.max_slip_days)
        for key in (ANCHOR_BAD, EMPTY_WINDOW):
            if reasons.get(key):
                self.log.warning("FLAGGED  %d row(s): %s", reasons[key], key)
        if inr:
            self.log.info("RANGE    cagr_inr %.*f%s .. %.*f%s (mean %.*f%s) | "
                          "cagr_usd %.*f%s .. %.*f%s (mean %.*f%s)",
                          a.cagr_precision, min(inr), tag, a.cagr_precision, max(inr), tag,
                          a.cagr_precision, sum(inr) / len(inr), tag,
                          a.cagr_precision, min(usd), tag, a.cagr_precision, max(usd), tag,
                          a.cagr_precision, sum(usd) / len(usd), tag)
        last = cells[-1]
        last_inr = fmt_cagr(last.cagr_inr if last else None, unit, a.cagr_precision)
        last_usd = fmt_cagr(last.cagr_usd if last else None, unit, a.cagr_precision)
        self.log.info("LATEST   %s: tri_inr=%s tri_usd=%s cagr_inr=%s cagr_usd=%s%s",
                      obs[-1].date.isoformat(), f"{obs[-1].tri_inr:.2f}",
                      f"{obs[-1].tri_usd:.2f}",
                      f"{last_inr}{tag}" if last_inr else "-",
                      f"{last_usd}{tag}" if last_usd else "-",
                      "" if last else f"  ({NO_HISTORY})")
        self.log.info("OUTPUT   %s", out)
        self.log.info("AUDIT    %s", audit or "none (nothing dropped or noted)")
        self.log.info("=" * 72)

    # ---- entry ------------------------------------------------------------
    def run(self) -> int:
        rc = 0
        for inp in self.inputs:
            try:
                rc |= self.process(inp)
            except FatalError as e:
                rc |= 1
                self.log.error("FAILED %s: %s", inp.path, e)
        return rc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
DESCRIPTION = """\
Compute the rolling N-year CAGR of an NSE index total-return series, in INR and USD.

For every date of the input file one output row is written:

  date            the observation date
  tri_inr         TRI as published in the input (INR)
  tri_usd         TRI / usdinr for that row (USD)
  usdinr          the USD/INR rate the input carries for that row
  cagr_inr_pct    annualised TRI growth over the trailing window, INR, in percent
  cagr_usd_pct    annualised TRI growth over the trailing window, USD, in percent

Rows whose window does not fit inside the file yet keep their TRI values and get
blank CAGR cells, so the output holds exactly one row per input row.

The file opens with a comment block (every line starting with '#') that describes the
file, the window and every column in machine-readable form -- `key: value` lines plus
one `column: name | unit | description` line per column, in order. Skip the '#' lines
and the rest is a plain CSV.
"""

EPILOG = """\
WINDOW CONVENTION
  The window is TRAILING: the CAGR on a row is the growth of the N years ENDING on
  that row's date, so no row ever uses a later value.  The window starts on the
  anniversary date (the row's date minus N calendar years); when that day is not a
  trading day the start is taken from --anchor:
    on_or_before  the last trading day on or before the anniversary (default: the
                  window is then N years or slightly longer)
    on_or_after   the first trading day on or after it (N years or slightly shorter)
    nearest       whichever is closer; ties go to the earlier day
  --years-mode then decides the exponent:
    actual        the real elapsed span in years (days / 365.2425) -- the honest
                  annualised rate for the window that was actually used (default)
    nominal       exactly N, whatever the anchor slipped to -- the "N-year CAGR" of
                  common practice, comparable across dates but slightly off when the
                  anniversary falls on a holiday
  Feb 29 anniversaries land on 28 Feb in non-leap years.

  Whichever anchor mode is used, the start found must lie within --max-slip-days
  (default 10) of the anniversary.  The NSE calendar never shuts for longer than a long
  weekend (5-6 days in the merged files), so anything further away is missing coverage,
  not a holiday: the row is then reported as having NO history rather than being given a
  window of the wrong length.  This is what stops a forward-looking anchor from handing
  the earliest rows of a file the file's own first row as their "N-year" start.

  CAGR formula: (end_value / start_value) ** (1 / years) - 1, applied twice per row:
  once on tri_inr and once on tri_usd (tri_inr / usdinr, same window dates for both).

CURRENCY
  tri_inr is the published INR TRI.  tri_usd = tri_inr / usdinr, using the official
  rate the input file already carries for that same date -- so the USD series is what
  an unhedged USD investor in the index would have seen, and both CAGRs share the
  same start and end dates.  That rate is repeated in the output (column named after
  --fx-col, default usdinr) so every row is self-contained.

HEADER BLOCK -- the file explains itself to a program
  The output starts with a comment block; every line begins with '#' so a CSV reader
  only has to skip those lines.  It carries two kinds of line:

    # key: value                                     file-level metadata
    # column: name | unit | description              one per column, in order

  Metadata covers the generator and version, the timestamp, the source file and which
  of its columns were used, the row count, how many rows have a CAGR and how many have
  no full window, the window definition (length, trailing direction, how its start is
  chosen, the exponent), the CAGR unit and formula, what a blank cell means, and the
  rounding.  The column lines give each column's name exactly as it appears in the CSV
  header, its unit, and what it holds -- so a program can map and scale columns from
  the block alone, without a human or a hard-coded schema.  The lines are stable:
  format_version 1, `key: value` with one space after the colon, column lines split on
  ' | ' into name, unit, description.

  Readers:
    pandas   pd.read_csv(path, comment='#')
    python   rows = [r for r in csv.reader(fh) if r and not r[0].startswith('#')]
  A parser that cannot skip comments at all: --header-block off gives a bare CSV.

AUDITING -- nothing is silently dropped
  Rows with a missing/unparseable date, TRI or rate are dropped and written to the
  audit CSV; duplicate dates keep the first occurrence; an input that is not in date
  order is sorted (and noted).  The audit CSV is written ONLY when there is something
  to record, and is named <output>_audit.csv or --audit PATH.  Rows that have no full
  window behind them are counted in the console report (NO HIST line) rather than
  audited one by one, because at the start of a file there are hundreds of them and
  they are expected.

EXIT CODES
  0  every usable row processed, nothing dropped      1  rows dropped / a file failed
  2  fatal error (no usable input)                  130  Ctrl+C

EXAMPLES
  # Rolling 5-year CAGR of NIFTY 50, written next to the input as *_cagr_5y.csv:
  python nse_rolling_cagr.py -w 5 nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv

  # Both indices, 10 years, into a separate directory:
  python nse_rolling_cagr.py -w 10 --out-dir cagr_out \\
         nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv \\
         nse_index_data/merged/NIFTY_500_price_and_tri_fx.csv

  # Fractional (not percent) output, nominal exponent, nearest trading day:
  python nse_rolling_cagr.py -w 3 --cagr-unit fraction --years-mode nominal \\
         --anchor nearest nse_index_data/merged/NIFTY_500_price_and_tri_fx.csv

  # Show the arithmetic behind specific dates (accepts 'latest'):
  python nse_rolling_cagr.py -w 5 --explain latest --explain 2026-01-01 \\
         nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv

  # Bare CSV (no comment block) for a reader that cannot skip '#' lines:
  python nse_rolling_cagr.py -w 5 --header-block off \\
         nse_index_data/merged/NIFTY_50_price_and_tri_fx.csv

INPUT LAYOUT
  Any CSV with a date column, a TRI column and a USD/INR rate column; the merged NSE
  files produced by nse_fx_decorator.py (merged/*_fx.csv) match out of the box:
    date,open,high,low,close,tri,ntr,usdinr,fx_date,fx_source,fx_status

DEPENDENCIES: none (Python 3.9+ standard library only)
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nse_rolling_cagr.py", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("inputs", nargs="*", metavar="CSV",
                   help="One or more index CSVs carrying a date, TRI and USD/INR column "
                        "(for example nse_index_data/merged/*_fx.csv). A new file is "
                        "written for each.")

    g = p.add_argument_group("window")
    g.add_argument("-w", "--window-years", type=int, required=True, metavar="N",
                   help=f"Width of the rolling window in whole years, "
                        f"{MIN_WINDOW}-{MAX_WINDOW}.")
    g.add_argument("--anchor", choices=ANCHOR_MODES, default=ON_OR_BEFORE,
                   help="Where the window starts when the anniversary date is a holiday: "
                        "on_or_before | on_or_after | nearest (default: %(default)s).")
    g.add_argument("--max-slip-days", type=int, default=DEFAULT_MAX_SLIP_DAYS, metavar="N",
                   help="A window start further than N days from its anniversary counts as "
                        "NO history instead of a window that is too short or too long "
                        "(default: %(default)s; the NSE calendar never shuts longer than "
                        "6 days, so this only ever catches missing data).")
    g.add_argument("--years-mode", choices=YEARS_MODES, default=ACTUAL,
                   help="Exponent: actual elapsed years | exactly N "
                        "(default: %(default)s).")

    g = p.add_argument_group("output: what the new file looks like")
    g.add_argument("--out-dir", metavar="DIR",
                   help="Directory for the new files (default: next to each input file).")
    g.add_argument("--suffix", metavar="STR",
                   help="Appended to each input file name (default: _cagr_<N>y).")
    g.add_argument("--cagr-unit", choices=(PCT, FRACTION), default=PCT,
                   help="CAGR as percent (cagr_inr_pct columns) or fraction "
                        "(default: %(default)s).")
    g.add_argument("--precision", type=int, default=4, metavar="N",
                   help="Decimals for the TRI and rate value columns (default: %(default)s).")
    g.add_argument("--cagr-precision", type=int, default=4, metavar="N",
                   help="Decimals for the CAGR columns (default: %(default)s).")
    g.add_argument("--date-col", default=DEFAULT_DATE_COL, metavar="NAME",
                   help="Date column of the input (default: %(default)s).")
    g.add_argument("--header-block", choices=HEADER_MODES, default=HEADER_MULTILINE,
                   help="Introduce the output with a comment block that describes the "
                        "file and every column in machine-readable form (multiline), or "
                        "write nothing but the CSV (off). Default: %(default)s.")
    g.add_argument("--tri-col", default=DEFAULT_TRI_COL, metavar="NAME",
                   help="Total-return-index column, in INR (default: %(default)s).")
    g.add_argument("--fx-col", default=DEFAULT_FX_COL, metavar="NAME",
                   help="USD/INR rate column used to convert the TRI to USD "
                        "(default: %(default)s).")

    g = p.add_argument_group("verification")
    g.add_argument("--explain", action="append", metavar="DATE",
                   help="Print the arithmetic behind DATE ('latest', or any date in the "
                        "file; the row on or after it is used). Repeatable: --explain X "
                        "--explain Y. Takes ONE value, so it never eats the CSV paths.")
    g.add_argument("--audit", metavar="PATH",
                   help="Where to write the audit CSV (default: <output>_audit.csv, "
                        "written only when a row was dropped or noted).")
    g.add_argument("-v", "--verbose", action="store_true", help="Show DEBUG messages.")
    return p


def validate(a, parser) -> None:
    if not MIN_WINDOW <= a.window_years <= MAX_WINDOW:
        parser.error(f"-w/--window-years must be between {MIN_WINDOW} and {MAX_WINDOW}")
    if not 0 <= a.precision <= 10:
        parser.error("--precision must be between 0 and 10")
    if not 0 <= a.cagr_precision <= 10:
        parser.error("--cagr-precision must be between 0 and 10")
    if a.max_slip_days < 0:
        parser.error("--max-slip-days must be >= 0")


def setup_logging(verbose: bool) -> logging.Logger:
    log = logging.getLogger("cagr")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))
    log.addHandler(ch)
    return log


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    validate(a, parser)
    log = setup_logging(a.verbose)
    log.info("nse_rolling_cagr %s | window=%d year(s) | anchor=%s | exponent=%s | inputs=%d",
             __version__, a.window_years, a.anchor, a.years_mode, len(a.inputs))
    if not a.inputs:
        log.critical("no input files given -- pass one or more CSVs (see --help)")
        return 2
    inputs = []
    for raw in a.inputs:
        try:
            inputs.append(read_input(Path(raw), a, log))
        except FatalError as e:
            log.error("cannot use %s: %s", raw, e)
    if not inputs:
        log.critical("no usable input file -- nothing to do")
        return 2
    try:
        return Runner(a, log, inputs).run()
    except FatalError as e:
        log.critical("FATAL: %s", e)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
