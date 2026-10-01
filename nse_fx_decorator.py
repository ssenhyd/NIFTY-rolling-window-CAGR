#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nse_fx_decorator.py -- add an official USD/INR rate column to time-series CSVs.

Reads one or more CSVs that contain a date column (e.g. the merged NSE index
files), downloads authoritative USD/INR rates, and writes a NEW csv per input
with the rate added for every date.  Dates the sources do not publish fall back
to the last rate used on the previous date (carry-forward), and every fill,
gap and download failure is written to an audit file.

Run with --help for the full documentation.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

try:
    import requests
except ImportError as _exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {_exc.name}.  Install with:  pip install requests")

__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# RBI's ASP.NET front end answers HTTP 502 to unfamiliar User-Agents on POST, so
# the two Indian sources send a browser string.  FRED is the opposite: it holds
# (and eventually times out) browser-style User-Agents coming from non-browser
# clients, so the Fed source sends no User-Agent override at all and lets the
# HTTP library's own string through.
BROWSER_SOURCES = ("rbi", "fbil")

RBI_URL = "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"
FBIL_URL = "https://www.fbil.org.in/wasdm/refrates/fetchfiltered"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXINUS"

SOURCES = ("rbi", "fbil", "h10")
AUTO_CHAIN = ("rbi", "fbil", "h10")

SOURCE_TITLE = {
    "rbi": "Reserve Bank of India reference rate (RBI archive; FBIL rate after 09-Jul-2018)",
    "fbil": "FBIL USD/INR reference rate, published 13:30 IST (benchmark administrator)",
    "h10": "US Federal Reserve H.10 noon buying rate for India (series DEXINUS, via FRED)",
}

# Coverage observed when this script was written.  Used only to skip pointless
# requests and to explain coverage gaps in the report -- never to fake data.
KNOWN_FIRST = {
    "rbi": date(1998, 8, 25),
    "fbil": date(2018, 7, 10),
    "h10": date(1973, 1, 2),
}
# USD/INR has traded inside this band over the covered era; outside it we warn
# (the value is still used, but it is almost certainly a source glitch).
PLAUSIBLE_RATE = (20.0, 300.0)

PENDING, OK, EMPTY, SANITY_FAILED, ABANDONED, WAIVED = (
    "PENDING", "OK", "EMPTY", "SANITY_FAILED", "ABANDONED", "WAIVED",
)
HAVE_DATA = {OK, SANITY_FAILED, WAIVED}       # statuses with observations on disk
USABLE = {OK, WAIVED}                          # statuses used to fill the CSV

EXACT, CARRIED, MISSING = "exact", "carried_forward", "missing"

AUDIT_COLS = ["severity", "file", "row", "date", "fx_status", "rate",
              "fx_source", "fx_date", "carry_days", "message"]

# date patterns: ISO first, then "10 Mar 2026", then day-first "10/03/2026"
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_TEXT_RE = re.compile(r"^\s*(\d{1,2})[\s\-/,.]+([A-Za-z]{3,9})[\s\-/,.]+(\d{4})")
_NUM_RE = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")
_DMY_RE = re.compile(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}$")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_LOOKUP = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_HIDDEN_RE = re.compile(r'<input type="hidden" name="([^"]+)"[^>]*?value="([^"]*)"')


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DownloadError(Exception):
    """A retryable problem with one HTTP attempt (network, HTTP status, bad body)."""

    def __init__(self, msg, http_status=None, retry_after=None):
        super().__init__(msg)
        self.http_status = http_status
        self.retry_after = retry_after


class SchemaError(Exception):
    """The response was fetched but does not look like FX data."""


class AttemptsExhausted(Exception):
    def __init__(self, last_error: str, attempts: int):
        super().__init__(last_error)
        self.last_error = last_error
        self.attempts = attempts


class FatalError(Exception):
    """Unrecoverable problem; the run stops (state is saved first)."""


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
    """'95.8179' -> 95.8179; blanks, dashes and junk -> None."""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None", "null", "&nbsp;"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def ua_header(a, source: str) -> Dict[str, str]:
    """Per-source User-Agent: a browser string for rbi/fbil, none for FRED."""
    if source not in BROWSER_SOURCES:
        return {}
    return {"User-Agent": a.user_agent}


