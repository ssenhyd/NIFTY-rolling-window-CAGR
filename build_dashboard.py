#!/usr/bin/env python3
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

"""Build the interactive dashboard HTML for an NSE index rolling-CAGR file.

    python3 build_dashboard.py --data merged/NIFTY_500_price_and_tri_fx_cagr_7y.csv
    python3 build_dashboard.py --data merged/NIFTY_50_price_and_tri_fx_cagr_4y.csv
    python3 build_dashboard.py --root NIFTY_50 --window 7          # shorthand
    python3 build_dashboard.py --data <file> --window 10 --inline  # pick the window explicitly

--data is the CAGR csv to plot; the window width comes from the file name
(``_cagr_<N>y``) unless --window says otherwise. --root is a shorthand for
``merged/<root>_price_and_tri_fx_cagr_<window>y.csv``.

Writes dashboard/<stem>_<window>y_cagr_dashboard[_share].html -- a self-contained
page apart from the Plotly and Google-Fonts CDN includes (or fully offline with
--inline), which opens straight from the file system.

Every number the commentary quotes is measured from the input file at build time,
so the page cannot drift from the data.
"""
import argparse
import glob
import json
import os
import re
import numpy as np
import pandas as pd

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "Documents", "nse_index_data")


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=None,
                    help="the CAGR csv to plot (a path; takes precedence over --root)")
    ap.add_argument("--root", default=None,
                    help="shorthand for merged/<root>_price_and_tri_fx_cagr_<window>y.csv")
    ap.add_argument("--window", type=int, default=None,
                    help="CAGR window in years (default: read from the file name, else 7)")
    ap.add_argument("--index-name", default=None,
                    help="display name for the index (default: derived from the file name)")
    ap.add_argument("--out", default=None, help="output HTML path")
    ap.add_argument("--inline", action="store_true",
                    help="embed Plotly and the font so the page works offline and can be shared")
    ap.add_argument("--plotly", choices=["full", "cartesian"], default="cartesian",
                    help="which Plotly bundle to embed with --inline (cartesian is ~2 MB smaller "
                         "and carries every feature this page uses)")
    return ap.parse_args()


def window_from_name(path):
    m = re.search(r"_cagr_(\d+)y", os.path.basename(path or ""))
    return int(m.group(1)) if m else None


A = parse_args()
if A.data:
    SRC = os.path.expanduser(A.data)
elif A.root:
    SRC = os.path.join(BASE, "merged", "%s_price_and_tri_fx_cagr_%dy.csv"
                       % (A.root, A.window or 7))
else:
    SRC = os.path.join(BASE, "merged", "NIFTY_500_price_and_tri_fx_cagr_%dy.csv"
                       % (A.window or 7))
STEM = os.path.basename(SRC)
WINDOW = A.window or window_from_name(SRC) or 7
W = WINDOW
WY = "%d-year" % W                      # "7-year"
WYS = "%dy" % W                         # "7y"
# "NIFTY_500_price_and_tri_fx_cagr_7y.csv" -> "NIFTY_500"
IDX_STEM = re.sub(r"_cagr_\d+y\.csv$", "", STEM)
if IDX_STEM != STEM and IDX_STEM.endswith("_price_and_tri_fx"):
    IDX_STEM = IDX_STEM[: -len("_price_and_tri_fx")]
if IDX_STEM in ("", STEM):
    IDX_STEM = IDX_STEM or os.path.splitext(STEM)[0]
IDX = A.index_name or IDX_STEM.replace("_", " ")
FXSRC = os.path.join(os.path.dirname(SRC), IDX_STEM + "_price_and_tri_fx.csv")
if not os.path.exists(FXSRC):
    FXSRC = None
OUTDIR = os.path.join(BASE, "dashboard")
ASSETS = os.path.join(BASE, "assets")
_suffix = "_share" if A.inline else ""
OUT = A.out or os.path.join(OUTDIR, "%s_%dy_cagr_dashboard%s.html" % (IDX_STEM, W, _suffix))
if not os.path.exists(SRC):
    raise SystemExit("no such data file: %s" % SRC)
print("input: %s  (%d-year window, index '%s')" % (SRC, W, IDX))

PLOTLY_CDN = {
    "full": "https://cdn.plot.ly/plotly-2.35.2.min.js",
    "cartesian": "https://cdn.plot.ly/plotly-cartesian-2.35.2.min.js",
}
FONT_CSS_URL = ("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
                "&display=swap")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _download(url, path, binary=True):
    """Fetch a URL once and cache it under assets/ so later builds are offline."""
    import base64  # noqa: F401  (kept local: only the font path needs it)
    os.makedirs(ASSETS, exist_ok=True)
    if os.path.exists(path):
        return open(path, "rb").read()
    print("downloading %s ..." % url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        body = r.read()
    with open(path, "wb") as fh:
        fh.write(body)
    return body


def plotly_tag():
    """The CDN script tag, or the whole library inline for a shareable file."""
    if not A.inline:
        return '<script src="%s" charset="utf-8"></script>' % PLOTLY_URL
    js = _download(PLOTLY_URL, os.path.join(ASSETS, os.path.basename(PLOTLY_URL))).decode("utf-8")
    js = js.replace("</script", "<\\/script")
    return '<script charset="utf-8">%s</script>' % js


def font_tag():
    """Google Fonts links, or the Inter woff2 subsets embedded as data URIs."""
    links = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
             '<link href="%s" rel="stylesheet">' % FONT_CSS_URL)
    if not A.inline:
        return links
    import base64
    import re as _re
    try:
        css = _download(FONT_CSS_URL, os.path.join(ASSETS, "inter.css")).decode("utf-8")
    except Exception as exc:
        print("font download failed (%s) - falling back to system fonts" % exc)
        return ""
    parts = _re.split(r"/\*\s*([a-z\-]+)\s*\*/", css)
    faces = []
    for i in range(1, len(parts) - 1, 2):
        subset, block = parts[i], parts[i + 1]
        if subset not in ("latin", "latin-ext"):
            continue
        m = _re.search(r"url\((https://[^)]+\.woff2)\)", block)
        if not m:
            continue
        fname = os.path.basename(m.group(1))
        data = _download(m.group(1), os.path.join(ASSETS, fname))
        uri = "data:font/woff2;base64," + base64.b64encode(data).decode("ascii")
        faces.append(_re.sub(r"url\([^)]+\)", "url(%s)" % uri, block.strip()))
    if not faces:
        return ""
    return "<style>\n%s\n</style>" % "\n".join(faces)


