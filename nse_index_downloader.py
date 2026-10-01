#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nse_index_downloader.py -- resumable, polite, self-checking downloader for
NIFTY index PRICE and TOTAL RETURN (TRI) history from niftyindices.com.

Run with --help for the full documentation.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import pandas as pd
    import requests
except ImportError as _exc:  # pragma: no cover
    sys.exit(f"Missing dependency: {_exc.name}.  Install with:  pip install requests pandas")

__version__ = "1.0.1"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "https://www.niftyindices.com"
REFERER_PATH = "/reports/historical-data"
# Current endpoints (Aug-2025+ site): capital-B "/BackPage/", no ".aspx". The old
# "/Backpage.aspx/..." paths now 302 to the Sitefinity login page.
ENDPOINTS = {
    "price": "/BackPage/getHistoricaldatatabletoString",
    "tri": "/BackPage/getTotalReturnIndexString",
}
SERIES_ORDER = ["price", "tri"]
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PENDING, OK, SANITY_FAILED, ABANDONED, WAIVED = (
    "PENDING", "OK", "SANITY_FAILED", "ABANDONED", "WAIVED",
)
HAVE_DATA = {OK, SANITY_FAILED, WAIVED}      # statuses that have a CSV on disk
GOOD = {OK, WAIVED}                           # statuses accepted into merged files

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_LOOKUP = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_TEXT_RE = re.compile(r"^\s*(\d{1,2})[\s\-/,.]+([A-Za-z]{3,9})[\s\-/,.]+(\d{4})")
_NUM_RE = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DownloadError(Exception):
    """A retryable problem with one HTTP attempt (network, HTTP status, bad body)."""

    def __init__(self, msg, http_status=None, retry_after=None):
        super().__init__(msg)
        self.http_status = http_status
        self.retry_after = retry_after


class AttemptsExhausted(Exception):
    def __init__(self, last_error: str, attempts: int):
        super().__init__(last_error)
        self.last_error = last_error
        self.attempts = attempts


class SchemaError(Exception):
    """The response parsed as JSON but does not look like index data."""


class FatalError(Exception):
    """Unrecoverable problem; the run stops (state is saved first)."""


# --------------------------------------------------------------------------- #
# Date helpers (locale independent)
# --------------------------------------------------------------------------- #
def fmt_site_date(d: date) -> str:
    """01-Jan-2020 -- the format the site's own form posts (avoids %b locale issues)."""
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


def parse_any_date(value) -> Optional[date]:
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
        if m:  # day-first, as used in India
            return date(int(m[3]), int(m[2]), int(m[1]))
    except ValueError:
        return None
    return None


def last_day_of_month(y: int, m: int) -> date:
    return (date(y + (m == 12), (m % 12) + 1, 1)) - timedelta(days=1)


def quarter_bounds(d: date) -> Tuple[date, date]:
    q = (d.month - 1) // 3
    return date(d.year, 3 * q + 1, 1), last_day_of_month(d.year, 3 * q + 3)