def fmt_ddmmyyyy(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def strip_tags(cell: str) -> str:
    return _TAG_RE.sub("", cell).replace("&nbsp;", " ").strip()


def html_rows(html: str) -> List[List[str]]:
    """Every <tr> of the page as a list of plain-text cells (view state is base64, so safe)."""
    out = []
    for m in _ROW_RE.finditer(html):
        cells = [strip_tags(c) for c in _CELL_RE.findall(m.group(1))]
        if cells:
            out.append(cells)
    return out


def hidden_fields(html: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in _HIDDEN_RE.finditer(html)}


def preview(dates: Sequence[date], n: int = 6) -> str:
    txt = ", ".join(d.isoformat() for d in dates[:n])
    return txt + (f" (+{len(dates) - n} more)" if len(dates) > n else "")


# --------------------------------------------------------------------------- #
# Source parsers
# --------------------------------------------------------------------------- #
def parse_rbi_table(html: str) -> Tuple[List[Tuple[date, float]], List[str], List[str]]:
    """RBI archive table -> ([obs], warnings, issues).  Column found by its USD header."""
    rows = html_rows(html)
    usd_idx, header = None, ""
    for cells in rows:
        for i, c in enumerate(cells):
            if c.upper().startswith("USD"):
                usd_idx, header = i, c
                break
        if usd_idx is not None:
            break
    if usd_idx is None:
        # RBI renders the table only when it has rows to show: this is an empty window
        # (a genuine coverage gap, or a page state that went stale -- the caller retries
        # once with a fresh page before accepting it).
        return [], ["the RBI page carries no data table for this window"], []
    obs, warns, issues = [], [], []
    blanks, seen = 0, set()
    for cells in rows:
        if not cells or not _DMY_RE.match(cells[0]):
            continue
        d = parse_any_date(cells[0])
        if d is None or usd_idx >= len(cells):
            issues.append(f"unparseable RBI row: {cells[:3]}")
            continue
        v = to_num(cells[usd_idx])
        if v is None or v <= 0:
            blanks += 1
            continue
        if d in seen:
            issues.append(f"duplicate date {d} in the RBI table")
            continue
        seen.add(d)
        obs.append((d, v))
    if blanks:
        warns.append(f"{blanks} row(s) with blank/zero USD value dropped")
    if not obs:
        warns.append("no USD values in the RBI table for this window")
    warns.append(f"USD column located by header {header!r} (index {usd_idx})")
    return obs, warns, issues


def parse_fbil_json(text: str) -> Tuple[List[Tuple[date, float]], List[str], List[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise SchemaError(f"FBIL response is not JSON: {text[:80]!r}")
    if not isinstance(data, list):
        raise SchemaError(f"FBIL response is not a JSON array: {str(data)[:80]!r}")
    obs, warns, issues = [], [], []
    seen, others = set(), 0
    for rec in data:
        if not isinstance(rec, dict):
            issues.append(f"non-object FBIL record: {str(rec)[:60]}")
            continue
        name = str(rec.get("subProdName", "")).strip()
        if not re.fullmatch(r"INR\s*/\s*1\s*USD", name, re.I):
            others += 1
            continue
        d = parse_any_date(str(rec.get("processRunDate", ""))[:10])
        v = to_num(rec.get("rate"))
        if d is None or v is None or v <= 0:
            issues.append(f"bad FBIL USD record: {rec}")
            continue
        if d in seen:
            issues.append(f"duplicate date {d} in the FBIL data")
            continue
        seen.add(d)
        obs.append((d, v))
    if others:
        warns.append(f"{others} non-USD FBIL record(s) ignored")
    if not obs:
        warns.append("no 'INR / 1 USD' records in the FBIL response")
    return obs, warns, issues


def parse_fred_csv(text: str) -> Tuple[List[Tuple[date, float]], List[str], List[str]]:
    obs, warns, issues = [], [], []
    lines = text.splitlines()
    if not lines:
        raise SchemaError("empty FRED csv")
    if "observation_date" not in lines[0]:
        raise SchemaError(f"FRED csv header unexpected: {lines[0][:80]!r}")
    blanks = 0
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d = parse_any_date(parts[0])
        v = to_num(parts[1])
        if v is None:                     # FRED writes holidays as an empty field
            blanks += 1
            continue
        if d is None:
            issues.append(f"unparseable FRED date {parts[0]!r}")
            continue
        obs.append((d, v))
    if blanks:
        warns.append(f"{blanks} blank row(s) (holidays) skipped")
    return obs, warns, issues


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
class Source:
    name = ""
    title = ""

    def fetch(self, a, sess, ws: date, we: date):
        """Return (obs, raw_text, raw_ext, warnings, issues)."""
        raise NotImplementedError



class RbiSource(Source):
    name = "rbi"
    title = SOURCE_TITLE["rbi"]

    def __init__(self):
        self._viewstate = None

    def _page(self, a, sess) -> Dict[str, str]:
        try:
            page = sess.get(RBI_URL, headers=ua_header(a, self.name),
                            timeout=(a.connect_timeout, a.read_timeout))
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"network error on the RBI archive page: {type(e).__name__}: {e}")
        if page.status_code != 200:
            raise DownloadError(f"HTTP {page.status_code} on the RBI archive page",
                                http_status=page.status_code)
        vs = hidden_fields(page.text)
        if "__VIEWSTATE" not in vs or "__EVENTVALIDATION" not in vs:
            raise SchemaError("RBI archive page has no ASP.NET view state (page layout changed?)")
        return vs

    def _query(self, a, sess, ws, we, vs):
        post = dict(vs)
        post.update({"txtFromDate": fmt_ddmmyyyy(ws), "txtToDate": fmt_ddmmyyyy(we),
                     "btnSubmit": " GO ", "chkAll": "on", "chkUSD": "on"})
        try:
            r = sess.post(RBI_URL, data=post, headers=ua_header(a, self.name),
                          timeout=(a.connect_timeout, a.read_timeout))
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"network error on the RBI archive query: {type(e).__name__}: {e}")
        if r.status_code != 200:
            raise DownloadError(f"HTTP {r.status_code} on the RBI archive query",
                                http_status=r.status_code)
        if "Reference Rate Archive" not in r.text:
            raise DownloadError("RBI response does not look like the reference-rate page "
                                "(login wall / block page?)")
        return r

    def fetch(self, a, sess, ws, we):
        """One page fetch per run: the ASP.NET view state is reused for every window,
        which halves the requests to RBI (they answer an expired state with an empty
        table, so that case is turned into a retry with a fresh state instead)."""
        fresh = self._viewstate is None
        if fresh:
            self._viewstate = self._page(a, sess)
        try:
            r = self._query(a, sess, ws, we, self._viewstate)
        except DownloadError:
            self._viewstate = None
            raise
        obs, warns, issues = parse_rbi_table(r.text)
        if not obs:
            self._viewstate = None          # whatever is wrong, do not reuse this state
            if not fresh:
                raise DownloadError("the RBI page returned no data table with a reused page "
                                    "state -- retrying with a fresh one")
        return obs, r.text, "html", warns, issues


class FbilSource(Source):
    name = "fbil"
    title = SOURCE_TITLE["fbil"]

    def fetch(self, a, sess, ws, we):
        params = {"fromDate": ws.isoformat(), "toDate": we.isoformat(), "authenticated": "false"}
        try:
            r = sess.get(FBIL_URL, params=params, headers=ua_header(a, self.name),
                         timeout=(a.connect_timeout, a.read_timeout))
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"network error on the FBIL endpoint: {type(e).__name__}: {e}")
        if r.status_code != 200:
            raise DownloadError(f"HTTP {r.status_code} from the FBIL endpoint",
                                http_status=r.status_code)
        obs, warns, issues = parse_fbil_json(r.text)
        return obs, r.text, "json", warns, issues


class H10Source(Source):
    name = "h10"
    title = SOURCE_TITLE["h10"]


    def fetch(self, a, sess, ws, we):
        try:
            r = sess.get(FRED_URL, timeout=(a.connect_timeout, max(a.read_timeout, 60)))
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"network error on FRED: {type(e).__name__}: {e}")
        if r.status_code != 200:
            raise DownloadError(f"HTTP {r.status_code} from FRED", http_status=r.status_code)
        obs, warns, issues = parse_fred_csv(r.text)
        return obs, r.text, "csv", warns, issues


SOURCE_CLASSES = {"rbi": RbiSource, "fbil": FbilSource, "h10": H10Source}


# --------------------------------------------------------------------------- #
# Input files
# --------------------------------------------------------------------------- #
@dataclass
class InputFile:
    path: Path
    header: List[str]
    rows: List[List[str]]
    date_idx: int = -1
    col_name: str = ""