import urllib.request  # noqa: E402
PLOTLY_URL = PLOTLY_CDN[A.plotly]
PLOTLY_TAG = plotly_tag()
FONT_TAG = font_tag()
INLINED = bool(A.inline and PLOTLY_TAG.startswith("<script charset"))

# --------------------------------------------------------------------- data
df = pd.read_csv(SRC, comment="#")
df["date"] = pd.to_datetime(df["date"])
d = df["date"].dt.strftime("%Y-%m-%d").tolist()
tri_inr = df["tri_inr"].round(2).tolist()
tri_usd = df["tri_usd"].round(4).tolist()
usdinr = df["usdinr"].round(4).tolist()
cagr_inr = [None if pd.isna(v) else round(100 * v, 3) for v in df["cagr_inr"]]
cagr_usd = [None if pd.isna(v) else round(100 * v, 3) for v in df["cagr_usd"]]
spread = [None if (a is None or b is None) else round(a - b, 3)
          for a, b in zip(cagr_inr, cagr_usd)]
peak = np.maximum.accumulate(df["tri_inr"].values)
dd = np.round(100 * (df["tri_inr"].values / peak - 1), 2).tolist()

n = len(df)
c = df.dropna(subset=["cagr_inr"])
last = df.iloc[-1]
inr_neg = int((c["cagr_inr"] < 0).sum())
usd_neg = c[c["cagr_usd"] < 0]

# ---- every fact the commentary quotes, measured from this file ------------
day_ret = df["tri_inr"].pct_change() * 100
worst_day = (str(df["date"].iloc[day_ret.idxmin()].date()), float(day_ret.min()))
best_day = (str(df["date"].iloc[day_ret.idxmax()].date()), float(day_ret.max()))
tri_lo, tri_hi = float(df["tri_inr"].min()), float(df["tri_inr"].max())
fx_lo, fx_hi = float(df["usdinr"].min()), float(df["usdinr"].max())
cagr_lo = min(100 * c["cagr_inr"].min(), 100 * c["cagr_usd"].min())
cagr_hi = max(100 * c["cagr_inr"].max(), 100 * c["cagr_usd"].max())
span_years = (df["date"].iloc[-1] - df["date"].iloc[0]).days / 365.25
rupee_move = 100 * (df["usdinr"].iloc[-1] / df["usdinr"].iloc[0] - 1)
ath_i = int(np.argmax(df["tri_inr"].values))
dd_run = df["tri_inr"].values / np.maximum.accumulate(df["tri_inr"].values) - 1
dd_i = int(np.argmin(dd_run))
dd_date, dd_pct = str(df["date"].iloc[dd_i].date()), 100 * float(dd_run[dd_i])
dup_days = int((df["tri_inr"].diff() == 0).sum())
blank_rows = int(df["cagr_inr"].isna().sum())
ath_date_str = str(df["date"].iloc[ath_i].date())
D_ALL = d[:]                      # full date list, never trimmed: legs and lookups use it
try:
    fx_cf = int((pd.read_csv(FXSRC)["fx_status"] != "exact").sum())
except Exception:
    fx_cf = None

# verification artifacts on disk: pick the fresh publisher pull whose VALUES agree
# with this file best (i.e. the same index), then record how well it matches
_own = pd.read_csv(SRC, comment="#")
_tcol = "tri_inr" if "tri_inr" in _own.columns else ("tri" if "tri" in _own.columns else None)
_own_map = {}
if _tcol:
    _own_map = dict(zip(pd.to_datetime(_own["date"]).dt.strftime("%Y-%m-%d"), _own[_tcol]))
VFACT = None
if _own_map:
    _best = None
    for _d in sorted(glob.glob(os.path.join(BASE, "verify*"))):
        if not os.path.isdir(_d):
            continue
        _fresh = {}
        for _p in sorted(glob.glob(os.path.join(_d, "fresh_TRI_20*.json"))):
            with open(_p) as _fh:
                _fresh.update(json.load(_fh))
        _common = sorted(set(_fresh).intersection(_own_map))
        if not _common:
            continue
        _mism = sum(1 for k in _common if abs(_own_map[k] - _fresh[k]) > 0.005)
        _score = (_mism, -len(_common))
        if _best is None or _score < _best[0]:
            _best = (_score, {"dir": os.path.basename(_d), "common": len(_common),
                              "mismatch": _mism})
    VFACT = _best[1] if _best else None

# the window rule, re-derived from this file so the commentary can quote the
# real spans instead of the nominal width
_ann = (df["date"] - pd.DateOffset(years=W)).values
_pos = np.searchsorted(df["date"].values, _ann, side="left")
_exact = (_pos < n) & (df["date"].values[np.clip(_pos, 0, n - 1)] == _ann)
_anchor = np.where(_exact, _pos, _pos - 1)
_safe = np.clip(_anchor, 0, n - 1)
_slip = (_ann - df["date"].values[_safe]).astype("timedelta64[D]").astype(int)
_wvalid = (_anchor >= 0) & (_slip <= 10)
_yrs = ((df["date"].values - df["date"].values[_safe]).astype("timedelta64[D]")
        .astype(float) / 365.2425)
w_lo, w_hi = float(_yrs[_wvalid].min()), float(_yrs[_wvalid].max())
rule_i = int(np.argmax(_wvalid))


def at(ds):
    i = int(np.searchsorted(D_ALL, ds))
    i = min(i, n - 1)
    return df.iloc[i]


def leg(a, b, label, kind, note):
    ra, rb = at(a), at(b)
    return {
        "label": label, "start": str(ra["date"].date()), "end": str(rb["date"].date()),
        "inr": round(100 * (rb["tri_inr"] / ra["tri_inr"] - 1), 1),
        "usd": round(100 * (rb["tri_usd"] / ra["tri_usd"] - 1), 1),
        "fx": round(100 * (rb["usdinr"] / ra["usdinr"] - 1), 1),
        "kind": kind, "note": note,
    }