def quarter_label(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def build_windows(first: date, last: date) -> List[Tuple[str, date, date]]:
    """Calendar-quarter windows covering [first, last]; edges are clipped."""
    out, cur = [], first
    while cur <= last:
        _, qe = quarter_bounds(cur)
        out.append((quarter_label(cur), cur, min(qe, last)))
        cur = qe + timedelta(days=1)
    return out


def preview(dates: List[date], n: int = 6) -> str:
    txt = ", ".join(d.isoformat() for d in dates[:n])
    return txt + (f" (+{len(dates) - n} more)" if len(dates) > n else "")


def iso_date_arg(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{s}' is not a valid YYYY-MM-DD date")


# --------------------------------------------------------------------------- #
# Response normalisation
# --------------------------------------------------------------------------- #
def to_num(v) -> float:
    if v is None:
        return float("nan")
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--", "nan", "NaN", "None", "null"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def normalise(series: str, records: List[dict]) -> "pd.DataFrame":
    cols = ["date", "open", "high", "low", "close"] if series == "price" else ["date", "tri", "ntr"]
    if not records:
        return pd.DataFrame(columns=cols)
    if not isinstance(records[0], dict):
        raise SchemaError(f"records are not JSON objects (first item: {str(records[0])[:80]!r})")
    keys = list(records[0].keys())
    low = {k.lower(): k for k in keys}
    dkey = next((k for k in keys if "date" in k.lower()), None)
    if dkey is None:
        raise SchemaError(f"no date field among keys {keys}")
    rows = []
    if series == "price":
        m = {c: low.get(c) for c in ("open", "high", "low", "close")}
        if m["close"] is None:
            raise SchemaError(f"no CLOSE field among keys {keys}")
        for r in records:
            rows.append({"date": parse_any_date(r.get(dkey)),
                         **{c: to_num(r.get(k)) if k else float("nan") for c, k in m.items()}})
    else:
        tri_key = next((k for k in keys if re.search(r"total.*return|^tri", k.lower())
                        and not re.search(r"net|ntr", k.lower())), None)
        ntr_key = next((k for k in keys if re.search(r"ntr|net", k.lower())), None)
        if tri_key is None:
            raise SchemaError(f"no Total Returns Index field among keys {keys}")
        for r in records:
            rows.append({"date": parse_any_date(r.get(dkey)),
                         "tri": to_num(r.get(tri_key)),
                         "ntr": to_num(r.get(ntr_key)) if ntr_key else float("nan")})
    df = pd.DataFrame(rows, columns=cols)
    df = df.sort_values("date", kind="stable", na_position="last").reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# Sanity checking
# --------------------------------------------------------------------------- #
@dataclass
class Peer:
    chunk_id: str
    start: date
    end: date
    dates: Set[date]


def load_holidays(path: Optional[str]) -> Tuple[Set[date], Set[int]]:
    if not path:
        return set(), set()
    hol: Set[date] = set()
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            for cell in re.split(r"[,;\t]", line):
                d = parse_any_date(cell)
                if d:
                    hol.add(d)
                    break
    if not hol:
        raise FatalError(f"--holidays-csv {path}: no parseable dates found")
    return hol, {d.year for d in hol}


def sanity_check(series: str, ws: date, we: date, df: "pd.DataFrame", peers: List[Peer],
                 holidays: Set[date], holiday_years: Set[int], a) -> Tuple[List[str], List[str]]:
    """Return (issues, warnings). Any issue => chunk is SANITY_FAILED."""
    issues: List[str] = []
    warns: List[str] = []
    n = len(df)
    if n == 0:
        return ["empty response: no rows returned for this window"], warns

    dates = df["date"].tolist()
    n_bad = sum(1 for d in dates if d is None)
    if n_bad:
        issues.append(f"{n_bad} row(s) with missing/unparseable date")
    good = [d for d in dates if d is not None]
    dset = set(good)
    if len(good) != len(dset):
        issues.append(f"{len(good) - len(dset)} duplicate date row(s)")
    outside = sorted(d for d in dset if d < ws or d > we)
    if outside:
        issues.append(f"{len(outside)} row(s) dated outside the chunk window: {preview(outside)}")
    inwin = sorted(d for d in dset if ws <= d <= we)

    # ---- values -----------------------------------------------------------
    req = "close" if series == "price" else "tri"
    opt = ["open", "high", "low"] if series == "price" else ["ntr"]
    n_nan = int(df[req].isna().sum())
    n_nonpos = int((df[req] <= 0).sum())
    if n_nan:
        issues.append(f"{n_nan} row(s) with blank/non-numeric {req.upper()}")
    if n_nonpos:
        issues.append(f"{n_nonpos} row(s) with {req.upper()} <= 0")
    for c in opt:
        k = int(df[c].isna().sum())
        if k == n:
            warns.append(f"column {c.upper()} is entirely blank")
        elif k:
            warns.append(f"column {c.upper()} blank on {k} row(s)")
    if series == "price":
        bad_hl = int(((df["high"] < df["low"]) & df["high"].notna() & df["low"].notna()).sum())
        if bad_hl:
            warns.append(f"{bad_hl} row(s) with HIGH < LOW")

    # ---- weekend rows (legit for special sessions, e.g. Budget day) -------
    wk = sorted(d for d in inwin if d.weekday() >= 5)
    if wk:
        warns.append(f"{len(wk)} weekend row(s) (special session?): {preview(wk)}")

    # ---- coverage vs calendar --------------------------------------------
    weekdays = [ws + timedelta(days=i) for i in range((we - ws).days + 1)
                if (ws + timedelta(days=i)).weekday() < 5]
    exact = bool(holidays) and all(y in holiday_years for y in range(ws.year, we.year + 1))
    if exact:
        missing = sorted(set(weekdays) - holidays - dset)
        if missing:
            issues.append(f"{len(missing)} trading day(s) missing vs holiday calendar: {preview(missing)}")
        on_hol = sorted(dset & holidays)
        if on_hol:
            warns.append(f"{len(on_hol)} row(s) on listed holiday(s) (Muhurat session?): {preview(on_hol)}")
    else:
        missing = [d for d in weekdays if d not in dset]
        allowed = max(2, math.ceil(a.max_missing_weekdays_pct / 100.0 * len(weekdays)))
        if len(missing) > allowed:
            issues.append(f"{len(missing)} of {len(weekdays)} weekdays have no row "
                          f"(tolerance {allowed} for holidays; no exact calendar): {preview(missing)}")

    # ---- boundaries and gaps ---------------------------------------------
    if inwin:
        first, last = inwin[0], inwin[-1]
        if (first - ws).days > a.boundary_tolerance_days:
            issues.append(f"first row {first} is {(first - ws).days} days after window start {ws}")
        if (we - last).days > a.boundary_tolerance_days:
            issues.append(f"last row {last} is {(we - last).days} days before window end {we}")
        for prev, cur in zip(inwin, inwin[1:]):
            if (cur - prev).days > a.max_gap_days:
                issues.append(f"gap of {(cur - prev).days} calendar days between {prev} and {cur}")
    else:
        issues.append("no rows fall inside the chunk window")

    # ---- cross-series reconciliation --------------------------------------
    if peers:
        cand: Set[date] = set()
        for p in peers:
            cand |= {d for d in p.dates if ws <= d <= we}
        miss_peer = []
        for d in sorted(cand):
            covering = [p for p in peers if p.start <= d <= p.end]
            k = len(covering)
            if k == 0:
                continue
            cnt = sum(1 for p in covering if d in p.dates)
            if cnt >= (k + 1) // 2 and d not in dset:
                miss_peer.append(d)
        if miss_peer:
            issues.append(f"{len(miss_peer)} date(s) present in peer series but missing here: {preview(miss_peer)}")
        extra = []
        for d in inwin:
            covering = [p for p in peers if p.start <= d <= p.end]
            if len(covering) >= 2 and not any(d in p.dates for p in covering):
                extra.append(d)
        if extra:
            warns.append(f"{len(extra)} date(s) present here but in none of the peer series: {preview(extra)}")
    return issues, warns


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, args, log: logging.Logger):
        self.a = args
        self.log = log
        self.out = Path(args.out_dir)
        self.state_path = self.out / "state.json"
        self.ledger_path = self.out / "attempt_ledger.csv"
        self.status_csv = self.out / "chunk_status.csv"
        self.state = self._load_state()
        self.chunks: Dict[str, dict] = self.state["chunks"]
        self.plan: List[str] = []
        self.holidays, self.holiday_years = load_holidays(args.holidays_csv)
        self._sess: Optional["requests.Session"] = None
        self._df_cache: Dict[str, Tuple[float, "pd.DataFrame"]] = {}
        self._schema_errors = 0
        self._request_count = 0
        self._used_auto = False
        self._auto_rounds = 0

    # ---- state ------------------------------------------------------------
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    st = json.load(fh)
                st.setdefault("earliest", {})
                st.setdefault("chunks", {})
                return st
            except (OSError, json.JSONDecodeError) as e:
                raise FatalError(f"cannot read {self.state_path}: {e} "
                                 f"(fix or delete it; downloaded CSVs are kept in chunks/)")
        return {"version": 1, "created": datetime.now().isoformat(timespec="seconds"),
                "earliest": {}, "chunks": {}}

    def save_state(self) -> None:
        self.state["updated"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)
        with open(self.status_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["chunk_id", "index", "series", "quarter", "start", "end", "status", "rows",
                        "attempts_total", "issues", "warnings", "last_error", "file", "updated_at"])
            for cid in sorted(self.chunks):
                r = self.chunks[cid]
                w.writerow([cid, r["index"], r["series"], r["label"], r["start"], r["end"], r["status"],
                            r["rows"], r["attempts_total"], " | ".join(r["issues"]),
                            " | ".join(r["warnings"]), r["last_error"], r["file"], r["updated_at"]])

    def _ledger(self, cid: str, attempt, outcome: str, http, rows, msg: str) -> None:
        new = not self.ledger_path.exists()
        with open(self.ledger_path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["timestamp", "chunk_id", "attempt", "outcome", "http_status", "rows", "message"])
            w.writerow([datetime.now().isoformat(timespec="seconds"), cid, attempt, outcome,
                        http if http is not None else "", rows if rows is not None else "", msg])

    # ---- HTTP -------------------------------------------------------------
    def _session(self) -> "requests.Session":
        if self._sess is None:
            s = requests.Session()
            base = self.a.base_url.rstrip("/")
            s.headers.update({
                "User-Agent": self.a.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": base,
                "Referer": base + REFERER_PATH,
            })
            try:  # best-effort warm-up so any session cookies are set
                s.get(base + REFERER_PATH, timeout=(self.a.connect_timeout, self.a.read_timeout))
            except requests.exceptions.RequestException as e:
                self.log.debug("session warm-up failed (continuing): %s", e)
            self._sess = s
        return self._sess

    def _reset_session(self) -> None:
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception:
                pass
        self._sess = None

    @staticmethod
    def _read_body(resp, deadline: float) -> bytes:
        parts = []
        for part in resp.iter_content(65536):
            if time.monotonic() > deadline:
                raise DownloadError("hard timeout exceeded while reading response (hang)")
            parts.append(part)
        return b"".join(parts)

    def _fetch_once(self, index: str, series: str, ws: date, we: date) -> Tuple[List[dict], str]:
        url = self.a.base_url.rstrip("/") + ENDPOINTS[series]
        cinfo = "{'name':'%s','startDate':'%s','endDate':'%s','indexName':'%s'}" % (
            index, fmt_site_date(ws), fmt_site_date(we), index)
        sess = self._session()
        deadline = time.monotonic() + self.a.hard_timeout
        self._request_count += 1
        try:
            resp = sess.post(url, json={"cinfo": cinfo}, stream=True,
                             timeout=(self.a.connect_timeout, self.a.read_timeout))
            try:
                status = resp.status_code
                retry_after = resp.headers.get("Retry-After")
                body = self._read_body(resp, deadline)
            finally:
                resp.close()
        except requests.exceptions.Timeout as e:
            raise DownloadError(f"timeout ({type(e).__name__})")
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"network error: {type(e).__name__}: {e}")

        if status != 200:
            hint = " (rate-limited/blocked? raise --min-wait/--max-wait)" if status in (403, 429) else ""
            ra = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
            raise DownloadError(f"HTTP {status}{hint}", http_status=status, retry_after=ra)
        text = body.decode("utf-8", errors="replace")
        try:
            outer = json.loads(text)
        except json.JSONDecodeError:
            raise DownloadError(f"response is not JSON (got {text[:80]!r}...); "
                                f"possibly an HTML block/login page", http_status=status)
        if isinstance(outer, list):        # current site layout: a bare JSON array of records
            return outer, text
        # legacy ASP.NET layout: {"d": "<json string>"}
        if not isinstance(outer, dict) or "d" not in outer:
            msg = outer.get("Message") if isinstance(outer, dict) else None
            raise DownloadError(f"server error / unexpected payload: {msg or str(outer)[:100]}", http_status=status)
        d = outer["d"]
        if d is None or (isinstance(d, str) and not d.strip()):
            return [], ""
        try:
            records = json.loads(d) if isinstance(d, str) else d
        except json.JSONDecodeError:
            raise DownloadError(f"inner 'd' payload is not valid JSON: {str(d)[:80]!r}", http_status=status)
        if not isinstance(records, list):
            raise DownloadError(f"inner payload is not a list: {str(records)[:100]}", http_status=status)
        return records, d if isinstance(d, str) else json.dumps(d)

    def _sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def _polite_wait(self) -> None:
        n = self.a.long_pause_every
        if n and self._request_count and self._request_count % n == 0:
            secs = random.uniform(self.a.long_pause_min, self.a.long_pause_max)
            self.log.info("  long courtesy pause: %.0fs (every %d requests)", secs, n)
        else:
            secs = random.uniform(self.a.min_wait, self.a.max_wait)
            self.log.debug("  waiting %.1fs before next request", secs)
        self._sleep(secs)

    def _attempt_loop(self, cid: str, label: str, index: str, series: str,
                      ws: date, we: date) -> Tuple[List[dict], str, int]:
        """1 initial attempt + max_retries retries, exponential back-off with jitter."""
        max_attempts = self.a.max_retries + 1
        last_err = "unknown error"
        for attempt in range(1, max_attempts + 1):
            try:
                records, raw = self._fetch_once(index, series, ws, we)
                self._ledger(cid, attempt, "DOWNLOADED", 200, len(records), "")
                return records, raw, attempt
            except DownloadError as e:
                last_err = str(e)
                self._ledger(cid, attempt, "ERROR", e.http_status, 0, last_err)
                self.log.warning("%s: attempt %d/%d failed: %s", label, attempt, max_attempts, last_err)
                self._reset_session()          # fresh connection/cookies for the next try
                if attempt < max_attempts:
                    delay = min(self.a.backoff_max, self.a.backoff_base * (2 ** (attempt - 1)))
                    delay *= random.uniform(0.8, 1.3)
                    if e.retry_after:
                        delay = max(delay, min(e.retry_after, self.a.backoff_max))
                    self.log.info("  exponential back-off: sleeping %.1fs before retry %d/%d",
                                  delay, attempt, self.a.max_retries)
                    self._sleep(delay)
        raise AttemptsExhausted(last_err, max_attempts)

    # ---- earliest-date discovery -----------------------------------------
    def ensure_earliest(self) -> None:
        for index in self.a.indices:
            for series in self.a.series:
                key = f"{index}|{series}"
                if key in self.state["earliest"] and not self.a.rediscover:
                    continue
                self.state["earliest"][key] = self._discover(index, series).isoformat()
                self.save_state()

    def _discover(self, index: str, series: str) -> date:
        tag = f"{index} | {series}"
        self.log.info("Discovering earliest available date for %s (probing from %s) ...",
                      tag, self.a.probe_from)
        first_year = None
        for year in range(self.a.probe_from.year, self.a.end_date.year + 1):
            ws = max(date(year, 1, 1), self.a.probe_from)
            we = min(date(year, 12, 31), self.a.end_date)
            try:
                recs, _, _ = self._attempt_loop(f"PROBE.{index}.{series}.{year}", f"probe {tag} {year}",
                                                index, series, ws, we)
            except AttemptsExhausted as e:
                raise FatalError(f"earliest-date discovery failed for {tag} at year {year}: {e.last_error}. "
                                 f"Try a later --probe-from (e.g. 1995-01-01) or re-run later.")
            self._polite_wait()
            if recs:
                first_year = year
                break
        if first_year is None:
            raise FatalError(f"no data at all found for {tag} between {self.a.probe_from} and {self.a.end_date}. "
                             f"Check the index name (must match the site's drop-down exactly).")
        for label, ws, we in build_windows(max(date(first_year, 1, 1), self.a.probe_from),
                                           min(date(first_year, 12, 31), self.a.end_date)):
            try:
                recs, _, _ = self._attempt_loop(f"PROBE.{index}.{series}.{label}", f"probe {tag} {label}",
                                                index, series, ws, we)
            except AttemptsExhausted as e:
                raise FatalError(f"earliest-date refinement failed for {tag} at {label}: {e.last_error}")
            self._polite_wait()
            try:
                df = normalise(series, recs)
            except SchemaError as e:
                raise FatalError(f"unrecognised response layout for {tag}: {e}")
            ds = [d for d in df["date"].tolist() if d is not None]
            if ds:
                earliest = min(ds)
                self.log.info("  earliest available date for %s: %s", tag, earliest)
                return earliest
        raise FatalError(f"discovery inconsistency for {tag}: data seen in {first_year} but not in any quarter")

    # ---- planning ---------------------------------------------------------
    def build_plan(self) -> None:
        self.plan = []
        idx_order = {n: i for i, n in enumerate(self.a.indices)}
        entries = []
        for index in self.a.indices:
            for series in self.a.series:
                key = f"{index}|{series}"
                if key not in self.state["earliest"]:
                    raise FatalError(f"earliest date for {index}/{series} unknown -- run once without "
                                     f"--retry-only/--status so it can be discovered.")
                first = date.fromisoformat(self.state["earliest"][key])
                if first > self.a.end_date:
                    continue
                slug = index.upper().replace(" ", "_")
                for label, ws, we in build_windows(first, self.a.end_date):
                    cid = f"{slug}.{series}.{label}"
                    rec = self.chunks.get(cid)
                    if rec is None or rec["start"] != ws.isoformat() or rec["end"] != we.isoformat():
                        rec = {"index": index, "series": series, "label": label,
                               "start": ws.isoformat(), "end": we.isoformat(), "status": PENDING,
                               "rows": None, "attempts_total": 0, "last_error": "", "issues": [],
                               "warnings": [], "file": "", "raw_file": "", "updated_at": None}
                        self.chunks[cid] = rec
                    entries.append((quarter_bounds(ws)[0], idx_order[index], SERIES_ORDER.index(series), cid))
        entries.sort()
        self.plan = [e[3] for e in entries]
        if self.a.force:
            for cid in self.plan:
                self.chunks[cid].update(status=PENDING, issues=[], warnings=[], last_error="")
        self.save_state()

    # ---- disk helpers -----------------------------------------------------
    def _paths(self, cid: str) -> Tuple[Path, Path]:
        r = self.chunks[cid]
        slug = r["index"].upper().replace(" ", "_")
        stem = f"{cid}_{r['start']}_{r['end']}"
        return (self.out / "chunks" / slug / r["series"] / f"{stem}.csv",
                self.out / "raw" / slug / r["series"] / f"{stem}.json")

    def _load_df(self, cid: str) -> "pd.DataFrame":
        path = self.out / self.chunks[cid]["file"]
        mtime = path.stat().st_mtime
        cached = self._df_cache.get(cid)
        if cached and cached[0] == mtime:
            return cached[1]
        df = pd.read_csv(path, dtype={"date": str})
        df["date"] = df["date"].map(parse_any_date)
        self._df_cache[cid] = (mtime, df)
        return df

    # ---- sanity evaluation --------------------------------------------------
    def _peers_for(self, cid: str) -> List[Peer]:
        me = self.chunks[cid]
        peers = []
        for oid in self.plan:
            o = self.chunks[oid]
            if oid == cid or o["label"] != me["label"] or o["status"] not in HAVE_DATA or not o["file"]:
                continue
            df = self._load_df(oid)
            peers.append(Peer(oid, date.fromisoformat(o["start"]), date.fromisoformat(o["end"]),
                              {d for d in df["date"] if d is not None}))
        return peers

    def evaluate_chunk(self, cid: str) -> None:
        rec = self.chunks[cid]
        if rec["status"] not in (OK, SANITY_FAILED) or not rec["file"]:
            return
        before = rec["status"]
        issues, warns = sanity_check(rec["series"], date.fromisoformat(rec["start"]),
                                     date.fromisoformat(rec["end"]), self._load_df(cid),
                                     self._peers_for(cid), self.holidays, self.holiday_years, self.a)
        rec["issues"], rec["warnings"] = issues, warns
        rec["status"] = SANITY_FAILED if issues else OK
        if rec["status"] != before or issues:
            level = logging.WARNING if issues else logging.INFO
            self.log.log(level, "  sanity %s for %s%s", "FAILED" if issues else "now passes", cid,
                         (": " + "; ".join(issues)) if issues else "")

    def evaluate_all(self) -> None:
        for cid in self.plan:
            self.evaluate_chunk(cid)
        self.save_state()

    # ---- downloading a chunk -----------------------------------------------
    def download_chunk(self, cid: str, tag: str = "") -> None:
        rec = self.chunks[cid]
        ws, we = date.fromisoformat(rec["start"]), date.fromisoformat(rec["end"])
        label = f"{tag}{rec['index']} | {rec['series']} | {rec['label']} ({ws} -> {we})"
        self.log.info("%s", label)
        try:
            records, raw, attempts = self._attempt_loop(cid, label, rec["index"], rec["series"], ws, we)
        except AttemptsExhausted as e:
            rec.update(status=ABANDONED, last_error=e.last_error, issues=[], warnings=[],
                       updated_at=datetime.now().isoformat(timespec="seconds"))
            rec["attempts_total"] += e.attempts
            self._ledger(cid, "-", "ABANDONED", None, 0, e.last_error)
            self.log.error("  ABANDONED after %d attempts: %s", e.attempts, e.last_error)
            self.save_state()
            return
        rec["attempts_total"] += attempts
        try:
            df = normalise(rec["series"], records)
        except SchemaError as e:
            self._schema_errors += 1
            rec.update(status=ABANDONED, last_error=f"schema error: {e}",
                       updated_at=datetime.now().isoformat(timespec="seconds"))
            self._ledger(cid, "-", "SCHEMA_ERROR", None, len(records), str(e))
            self.log.error("  response layout not recognised: %s", e)
            self.save_state()
            if self._schema_errors >= 3:
                dbg = self.out / "debug_last_response.json"
                dbg.write_text(raw[:20000], encoding="utf-8")
                raise FatalError(f"3 consecutive unrecognised responses -- the site's layout probably changed. "
                                 f"A sample was saved to {dbg}. Adjust normalise() accordingly.")
            return
        self._schema_errors = 0

        csv_path, raw_path = self._paths(cid)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(csv_path, index=False)
        raw_path.write_text(raw, encoding="utf-8")
        self._df_cache.pop(cid, None)
        rec.update(status=OK, rows=len(df), file=str(csv_path.relative_to(self.out)),
                   raw_file=str(raw_path.relative_to(self.out)), last_error="",
                   updated_at=datetime.now().isoformat(timespec="seconds"))
        self.log.info("  downloaded %d rows", len(df))
        self.evaluate_chunk(cid)
        self._ledger(cid, "-", "SANITY_OK" if rec["status"] == OK else "SANITY_FAILED", None, len(df),
                     " | ".join(rec["issues"]))
        self.save_state()

    def download_batch(self, cids: List[str]) -> None:
        n = len(cids)
        for i, cid in enumerate(cids, 1):
            self.download_chunk(cid, tag=f"[{i}/{n}] ")
            nxt = self.chunks[cids[i]]["label"] if i < n else None
            if nxt != self.chunks[cid]["label"]:      # quarter finished: cross-check with all peers
                for oid in self.plan:
                    if self.chunks[oid]["label"] == self.chunks[cid]["label"]:
                        self.evaluate_chunk(oid)
                self.save_state()
            if i < n:
                self._polite_wait()

    def download_pending(self) -> None:
        todo = [c for c in self.plan if self.chunks[c]["status"] == PENDING]
        self.log.info("Chunks planned: %d | already done: %d | to download now: %d",
                      len(self.plan), len(self.plan) - len(todo), len(todo))
        if len(self.a.indices) * len(self.a.series) == 1 and not self.holidays:
            self.log.warning("Only one series selected and no --holidays-csv: cross-series reconciliation is "
                             "unavailable, so sanity checking is weaker (structure/gaps/coverage heuristics only).")
        self.download_batch(todo)

    # ---- retry offers ---------------------------------------------------------
    def _ask(self, question: str) -> bool:
        try:
            return input(f"\n{question} [y/N]: ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def _decide(self, kind: str, count: int) -> bool:
        flag = self.a.retry_abandoned if kind == "abandoned" else self.a.retry_sanity_failed
        if flag or self.a.yes:
            if self._auto_rounds < self.a.max_auto_retry_rounds:
                self._used_auto = True
                return True
            self.log.info("Auto-retry limit (%d round(s)) reached; not retrying %d %s chunk(s) again "
                          "automatically.", self.a.max_auto_retry_rounds, count, kind)
            return False
        if self.a.no_prompt or not sys.stdin.isatty():
            return False
        what = ("could not be downloaded even after all retries (ABANDONED)" if kind == "abandoned"
                else "were downloaded but FAILED the sanity check")
        return self._ask(f"{count} chunk(s) {what}. Retry downloading them now?")

    def retry_loop(self) -> None:
        while True:
            sf = [c for c in self.plan if self.chunks[c]["status"] == SANITY_FAILED]
            ab = [c for c in self.plan if self.chunks[c]["status"] == ABANDONED]
            if not sf and not ab:
                return
            self.print_failures(sf, ab)
            chosen: List[str] = []
            self._used_auto = False
            if ab and self._decide("abandoned", len(ab)):
                chosen += ab
            if sf and self._decide("sanity", len(sf)):
                chosen += sf
            if not chosen:
                return
            if self._used_auto:
                self._auto_rounds += 1
            chosen.sort(key=self.plan.index)
            self.log.info("Retrying %d chunk(s) ...", len(chosen))
            self.download_batch(chosen)
            self.evaluate_all()

    # ---- output ---------------------------------------------------------------
    def merge_outputs(self) -> None:
        mdir = self.out / "merged"
        mdir.mkdir(exist_ok=True)
        for index in self.a.indices:
            slug = index.upper().replace(" ", "_")
            mine = [c for c in self.plan if self.chunks[c]["index"] == index]
            if not mine:
                continue
            partial = any(self.chunks[c]["status"] not in GOOD for c in mine)
            suffix = "_PARTIAL" if partial else ""
            frames = {}
            for series in self.a.series:
                dfs = [self._load_df(c) for c in mine
                       if self.chunks[c]["series"] == series and self.chunks[c]["status"] in GOOD
                       and (self.chunks[c]["rows"] or 0) > 0]
                if not dfs:
                    continue
                df = pd.concat(dfs, ignore_index=True).dropna(subset=["date"])
                df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
                frames[series] = df
                self._write_merged(df, mdir, f"{slug}_{series}", suffix)
            if "price" in frames and "tri" in frames:
                both = frames["price"].merge(frames["tri"], on="date", how="outer").sort_values("date")
                self._write_merged(both, mdir, f"{slug}_price_and_tri", suffix)

    @staticmethod
    def _write_merged(df: "pd.DataFrame", mdir: Path, stem: str, suffix: str) -> None:
        other = mdir / (f"{stem}{'' if suffix else '_PARTIAL'}.csv")
        if other.exists():
            other.unlink()
        df.to_csv(mdir / f"{stem}{suffix}.csv", index=False)

    def print_failures(self, sf: List[str], ab: List[str]) -> None:
        if sf:
            self.log.warning("---- chunks that FAILED the sanity check (%d) ----", len(sf))
            for c in sf:
                r = self.chunks[c]
                self.log.warning("  %s [%s..%s] rows=%s: %s", c, r["start"], r["end"], r["rows"],
                                 "; ".join(r["issues"]))
        if ab:
            self.log.error("---- chunks ABANDONED after %d attempts (%d) ----", self.a.max_retries + 1, len(ab))
            for c in ab:
                r = self.chunks[c]
                self.log.error("  %s [%s..%s]: %s", c, r["start"], r["end"], r["last_error"])

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in (OK, WAIVED, SANITY_FAILED, ABANDONED, PENDING)}
        for c in self.plan:
            out[self.chunks[c]["status"]] += 1
        return out

    def print_report(self) -> None:
        cnt = self.counts()
        sf = [c for c in self.plan if self.chunks[c]["status"] == SANITY_FAILED]
        ab = [c for c in self.plan if self.chunks[c]["status"] == ABANDONED]
        pend = [c for c in self.plan if self.chunks[c]["status"] == PENDING]
        self.log.info("=" * 72)
        self.log.info("SUMMARY: %d chunks | OK %d | WAIVED %d | SANITY_FAILED %d | ABANDONED %d | PENDING %d",
                      len(self.plan), cnt[OK], cnt[WAIVED], cnt[SANITY_FAILED], cnt[ABANDONED], cnt[PENDING])
        self.print_failures(sf, ab)
        if pend:
            self.log.warning("---- chunks not yet attempted (%d): re-run the same command to continue ----", len(pend))
        self.log.info("Per-chunk status : %s", self.status_csv)
        self.log.info("Attempt ledger   : %s", self.ledger_path)
        self.log.info("Merged data      : %s", self.out / "merged")
        if sf or ab or pend:
            self.log.info("To retry later   : python %s --out-dir %s --retry-only   (add --yes to skip the prompt)",
                          Path(sys.argv[0]).name, self.a.out_dir)
        self.log.info("=" * 72)

    def exit_code(self) -> int:
        c = self.counts()
        return 0 if not (c[SANITY_FAILED] or c[ABANDONED] or c[PENDING]) else 1

    # ---- entry ------------------------------------------------------------------
    def waive(self) -> None:
        ids = set(self.a.waive_sanity)
        hit = 0
        for cid in self.plan:
            r = self.chunks[cid]
            if r["status"] == SANITY_FAILED and ("all" in ids or cid in ids):
                r["status"] = WAIVED
                r["warnings"] = r["warnings"] + [f"sanity issues waived by user on "
                                                 f"{datetime.now():%Y-%m-%d %H:%M}: " + "; ".join(r["issues"])]
                hit += 1
                self.log.info("Waived sanity failure for %s", cid)
        self.save_state()
        self.log.info("%d chunk(s) waived. (Only SANITY_FAILED chunks can be waived; ABANDONED ones have no data.)", hit)

    def run(self) -> int:
        try:
            if self.a.status or self.a.waive_sanity or self.a.retry_only:
                self.build_plan()
                if self.a.status:
                    self.print_report()
                    return self.exit_code()
                if self.a.waive_sanity:
                    self.waive()
                    self.merge_outputs()
                    self.print_report()
                    return self.exit_code()
            else:
                self.ensure_earliest()
                self.build_plan()
                self.download_pending()
            self.evaluate_all()
            self.retry_loop()
            self.evaluate_all()
            self.merge_outputs()
            self.print_report()
            return self.exit_code()
        except KeyboardInterrupt:
            self.save_state()
            self.log.warning("Interrupted by user. Progress is saved; re-run the same command to resume.")
            return 130
        except FatalError as e:
            self.save_state()
            self.log.critical("FATAL: %s", e)
            return 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