def find_date_column(header: List[str], rows: List[List[str]], a) -> Tuple[int, str, str]:
    """Return (index, name, how).  Explicit --date-col first, then name, then sniffing."""
    if a.date_col:
        for i, name in enumerate(header):
            if name.strip().lower() == a.date_col.lower():
                return i, name, "chosen by --date-col"
        raise FatalError(f"--date-col {a.date_col!r} is not a column of this file "
                         f"(columns: {', '.join(header)})")
    for i, name in enumerate(header):
        if re.fullmatch(r"(?i)\s*(date|dt|as_of|timestamp)\s*", name):
            return i, name, "named 'date'"
    for i, name in enumerate(header):
        if "date" in name.lower():
            return i, name, "name contains 'date'"
    sample = [r for r in rows[:50]]
    for i in range(len(header)):
        vals = [r[i] for r in sample if i < len(r)]
        if vals and sum(1 for v in vals if parse_any_date(v)) >= 0.8 * len(vals):
            return i, header[i], "first date-like column"
    raise FatalError("no date column found; pass --date-col NAME")


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
    idx, name, how = find_date_column(header, rows, a)
    log.info("%s: %d row(s); date column %r (%s)", path.name, len(rows), name, how)
    return InputFile(path=path, header=header, rows=rows, date_idx=idx, col_name=name)


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
class Runner:
    _file_failures = 0

    def __init__(self, a, log: logging.Logger, inputs: List[InputFile], needed: List[date]):
        self.a = a
        self.log = log
        self.inputs = inputs
        self.needed = sorted(set(needed))
        self.min_needed = self.needed[0] if self.needed else date.today()
        self.max_needed = self.needed[-1] if self.needed else date.today()
        self.today = date.today()
        self.work = Path(a.work_dir)
        self.state_path = self.work / "state.json"
        self.ledger_path = self.work / "attempt_ledger.csv"
        self.status_csv = self.work / "chunk_status.csv"
        self.audit_csv = self.work / "fx_audit.csv"
        self.state = self._load_state()
        self.chunks: Dict[str, dict] = self.state["chunks"]
        self.plan: List[str] = []
        self.audit_rows: List[dict] = []
        self.series: Dict[date, Tuple[Optional[float], str, Optional[date], str]] = {}
        self.obs: Dict[date, Tuple[float, str]] = {}
        self.stats = {"rows": 0, EXACT: 0, CARRIED: 0, MISSING: 0}
        self._sess = None
        self._requests = 0
        self._obs_cache: Dict[str, List[Tuple[date, float]]] = {}
        self._sources: Dict[str, Source] = {}

    # ---- sources ----------------------------------------------------------
    def active_sources(self) -> List[str]:
        want = self.a.source
        if "auto" in want:
            want = list(AUTO_CHAIN)
        out = []
        for s in want:
            if s not in out:
                out.append(s)
        return out

    # ---- state ------------------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    st = json.load(fh)
                st.setdefault("chunks", {})
                return st
            except (OSError, json.JSONDecodeError) as e:
                raise FatalError(f"cannot read {self.state_path}: {e} (fix or delete it; "
                                 f"cached FX data under {self.work / 'cache'} is kept)")
        return {"version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "chunks": {}}

    def save_state(self) -> None:
        self.state["updated"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=True)
        tmp.replace(self.state_path)
        with open(self.status_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["chunk_id", "source", "window", "start", "end", "status", "rows",
                        "attempts_total", "issues", "warnings", "last_error", "file", "updated_at"])
            for cid in sorted(self.chunks):
                r = self.chunks[cid]
                w.writerow([cid, r["source"], r["label"], r["start"], r["end"], r["status"],
                            r["rows"], r["attempts_total"], " | ".join(r["issues"]),
                            " | ".join(r["warnings"]), r["last_error"], r["file"], r["updated_at"]])

    def ledger(self, cid: str, attempt, outcome: str, http, rows, msg: str) -> None:
        new = not self.ledger_path.exists()
        with open(self.ledger_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp", "chunk_id", "attempt", "outcome", "http_status",
                            "rows", "message"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), cid, attempt, outcome,
                        http if http is not None else "", rows if rows is not None else "", msg])

    def audit(self, severity: str, file: str, row, d, status, rate, source, fx_date,
              carry_days, message: str) -> None:
        self.audit_rows.append({
            "severity": severity, "file": file, "row": row, "date": d.isoformat() if d else "",
            "fx_status": status, "rate": "" if rate is None else f"{rate:.6f}",
            "fx_source": source, "fx_date": fx_date.isoformat() if fx_date else "",
            "carry_days": carry_days, "message": message})

    def write_audit(self) -> None:
        with open(self.audit_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=AUDIT_COLS)
            w.writeheader()
            for r in self.audit_rows:
                w.writerow(r)

    # ---- HTTP -------------------------------------------------------------
    def session(self):
        if self._sess is None:
            s = requests.Session()
            s.headers.update({"Accept": "text/html,application/json,*/*;q=0.8",
                              "Accept-Language": "en-US,en;q=0.9"})
            self._sess = s
        return self._sess

    def reset_session(self) -> None:
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:
                pass
        self._sess = None

    def polite_wait(self) -> None:
        n = self.a.long_pause_every
        if n and self._requests and self._requests % n == 0:
            secs = random.uniform(self.a.long_pause_min, self.a.long_pause_max)
            self.log.info("  long courtesy pause: %.0fs (every %d requests)", secs, n)
        else:
            secs = random.uniform(self.a.min_wait, self.a.max_wait)
            self.log.debug("  waiting %.1fs before the next request", secs)
        time.sleep(max(0.0, secs))

    def source_for(self, name: str) -> Source:
        if name not in self._sources:
            self._sources[name] = SOURCE_CLASSES[name]()
        return self._sources[name]

    # ---- planning ---------------------------------------------------------
    def windows_for(self, name: str, unfilled: Set[date]) -> List[Tuple[str, date, date]]:
        """Windows are whole calendar years clipped only by the source's coverage and by
        today -- NOT by the input's date range -- so the cache survives a later run on a
        file with a different range (only the current year's window grows with time)."""
        if name == "h10":                       # one file holds the whole history
            return [("all", KNOWN_FIRST[name], min(self.today, self.max_needed))] \
                if any(KNOWN_FIRST[name] <= d <= min(self.today, self.max_needed) for d in unfilled) else []
        first, last = max(KNOWN_FIRST[name], self.min_needed), min(self.today, self.max_needed)
        if first > last:
            return []
        out = []
        for y in range(first.year, last.year + 1):
            ws = max(date(y, 1, 1), KNOWN_FIRST[name])
            we = min(date(y, 12, 31), self.today)
            if ws <= we and any(ws <= d <= we for d in unfilled):
                out.append((str(y), ws, we))
        return out

    def plan_chunks(self, unfilled: Set[date], name: str) -> None:
        windows = self.windows_for(name, unfilled)
        for label, ws, we in windows:
            cid = f"{name}.{label}"
            rec = self.chunks.get(cid)
            if rec is None:
                rec = {"source": name, "label": label, "start": ws.isoformat(),
                       "end": we.isoformat(), "status": PENDING, "rows": None,
                       "attempts_total": 0, "last_error": "", "issues": [], "warnings": [],
                       "file": "", "raw_file": "", "updated_at": None}
                self.chunks[cid] = rec
            else:                               # keep the status: only the bounds move
                rec["start"], rec["end"] = ws.isoformat(), we.isoformat()
            if cid not in self.plan:
                self.plan.append(cid)
        if windows:
            self.log.info("source %-4s : %d window(s) planned (%s)", name, len(windows),
                          SOURCE_CLASSES[name].title)
        else:
            self.log.info("source %-4s : nothing needed in %s .. %s -- skipped", name,
                          max(KNOWN_FIRST[name], self.min_needed), min(self.today, self.max_needed))

    # ---- downloading ------------------------------------------------------
    def _paths(self, cid: str) -> Tuple[Path, Path]:
        r = self.chunks[cid]
        d = self.work / "cache" / r["source"]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{cid}.csv", d / f"{cid}.raw"

    def _cache_usable(self, cid: str) -> bool:
        """Reuse a cached window unless it is absent, or it ends in the current year and
        was fetched on an earlier day (a newer rate may have been published since)."""
        rec = self.chunks[cid]
        if self.a.refresh or (rec["status"] not in HAVE_DATA and rec["status"] != EMPTY):
            return False
        if not self._paths(cid)[0].exists():
            return False
        stamp = (rec.get("updated_at") or "")[:10]
        if int(rec["end"][:4]) == date.today().year and stamp != date.today().isoformat():
            self.log.info("%s: cached on %s -- refreshing this year's window", cid,
                          stamp or "an earlier run")
            return False
        return True

    def _load_cached(self, cid: str) -> Optional[List[Tuple[date, float]]]:
        r = self.chunks[cid]
        parsed = Path(r["file"]) if r["file"] else self._paths(cid)[0]
        if r["status"] not in HAVE_DATA or not parsed.exists():
            return None
        obs = []
        with open(parsed, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i == 0:
                    continue
                p = line.strip().split(",")
                if len(p) != 2:
                    continue
                d, v = parse_any_date(p[0]), to_num(p[1])
                if d and v:
                    obs.append((d, v))
        if r["rows"] is not None and len(obs) != r["rows"]:
            self.log.warning("%s: cache holds %d observation(s) but state says %s",
                             cid, len(obs), r["rows"])
        return obs

    def _download(self, cid: str) -> List[Tuple[date, float]]:
        """Cached observations for one window; retries, back-off, ledger, polite waits."""
        rec = self.chunks[cid]
        if self.a.offline:
            obs = self._load_cached(cid)
            if obs is None:
                rec["attempts_total"] = (rec["attempts_total"] or 0) + 1
                rec.update(status=ABANDONED, last_error="--offline and this window is not cached")
                self.log.error("%s: --offline, no cache -- cannot fill this window", cid)
            return obs or []
        if self._cache_usable(cid):
            self.log.debug("%s: using the cached result (%s, %s row(s))", cid,
                           rec["status"], rec["rows"])
            return self._load_cached(cid) or []
        src = self.source_for(rec["source"])
        ws, we = date.fromisoformat(rec["start"]), date.fromisoformat(rec["end"])
        max_attempts = self.a.max_retries + 1
        last_err = "no attempt made"
        for attempt in range(1, max_attempts + 1):
            rec["attempts_total"] = (rec["attempts_total"] or 0) + 1
            self._requests += 1
            try:
                obs, raw, ext, warns, issues = src.fetch(self.a, self.session(), ws, we)
                if not obs and attempt == 1:
                    raise DownloadError("the source returned no rows for a window that is "
                                        "already complete -- retrying once to be sure")
            except (DownloadError, SchemaError) as e:
                http = getattr(e, "http_status", None)
                last_err = f"{type(e).__name__}: {e}"
                rec["last_error"] = last_err
                self.ledger(cid, attempt, "ERROR", http, 0, str(e))
                self.log.warning("%s: attempt %d/%d failed: %s", cid, attempt, max_attempts, e)
                self.reset_session()
                if attempt < max_attempts:
                    delay = min(self.a.backoff_max, self.a.backoff_base * (2 ** (attempt - 1)))
                    delay *= random.uniform(0.8, 1.3)
                    ra = getattr(e, "retry_after", None)
                    if ra:
                        delay = max(delay, min(ra, self.a.backoff_max))
                    self.log.info("  exponential back-off: sleeping %.1fs before retry %d/%d",
                                  delay, attempt, self.a.max_retries)
                    time.sleep(max(0.0, delay))
                self.polite_wait()
                continue
            obs, issues = self._sanity(rec, obs, issues)
            self._save_window(cid, obs, raw, warns, issues)
            self.ledger(cid, attempt, "DOWNLOADED", 200, len(obs), "" if obs else "no rows")
            self.polite_wait()
            return obs
        rec["last_error"] = last_err
        raise AttemptsExhausted(last_err, max_attempts)

    @staticmethod
    def _sanity(rec: dict, obs: List[Tuple[date, float]], issues: List[str]):
        """Window-level checks: window bounds, duplicates, future dates, plausible values."""
        ws, we = date.fromisoformat(rec["start"]), date.fromisoformat(rec["end"])
        seen, clean, warns = set(), [], []
        today = date.today()
        outside = sorted(d for d, _ in obs if d < ws or d > we)
        if outside:
            warns.append(f"{len(outside)} row(s) dated outside the window: {preview(outside)}")
        future = sorted(d for d, _ in obs if d > today)
        if future:
            issues.append(f"{len(future)} row(s) dated in the future: {preview(future)}")
        odd = sorted(d for d, v in obs if not (PLAUSIBLE_RATE[0] <= v <= PLAUSIBLE_RATE[1]))
        if odd:
            warns.append(f"{len(odd)} value(s) outside the plausible band {PLAUSIBLE_RATE}: "
                         f"{preview(odd)}")
        for d, v in sorted(obs):
            if d in seen:
                issues.append(f"duplicate date {d} in one window")
                continue
            seen.add(d)
            clean.append((d, v))
        return clean, issues + ([] if clean else ["no observations returned"])

    def _save_window(self, cid: str, obs, raw: str, warns, issues) -> None:
        parsed, rawp = self._paths(cid)
        with open(parsed, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "usdinr"])
            for d, v in sorted(obs):
                w.writerow([d.isoformat(), f"{v:.6f}"])
        with open(rawp, "w", encoding="utf-8") as fh:
            fh.write(raw)
        rec = self.chunks[cid]
        if not obs:
            status = EMPTY
            warns = list(warns) + [f"the source published nothing for {rec['start']} .. "
                                   f"{rec['end']} -- this window is a coverage gap at the "
                                   f"source, not a rate of 0"]
        elif issues:
            status = SANITY_FAILED
        else:
            status = OK
        rec.update(rows=len(obs), warnings=list(warns), issues=list(issues), status=status,
                   file=str(parsed), raw_file=str(rawp), last_error="",
                   updated_at=datetime.now().isoformat(timespec="seconds"))
        self.save_state()
        level = logging.INFO if status == OK else logging.WARNING
        self.log.log(level, "%s: %s, %d observation(s)%s", cid, status, len(obs),
                     "  <-- no data at the source for this window" if status == EMPTY else "")
        for w in warns:
            self.log.debug("  %s: %s", cid, w)
        for i in issues:
            self.log.warning("  %s: %s", cid, i)

    def download_pass(self) -> Set[date]:
        """Fetch the windows each active source needs; return the still-unfilled dates."""
        unfilled = set(self.needed)
        for name in self.active_sources():
            self.plan_chunks(unfilled, name)
            done = set()
            for cid in [c for c in self.plan if self.chunks[c]["source"] == name]:
                try:
                    obs = self._download(cid)
                except AttemptsExhausted as e:
                    rec = self.chunks[cid]
                    rec.update(status=ABANDONED, last_error=e.last_error,
                               updated_at=datetime.now().isoformat(timespec="seconds"))
                    self.save_state()
                    self.log.error("%s: ABANDONED after %d attempt(s): %s",
                                   cid, rec["attempts_total"], e.last_error)
                    obs = []
                if self.chunks[cid]["status"] in USABLE:
                    done |= {d for d, _ in obs}
            unfilled -= done
            if unfilled:
                self.log.info("source %-4s : %d needed date(s) still without a rate",
                              name, len(unfilled))
            else:
                self.log.info("source %-4s : all needed dates are covered", name)
                break
        return unfilled

    def retry_abandoned(self) -> None:
        ab = [c for c in self.plan if self.chunks[c]["status"] == ABANDONED]
        if not ab:
            return
        self.log.error("---- %d window(s) ABANDONED after all retries ----", len(ab))
        for cid in ab:
            r = self.chunks[cid]
            self.log.error("  %s [%s..%s]: %s", cid, r["start"], r["end"], r["last_error"])
        retry = self.a.yes or (not self.a.no_prompt and sys.stdin.isatty() and
                               self._ask(f"Retry these {len(ab)} window(s) now?"))
        if not retry:
            return
        self.log.info("Retrying %d window(s) ...", len(ab))
        for cid in ab:
            try:
                self._download(cid)
            except AttemptsExhausted:
                pass

    def _ask(self, question: str) -> bool:
        try:
            return input(f"\n{question} [y/N]: ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    # ---- assembling the rate series ---------------------------------------
    def build_series(self) -> Set[date]:
        """Classify every needed date: exact, carried forward, or missing."""
        for cid in self.plan:
            rec = self.chunks[cid]
            if rec["status"] not in USABLE:
                continue
            if cid not in self._obs_cache:
                self._obs_cache[cid] = self._load_cached(cid) or []
        obs: Dict[date, Tuple[float, str]] = {}
        for name in self.active_sources():            # priority order wins
            for cid in self.plan:
                rec = self.chunks[cid]
                if rec["source"] != name or rec["status"] not in USABLE:
                    continue
                for d, v in self._obs_cache.get(cid, []):
                    obs.setdefault(d, (v, name))
        self.obs = obs
        if not obs:
            self.log.error("no FX observations at all -- every row will be left blank")
            for d in self.needed:
                self.series[d] = (None, "", None, MISSING)
            return set(self.needed)
        ds = sorted(obs)
        self.log.info("rate series: %d observation(s) from %s", len(ds), ", ".join(self.active_sources()))
        self.log.info("  coverage %s .. %s", ds[0].isoformat(), ds[-1].isoformat())
        missing, cur = set(), None
        for d in self.needed:
            if d in obs:
                v, s = obs[d]
                self.series[d] = (v, s, d, EXACT)
                cur = (v, s, d)
            elif cur is None:                          # before the first published rate
                missing.add(d)
                self.series[d] = (None, "", None, MISSING)
            else:
                v, s, fd = cur
                self.series[d] = (v, s, fd, CARRIED)
        return missing

    # ---- decorating -------------------------------------------------------
    def output_path(self, inp: InputFile) -> Path:
        base = Path(self.a.out_dir) if self.a.out_dir else inp.path.parent
        return base / f"{inp.path.stem}{self.a.suffix}{inp.path.suffix or '.csv'}"

    def decorate(self, inp: InputFile) -> None:
        out = self.output_path(inp)
        if out.resolve() == inp.path.resolve():
            raise FatalError(f"refusing to overwrite the input file {inp.path} "
                             f"(change --out-dir or --suffix)")
        out.parent.mkdir(parents=True, exist_ok=True)
        pr = self.a.precision
        first_obs = min(self.obs).isoformat() if self.obs else "n/a"
        dupes, unparsed = set(), 0
        tmp = out.with_name(out.name + ".part")      # never leave a half-written output
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(inp.header + [self.a.fx_col, "fx_date", "fx_source", "fx_status"])
            for n, row in enumerate(inp.rows, start=1):
                d = parse_any_date(row[inp.date_idx]) if inp.date_idx < len(row) else None
                if d is None:
                    unparsed += 1
                    rate, fd, src, st = None, None, "", MISSING
                    self.audit("ERROR", inp.path.name, n, None, MISSING, None, "", None, "",
                               "date column value is missing or unparseable")
                else:
                    if d in dupes:
                        self.audit("WARNING", inp.path.name, n, d, "", None, "", None, "",
                                   "duplicate date in the input file")
                    dupes.add(d)
                    rate, src, fd, st = self.series.get(d, (None, "", None, MISSING))
                    if st == MISSING:
                        self.audit("ERROR", inp.path.name, n, d, MISSING, None, "", None, "",
                                   f"no FX rate on or before {d} (first published rate is {first_obs})")
                    elif st == CARRIED:
                        age = (d - fd).days
                        self.audit("WARNING", inp.path.name, n, d, CARRIED, rate, src, fd, age,
                                   f"no rate published for {d}; carried forward the rate observed "
                                   f"on {fd} ({age} day(s) earlier)")
                        if age > self.a.stale_warn_days:
                            self.audit("WARNING", inp.path.name, n, d, CARRIED, rate, src, fd, age,
                                       f"carried-forward rate is {age} day(s) old "
                                       f"(> --stale-warn-days {self.a.stale_warn_days})")
                    elif not (PLAUSIBLE_RATE[0] <= rate <= PLAUSIBLE_RATE[1]):
                        self.audit("WARNING", inp.path.name, n, d, EXACT, rate, src, fd, 0,
                                   f"rate {rate} is outside the plausible band {PLAUSIBLE_RATE}")
                w.writerow(row + ["" if rate is None else f"{rate:.{pr}f}",
                                  fd.isoformat() if fd else "", src, st])
                self.stats["rows"] += 1
                if st in (EXACT, CARRIED, MISSING):
                    self.stats[st] += 1
        os.replace(tmp, out)
        if unparsed:
            self.log.error("%s: %d row(s) with a missing/unparseable date (left blank, listed "
                           "in the audit)", inp.path.name, unparsed)
        self.log.info("%s -> %s", inp.path.name, out)

    def cross_check(self) -> None:
        """Compare the sources on the dates where more than one has a value."""
        if not self.a.cross_check:
            return
        by_src: Dict[str, Dict[date, float]] = {}
        for cid in self.plan:
            rec = self.chunks[cid]
            if rec["status"] not in USABLE:
                continue
            bucket = by_src.setdefault(rec["source"], {})
            for d, v in (self._load_cached(cid) or []):
                bucket.setdefault(d, v)
        names = [n for n in self.active_sources() if n in by_src]
        if len(names) < 2:
            self.log.info("cross-check: fewer than two sources returned data -- nothing to compare")
            return
        base = names[0]
        for other in names[1:]:
            diffs = [(abs(v - by_src[other][d]) / v * 100.0, d, v, by_src[other][d])
                     for d, v in by_src[base].items() if d in by_src[other]]
            if not diffs:
                self.log.info("cross-check %s vs %s: no overlapping dates", base, other)
                continue
            diffs.sort(reverse=True)
            over = [x for x in diffs if x[0] > self.a.cross_tolerance_pct]
            worst = diffs[0]
            self.log.info("cross-check %s vs %s: %d common date(s); worst %.3f%% on %s "
                          "(%s %s vs %s %s); %d above --cross-tolerance-pct %.2f%%",
                          base, other, len(diffs), worst[0], worst[1], base, worst[2],
                          other, worst[3], len(over), self.a.cross_tolerance_pct)
            for pct, d, va, vb in over:          # every deviation is recorded
                self.audit("WARNING", "<cross-check>", "", d, "", va, base, d, "",
                           f"{base} {va} vs {other} {vb} differ by {pct:.3f}% "
                           f"(> {self.a.cross_tolerance_pct}%)")
            self.log.info("  %d deviation(s) recorded in %s%s", len(over), self.audit_csv.name,
                          "" if len(over) <= self.a.report_limit else
                          f"; the console report below shows the first {self.a.report_limit}")

    # ---- reporting --------------------------------------------------------
    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in (PENDING, OK, EMPTY, SANITY_FAILED, ABANDONED, WAIVED)}
        for c in self.plan:
            out[self.chunks[c]["status"]] += 1
        return out

    def print_report(self) -> None:
        cnt = self.counts()
        ab = [c for c in self.plan if self.chunks[c]["status"] == ABANDONED]
        sf = [c for c in self.plan if self.chunks[c]["status"] == SANITY_FAILED]
        errs = [r for r in self.audit_rows if r["severity"] == "ERROR"]
        warns = [r for r in self.audit_rows if r["severity"] == "WARNING"]
        lim = self.a.report_limit
        self.log.info("=" * 72)
        self.log.info("SUMMARY  sources=%s | windows: OK %d EMPTY %d SANITY_FAILED %d "
                      "ABANDONED %d PENDING %d", ",".join(self.active_sources()),
                      cnt[OK], cnt[EMPTY], cnt[SANITY_FAILED], cnt[ABANDONED], cnt[PENDING])
        self.log.info("ROWS     %d total | %d exact | %d carried forward | %d unfillable",
                      self.stats["rows"], self.stats[EXACT], self.stats[CARRIED],
                      self.stats[MISSING])
        if self.obs:
            ds = sorted(self.obs)
            self.log.info("SERIES   %d observation(s), %s .. %s", len(ds),
                          ds[0].isoformat(), ds[-1].isoformat())
        if ab:
            self.log.error("---- FX DOWNLOAD FAILURES: %d window(s) abandoned ----", len(ab))
            for cid in ab:
                r = self.chunks[cid]
                self.log.error("  %s [%s..%s] attempts=%s: %s", cid, r["start"], r["end"],
                               r["attempts_total"], r["last_error"])
        else:
            self.log.info("---- FX DOWNLOAD FAILURES: none ----")
        em = [c for c in self.plan if self.chunks[c]["status"] == EMPTY]
        if em:
            self.log.warning("---- WINDOWS WITH NO DATA AT THE SOURCE (coverage gaps, not "
                             "failures) (%d) ----", len(em))
            for cid in em:
                r = self.chunks[cid]
                self.log.warning("  %s [%s..%s]: the source returns nothing here; those dates "
                                 "come from another source or are carried forward",
                                 cid, r["start"], r["end"])
        if sf:
            self.log.warning("---- windows FAILED the sanity check and were NOT used (%d) ----", len(sf))
            for cid in sf:
                r = self.chunks[cid]
                self.log.warning("  %s [%s..%s]: %s", cid, r["start"], r["end"],
                                 "; ".join(r["issues"]))
            self.log.warning("  accept them with --waive-sanity %s",
                             " ".join(sf) if len(sf) < 4 else "all")
        self.log.info("---- FAILURES / ROWS THAT COULD NOT BE FILLED (%d) ----", len(errs))
        for r in errs[:lim]:
            self.log.error("  %s line %s %s: %s", r["file"], r["row"] or "--", r["date"], r["message"])
        if len(errs) > lim:
            self.log.error("  (+%d more in %s)", len(errs) - lim, self.audit_csv)
        self.log.info("---- WARNINGS: %d (carry-forwards and other notes) ----", len(warns))
        for r in warns[:lim]:
            self.log.warning("  %s line %s %s: %s", r["file"], r["row"] or "--", r["date"], r["message"])
        if len(warns) > lim:
            self.log.warning("  (+%d more in %s)", len(warns) - lim, self.audit_csv)
        self.log.info("Chunk status     : %s", self.status_csv)
        self.log.info("Attempt ledger   : %s", self.ledger_path)
        self.log.info("Row-level audit  : %s", self.audit_csv)
        for inp in self.inputs:
            self.log.info("Decorated output : %s", self.output_path(inp))
        self.log.info("=" * 72)

    def status_report(self) -> int:
        self.log.info("Work dir : %s", self.work)
        if not self.chunks:
            self.log.info("No run recorded yet in this work dir.")
        for cid in sorted(self.chunks):
            r = self.chunks[cid]
            self.log.info("  %-14s %-24s %-14s rows=%-6s %s", cid,
                          f"{r['start']}..{r['end']}", r["status"], r["rows"], r["last_error"])
        if self.audit_csv.exists():
            self.log.info("Last row-level audit : %s", self.audit_csv)
        return 0

    def exit_code(self) -> int:
        errs = [r for r in self.audit_rows if r["severity"] == "ERROR"]
        return 1 if errs else 0

    def waive(self) -> None:
        ids = set(self.a.waive_sanity or [])
        if not ids:
            return
        hit = 0
        for cid in list(self.chunks):
            r = self.chunks[cid]
            if r["status"] == SANITY_FAILED and ("all" in ids or cid in ids):
                r["status"] = WAIVED
                r["warnings"] = r["warnings"] + [f"sanity issues waived by the user on "
                                                 f"{datetime.now():%Y-%m-%d %H:%M}: "
                                                 + "; ".join(r["issues"])]
                hit += 1
                self.log.info("Waived sanity failure for %s", cid)
        self.save_state()
        self.log.info("%d window(s) waived", hit)

    # ---- entry ------------------------------------------------------------
    def run(self) -> int:
        if self.a.status:
            return self.status_report()
        if self.a.refresh:
            for cid in self.chunks:
                self.chunks[cid].update(status=PENDING, issues=[], warnings=[], last_error="")
        self.waive()
        missed = self.download_pass()
        self.retry_abandoned()
        missing = self.build_series()
        if missing:
            self.log.error("%d date(s) have no FX rate at all (they fall before the first "
                           "published rate); they are listed in the audit file", len(missing))
        for inp in self.inputs:
            try:
                self.decorate(inp)
            except FatalError as e:
                Runner._file_failures += 1
                self.log.error("FAILED %s: %s", inp.path, e)
                self.audit("ERROR", inp.path.name, "", None, MISSING, None, "", None, "",
                           f"file could not be decorated: {e}")
        self.cross_check()
        self.write_audit()
        self.save_state()
        self.print_report()
        if Runner._file_failures:
            self.log.error("%d input file(s) could not be decorated", Runner._file_failures)
        return self.exit_code()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