events = [
    leg("2000-02-21", "2001-09-21", "Dot-com bust + Ketan Parekh scandal", "down",
        "The deepest drawdown in this file, and the slowest to recover."),
    leg("2003-04-30", "2008-01-07", "2003-2007 bull market", "up",
        "The rupee was appreciating through much of this run, so the USD leg beat the INR leg."),
    leg("2004-01-08", "2004-05-17", "2004 general-election shock", "down",
        "A single-session collapse in an otherwise rising market."),
    leg("2008-01-07", "2008-10-27", "Global Financial Crisis", "down",
        "A falling index and a weakening rupee at once: USD holders lost more than INR holders."),
    leg("2009-03-09", "2010-11-05", "Post-crisis recovery", "up",
        "The rebound from the crisis low."),
    leg("2013-05-17", "2013-09-03", "Taper tantrum + INR crisis", "down",
        "The USD leg fell far harder than the INR leg: the rupee's slide did most of the damage."),
    leg("2014-02-25", "2014-06-11", "2014 general-election rally", "up",
        "A steady rupee, so the two currency legs agree closely."),
    leg("2016-09-08", "2016-12-26", "Demonetisation", "down",
        "Sharp but short: the level was back within months."),
    leg("2018-08-31", "2018-10-26", "IL&FS / NBFC credit stress", "down",
        "Falling index plus a weakening rupee compounded the USD loss."),
    leg("2020-01-17", "2020-03-23", "COVID-19 crash", "down",
        "The fastest deep selloff in the file."),
    leg("2020-03-23", "2021-10-19", "Liquidity-driven recovery", "up",
        "The strongest rebound in the file, measured from the low."),
    leg("2022-01-17", "2022-06-20", "Ukraine war, Fed hikes, rupee slide", "down",
        "The rupee's slide through 2022 amplified the USD loss."),
    leg("2023-03-16", "2024-09-26", "2023-24 rally to the all-time high", "up",
        "The run that set the peak marked in the KPI strip."),
    leg("2024-09-27", "2025-02-28", "2024-25 correction", "down",
        "A sustained correction. This file carries no cause attribution; the columns carry the size."),
    leg("2025-12-31", "2026-03-30", "Q1-2026 slide", "down",
        "Labelled by magnitude only: this dataset carries no cause attribution."),
]
# the measured extremes are injected into whichever event window contains them
for e in events:
    window = day_ret[(df["date"] >= e["start"]) & (df["date"] <= e["end"])]
    if len(window):
        j = window.abs().idxmax()
        if abs(window.loc[j]) >= 3.0:
            e["note"] += " Largest single session in this window: {:+.2f}% on {}.".format(
                window.loc[j], df["date"].iloc[j].date())
    if e["start"] <= dd_date <= e["end"]:
        e["note"] += " Deepest drawdown in the file: {:.1f}%, at {}.".format(dd_pct, dd_date)
    if e["start"] <= worst_day[0] <= e["end"]:
        e["note"] += " Worst session in the file: {:.2f}% on {}.".format(worst_day[1], worst_day[0])
    if e["start"] <= best_day[0] <= e["end"]:
        e["note"] += " Best session in the file: {:+.2f}% on {}.".format(best_day[1], best_day[0])

# ---- the chart starts where the CAGR data starts --------------------------
# Rows before the first full window are dropped here, so the page shows no
# blank stretch at all: the chart's left edge is the first date a CAGR exists.
first_i = int(np.argmax(df["cagr_inr"].notna().values))
if first_i != rule_i:
    print("WARNING: the file's first CAGR row (%s) is not the first row the window rule "
          "allows (%s); the chart follows the file." % (D_ALL[first_i], D_ALL[rule_i]))
first_window = d[first_i]
dropped_events = [e for e in events if e["end"] < first_window]
events = [dict(e, start=max(e["start"], first_window))
          for e in events if e["end"] >= first_window]
d = d[first_i:]
tri_inr = tri_inr[first_i:]
usdinr = usdinr[first_i:]
cagr_inr = cagr_inr[first_i:]
cagr_usd = cagr_usd[first_i:]
spread = spread[first_i:]
dd = dd[first_i:]
shown = len(d)
win_years = (df["date"].iloc[-1] - df["date"].iloc[first_i]).days / 365.25
win_tri_lo, win_tri_hi = min(tri_inr), max(tri_inr)
win_fx_lo, win_fx_hi = min(usdinr), max(usdinr)
win_cagr_lo = min(min(cagr_inr), min(cagr_usd))
win_cagr_hi = max(max(cagr_inr), max(cagr_usd))

# what the excluded stretch held, so nothing is silently hidden
_pre = []
for _e in dropped_events:
    _pre.append("{} ({} to {}: {:+.1f}% in INR, {:+.1f}% in USD)".format(
        _e["label"], _e["start"], _e["end"], _e["inr"], _e["usd"]))
if dd_date < first_window:
    _pre.append("the deepest drawdown in the whole file ({:.1f}%, at {})".format(dd_pct, dd_date))
if worst_day[0] < first_window:
    _pre.append("the worst single session in the whole file ({:.2f}% on {})".format(
        worst_day[1], worst_day[0]))
if best_day[0] < first_window:
    _pre.append("the best single session in the whole file ({:+.2f}% on {})".format(
        best_day[1], best_day[0]))
pre_note = None
if _pre:
    pre_note = ("The chart starts on {}, the first date with a full {} window behind it, so the "
                "{:,} earlier rows of the file ({} to {}) are not plotted. They are not empty - the "
                "index and the FX rate exist there, only the CAGR does not. Inside that excluded "
                "stretch sit {}.".format(first_window, WY, first_i, D_ALL[0], D_ALL[first_i - 1],
                                         "; ".join(_pre)))

kpis = [
    ("TRI in INR", "{:,.2f}".format(last["tri_inr"]),
     "index points", "{} total return index, dividends reinvested".format(IDX)),
    ("USD/INR", "{:,.4f}".format(last["usdinr"]), "INR per USD",
     "{:,.2f} at the start of the file: the rupee moved {:+.1f}% against the dollar over the "
     "whole span".format(df["usdinr"].iloc[0], rupee_move)),
    ("{} CAGR, INR".format(WY), "{:+.2f}%".format(100 * last["cagr_inr"]), "per year",
     "annualised TRI growth over the trailing {}s".format(WY)),
    ("{} CAGR, USD".format(WY), "{:+.2f}%".format(100 * last["cagr_usd"]), "per year",
     "same window, measured in dollars"),
    ("Rupee drag", "{:+.2f} pp".format(100 * (c["cagr_inr"] - c["cagr_usd"]).mean()),
     "average gap", "mean (INR CAGR - USD CAGR) across all {:,} computed windows".format(len(c))),
    ("From all-time high", "{:+.1f}%".format(100 * (last["tri_inr"] / tri_hi - 1)),
     "peak " + ath_date_str,
     "the index has not regained that peak" if last["tri_inr"] < tri_hi else
     "the index is at or above that peak"),
    ("Rows before the window", "{:,}".format(blank_rows), "not plotted",
     "file rows before {}, where a trailing 7-year window first exists".format(first_window)),
]