DESCRIPTION = textwrap.dedent("""\
    nse_index_downloader.py  --  download NIFTY index PRICE and TOTAL RETURN (TRI)
    history from NSE Indices Ltd (niftyindices.com), in polite, resumable 3-month chunks,
    with per-chunk sanity checks and interactive/automatic retry of failures.

    WHAT IT DOES
      1. Discovers, for each index x series, the earliest date the site has data for.
      2. Splits [earliest .. --end-date] into calendar-quarter (3-month) chunks
         (Jan-Mar, Apr-Jun, Jul-Sep, Oct-Dec; the first/last chunk are clipped).
      3. Downloads one chunk at a time with random waits in between.
      4. Retries a failing chunk up to --max-retries times with exponential back-off.
      5. Sanity-checks every chunk (see SANITY CHECKS below).
      6. Records every outcome (log file, attempt ledger, state file, status CSV).
      7. At the end, OFFERS to retry (a) chunks that failed sanity checks and
         (b) chunks abandoned after all retries.
      8. Merges good chunks into one CSV per index/series, plus price+TRI combined.

    DATA SOURCE
      The same back-end calls the "Historical Data" page makes in your browser
      (endpoints as of the Aug-2025 site rewrite; the older "/Backpage.aspx/..." paths
      now redirect to a login page and are gone):
        POST <base>/BackPage/getHistoricaldatatabletoString   (price: OHLC)
        POST <base>/BackPage/getTotalReturnIndexString        (TRI and Net TRI)
      Both answer with a bare JSON array of records (older builds wrapped them in
      {"d": "<json string>"}; both layouts are accepted). The site allows at most one
      year per request, which the 3-month chunks respect.
      These are NOT a documented public API; NSE may change or restrict them. If the
      script reports an unrecognised layout, open the page's Network tab and adapt
      normalise(). Check the site's terms of use before bulk downloading, keep the
      waits generous, and do not run several copies in parallel.
    """)