DESCRIPTION = """\
Add an official USD/INR rate to every date of one or more time-series CSVs.

For each input file a NEW file is written with four extra columns -- the rate,
the observation date the rate came from, the source, and how the value was
obtained -- so nothing about the decoration is implicit:
  usdinr      the USD/INR rate applied to that row
  fx_date     the date the rate was actually published for (== date when exact)
  fx_source   rbi | fbil | h10
  fx_status   exact | carried_forward | missing
"""

EPILOG = """\
SOURCES (all official; no aggregator or scraped rate site is ever used)
  rbi   Reserve Bank of India reference rate, from RBI's own "Reference Rate
        Archive" (www.rbi.org.in/Scripts/ReferenceRateArchive.aspx).  RBI
        publishes it for every business day from 25-Aug-1998 to 09-Jul-2018 and
        again from 2023 onward.  NOTE: RBI's archive currently returns an EMPTY
        table for the whole of 2019-2022 and for 10-Jul-2018..2018-12-31; those
        dates are a coverage gap at RBI (checked from several sessions, all
        currencies) and are filled from FBIL, which is the publisher for that
        era anyway.  The run reports every such window explicitly.
  fbil  FBIL USD/INR reference rate, straight from the benchmark administrator
        (www.fbil.org.in/wasdm/refrates/fetchfiltered), published 13:30 IST on
        business days from 10-Jul-2018 onward.
  h10   US Federal Reserve H.10 "noon buying rate" for India (series DEXINUS,
        mirrored as a CSV by FRED: fred.stlouisfed.org/graph/fredgraph.csv).
        Covers 1973-01-02 to the present; blank on US holidays.

  --source ORDER sets the priority order (default: auto = rbi fbil h10).  A date
  is taken from the first source that publishes it and the fx_source column
  always records which one.  With 'auto' a source is queried only for the dates
  earlier sources did not cover, so the default run is a single pass over RBI.

FILL RULE
  A date with no published rate is filled with the last rate used for the
  previous date (carry-forward) and marked fx_status=carried_forward, with
  fx_date naming the observation it came from.  Dates before the first published
  rate of every source cannot be filled at all: fx_status=missing, left blank,
  reported as ERRORS.  Carry-forwards older than --stale-warn-days are WARNINGS.

AUDITING -- every failure is written down and reported
  chunk_status.csv   one row per downloaded window: status, rows, issues,
                     warnings, last error.   Statuses: OK; EMPTY (the source does
                     not cover that window); SANITY_FAILED (data kept on disk but
                     NOT used unless --waive-sanity); ABANDONED (still failing
                     after all retries); PENDING.
  attempt_ledger.csv every HTTP attempt, its outcome and its error message.
  fx_audit.csv       one row per event: unfillable dates (ERROR), carry-forwards
                     and stale carry-forwards (WARNING), unparseable/duplicate
                     dates in the input, implausible rates, cross-check
                     deviations and per-file failures.
  console            a final SUMMARY block: abandoned windows, every failure, and
                     the first --report-limit warnings; the complete list is
                     always in fx_audit.csv.
  Raw responses and parsed tables are cached under <work-dir>/cache/, so re-runs,
  --offline runs and --status never touch the network.

EXIT CODES
  0  every row decorated, no abandoned window      1  some rows unfillable /
     abandoned windows / an input file failed      2  fatal error     130 Ctrl+C

OUTPUT FILES
  <--out-dir, or the input's own directory>/<name><--suffix>.csv   decorated data
  <work-dir>/{state.json,chunk_status.csv,attempt_ledger.csv,fx_audit.csv,
              cache/,logs/run_*.log}                               run bookkeeping

EXAMPLES
  # Decorate both merged NSE files, writing *_fx.csv next to each input:
  python nse_fx_decorator.py nse_index_data/merged/NIFTY_500_price_and_tri.csv \\
                             nse_index_data/merged/NIFTY_50_price_and_tri.csv

  # Write elsewhere and use RBI's own series only (no fallback sources):
  python nse_fx_decorator.py merged/*.csv --out-dir decorated --source rbi

  # Also cross-check the three official sources against each other:
  python nse_fx_decorator.py merged/NIFTY_50_price_and_tri.csv --cross-check

  # Re-run later (uses the cache), re-download everything, or stay offline:
  python nse_fx_decorator.py merged/*.csv
  python nse_fx_decorator.py merged/*.csv --refresh
  python nse_fx_decorator.py merged/*.csv --offline
  python nse_fx_decorator.py --status            # no network, no writing

  # Accept a window that failed a sanity check (ids from chunk_status.csv):
  python nse_fx_decorator.py merged/*.csv --waive-sanity rbi.2019

DEPENDENCIES: pip install requests
RUNTIME: roughly 3-5 minutes for a 27-year RBI history -- one request per year plus a
single page fetch, spaced 2-7 s apart on purpose (the sources are third-party sites and
are not built for bulk traffic).  Re-runs reuse the cache and take seconds.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nse_fx_decorator.py", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("inputs", nargs="*", metavar="CSV",
                   help="One or more CSVs with a date column (for example the files under "
                        "nse_index_data/merged/). A new decorated file is written for each.")

    g = p.add_argument_group("output: what the decorated file looks like")
    g.add_argument("--out-dir", metavar="DIR",
                   help="Directory for the decorated files (default: next to each input file).")
    g.add_argument("--suffix", default="_fx", metavar="STR",
                   help="Appended to each input file name (default: %(default)s).")
    g.add_argument("--fx-col", default="usdinr", metavar="NAME",
                   help="Name of the added rate column (default: %(default)s).")
    g.add_argument("--precision", type=int, default=4, metavar="N",
                   help="Decimals for the rate column (default: %(default)s; use 6 to keep "
                        "FBIL's full published precision).")
    g.add_argument("--date-col", metavar="NAME",
                   help="Date column of the input (default: auto-detect 'date', then any name "
                        "containing 'date', then the first date-like column).")

    g = p.add_argument_group("FX sources")
    g.add_argument("--source", nargs="+", default=["auto"], metavar="S",
                   choices=["auto", "rbi", "fbil", "h10"],
                   help="Priority order of official sources: rbi (RBI reference rate, 1998+), "
                        "fbil (FBIL reference rate, 2018+), h10 (Fed H.10 via FRED, 1973+), or "
                        "auto = rbi fbil h10 (default: %(default)s).")

    g = p.add_argument_group("fill rule")
    g.add_argument("--stale-warn-days", type=int, default=5, metavar="N",
                   help="Warn when a carry-forward is older than N days (default: %(default)s).")

    g = p.add_argument_group("pacing: being polite to the servers")
    g.add_argument("--min-wait", type=float, default=2.0, metavar="SEC",
                   help="Minimum random pause between requests (default: %(default)s s).")
    g.add_argument("--max-wait", type=float, default=7.0, metavar="SEC",
                   help="Maximum random pause between requests (default: %(default)s s).")
    g.add_argument("--long-pause-every", type=int, default=20, metavar="N",
                   help="Longer pause every N requests; 0 disables (default: %(default)s).")
    g.add_argument("--long-pause-min", type=float, default=15.0, metavar="SEC",
                   help="Long pause minimum (default: %(default)s s).")
    g.add_argument("--long-pause-max", type=float, default=35.0, metavar="SEC",
                   help="Long pause maximum (default: %(default)s s).")
    g.add_argument("--user-agent", default=DEFAULT_UA, metavar="STR",
                   help="User-Agent for the RBI and FBIL sources (default: a desktop browser "
                        "string; they reject unfamiliar agents). The Fed/FRED source always "
                        "sends the HTTP library's own User-Agent, because FRED stalls on "
                        "browser-style agents from non-browser clients.")

    g = p.add_argument_group("reliability: timeouts, retries, back-off")
    g.add_argument("--max-retries", type=int, default=3, metavar="N",
                   help="Retries per window AFTER the first attempt (default: %(default)s).")
    g.add_argument("--backoff-base", type=float, default=5.0, metavar="SEC",
                   help="First back-off delay; doubles each retry (default: %(default)s s).")
    g.add_argument("--backoff-max", type=float, default=120.0, metavar="SEC",
                   help="Cap on a single back-off delay (default: %(default)s s).")
    g.add_argument("--connect-timeout", type=float, default=15.0, metavar="SEC",
                   help="TCP connect timeout (default: %(default)s s).")
    g.add_argument("--read-timeout", type=float, default=45.0, metavar="SEC",
                   help="Max silence while waiting for data (default: %(default)s s).")

    g = p.add_argument_group("audit and reporting")
    g.add_argument("--cross-check", action="store_true",
                   help="Download every source in the chain too, and report dates where they "
                        "disagree by more than --cross-tolerance-pct.")
    g.add_argument("--cross-tolerance-pct", type=float, default=1.0, metavar="PCT",
                   help="Tolerance for --cross-check deviations (default: %(default)s%%).")
    g.add_argument("--report-limit", type=int, default=25, metavar="N",
                   help="Failures/warnings printed per section of the console report (default: "
                        "%(default)s; the audit file always holds them all).")

    g = p.add_argument_group("maintenance")
    g.add_argument("--work-dir", default="fx_work", metavar="DIR",
                   help="Where state, cache, ledger and audit files live (default: %(default)s).")
    g.add_argument("--status", action="store_true",
                   help="Print the saved state of the windows and exit (no network, no writing).")
    g.add_argument("--refresh", action="store_true",
                   help="Ignore the cache and download every needed window again.")
    g.add_argument("--offline", action="store_true",
                   help="Use only what is already cached; never touch the network.")
    g.add_argument("--waive-sanity", nargs="+", metavar="CHUNK_ID",
                   help="Accept SANITY_FAILED window(s) as they are (ids from chunk_status.csv, "
                        "or 'all').")
    g.add_argument("--yes", action="store_true",
                   help="Answer 'yes' to the retry offer for abandoned windows (unattended runs).")
    g.add_argument("--no-prompt", action="store_true",
                   help="Never ask questions (the default too when stdin is not a terminal).")
    g.add_argument("-v", "--verbose", action="store_true", help="Show DEBUG messages on the console.")
    return p


def setup_logging(work: Path, verbose: bool) -> logging.Logger:
    (work / "logs").mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("fx")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(work / "logs" / f"run_{datetime.now():%Y%m%d_%H%M%S}.log",
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))
    log.addHandler(fh)
    log.addHandler(ch)
    return log


def validate(a, parser) -> None:
    if not 0 <= a.precision <= 10:
        parser.error("--precision must be between 0 and 10")
    if a.min_wait < 0 or a.max_wait < a.min_wait:
        parser.error("--max-wait must be >= --min-wait >= 0")
    if a.long_pause_max < a.long_pause_min:
        parser.error("--long-pause-max must be >= --long-pause-min")
    if a.max_retries < 0:
        parser.error("--max-retries must be >= 0")
    if a.stale_warn_days < 0:
        parser.error("--stale-warn-days must be >= 0")
    if a.cross_check and a.source == ["auto"]:
        a.source = list(AUTO_CHAIN)          # cross-checking needs them all anyway


def collect_dates(inputs: List[InputFile]) -> List[date]:
    needed = []
    for inp in inputs:
        for row in inp.rows:
            if inp.date_idx < len(row):
                d = parse_any_date(row[inp.date_idx])
                if d:
                    needed.append(d)
    return needed


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    validate(a, parser)
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    log = setup_logging(work, a.verbose)
    log.info("nse_fx_decorator %s | work-dir=%s | inputs=%d | source=%s",
             __version__, work, len(a.inputs), " ".join(a.source))
    if not a.inputs:
        if a.status:
            return Runner(a, log, [], []).run()
        log.critical("no input files given -- pass one or more CSVs (see --help)")
        return 2
    for name in (AUTO_CHAIN if "auto" in a.source else a.source):
        log.info("source %-4s : %s", name, SOURCE_TITLE[name])
    inputs, failures = [], 0
    for raw in a.inputs:
        try:
            inputs.append(read_input(Path(raw), a, log))
        except FatalError as e:
            failures += 1
            log.error("cannot use %s: %s", raw, e)
    if failures:
        log.error("%d input file(s) skipped", failures)
    if not inputs:
        log.critical("no usable input file -- nothing to do")
        return 2
    needed = collect_dates(inputs)
    if not needed:
        log.critical("no parseable dates in the input file(s) -- nothing to do")
        return 2
    log.info("needed dates: %d unique, %s .. %s", len(set(needed)),
             min(needed).isoformat(), max(needed).isoformat())
    Runner._file_failures = failures
    try:
        rc = Runner(a, log, inputs, needed).run()
    except FatalError as e:
        log.critical("FATAL: %s", e)
        return 2
    except KeyboardInterrupt:
        log.warning("interrupted; saved state is in %s -- re-run the same command to continue", work)
        return 130
    return 1 if (rc or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