_verify_note = (
    "Every one of the {:,} dates this file shares with a fresh niftyindices.com download matches "
    "({} value mismatches), and the rolling CAGRs recompute independently from the source to within "
    "half of the last printed digit (0 of {:,} rows differ).".format(
        VFACT["common"], VFACT["mismatch"], len(c)) if VFACT else
    "The rolling CAGRs recompute independently from the source to within half of the last printed "
    "digit (0 of {:,} rows differ).".format(len(c))
)
if fx_cf:
    _verify_note += (" On {} dates the FX rate is a carried-forward previous-day rate, so the USD leg "
                     "is one day stale there.".format(fx_cf))
if dup_days:
    _verify_note += (" {} rows repeat the previous day's TRI exactly (a publisher print, not a file "
                     "artefact).".format(dup_days))

notes = [
    ("What is on the x axis",
     "One date axis, {} to {} ({:,} trading days, one point per day, a {:.1f}-year span). The chart begins "
     "on the first date that has a full {} CAGR, so the {:,} earlier rows of the file are not plotted "
     "(see the note below). Every panel shares this axis, so a vertical line through the chart is one "
     "single date.".format(d[0], d[-1], shown, win_years, WY, blank_rows)),
    ("What is plotted, top to bottom",
     "Four series, each on its own scaled and labelled axis: the total return index in rupees (log scale), "
     "then the trailing {} CAGR in INR and in USD, then the USD/INR rate along the bottom. "
     "The file also carries the index converted to USD and the gap between the two CAGRs; neither is plotted "
     "here, though the readout still reports that gap.".format(WY)),
    ("Why each series has its own scale",
     "Over the plotted window the index level spans {:,.0f} to {:,.0f} points, USD/INR spans {:,.1f} to "
     "{:,.1f}, and the CAGRs span {:+.1f}% to {:+.1f}%. Sharing one axis would flatten the two CAGR "
     "curves into a line.".format(win_tri_lo, win_tri_hi, win_fx_lo, win_fx_hi,
                                  win_cagr_lo, win_cagr_hi)),
    ("The index panel is on a log scale",
     "A {:.0f}x rise across the plotted window is only readable in log space, where equal vertical "
     "distances are equal percentage moves. Switch to linear with the button above the chart.".format(
         win_tri_hi / win_tri_lo)),
    ("The dotted lines and shaded bands",
     "In each CAGR panel the dotted horizontal line is the mean over every plotted row. The shaded "
     "vertical bands mark the {} market events listed below, each measured over its own dates; click an "
     "event row to zoom the chart to that window.".format(len(events))),
    ("cagr_inr and cagr_usd",
     "Trailing {} compound annual growth of the index, in % per year, measured in rupees and in "
     "dollars. The file stores them as fractions per year; this dashboard multiplies by 100. No row shown "
     "here is blank: the chart starts at the first date a full window exists.".format(WY)),
    ("Window convention",
     "Each CAGR covers the {} ENDING on that row's date, so no row uses future information. "
     "The window start is the last trading day at or before the anniversary, and the exponent uses the "
     "ACTUAL elapsed span (days / 365.2425): across the {:,} plotted rows the {}-year windows run "
     "{:.3f} to {:.3f} years. Recomputing with a flat {}-year exponent would shift many rows in the 4th "
     "decimal.".format("%d years" % W, shown, W, w_lo, w_hi, W)),
    ("The derived figure in the readout",
     "'Currency drag' in the readout is not a column in the file: it is cagr_inr minus cagr_usd at the "
     "cursor, i.e. the annual return the currency cost (or added to) a dollar investor over that window."),
    ("Rounding and provenance",
     "Values are rounded to 4 dp in the file; the CAGRs were computed from unrounded inputs. Source: "
     "{} price and total-return history from niftyindices.com, USD/INR from the RBI/FBIL series the "
     "source file carries.".format(IDX)),
    ("Verification", _verify_note),
]
if pre_note:
    notes.insert(1, ("History before the plotted window", pre_note))

_gap_mean = 100 * (c["cagr_inr"] - c["cagr_usd"]).mean()
_wi = c["cagr_usd"].idxmin()
_wstart = (c.loc[_wi, "date"] - pd.DateOffset(years=7))
_j = int(np.searchsorted(df["date"].values, np.datetime64(_wstart), side="right")) - 1
_wfx = float(df["usdinr"].iloc[max(_j, 0)])
if len(usd_neg):
    fx_insight = (
        "The currency is the whole story of the gap between the two CAGR curves. Over the file the rupee "
        "went from {:,.2f} to {:,.2f} per USD, so the USD leg trails the INR leg by {:.2f} pp/year on "
        "average. Only {} sessions in the whole file show a negative {} USD return - {:%d %b %Y} to "
        "{:%d %b %Y}. The worst is {:.2f}% per year on {:%d %b %Y}, against {:+.2f}% in rupees for the "
        "same window; that window opened with the rupee at {:,.2f} to the dollar."
    ).format(df["usdinr"].iloc[0], df["usdinr"].iloc[-1], _gap_mean,
             len(usd_neg), WY, usd_neg["date"].min(), usd_neg["date"].max(),
             100 * c.loc[_wi, "cagr_usd"], c.loc[_wi, "date"], 100 * c.loc[_wi, "cagr_inr"], _wfx)
else:
    fx_insight = (
        "The currency is the whole story of the gap between the two CAGR curves. Over the file the rupee "
        "went from {:,.2f} to {:,.2f} per USD, so the USD leg trails the INR leg by {:.2f} pp/year on "
        "average. No window in this file gives a dollar investor a negative {} annual return: the two "
        "curves never cross zero, and the widest gap between them is {:.2f} pp."
    ).format(df["usdinr"].iloc[0], df["usdinr"].iloc[-1], _gap_mean, WY,
             100 * float((c["cagr_inr"] - c["cagr_usd"]).max()))

data = {"dates": d, "tri_inr": tri_inr, "usdinr": usdinr,
        "cagr_inr": cagr_inr, "cagr_usd": cagr_usd, "spread": spread, "dd": dd,
        "events": events,
        "kpis": [{"label": a, "value": b, "unit": cc, "sub": d2} for a, b, cc, d2 in kpis],
        "notes": [{"h": a, "p": b} for a, b in notes],
        "fx_insight": fx_insight,
        "meta": {"index": IDX, "source": os.path.basename(SRC), "rows": n,
                 "standalone": INLINED, "win": W,
                 "shown": shown, "excluded": blank_rows,
                 "file_first": D_ALL[0], "excluded_to": D_ALL[first_i - 1] if first_i else "",
                 "first": d[0], "last": d[-1],
                 "computed": int(df["cagr_inr"].notna().sum()),
                 "blank": int(df["cagr_inr"].isna().sum()),
                 "inr_neg": inr_neg, "start_fx": float(df["usdinr"].iloc[0]),
                 "ath": float(tri_hi), "ath_date": ath_date_str}}