EPILOG = textwrap.dedent("""\
    RETRIES AND BACK-OFF
      --max-retries N means N RETRIES after the first attempt, i.e. up to N+1 attempts
      per chunk (default 3 -> 4 attempts). Before retry k the script sleeps
        min(--backoff-max, --backoff-base * 2**(k-1)) seconds, multiplied by a random
      0.8-1.3 jitter (defaults: ~5s, ~10s, ~20s). A fresh HTTP session is used for each
      retry. A "hang" is caught two ways: per-read socket timeout (--read-timeout) and
      an overall wall-clock limit per request (--hard-timeout). Errors that trigger a
      retry: network errors, timeouts, non-200 HTTP status, non-JSON/HTML block pages,
      server error payloads. Between chunks the script sleeps a random
      --min-wait..--max-wait seconds, plus a longer pause every --long-pause-every
      requests.

    SANITY CHECKS (per chunk; any failure => status SANITY_FAILED, data kept for review)
      Structure   non-empty; every date parses; no duplicate dates; no rows outside the
                  chunk window; CLOSE (price) / TRI (total return) present and > 0.
      Coverage    Trading-day completeness is checked against, in order of strength:
                  (a) --holidays-csv: if given (and it covers the chunk's years) the
                      expected days = Mon-Fri minus listed holidays, and ANY missing
                      day fails the chunk. This is the only exact check.
                  (b) Cross-series reconciliation: all series of the same quarter
                      (NIFTY 50 price/TRI, NIFTY 500 price/TRI ...) trade on the same
                      days, so a date present in the majority of the other series
                      but absent here fails this chunk. Catches truncated or partial
                      responses even without a holiday file. Needs >= 2 series.
                  (c) Heuristics: more than --max-missing-weekdays-pct % of weekdays
                      missing, first/last row further than --boundary-tolerance-days
                      from the window edges, or a gap > --max-gap-days between
                      consecutive rows.
      Warnings    (do not fail a chunk): weekend rows (special sessions such as
                  Budget day), rows on a listed holiday (Muhurat trading), blank
                  OPEN/HIGH/LOW/NTR columns (common in older history), HIGH < LOW.
      HONEST LIMIT: no free machine-readable calendar of NSE trading days back to the
      1990s exists, so without --holidays-csv the checks are strong evidence of
      completeness, not proof (e.g. a day missing from ALL series at the source
      would pass). Supply --holidays-csv (one date per line or a 'date' column;
      take the dates from NSE's published holiday lists) for exact verification.

    CHUNK STATUSES
      PENDING        not attempted yet (or reset by --force / changed window)
      OK             downloaded and passed all sanity checks
      SANITY_FAILED  downloaded, but a check failed; CSV kept, reasons recorded
      ABANDONED      still failing after all retries; no data for this chunk
      WAIVED         SANITY_FAILED chunk you accepted via --waive-sanity

    RETRY WORKFLOW
      After the download pass, if SANITY_FAILED or ABANDONED chunks exist, you are
      asked separately whether to retry each group (interactive terminals only). Say
      'n' and come back later with:
          python nse_index_downloader.py --out-dir DIR --retry-only
      Unattended: add --yes (retry everything) or --retry-sanity-failed /
      --retry-abandoned (retry that group). Automatic retry rounds are capped by
      --max-auto-retry-rounds (default 2) so an unattended run cannot loop forever.
      If a chunk keeps failing the sanity check because the SOURCE really is
      incomplete/unusual, inspect it and accept it:  --waive-sanity CHUNK_ID [...]
      (or 'all').  Chunk ids look like NIFTY_50.price.2010Q1 (see chunk_status.csv).

    OUTPUT LAYOUT (under --out-dir)
      state.json               machine-readable state; enables resume
      chunk_status.csv         one row per chunk: status, rows, issues, errors
      attempt_ledger.csv       every attempt/sanity outcome, timestamped
      logs/run_<time>.log      full DEBUG log of each run
      chunks/<INDEX>/<series>/ one CSV per chunk (date, open, high, low, close | date, tri, ntr)
      raw/<INDEX>/<series>/    raw JSON exactly as received (audit trail)
      merged/<INDEX>_price.csv, <INDEX>_tri.csv, <INDEX>_price_and_tri.csv
                               built from OK/WAIVED chunks only; named *_PARTIAL.csv
                               while any chunk of that index is still unresolved

    EXIT CODES
      0  every chunk OK/WAIVED       1  some chunks failed/abandoned/pending
      2  fatal error (state saved)   130 interrupted with Ctrl+C (state saved)

    EXAMPLES
      # Full history of NIFTY 50 and NIFTY 500 (price + TRI) up to 28-Sep-2026:
      python nse_index_downloader.py --out-dir nse_data

      # Same, more cautious (slower) and unattended, auto-retrying failures:
      python nse_index_downloader.py --out-dir nse_data --min-wait 8 --max-wait 20 --yes

      # Resume an interrupted run (just repeat the command) or only retry failures:
      python nse_index_downloader.py --out-dir nse_data --retry-only

      # Exact trading-day verification with your own holiday list:
      python nse_index_downloader.py --out-dir nse_data --holidays-csv nse_holidays.csv

      # Show progress/failures without touching the network:
      python nse_index_downloader.py --out-dir nse_data --status

      # Accept a chunk you inspected and judge to be genuinely as published:
      python nse_index_downloader.py --out-dir nse_data --waive-sanity NIFTY_500.tri.2004Q3

      # Test the whole pipeline against a local mock server:
      python nse_index_downloader.py --base-url http://127.0.0.1:8765 --out-dir /tmp/t

    RUNTIME: roughly 450 requests for both indices x both series; at the default
    4-9 s waits plus long pauses expect about 1 hour. Requires: pip install requests pandas
    """)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nse_index_downloader.py", description=DESCRIPTION, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    g = p.add_argument_group("scope: what to download")
    g.add_argument("--out-dir", default="nse_index_data", metavar="DIR",
                   help="Directory for all output, state and logs (default: %(default)s). "
                        "Re-using the directory resumes the previous run.")
    g.add_argument("--indices", nargs="+", default=["NIFTY 50", "NIFTY 500"], metavar="NAME",
                   help="Index names EXACTLY as in the site's drop-down (quote names with spaces). "
                        "Default: 'NIFTY 50' 'NIFTY 500'.")
    g.add_argument("--series", nargs="+", choices=SERIES_ORDER, default=SERIES_ORDER,
                   help="Which series: price (OHLC), tri (Total Returns + Net TRI). Default: both.")
    g.add_argument("--end-date", type=iso_date_arg, default=date(2026, 9, 28), metavar="YYYY-MM-DD",
                   help="Last date to download (default: %(default)s). Changing it re-plans only "
                        "the final chunk.")
    g.add_argument("--probe-from", type=iso_date_arg, default=date(1990, 1, 1), metavar="YYYY-MM-DD",
                   help="Where the earliest-date search starts (default: %(default)s). Must be before "
                        "the index inception; a later value speeds discovery.")
    g.add_argument("--rediscover", action="store_true",
                   help="Ignore cached earliest dates and probe the site again.")
    g.add_argument("--base-url", default=DEFAULT_BASE_URL, metavar="URL",
                   help="Site root (default: %(default)s). Mainly for testing against a mock server.")

    g = p.add_argument_group("pacing: being polite to the server")
    g.add_argument("--min-wait", type=float, default=4.0, metavar="SEC",
                   help="Minimum random pause between requests (default: %(default)s s).")
    g.add_argument("--max-wait", type=float, default=9.0, metavar="SEC",
                   help="Maximum random pause between requests (default: %(default)s s).")
    g.add_argument("--long-pause-every", type=int, default=25, metavar="N",
                   help="Take a longer pause every N requests; 0 disables (default: %(default)s).")
    g.add_argument("--long-pause-min", type=float, default=20.0, metavar="SEC",
                   help="Long pause minimum (default: %(default)s s).")
    g.add_argument("--long-pause-max", type=float, default=45.0, metavar="SEC",
                   help="Long pause maximum (default: %(default)s s).")
    g.add_argument("--user-agent", default=DEFAULT_UA, metavar="STR",
                   help="User-Agent header (default: a desktop-browser string, as the site's own page sends).")

    g = p.add_argument_group("reliability: timeouts, retries, back-off")
    g.add_argument("--max-retries", type=int, default=3, metavar="N",
                   help="Retries per chunk AFTER the first attempt (default: %(default)s => up to 4 attempts).")
    g.add_argument("--backoff-base", type=float, default=5.0, metavar="SEC",
                   help="First back-off delay; doubles each retry (default: %(default)s s).")
    g.add_argument("--backoff-max", type=float, default=120.0, metavar="SEC",
                   help="Cap on a single back-off delay (default: %(default)s s).")
    g.add_argument("--connect-timeout", type=float, default=15.0, metavar="SEC",
                   help="TCP connect timeout (default: %(default)s s).")
    g.add_argument("--read-timeout", type=float, default=45.0, metavar="SEC",
                   help="Max silence while waiting for data (default: %(default)s s).")
    g.add_argument("--hard-timeout", type=float, default=90.0, metavar="SEC",
                   help="Wall-clock limit for one whole request incl. slow trickles (default: %(default)s s).")

    g = p.add_argument_group("sanity checking")
    g.add_argument("--holidays-csv", metavar="FILE",
                   help="File of NSE trading holidays (first parseable date on each line; header ignored). "
                        "Enables EXACT missing-day detection for years it covers.")
    g.add_argument("--max-gap-days", type=int, default=7, metavar="N",
                   help="Fail if consecutive rows are more than N calendar days apart (default: %(default)s).")
    g.add_argument("--boundary-tolerance-days", type=int, default=7, metavar="N",
                   help="Fail if first/last row is more than N days from the window edge (default: %(default)s). "
                        "Raise it if the latest TRI values are published with a delay.")
    g.add_argument("--max-missing-weekdays-pct", type=float, default=15.0, metavar="PCT",
                   help="Without an exact calendar, fail if more than PCT%% of weekdays (min. 2) have no row "
                        "(default: %(default)s).")

    g = p.add_argument_group("retry of failed chunks")
    g.add_argument("--retry-only", action="store_true",
                   help="Do not download new chunks; only work on SANITY_FAILED/ABANDONED ones from a "
                        "previous run (you are prompted unless a flag below is given).")
    g.add_argument("--retry-sanity-failed", action="store_true",
                   help="Automatically retry chunks that failed the sanity check (no prompt).")
    g.add_argument("--retry-abandoned", action="store_true",
                   help="Automatically retry chunks abandoned after all retries (no prompt).")
    g.add_argument("--yes", action="store_true", help="Answer 'yes' to every retry offer (unattended mode).")
    g.add_argument("--no-prompt", action="store_true",
                   help="Never ask questions; just report failures (also the default when stdin is not a terminal).")
    g.add_argument("--max-auto-retry-rounds", type=int, default=2, metavar="N",
                   help="Cap on automatic (flag/--yes) retry rounds per run (default: %(default)s).")

    g = p.add_argument_group("maintenance")
    g.add_argument("--status", action="store_true",
                   help="Print a status report from saved state and exit (no network access).")
    g.add_argument("--waive-sanity", nargs="+", metavar="CHUNK_ID",
                   help="Accept SANITY_FAILED chunk(s) as they are (ids from chunk_status.csv, or 'all').")
    g.add_argument("--force", action="store_true",
                   help="Reset every planned chunk to PENDING and download everything again.")
    g.add_argument("-v", "--verbose", action="store_true", help="Show DEBUG messages on the console too.")
    return p


def setup_logging(out: Path, verbose: bool) -> logging.Logger:
    (out / "logs").mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("nse")
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fh = logging.FileHandler(out / "logs" / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))
    log.addHandler(fh)
    log.addHandler(ch)
    return log


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.min_wait < 0 or a.max_wait < a.min_wait:
        parser.error("--max-wait must be >= --min-wait >= 0")
    if a.long_pause_max < a.long_pause_min:
        parser.error("--long-pause-max must be >= --long-pause-min")
    if a.max_retries < 0:
        parser.error("--max-retries must be >= 0")
    if a.end_date < a.probe_from:
        parser.error("--end-date is before --probe-from")
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log = setup_logging(out, a.verbose)
    log.info("nse_index_downloader %s | out-dir=%s | end-date=%s | indices=%s | series=%s",
             __version__, out, a.end_date, a.indices, a.series)
    try:
        return Runner(a, log).run()
    except FatalError as e:
        log.critical("FATAL: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