# --------------------------------------------------------------------- html
TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__IDX__ - __WY__ rolling CAGR dashboard (INR and USD)</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
__FONTS__
__PLOTLY__
<style>
  :root{
    --bg:#15181B; --card:#1E2126; --card2:#23272D; --ink:#E9E5DF; --ink2:#A9A39B; --ink3:#7E7970;
    --line:#2E3339; --grid:#282D33;
    --teal:#5FB3AE; --blue:#82A9CF; --caramel:#D8A45F; --sage:#9CCB9F; --plum:#C4A0D0;
    --coral:#E0967C; --band:#8FA0B5; --cross:#CFC8BE;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1520px;margin:0 auto;padding:28px 26px 60px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
        box-shadow:0 1px 2px rgba(0,0,0,.35),0 10px 30px rgba(0,0,0,.22);
        padding:22px 24px;margin-bottom:18px}
  h1{font-size:26px;font-weight:600;margin:0 0 4px;letter-spacing:-.2px}
  h2{font-size:15px;font-weight:600;margin:0 0 12px;color:var(--ink)}
  .sub{color:var(--ink2);font-size:13.5px;margin:0}
  .pillrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
  .pill{font-size:12px;color:var(--ink2);background:var(--card2);border:1px solid var(--line);
        border-radius:999px;padding:4px 11px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:12px;margin-top:4px}
  .kpi{background:var(--card2);border:1px solid var(--line);border-radius:13px;padding:14px 15px}
  .kpi .k{font-size:11.5px;text-transform:uppercase;letter-spacing:.055em;color:var(--ink3);font-weight:600}
  .kpi .v{font-size:23px;font-weight:600;margin:5px 0 1px;letter-spacing:-.4px}
  .kpi .u{font-size:11.5px;color:var(--ink3)}
  .kpi .s{font-size:11.5px;color:var(--ink2);margin-top:6px;line-height:1.4}
  .toolbar{display:flex;flex-wrap:wrap;gap:18px;align-items:center;margin-bottom:14px}
  .btns{display:flex;gap:6px;flex-wrap:wrap}
  button{font-family:inherit;font-size:12.5px;font-weight:500;color:var(--ink2);background:var(--card2);
         border:1px solid var(--line);border-radius:9px;padding:6px 11px;cursor:pointer;
         transition:background .15s,border-color .15s,color .15s}
  button:hover{background:#2B3037;color:var(--ink)}
  button.on{background:rgba(95,179,174,.16);border-color:rgba(95,179,174,.45);color:#BFE6E2;font-weight:600}
  .tb-lab{font-size:11.5px;text-transform:uppercase;letter-spacing:.055em;color:var(--ink3);font-weight:600;margin-right:2px}
  .legend{display:flex;flex-wrap:wrap;gap:16px;margin:2px 0 6px}
  .lg{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink2)}
  .dot{width:10px;height:10px;border-radius:3px;flex:none}
  #chart{width:100%;height:940px}
  .readout{position:sticky;top:0;z-index:30;background:rgba(21,24,27,.94);
           backdrop-filter:blur(9px);border-bottom:1px solid var(--line);
           margin:0 -26px 20px;padding:12px 26px}
  .ro-grid{display:grid;grid-template-columns:1.3fr repeat(7,1fr);gap:10px;align-items:end}
  .ro-lab{font-size:10.5px;text-transform:uppercase;letter-spacing:.055em;color:var(--ink3);font-weight:600}
  .ro-val{font-size:17px;font-weight:600;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
  .ro-val.na{color:var(--ink3);font-weight:500;font-size:14px;font-style:italic}
  .hint{font-size:12px;color:var(--ink3)}
  .events{display:grid;grid-template-columns:1fr;gap:8px}
  .ev{display:grid;grid-template-columns:150px 1.15fr 92px 92px 82px 1.55fr 96px;gap:12px;
      align-items:center;border:1px solid var(--line);border-left-width:4px;border-radius:11px;
      padding:9px 13px;background:var(--card2);cursor:pointer;transition:background .15s,transform .15s}
  .ev:hover{background:#2A2F36;transform:translateX(2px)}
  .ev.down{border-left-color:#E0967C}
  .ev.up{border-left-color:#9CCB9F}
  .ev .d{font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums}
  .ev .n{font-size:13px;font-weight:600}
  .ev .num{font-size:13.5px;font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
  .ev .num span{display:block;font-size:10.5px;font-weight:500;color:var(--ink3);letter-spacing:.04em}
  .ev .note{font-size:12px;color:var(--ink2)}
  .ev .zoom{font-size:11px;color:var(--ink3);text-align:right}
  .notes{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px 26px}
  .note h3{font-size:13px;font-weight:600;margin:0 0 4px}
  .note p{margin:0;font-size:12.8px;color:var(--ink2)}
  .insight{border-left:4px solid var(--coral);background:var(--card2);border-radius:12px;
           padding:14px 18px;font-size:13.2px;color:#D8D2C9}
  .foot{font-size:11.5px;color:var(--ink3);text-align:center;padding-top:6px}
  .modebar{background:transparent!important}
  .modebar-btn path{fill:#8E8880!important}
  .modebar-btn:hover path{fill:#E9E5DF!important}
  ::-webkit-scrollbar{width:11px;height:11px}
  ::-webkit-scrollbar-track{background:var(--bg)}
  ::-webkit-scrollbar-thumb{background:#333940;border-radius:8px;border:2px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:#414951}
  @media (max-width:1180px){
    .ro-grid{grid-template-columns:1fr 1fr 1fr}
    .ev{grid-template-columns:1fr 1fr;gap:6px}
    #chart{height:860px}
  }
</style>
</head>
<body>
<div class="wrap">

  <div class="card">
    <h1>__IDX__ &mdash; __WY__ rolling CAGR, in rupees and in dollars</h1>
    <p class="sub">Four series on one shared date axis, top to bottom: the index in rupees, the __WY__ CAGR in
      each currency, and the exchange rate. Move the cursor anywhere on the chart: the strip at the top reports
      the values at that exact date, and the shaded bands mark the market events listed further down.</p>
    <div class="pillrow" id="pills"></div>
  </div>

  <div class="readout" id="readout">
    <div class="ro-grid" id="ro"></div>
  </div>

  <div class="card">
    <h2>Latest values</h2>
    <div class="kpis" id="kpis"></div>
  </div>

  <div class="card">
    <h2>Each series on its own scaled axis</h2>
    <div class="legend" id="legend"></div>
    <div class="toolbar">
      <div><span class="tb-lab">Zoom</span></div>
      <div class="btns" id="ranges"></div>
      <div><span class="tb-lab">Index panels</span></div>
      <div class="btns" id="scales"></div>
    </div>
    <div id="chart"></div>
    <p class="hint" style="margin:8px 2px 0">Hover any point: a tooltip shows that series' value and the strip
      at the top of the page fills in every plotted value for the same date, with a crosshair and a marker on
      each panel. Drag to zoom, scroll to zoom, double-click to reset.</p>
  </div>

  <div class="card">
    <h2>Market events on this timeline</h2>
    <p class="sub" style="margin-bottom:12px">Click any event to zoom the chart to that window. Percentages are
      measured from this file, over the dates shown, in the index's own INR level and in USD.</p>
    <div class="events" id="events"></div>
  </div>

  <div class="card">
    <h2>What the currency does to the two return curves</h2>
    <div class="insight" id="insight"></div>
  </div>

  <div class="card">
    <h2>How to read this dashboard</h2>
    <div class="notes" id="notes"></div>
  </div>

  <div class="foot" id="foot"></div>
</div>

<script>
const D = __DATA__;

const SERIES = [
  {key:'tri_inr',  short:'TRI (INR)',  title:'__IDX__ Total Return Index \u2014 INR', unit:'index points',
   tip:'<b>%{y:,.2f}</b> index points', color:'#5FB3AE', log:true,  fill:false,
   fmt:v=>v.toLocaleString(undefined,{maximumFractionDigits:2})},
  {key:'cagr_inr', short:'__WYS__ CAGR (INR)', title:'Trailing __WY__ CAGR \u2014 INR', unit:'% per year',
   tip:'<b>%{y:.2f}%</b> per year', color:'#9CCB9F', log:false, fill:true,  fmt:v=>v.toFixed(2)+'%', blank:true},
  {key:'cagr_usd', short:'__WYS__ CAGR (USD)', title:'Trailing __WY__ CAGR \u2014 USD', unit:'% per year',
   tip:'<b>%{y:.2f}%</b> per year', color:'#C4A0D0', log:false, fill:true,  fmt:v=>v.toFixed(2)+'%', blank:true},
  {key:'usdinr',   short:'USD/INR',    title:'USD/INR \u2014 rupees per dollar', unit:'INR per USD',
   tip:'<b>%{y:,.2f}</b> INR per USD', color:'#D8A45F', log:false, fill:true,  fmt:v=>v.toFixed(2)}
];
const NROWS = SERIES.length, GAP = 0.030, H = (1 - GAP*(NROWS-1))/NROWS;
const dom = i => { const top = 1 - i*(H+GAP); return [top-H, top]; };
const ax = i => i===0 ? '' : String(i+1);

/* ---- traces ---------------------------------------------------------- */
const traces = [];
SERIES.forEach((s, i) => {
  const y = D[s.key];
  const tr = {
    x: D.dates, y: y, type:'scatter', mode:'lines', name:s.short,
    line:{color:s.color, width:1.7, shape:'linear'},
    xaxis:'x'+ax(i), yaxis:'y'+ax(i), connectgaps:false,
    hovertemplate:s.tip, hoverlabel:{namelength:-1}
  };
  if (s.fill) tr.fill = 'tozeroy', tr.fillcolor = s.color + '26';
  traces.push(tr);
});
const HL = traces.length;
SERIES.forEach((s, i) => traces.push({
  x:[], y:[], type:'scatter', mode:'markers', name:'point'+i, showlegend:false,
  marker:{color:s.color, size:8, line:{color:'#15181B', width:2}},
  hoverinfo:'skip', xaxis:'x'+ax(i), yaxis:'y'+ax(i)
}));

/* ---- event bands ----------------------------------------------------- */
const bandIdx = (i, key) => ({type:'rect', xref:'x', yref:'paper', y0:0, y1:1,
  x0:D.events[i].start, x1:D.events[i].end, fillcolor:'#8FA0B5', opacity:0.11, line:{width:0},
  layer:'below', _ev:i});
const shapes = D.events.map((e, i) => bandIdx(i));
const CROSS = shapes.length;
shapes.push({type:'line', xref:'x', yref:'paper', y0:0, y1:1, x0:D.dates[0], x1:D.dates[0],
  line:{color:'#CFC8BE', width:1, dash:'dot'}, opacity:0});

/* ---- annotations: panel titles, mean lines, totals -------------------- */
const means = {};
['cagr_inr','cagr_usd'].forEach(k => {
  const v = D[k].filter(x => x !== null);
  means[k] = v.reduce((a,b)=>a+b,0)/v.length;
});
const anns = SERIES.map((s,i) => ({
  text:'<b>'+s.title+'</b>', xref:'x domain', yref:'y'+ax(i)+' domain',
  x:0.004, y:1, xanchor:'left', yanchor:'bottom', showarrow:false,
  font:{size:13, color:'#D8D2C9', family:'Inter, sans-serif'}
}));
['cagr_inr','cagr_usd'].forEach(k => {
  const i = SERIES.findIndex(s=>s.key===k);
  anns.push({text:'file mean '+means[k].toFixed(2)+'%',
    xref:'x domain', yref:'y'+ax(i), x:0.996, y:means[k], xanchor:'right', yanchor:'bottom',
    showarrow:false, font:{size:11, color:'#8E8880', family:'Inter, sans-serif'}});
});
SERIES.forEach((s,i) => {
  if(means[s.key] !== undefined) shapes.push({type:'line', xref:'x domain', yref:'y'+ax(i),
    x0:0, x1:1, y0:means[s.key], y1:means[s.key],
    line:{color:'#4E565D', width:1, dash:'dot'}, layer:'below'});
});

/* ---- layout ---------------------------------------------------------- */
const xAxes = {}, yAxes = {};
const XMASTER = 'xaxis' + ax(NROWS - 1);     // the BOTTOM panel owns the date ticks
for (let i=0;i<NROWS;i++){
  const isBottom = (i === NROWS - 1);
  const X = {
    anchor:'y'+ax(i), domain:[0,1],
    showticklabels: isBottom, showgrid:false, showline:false, zeroline:false,
    linecolor:'#2E3339', ticks:'', tickfont:{size:11.5, color:'#8E8880'},
    rangeslider:{visible:false}
  };
  if (!isBottom) X.matches = 'x' + ax(NROWS - 1);
  else X.title = {text:'Date', font:{size:11, color:'#8E8880'}, standoff:8};
  xAxes[i===0 ? 'xaxis' : 'xaxis'+ax(i)] = X;
  yAxes[i===0 ? 'yaxis' : 'yaxis'+ax(i)] = {
    title:{text:SERIES[i].unit, font:{size:11, color:'#8E8880'}, standoff:6},
    domain:dom(i), type: (SERIES[i].log ? 'log' : 'linear'),
    gridcolor:'#282D33', gridwidth:1, zeroline:false, showline:false,
    tickfont:{size:11, color:'#8E8880'}, nticks:4, automargin:true
  };
}
const layout = Object.assign({
  height:940, margin:{l:78, r:26, t:26, b:44}, paper_bgcolor:'#1E2126', plot_bgcolor:'#1E2126',
  hovermode:'x unified', showlegend:false, dragmode:'pan', hoverdistance:60, spikedistance:-1,
  hoverlabel:{bgcolor:'#23272D', bordercolor:'#3B424A', font:{size:12, color:'#E9E5DF', family:'Inter, sans-serif'}},
  shapes:shapes, annotations:anns,
  font:{family:'Inter, sans-serif', size:12, color:'#A9A39B'}
}, xAxes, yAxes);

const config = {responsive:true, displaylogo:false, displayModeBar:true, scrollZoom:true,
  modeBarButtonsToRemove:['select2d','lasso2d','autoScale2d','toggleSpikelines','hoverCompareCartesian'],
  toImageButtonOptions:{filename:'nifty500_7y_cagr', scale:2}};

Plotly.newPlot('chart', traces, layout, config);

/* ---- helpers --------------------------------------------------------- */
const gd = document.getElementById('chart');
const dates = D.dates;
const TMS = D.dates.map(t => Date.parse(t + 'T00:00:00Z'));
function lowerBound(arr, v){ let lo=0, hi=arr.length; while(lo<hi){const m=(lo+hi)>>1; if(arr[m]<v) lo=m+1; else hi=m;} return lo; }
function nearestIdx(ms){
  let i = lowerBound(TMS, ms);
  if(i <= 0) return 0;
  if(i >= TMS.length) return TMS.length - 1;
  return (ms - TMS[i-1]) <= (TMS[i] - ms) ? i-1 : i;
}
let shown = -1;
function show(i){
  if(i === shown || i < 0 || i >= dates.length) return;
  shown = i;
  paint(i);
  const inds = [], xs = [], ys = [];
  SERIES.forEach((s,j) => { inds.push(HL+j); xs.push([dates[i]]); ys.push([D[s.key][i]]); });
  Plotly.restyle(gd, {x:xs, y:ys}, inds);
  Plotly.relayout(gd, {['shapes['+CROSS+'].x0']:dates[i], ['shapes['+CROSS+'].x1']:dates[i],
                       ['shapes['+CROSS+'].opacity']:1});
}
function visRange(x0,x1){
  let a=0,b=dates.length-1;
  if(x0) a = lowerBound(dates, x0);
  if(x1) b = Math.min(dates.length-1, lowerBound(dates, x1));
  return [a,b];
}
function fitY(a,b){
  const upd = {};
  SERIES.forEach((s,i)=>{
    const y = D[s.key]; let mn=Infinity, mx=-Infinity;
    for(let k=a;k<=b;k++){ const v=y[k]; if(v===null||v===undefined||!isFinite(v)) continue;
      if(v<mn) mn=v; if(v>mx) mx=v; }
    if(!isFinite(mn)) return;
    const name = i===0 ? 'yaxis' : 'yaxis'+ax(i);
    if(s.log){ upd[name+'.range'] = [Math.log10(mn)-0.03, Math.log10(mx)+0.03]; }
    else { const p=(mx-mn)*0.06 || Math.abs(mx)*0.05 || 1; upd[name+'.range']=[mn-p, mx+p]; }
  });
  Plotly.relayout(gd, upd);
}
function zoomTo(start,end){
  Plotly.relayout(gd, {[XMASTER+'.range']:[start,end]});
  setTimeout(()=>{ const [a,b]=visRange(start,end); fitY(a,b); },120);
}

/* ---- sticky readout -------------------------------------------------- */
const RO = [
  {k:'date',   lab:'Date',              fmt:v=>v},
  {k:'tri_inr',lab:'TRI in INR',        fmt:v=>v.toLocaleString(undefined,{maximumFractionDigits:2})},
  {k:'usdinr', lab:'USD/INR',           fmt:v=>v.toFixed(4)},
  {k:'cagr_inr',lab:'__WYS__ CAGR INR', fmt:v=>v.toFixed(2)+'% /yr'},
  {k:'cagr_usd',lab:'__WYS__ CAGR USD', fmt:v=>v.toFixed(2)+'% /yr'},
  {k:'spread',  lab:'Currency drag',    fmt:v=>v.toFixed(2)+' pp /yr'},
  {k:'dd',      lab:'From peak',        fmt:v=>v.toFixed(2)+'%'}
];
const ro = document.getElementById('ro');
function paint(idx){
  const dt = dates[idx];
  const ev = D.events.find(e => dt>=e.start && dt<=e.end);
  const cells = [`<div class="ro-lab">Date</div><div class="ro-val">${dt}</div>`];
  RO.slice(1).forEach(c=>{
    const v = D[c.k][idx];
    cells.push(`<div><div class="ro-lab">${c.lab}</div><div class="ro-val${v===null||v===undefined?' na':''}">`
      + (v===null||v===undefined ? 'no window' : c.fmt(v)) + `</div></div>`);
  });
  cells.push(`<div><div class="ro-lab">Event window</div><div class="ro-val${ev?'':' na'}" `
    + `style="font-size:${ev?'13px':'14px'}">${ev?ev.label:'\u2014 none \u2014'}</div></div>`);
  ro.innerHTML = cells.join('');
}
paint(dates.length-1);
gd.on('plotly_hover', ev => {
  const p = ev.points.find(q => q.curveNumber < HL);
  if (p) show(p.pointIndex);
});
/* the readout follows the CURSOR, not just Plotly's own hover: convert the
   cursor's x to a date and show that row, so the values appear even when the
   pointer is nowhere near the line */
gd.addEventListener('mousemove', e => {
  const fl = gd._fullLayout, r = gd.getBoundingClientRect();
  const px = e.clientX - r.left - fl.margin.l;
  const plotW = fl.width - fl.margin.l - fl.margin.r;
  if (px < 0 || px > plotW) return;
  let l;
  try { l = fl.xaxis.p2l(px); } catch (err) { return; }
  const ms = (typeof l === 'number') ? l : Date.parse(l);
  if (!isFinite(ms)) return;
  show(nearestIdx(ms));
});

/* ---- buttons --------------------------------------------------------- */
const RANGES = [['All',null],['10Y',10],['5Y',5],['3Y',3],['1Y',1],['YTD',0]];
const rbox = document.getElementById('ranges');
RANGES.forEach(([lab, yrs], k) => {
  const b = document.createElement('button'); b.textContent = lab; if(k===0) b.className='on';
  b.onclick = () => {
    [...rbox.children].forEach(x=>x.classList.remove('on')); b.classList.add('on');
    let end = dates[dates.length-1], start;
    if(yrs === null){ start = dates[0]; }
    else if(yrs === 0){ start = end.slice(0,4)+'-01-01'; }
    else {
      const dt = new Date(end+'T00:00:00Z'); dt.setUTCFullYear(dt.getUTCFullYear()-yrs);
      start = dt.toISOString().slice(0,10); if(start < dates[0]) start = dates[0];
    }
    zoomTo(start, end);
  };
  rbox.appendChild(b);
});
const sbox = document.getElementById('scales');
[['Log',true],['Linear',false]].forEach(([lab,isLog], k) => {
  const b = document.createElement('button'); b.textContent = lab; if(k===0) b.className='on';
  b.onclick = () => {
    [...sbox.children].forEach(x=>x.classList.remove('on')); b.classList.add('on');
    Plotly.relayout(gd, {'yaxis.type': isLog?'log':'linear', 'yaxis2.type': isLog?'log':'linear'});
  };
  sbox.appendChild(b);
});

/* ---- events list ----------------------------------------------------- */
const evbox = document.getElementById('events');
D.events.forEach(e => {
  const el = document.createElement('div');
  el.className = 'ev ' + e.kind;
  el.innerHTML = `<div class="d">${e.start}<br>${e.end}</div>
    <div class="n">${e.label}</div>
    <div class="num">${e.inr>0?'+':''}${e.inr.toFixed(1)}%<span>INR</span></div>
    <div class="num">${e.usd>0?'+':''}${e.usd.toFixed(1)}%<span>USD</span></div>
    <div class="num">${e.fx>0?'+':''}${e.fx.toFixed(1)}%<span>USD/INR</span></div>
    <div class="note">${e.note}</div>
    <div class="zoom">click to zoom</div>`;
  el.onclick = () => { zoomTo(e.start, e.end);
    [...rbox.children].forEach(x=>x.classList.remove('on')); };
  evbox.appendChild(el);
});

/* ---- kpis, legend, notes, pills -------------------------------------- */
document.getElementById('kpis').innerHTML = D.kpis.map(k =>
  `<div class="kpi"><div class="k">${k.label}</div><div class="v">${k.value}</div>
   <div class="u">${k.unit}</div><div class="s">${k.sub}</div></div>`).join('');
document.getElementById('legend').innerHTML = SERIES.map(s =>
  `<div class="lg"><span class="dot" style="background:${s.color}"></span>${s.title}
   <span style="color:#9A938A">&middot; ${s.unit}</span></div>`).join('');
document.getElementById('notes').innerHTML = D.notes.map(n =>
  `<div class="note"><h3>${n.h}</h3><p>${n.p}</p></div>`).join('');
document.getElementById('insight').textContent = D.fx_insight;
const M = D.meta;
document.getElementById('pills').innerHTML = [
  `Source file: ${M.source}`,
  `${M.rows.toLocaleString()} rows in the file; ${M.shown.toLocaleString()} plotted`,
  `Chart window ${M.first} to ${M.last}`,
  `Starts at the first date with a ${M.win}-year window: the ${M.excluded.toLocaleString()} earlier rows `
    + `(${M.file_first} to ${M.excluded_to}) are not shown`,
  `${M.computed.toLocaleString()} rows carry a ${M.win}-year CAGR`,
  (M.inr_neg === 0 ? 'INR CAGR never negative in this file'
                   : `INR CAGR negative on ${M.inr_neg} rows`),
  `All-time high ${M.ath.toLocaleString(undefined,{maximumFractionDigits:2})} on ${M.ath_date}`,
  (M.standalone ? 'Self-contained file: no internet needed, safe to email'
                : 'Loads Plotly and Inter from a CDN when opened'),
  `Hover anywhere for the values at that date`
].map(t => `<span class="pill">${t}</span>`).join('');
document.getElementById('foot').textContent =
  'Built from ' + M.source + ': every plotted point is the value the file carries, unsmoothed and '
  + 'undownsampled; rows before the first full ' + M.win + '-year window are excluded, not summarised.';
</script>
</body>
</html>
"""

html = (TEMPLATE.replace("__DATA__", json.dumps(data, separators=(",", ":")))
                .replace("__IDX__", IDX)
                .replace("__WY__", WY)
                .replace("__WYS__", WYS)
                .replace("__FONTS__", FONT_TAG)
                .replace("__PLOTLY__", PLOTLY_TAG))
os.makedirs(OUTDIR, exist_ok=True)
with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(html)
print("wrote %s  (%.1f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))
print("%s: rows %d, events %d, kpis %d, notes %d" % (IDX, n, len(events), len(kpis), len(notes)))
print("plotted span %s .. %s (%d rows, %.1f years); %d earlier rows excluded; last row TRI %.2f, "
      "USD/INR %.4f, %s CAGR %.2f%% INR / %.2f%% USD"
      % (d[0], d[-1], shown, win_years, blank_rows, last["tri_inr"], last["usdinr"], WYS,
         100 * last["cagr_inr"], 100 * last["cagr_usd"]))
print("facts used: deepest drawdown %.1f%% (%s), worst day %.2f%% (%s), blank rows %d, "
      "carried-forward FX %s"
      % (dd_pct, dd_date, worst_day[1], worst_day[0], blank_rows,
         fx_cf if fx_cf is not None else "n/a"))
print("external verification on disk: %s"
      % ("none found" if not VFACT else
         "%s: %d dates, %d mismatches" % (VFACT["dir"], VFACT["common"], VFACT["mismatch"])))
print("window rule re-derived: %d-year windows span %.3f to %.3f elapsed years; first window row %s"
      % (W, w_lo, w_hi, D_ALL[rule_i]))
print("assets: %s" % ("Plotly + font inlined (no network needed)"
                      if INLINED else "CDN links (needs internet when opened)"))
