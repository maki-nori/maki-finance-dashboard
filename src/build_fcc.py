#!/usr/bin/env python3
"""Finance Control Centre builder (tabs 1 Today, 2 Calendar, 3 Company Health, 4 Management Accounts).

Offline, stdlib only, idempotent. Reads what the pipeline already produces:

  cash-snapshots/YYYY-MM-DD.json          group + per-company cash (the locked cash rule)
  xero_finance/scripts/output/daily_bank_<CODE>.csv   the bank feed per company
  xero_finance/xero_finance.db            bank_accounts (unreconciled lines), read only
  cash-dashboard.html                     copied beside the board and embedded in tab 1
  finance_control_centre/ma_history.json  management accounts (extract_ma_history.py writes it)
  fa-status/last_run_<CODE>.json          financial-analysis pipeline state per company

Writes finance_control_centre/site/:
  index.html                 the board (copied from the template beside this script)
  cash-dashboard.html        copy of the daily cash dashboard for the embed
  data/fcc.json              everything the board shows, with "as at" + source per block
  data/fcc.js                same payload as window.FCC (works on file:// and Pages)

Nothing here touches Xero. Missing inputs are named, never filled with zero.

Usage:
  python3 finance_control_centre/build_fcc.py [--as-of YYYY-MM-DD] [--root PATH]
Exit codes: 0 ok, 1 no cash snapshot, 2 config/template missing.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "fcc_config.json"
TEMPLATE_PATH = HERE / "index.template.html"
SITE_DIR = HERE / "site"

CAT_LABELS = {
    "vat": "VAT",
    "paye": "PAYE to HMRC",
    "payroll": "Payroll (net wages)",
    "nest": "Nest pension",
    "rates": "Business rates",
    "rent": "Rent",
    "supplier_run": "Supplier payment run",
    "corp_tax": "Corporation tax",
    "companies_house": "Companies House filing",
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def d(s: str) -> date:
    return date.fromisoformat(s[:10])


def money(x) -> float:
    return round(float(x or 0), 2)


def add_months(dt: date, n: int) -> date:
    y, m = dt.year, dt.month + n
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    last = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(dt.day, last))


def median_day(dates: list[date]) -> int:
    return int(statistics.median([x.day for x in dates]))


# ---------------------------------------------------------------------------
# classification of a bank-feed row
# ---------------------------------------------------------------------------
COUNCIL_RE = re.compile(r"council|city of |borough|glasgow city|edinburgh cou|business rates|non.?domestic rate", re.I)
LANDLORD_RE = re.compile(r"savills|jll|british land|derwent|pinnacle property|property compan|real estate|landswood|mqm commercial|consolidated property|estates? ltd|land securities|hammerson|intu|westfield", re.I)


def classify(row: dict, company: dict, cfg: dict) -> str | None:
    """Return a category key for an OUT row, or None if it is not a cash leaving item."""
    t = row.get("Type", "")
    if "TRANSFER" in t:
        return None  # own-account moves (main bank -> AP Only etc.), never a payment
    contact = (row.get("Contact") or "").strip().lower()
    acct = (row.get("AccountNames") or "").lower()
    desc = (row.get("Description") or "").lower()
    blob = " ".join([contact, acct, desc, (row.get("Reference") or "").lower()])

    if "hmrc" in blob or "h m r c" in blob:
        if "vat" in blob:
            return "vat"
        return "paye"
    if contact.startswith("nest") or "pension" in acct:
        return "nest"  # shown inside the payroll line (ruled 8-Sep), projected on its own date
    if contact in ("payroll", "modulr payroll", "modular payroll") or acct.startswith("direct wages"):
        return "payroll"
    if COUNCIL_RE.search(contact) or acct.startswith("rates"):
        return "rates"
    if company.get("is_property_co"):
        if "rent" in acct or "service charge" in acct or LANDLORD_RE.search(contact):
            return "rent"
    else:
        if contact.startswith("maki property"):
            return "rent"
    for pat in cfg["intercompany_contact_patterns"]:
        if contact.startswith(pat):
            return "intercompany"
    if not contact:
        return None  # blank contact = uncoded line; named as a gap, never guessed
    return "supplier_run"


# ---------------------------------------------------------------------------
# pattern engine
# ---------------------------------------------------------------------------
def monthly_pattern(obs: list[tuple[date, float]], today: date, horizon: int, grace: int, label: str, source: str, code: str, note: str = ""):
    """Project a monthly item from its observed payments. obs sorted by date. One entry per month (summed)."""
    if not obs:
        return [], None
    by_month: dict[tuple[int, int], list[tuple[date, float]]] = defaultdict(list)
    for dt, amt in obs:
        by_month[(dt.year, dt.month)].append((dt, amt))
    months = sorted(by_month)
    recent = months[-6:]
    first_dates = [min(x[0] for x in by_month[m]) for m in recent]
    typical_day = median_day(first_dates)
    last_m = months[-1]
    last_amt = money(sum(a for _, a in by_month[last_m]))
    avg_amt = money(statistics.mean([sum(a for _, a in by_month[m]) for m in recent]))
    items = []
    # actual paid this month and last month (lookback), then projections
    cursor = date(today.year, today.month, 1)
    start = add_months(cursor, -1)
    end = today + timedelta(days=horizon)
    last_paid_dt = max(x[0] for x in by_month[last_m])
    stale = (today - last_paid_dt).days > 75  # pattern may have stopped: never call it overdue, ask instead
    basis = f"estimate, last paid {last_amt:,.2f} on {last_paid_dt.isoformat()}"
    m = start
    while m <= end:
        key = (m.year, m.month)
        last_dom = (add_months(m, 1) - timedelta(days=1)).day
        due = date(m.year, m.month, min(typical_day, last_dom))
        if key in by_month:
            paid_dt = max(x[0] for x in by_month[key])
            amt = money(sum(a for _, a in by_month[key]))
            items.append(dict(company=code, category=label, date=paid_dt.isoformat(), amount=amt, basis="actual", status="paid", note=note, source=source))
        elif key < months[-1]:
            pass  # before the last observed month: history, nothing to project
        elif stale:
            items.append(dict(company=code, category=label, date=due.isoformat(), amount=last_amt, basis=basis, status="check",
                              note=(note + f" No payment seen since {last_paid_dt.isoformat()}: confirm whether this still applies.").strip(), source=source))
        elif due >= today or (today - due).days <= grace:
            items.append(dict(company=code, category=label, date=due.isoformat(), amount=last_amt, basis=basis, status="due", note=note, source=source))
        else:
            items.append(dict(company=code, category=label, date=due.isoformat(), amount=last_amt, basis=basis, status="overdue",
                              note=(note + " Expected by pattern, not seen in the bank feed.").strip(), source=source))
        m = add_months(m, 1)
    summary = dict(typical_day=typical_day, last_amount=last_amt, avg_6m=avg_amt, months_observed=len(months), last_paid=max(x[0] for x in by_month[last_m]).isoformat())
    return items, summary


def vat_pattern(obs: list[tuple[date, float]], today: date, horizon: int, grace: int, source: str, code: str):
    """VAT: quarterly by default; monthly-payment companies flagged from the observed gap."""
    if not obs:
        return [], None
    # merge same-month payments (e.g. a payment on account + balance)
    by_month: dict[tuple[int, int], list[tuple[date, float]]] = defaultdict(list)
    for dt, amt in obs:
        by_month[(dt.year, dt.month)].append((dt, amt))
    months = sorted(by_month)
    pays = [(max(x[0] for x in by_month[m]), money(sum(a for _, a in by_month[m]))) for m in months]
    gaps = [(pays[i][0] - pays[i - 1][0]).days for i in range(1, len(pays))]
    cadence_months = 3
    monthly_flag = False
    if gaps and statistics.median(gaps) < 45:
        cadence_months, monthly_flag = 1, True
    last_dt, last_amt = pays[-1]
    items = []
    for dt, amt in pays:
        if dt >= today - timedelta(days=45):
            items.append(dict(company=code, category="VAT", date=dt.isoformat(), amount=amt, basis="actual", status="paid",
                              note=("Pays VAT monthly." if monthly_flag else "Quarterly VAT return."), source=source))
    nxt = add_months(last_dt, cadence_months)
    end = today + timedelta(days=horizon)
    while nxt <= end:
        status = "due"
        note = ("Pays VAT monthly, refund tracked if over-paid." if monthly_flag else "Quarterly VAT return, dated from the last payment.")
        if nxt < today - timedelta(days=grace):
            if (today - last_dt).days > (75 if monthly_flag else 130):
                status, note = "check", note + f" No VAT payment seen since {last_dt.isoformat()}: confirm the stagger."
            else:
                status, note = "overdue", note + " Expected by pattern, not seen in the bank feed."
        items.append(dict(company=code, category="VAT", date=nxt.isoformat(), amount=last_amt,
                          basis=f"estimate, last paid {last_amt:,.2f} on {last_dt.isoformat()}", status=status, note=note, source=source))
        nxt = add_months(nxt, cadence_months)
    if len(pays) < 2:
        for i in items:
            if i["status"] != "paid":
                i["note"] += " Only one VAT payment on record, so the cadence is assumed."
    summary = dict(cadence="monthly" if monthly_flag else "quarterly", last_amount=last_amt, last_paid=last_dt.isoformat(), payments_observed=len(pays))
    return items, summary


def weekly_pattern(rows: list[tuple[date, float, str]], today: date, horizon: int, code: str, label: str, source: str, weeks: int = 8):
    """Weekly run: weekday by amount over the last N weeks, average weekly total, top counterparties."""
    cutoff = today - timedelta(days=7 * weeks)
    recent = [(dt, amt, c) for dt, amt, c in rows if cutoff <= dt <= today]
    if not recent:
        return [], None
    by_wd = Counter()
    by_week = Counter()
    by_contact = Counter()
    for dt, amt, c in recent:
        by_wd[dt.weekday()] += amt
        by_week[dt.isocalendar()[:2]] += amt
        by_contact[c] += amt
    run_day = by_wd.most_common(1)[0][0]
    weeks_seen = max(1, len(by_week))
    avg_week = money(sum(by_week.values()) / weeks_seen)
    last_week_key = max(by_week)
    last_week_amt = money(by_week[last_week_key])
    top = [dict(name=c, amount=money(a)) for c, a in by_contact.most_common(8)]
    items = []
    # actual: last two weeks
    for wk in sorted(by_week)[-2:]:
        wk_dates = [dt for dt, _, _ in recent if dt.isocalendar()[:2] == wk]
        items.append(dict(company=code, category=label, date=max(wk_dates).isoformat(), amount=money(by_week[wk]), basis="actual, week total", status="paid",
                          note=f"Week {wk[1]}: {len(wk_dates)} payments.", source=source))
    # projections: next run days
    nxt = today + timedelta(days=(run_day - today.weekday()) % 7 or 7)
    end = today + timedelta(days=horizon)
    while nxt <= end:
        items.append(dict(company=code, category=label, date=nxt.isoformat(), amount=avg_week,
                          basis=f"estimate, {weeks}-week average (last week {last_week_amt:,.2f})", status="due",
                          note=f"Usually paid on a {WEEKDAYS[run_day]}. Top: " + ", ".join(f"{t['name']} {t['amount']:,.0f}" for t in top[:3]) + ".", source=source))
        nxt += timedelta(days=7)
    summary = dict(run_day=WEEKDAYS[run_day], avg_week=avg_week, last_week=last_week_amt, weeks=weeks_seen, top=top)
    return items, summary


# ---------------------------------------------------------------------------
# tab 3: company health
# ---------------------------------------------------------------------------
HEALTH_RULES = [
    ("overdrawn",    "Overdrawn"),
    ("vat",          "VAT up to date"),
    ("filing",       "Companies House filing"),
    ("ma_closed",    "Management account closed"),
    ("np_negative",  "Net profit"),
]


def load_analysis(cfg: dict, period: str | None):
    """Monica's monthly analysis, if extract_analysis_docx.py has run. Exact pounds per
    line per site, her prior-year column, and her per-site notes."""
    if period:
        fp = HERE / f"analysis_{period}.json"
        if fp.exists():
            return json.loads(fp.read_text())
    cands = sorted(HERE.glob("analysis_*.json"))
    return json.loads(cands[-1].read_text()) if cands else None


def load_bep(cfg: dict):
    """The break-even store, plus any figures Michael has given directly that are not in
    the monthly analysis document. Those are merged in and marked with their source."""
    fp = HERE / "bep_data.json"
    if not fp.exists():
        return None
    b = json.loads(fp.read_text())
    ov = cfg.get("bep_overrides") or {}
    by_code = {r["code"]: r for r in b["rows"]}
    for code, o in ov.items():
        r = by_code.get(code) or dict(code=code, notes=[], recovered_note=None, sign_conflict=False,
                                      sign_corrected=None, fully_recovered=False, start=None,
                                      start_precision=None, bep_pct_monica=None, capex_label="Total capex")
        r["capex"] = o.get("capex", r.get("capex"))
        r["profit_to_date"] = o.get("profit_to_date", r.get("profit_to_date"))
        r["start"] = o.get("start", r.get("start"))
        r["start_precision"] = o.get("start_precision", r.get("start_precision"))
        r["source_note"] = o.get("source", "Given directly by Michael, not in the monthly analysis.")
        r["bep_pct"] = None if not r.get("capex") else round((r.get("profit_to_date") or 0) / r["capex"] * 100, 2)
        if code not in by_code:
            b["rows"].append(r)
            by_code[code] = r
    # A site Monica records as fully recovered counts as having earned its whole build cost back,
    # even where the cumulative profit on record falls short of it (M9: her recovery note against
    # her own two figures). Without this the bar, the percentage and the totals tell three
    # different stories for the same site. The raw profit is kept alongside.
    for r in b["rows"]:
        cap, prof = r.get("capex"), (r.get("profit_to_date") or 0)
        r["done"] = bool(r.get("fully_recovered")) or (cap is not None and prof >= cap)
        r["recovered_counted"] = float(cap) if (r["done"] and cap) else float(prof)
        r["outstanding"] = 0.0 if r["done"] else (round(cap - prof, 2) if cap else None)
        r["bep_pct_display"] = 100.0 if r["done"] else r.get("bep_pct")
        r["counted_uplift"] = round(r["recovered_counted"] - prof, 2)
    b["rows"].sort(key=lambda r: (r["bep_pct_display"] is None, -(r["bep_pct_display"] or -1e9)))
    known = [r for r in b["rows"] if r.get("capex")]
    b["total_capex"] = round(sum(r["capex"] for r in known), 2)
    b["total_recovered"] = round(sum(r["recovered_counted"] for r in known), 2)
    b["total_profit_on_record"] = round(sum(r.get("profit_to_date") or 0 for r in known), 2)
    b["capped_codes"] = [r["code"] for r in known if r["counted_uplift"] > 0]
    b["capped_amount"] = round(b["total_recovered"] - b["total_profit_on_record"], 2)
    b["sites_with_capex"] = len(known)
    b["sites_without_capex"] = [r["code"] for r in b["rows"] if not r.get("capex")]
    b["percent_recovered"] = round(b["total_recovered"] / b["total_capex"] * 100, 2) if b["total_capex"] else None
    b["outstanding"] = round(b["total_capex"] - b["total_recovered"], 2)
    return b


def load_ma_years(cfg: dict, store: dict | None, alias: dict, gaps: list):
    """The multi-year MA store (ma_years.json) shaped for the board.

    Columnar on purpose: one array of twelve numbers per line per year per site,
    so three years of every P&L line for the whole estate costs about 90 KB on the
    page instead of a megabyte of repeated keys. Percentages are NOT shipped, they
    are derived from the pounds in the browser, so a rate can never disagree with
    the two figures it sits between.

    Where the banked monthly store (ma_history.json, Monica's published dashboard)
    covers the same site-month, THAT figure wins and the disagreement is recorded,
    so this tab and the Today tab can never show two different numbers for the
    same month.
    """
    fp = HERE / "ma_years.json"
    if not fp.exists():
        gaps.append(dict(company="Group", text="ma_years.json not built yet: click "
                         "'Fetch MA History (2024-2026).command' then run "
                         "finance_control_centre/extract_ma_annuals.py. The Management "
                         "Accounts tab shows the latest two months only without it."))
        return None
    raw = json.loads(fp.read_text())
    amap = {k: v for k, v in (cfg.get("ma_annual_code_aliases") or {}).items()
            if not k.startswith("_")}
    meta_all = {k: v for k, v in (cfg.get("site_meta") or {}).items() if not k.startswith("_")}
    LINES = ["sales", "tips", "net_sales", "wages", "rent", "food", "variable",
             "vat", "total_costs", "np"]

    sites: dict[str, dict] = {}
    for code0, srec in raw.get("sites", {}).items():
        code = amap.get(code0, code0)
        meta = meta_all.get(code, {})
        entry = sites.setdefault(code, dict(
            code=code, name=meta.get("name") or code,
            region=meta.get("region"), cohort=meta.get("cohort"),
            kind=meta.get("kind") or "unknown", note=meta.get("note"),
            y={}, sources={}))
        for yr, y in (srec.get("years") or {}).items():
            cols = {ln: [None] * 12 for ln in LINES}
            for mn, rec in (y.get("months") or {}).items():
                i = int(mn) - 1
                if not 0 <= i < 12:
                    continue
                for ln in LINES:
                    if rec.get(ln) is not None:
                        cols[ln][i] = rec[ln]
            entry["y"][yr] = cols
            entry["sources"][yr] = y.get("source")

    # the banked dashboard months override the workbook, and the gap is recorded
    overrides = []
    if store:
        for mkey, mrec in (store.get("months") or {}).items():
            yr, mn = mkey.split("-")
            i = int(mn) - 1
            for c0, row in (mrec.get("sites") or {}).items():
                code = alias.get(c0, c0)
                if code == "ALL" or code not in sites:
                    continue
                cols = sites[code]["y"].get(yr)
                if cols is None:
                    continue
                for ln, key in (("sales", "sales"), ("np", "np")):
                    banked = row.get(key)
                    if banked is None:
                        continue
                    was = cols[ln][i]
                    if was is not None and abs(was - banked) > max(50.0, abs(banked) * 0.005):
                        overrides.append(dict(code=code, month=mkey, line=ln,
                                              workbook=round(was, 2), banked=round(banked, 2)))
                    cols[ln][i] = round(float(banked), 2)
                pounds = row.get("pounds") or {}
                for ln in ("wages", "rent", "food", "variable", "vat"):
                    if pounds.get(ln) is not None:
                        cols[ln][i] = round(float(pounds[ln]), 2)
                # total costs must stay sales less net profit for the month it was
                # overridden in, or the table stops adding up in front of the reader
                if row.get("total_costs") is not None:
                    cols["total_costs"][i] = round(float(row["total_costs"]), 2)
                elif cols["sales"][i] is not None and cols["np"][i] is not None:
                    cols["total_costs"][i] = round(cols["sales"][i] - cols["np"][i], 2)

    years = sorted({y for s in sites.values() for y in s["y"]})
    months = []
    for yr in years:
        for i in range(12):
            if any((s["y"].get(yr, {}).get("sales") or [None] * 12)[i] is not None
                   for s in sites.values()):
                months.append(f"{yr}-{i + 1:02d}")
    regions, cohorts = [], []
    for s in sites.values():
        if s["kind"] != "restaurant":
            continue
        if s["region"] and s["region"] not in regions:
            regions.append(s["region"])
        if s["cohort"] and s["cohort"] not in cohorts:
            cohorts.append(s["cohort"])
    counted = [c for c, s in sites.items() if s["kind"] == "restaurant"]
    not_counted = sorted(c for c, s in sites.items() if s["kind"] != "restaurant")

    return dict(
        schema=raw.get("schema"), built=raw.get("built"), lines=LINES,
        years=years, months=months, sites=sites,
        regions=sorted(regions), cohorts=cohorts,
        cohort_labels={"first_ten": "M1 to M10", "new": "M11 onwards",
                       "brands": "Ikigai and Nori"},
        counted=sorted(counted, key=lambda c: (len(c), c)), not_counted=not_counted,
        overrides=overrides,
        conflicts=raw.get("conflicts") or [], rejected=raw.get("rejected") or [],
        warnings=raw.get("warnings") or [],
        source=raw.get("source", ""),
        note=("Monica's per-site annual workbooks, one file per site per year. Where her "
              "published monthly dashboard covers the same month, that figure is the one "
              "shown. Rates are worked out from the pounds beside them. Dubai is reported "
              "in dirhams and is never inside a UK total."),
    )


def load_pl_prior(cfg: dict):
    fp = HERE / "pl_2025.json"
    return json.loads(fp.read_text()) if fp.exists() else None


def revenue_target(cfg: dict, store, prior, gaps):
    """Progress against the revenue target Michael has set for the year, with the run rate
    needed for the rest of it and two ways of projecting where the year actually lands."""
    tgt = (cfg.get("targets") or {}).get("revenue_year")
    if not tgt or not store:
        return None
    latest = store["latest_month"]
    g = store["months"][latest]["sites"].get("ALL") or {}
    ytd = g.get("ytd_sales")
    if ytd is None:
        return None
    month_no = int(latest.split("-")[1])
    months_left = 12 - month_no
    series = (store.get("group_series") or {}).get("sales") or []
    avg = round(ytd / month_no, 2) if month_no else None
    needed = round(max(0.0, tgt - ytd), 2)
    per_month = round(needed / months_left, 2) if months_left else None

    # projection one: keep the year-to-date average going
    run_rate = round(ytd + (avg or 0) * months_left, 2)
    # projection two: repeat last year's shape from this month onwards
    seasonal = None
    pri = ((prior or {}).get("lines") or {}).get("revenue") or []
    if pri and len(pri) == 12 and month_no < 12:
        base = pri[month_no - 1]["value"]
        if base:
            factor = (g.get("sales") or 0) / base
            seasonal = round(ytd + sum(x["value"] for x in pri[month_no:]) * factor, 2)
    return dict(
        target=tgt, ytd=ytd, through=latest, months_done=month_no, months_left=months_left,
        needed=needed, needed_per_month=per_month, ytd_average=avg,
        pct=round(ytd / tgt * 100, 1),
        projection_run_rate=run_rate,
        projection_seasonal=seasonal,
        prior_year_total=(prior or {}).get("total_revenue"),
        basis=("Year to date is the management-accounts figure. The run-rate projection keeps the "
               "year-to-date average going. The seasonal projection repeats last year's shape from here, "
               "scaled by how this month compares with the same month last year."),
    )


def load_ma(cfg: dict):
    """Read the MA store written by extract_ma_history.py. Returns (store, code->code map)."""
    fp = HERE / cfg.get("ma_history_file", "ma_history.json")
    if not fp.exists():
        return None, {}
    store = json.loads(fp.read_text())
    alias = cfg.get("ma_code_aliases", {})
    return store, alias


def ma_by_company(store: dict, alias: dict, month: str) -> dict[str, dict]:
    """MA rows keyed by the board's company code (not the MA sheet's code)."""
    out = {}
    for k, v in (store or {}).get("months", {}).get(month, {}).get("sites", {}).items():
        if k == "ALL":
            continue
        out[alias.get(k, k)] = v
    return out


def fa_status(root: Path, cfg: dict) -> dict[str, dict]:
    out = {}
    sd = root / cfg.get("fa_status_dir", "fa-status")
    if not sd.exists():
        return out
    for fp in sd.glob("last_run_*.json"):
        try:
            j = json.loads(fp.read_text())
        except Exception:
            continue
        code = fp.stem.replace("last_run_", "")
        if code == "last_run":
            code = j.get("site") or "M15"   # the original single-site file, before codes were suffixed
        out[code] = j
    return out


def health_block(cfg, companies, cash_block, calendar, feed_as_at, today, root, store, alias, gaps):
    """One row per company. Red / amber / green from the five ruled tests, plus the
    running-state checks. A test with no data behind it is 'not checked', never green."""
    latest = (store or {}).get("latest_month")
    prior = (store or {}).get("prior_month")
    ma_latest = ma_by_company(store, alias, latest) if latest else {}
    ma_prior = ma_by_company(store, alias, prior) if prior else {}
    h1 = {alias.get(k, k): v for k, v in ((store or {}).get("first_half", {}).get("sites", {}) or {}).items() if k != "ALL"}
    ma_flags = defaultdict(list)
    for f in (store or {}).get("flags", []):
        ma_flags[alias.get(f["code"], f["code"])].append(f)
    fa = fa_status(root, cfg)
    cash_by_code = {r["code"]: r for r in cash_block["companies"]}
    cal_by_code = defaultdict(list)
    for i in calendar:
        cal_by_code[i["company"]].append(i)
    stale_days = cfg["health"]["feed_stale_days"]
    need_months = cfg["health"]["negative_np_months_for_red"]

    rows = []
    for c in companies:
        code = c["code"]
        tests = {}
        notes = []

        # 1. overdrawn
        cr = cash_by_code.get(code)
        if cr is None or cr.get("cash") is None:
            tests["overdrawn"] = dict(state="unknown", text="Not in today's cash snapshot.")
        elif cr["cash"] < 0:
            tests["overdrawn"] = dict(state="red", text=f"Operational cash is {cr['cash']:,.2f}.")
        else:
            neg_tide = [t for t in cr.get("tide", []) if t["balance"] < 0]
            if neg_tide:
                tests["overdrawn"] = dict(state="amber", text="Operational cash is positive but a Tide account is negative: " +
                                          ", ".join(f"{t['name']} {t['balance']:,.2f}" for t in neg_tide) + ".")
            else:
                tests["overdrawn"] = dict(state="green", text=f"Cash {cr['cash']:,.2f}.")

        # 2. VAT
        vat_items = [i for i in cal_by_code[code] if i["category"] == "VAT"]
        vat_overdue = [i for i in vat_items if i["status"] == "overdue"]
        vat_check = [i for i in vat_items if i["status"] == "check"]
        paid = [i for i in vat_items if i["status"] == "paid"]
        if not vat_items:
            tests["vat"] = dict(state="unknown", text="No VAT payment found in the 2026 bank feed, so nothing to check against.")
        elif vat_overdue:
            w = vat_overdue[0]
            tests["vat"] = dict(state="red", text=f"VAT expected {w['date']} ({w['amount']:,.0f}) has not left the bank.")
        elif vat_check:
            tests["vat"] = dict(state="amber", text=vat_check[0]["note"])
        else:
            last = max((i["date"] for i in paid), default=None)
            tests["vat"] = dict(state="green", text=f"Last VAT payment {last}." if last else "On the expected pattern.")

        # 3. Companies House filing: not wired
        tests["filing"] = dict(state="unknown", text="Companies House is not connected, so filing and corporation tax dates cannot be checked.")

        # 4. MA closed
        row_l, row_p = ma_latest.get(code), ma_prior.get(code)
        flags = ma_flags.get(code, [])
        if not latest:
            tests["ma_closed"] = dict(state="unknown", text="No management-accounts store found.")
        elif row_l is None:
            if c.get("is_property_co") or not c.get("feed"):
                tests["ma_closed"] = dict(state="n/a", text="Not a trading site, so it has no site management account.")
            else:
                tests["ma_closed"] = dict(state="red", text=f"No {latest} management account on the board.")
        elif any(f["level"] == "red" for f in flags):
            tests["ma_closed"] = dict(state="red", text=[f["text"] for f in flags if f["level"] == "red"][0])
        else:
            tests["ma_closed"] = dict(state="green", text=f"{latest} closed. Sales {row_l['sales']:,.0f}, net profit {row_l['np']:,.0f}.")

        # 5. negative net profit for N months
        seq = []
        for m, r in ((latest, row_l), (prior, row_p)):
            if r and r.get("np") is not None:
                seq.append((m, r["np"]))
        neg = [m for m, v in seq if v < 0]
        if not seq:
            tests["np_negative"] = dict(state="unknown", text="No monthly net profit on the board for this company.")
        elif len(neg) == len(seq) and len(seq) < need_months:
            h = h1.get(code)
            extra = ""
            if h and h.get("np") is not None and (h.get("sales") or 0) > 1000:
                extra = f" January to June together was {h['np']:,.0f}."
            elif h and (h.get("sales") or 0) <= 1000:
                extra = " The site was not trading in the first half, so there is no earlier month to add."
            tests["np_negative"] = dict(state="amber", text=f"Net profit negative in all {len(seq)} months on the board ({', '.join(neg)}), "
                                                            f"but the rule needs {need_months} months and only {len(seq)} are loaded.{extra}")
        elif len(neg) == len(seq) and len(seq) >= need_months:
            tests["np_negative"] = dict(state="red", text=f"Net profit negative {len(neg)} months running.")
        elif neg:
            tests["np_negative"] = dict(state="amber", text=f"Net profit negative in {', '.join(neg)}, positive in the other month on the board.")
        else:
            tests["np_negative"] = dict(state="green", text=f"{latest} net profit {seq[0][1]:,.0f}.")

        # running state (supporting, not one of the five)
        run = []
        fs = feed_as_at.get(code)
        if fs:
            age = (today - d(fs)).days
            run.append(dict(state="amber" if age > stale_days else "green", text=f"Bank feed last row {fs} ({age} days old)."))
        elif c.get("feed"):
            run.append(dict(state="amber", text="No bank feed rows read."))
        else:
            run.append(dict(state="unknown", text="Not on the bank-feed pipeline."))
        j = fa.get(code) or fa.get(code.replace(" ", "")) or fa.get(code.replace(" ", "").upper())
        if j:
            ok = j.get("status") == "ok"
            cf = (j.get("bank") or {}).get("checkfail") or 0
            un = (j.get("bank") or {}).get("unalloc") or 0
            bits = f"Financial analysis last ran {j.get('date')}: {j.get('status')}"
            if (j.get("bank") or {}).get("checkfail") is not None:
                bits += f", {cf} lines that do not tie and {un} unallocated"
            st = "red" if not ok else ("amber" if (cf or un) else "green")
            run.append(dict(state=st, text=bits + "."))
            if ok and (cf or un):
                notes.append(f"Financial analysis for {code} finished with {cf} lines that do not tie and {un} unallocated.")
        if cr and cr.get("unrec_n"):
            run.append(dict(state="amber" if cr["unrec_n"] else "green",
                            text=f"{cr['unrec_n']} unreconciled bank lines worth {cr['unrec_gbp']:,.0f}."))

        states = [t["state"] for t in tests.values()]
        checked = sum(1 for s in states if s in ("red", "amber", "green"))
        overall = "red" if "red" in states else "amber" if "amber" in states else "green"
        if overall == "green" and checked < 3:
            overall = "unknown"   # too little behind it to call a company healthy
        unchecked = [HEALTH_RULES[i][1] for i, (k, _) in enumerate(HEALTH_RULES) if tests[k]["state"] == "unknown"]
        rows.append(dict(code=code, tenant=c["tenant"], cluster=c["cluster"], overall=overall,
                         tests=tests, running=run, unchecked=unchecked,
                         cash=(cr or {}).get("cash"), ma=row_l, ma_prior=row_p, flags=flags, notes=notes))

    order = {"red": 0, "amber": 1, "unknown": 2, "green": 3}
    rows.sort(key=lambda r: (order[r["overall"]], -(r["cash"] or 0)))
    counts = Counter(r["overall"] for r in rows)
    gaps.append(dict(company="Group", text="Company Health: the Companies House filing test is not wired, and only "
                                           f"{len([m for m in ((store or {}).get('months') or {})])} months of net profit are loaded, "
                                           f"so the {cfg['health']['negative_np_months_for_red']}-month loss rule cannot go red yet."))
    return dict(rows=rows, counts=dict(counts), rules=[dict(key=k, label=l) for k, l in HEALTH_RULES],
                as_of=today.isoformat(),
                basis="Red when a ruled test fails. A test with nothing behind it says not checked and never counts as green.",
                latest_ma_month=latest, prior_ma_month=prior)


# ---------------------------------------------------------------------------
# tab 4: management accounts
# ---------------------------------------------------------------------------
def ma_block(cfg, store, alias, gaps, analysis=None):
    if not store:
        gaps.append(dict(company="Group", text="No ma_history.json: run finance_control_centre/extract_ma_history.py."))
        return None
    latest, prior = store["latest_month"], store["prior_month"]
    L = store["months"][latest]["sites"]
    P = store["months"][prior]["sites"]
    H1 = store.get("first_half", {}).get("sites", {})
    target = store.get("targets", {}).get("food_pct", 25.0)
    wage_target = (cfg.get("targets") or {}).get("wages_pct",
                   store.get("targets", {}).get("wages_pct", 27.0))

    def site_row(k, v):
        code = alias.get(k, k)
        pv = P.get(k)
        h = H1.get(k)
        sales_delta = None if not pv or pv.get("sales") is None or v.get("sales") is None else round(v["sales"] - pv["sales"], 2)
        np_delta = None if not pv or pv.get("np") is None or v.get("np") is None else round(v["np"] - pv["np"], 2)
        exact = ((analysis or {}).get("group_table") or {}).get(code)
        if k == "ALL" and (analysis or {}).get("group_total"):
            exact = analysis["group_total"]        # the analysis's own Total row, not a sum of ours
        pounds_basis = "worked back from the percentage of sales"
        if exact:
            pounds = dict(wages=exact.get("wage"), rent=exact.get("rent"), food=exact.get("cost_of_sales"),
                          variable=exact.get("variable"), vat=exact.get("tax"))
            pounds_basis = "as written in the monthly analysis"
        else:
            pounds = {}
            for key in ("wages", "rent", "food", "variable", "vat"):
                pct = v.get(f"{key}_pct")
                pounds[key] = None if pct is None or v.get("sales") is None else round(v["sales"] * pct / 100.0, 2)
        py = ((analysis or {}).get("prior_year") or {}).get(code) or {}
        py_sales = (py.get("prior_year") or {}).get("sales")
        py_delta_pct = None
        if py_sales and v.get("sales"):
            py_delta_pct = round((v["sales"] - py_sales) / py_sales * 100, 1)
        return dict(code=code, ma_code=k, name=v.get("name"), sales=v.get("sales"), tips=v.get("tips"),
                    wages_pct=v.get("wages_pct"), rent_pct=v.get("rent_pct"), food_pct=v.get("food_pct"),
                    variable_pct=v.get("variable_pct"), vat_pct=v.get("vat_pct"), pounds=pounds,
                    pounds_basis=pounds_basis, prior_year_sales=py_sales,
                    prior_year_label=py.get("prior_year_label"), prior_year_delta_pct=py_delta_pct,
                    site_notes=(((analysis or {}).get("sites") or {}).get(code) or {}).get("notes") or [],
                    total_costs=v.get("total_costs"), np=v.get("np"), np_pct=v.get("np_pct"),
                    ytd_sales=v.get("ytd_sales"), ytd_np=v.get("ytd_np"),
                    sales_delta=sales_delta, np_delta=np_delta,
                    h1_sales=(h or {}).get("sales"), h1_np=(h or {}).get("np"),
                    food_over_target=None if v.get("food_pct") is None else round(v["food_pct"] - target, 1),
                    prior_np=(pv or {}).get("np"))

    sites = [site_row(k, v) for k, v in L.items() if k != "ALL"]
    sites.sort(key=lambda r: -(r["np"] if r["np"] is not None else -1e12))

    def site_no(code: str):
        m = re.match(r"^M(\d+)$", code)
        return (0, int(m.group(1))) if m else (1, code)
    by_number = sorted(sites, key=lambda r: site_no(r["code"]))
    group = site_row("ALL", L["ALL"]) if "ALL" in L else None
    losers = [r for r in sites if r["np"] is not None and r["np"] < 0]
    over_food = [r for r in sites if r["food_pct"] is not None and r["food_pct"] > target]
    over_wages = [r for r in sites if r["wages_pct"] is not None and r["wages_pct"] > wage_target]
    movers_up = sorted([r for r in sites if r["np_delta"] is not None], key=lambda r: -r["np_delta"])[:5]
    movers_dn = sorted([r for r in sites if r["np_delta"] is not None and r["np_delta"] < 0], key=lambda r: r["np_delta"])[:5]

    gaps.append(dict(company="Group", text="Management Accounts: 2023 to 2025 and January to June 2026 month by month are still in "
                                           "Monica's annual workbooks in Drive. This machine has no network from the shell, so the board "
                                           "shows the two closed months plus the 2026 group totals by month."))
    return dict(
        latest_month=latest, prior_month=prior,
        latest_label=store["months"][latest].get("label", latest), prior_label=store["months"][prior].get("label", prior),
        group=group, group_prior=(site_row("ALL", P["ALL"]) if "ALL" in P else None), sites=sites,
        by_number=[r["code"] for r in by_number], losers=[r["code"] for r in losers],
        loser_total=round(sum(r["np"] for r in losers), 2) if losers else 0.0,
        over_food=[dict(code=r["code"], food_pct=r["food_pct"],
                        caution=next((f["text"] for f in (store.get("flags") or [])
                                      if alias.get(f["code"], f["code"]) == r["code"] and f["level"] == "red"), None))
                   for r in sorted(over_food, key=lambda r: -r["food_pct"])],
        food_target=target, wage_target=wage_target,
        over_wages=[dict(code=r["code"], wages_pct=r["wages_pct"])
                    for r in sorted(over_wages, key=lambda r: -r["wages_pct"])],
        movers_up=movers_up, movers_down=movers_dn,
        group_series=store.get("group_series", {}), first_half_group=H1.get("ALL"),
        flags=store.get("flags", []), store_gaps=store.get("gaps", []),
        closing_notes=(analysis or {}).get("closing_notes", []),
        analysis_source=(analysis or {}).get("source"),
        source=store["months"][latest].get("source", ""), notes=[store["months"][latest].get("note", ""), store["months"][prior].get("note", "")],
    )


# ---------------------------------------------------------------------------
# main build
# ---------------------------------------------------------------------------
def load_snapshot(snap_dir: Path, today: date):
    files = sorted(p for p in snap_dir.glob("????-??-??.json"))
    files = [p for p in files if d(p.stem) <= today]
    if not files:
        return None, None, None
    latest = json.loads(files[-1].read_text())
    prev = json.loads(files[-2].read_text()) if len(files) > 1 else None
    week = None
    target = d(files[-1].stem) - timedelta(days=7)
    for p in reversed(files):
        if d(p.stem) <= target:
            week = json.loads(p.read_text())
            break
    return latest, prev, week


def tenant_cash(snap) -> dict[str, dict]:
    return {t["tenant"]: t for t in (snap or {}).get("tenants", [])}


def build(root: Path, today: date) -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    companies = [c for c in cfg["companies"] if not c.get("hidden")]
    by_tenant = {c["tenant"]: c for c in companies}
    gaps: list[dict] = []
    attention: list[dict] = []

    # ---- cash ---------------------------------------------------------------
    latest, prev, week = load_snapshot(root / cfg["snapshot_dir"], today)
    if not latest:
        raise SystemExit(1)
    snap_date = d(latest["date"])
    if (today - snap_date).days > 1:
        attention.append(dict(level="red", company="Group", text=f"Cash snapshot is stale: last one is {snap_date.isoformat()}. The 09:00 Xero sync has not run.", tab="today"))
    lc, pc, wc = tenant_cash(latest), tenant_cash(prev), tenant_cash(week)

    # unreconciled per tenant from the DB (read only)
    unrec: dict[str, tuple[int, float]] = {}
    tide_db: dict[str, list] = defaultdict(list)  # Tide is dropped from the snapshot by the cash rule, so it comes from the DB
    db = root / cfg["db_path"]
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        q = ("select t.tenant_name, sum(coalesce(b.unreconciled_count,0)), sum(coalesce(b.unreconciled_total,0)) "
             "from bank_accounts b join tenants t on t.xero_tenant_id=b.xero_tenant_id group by 1")
        for name, n, tot in con.execute(q):
            unrec[name] = (int(n or 0), money(tot))
        q2 = ("select t.tenant_name, b.name, b.statement_balance, b.xero_balance, b.statement_balance_date "
              "from bank_accounts b join tenants t on t.xero_tenant_id=b.xero_tenant_id where lower(b.name) like '%tide%'")
        for name, acct, sb, xb, sbd in con.execute(q2):
            bal = sb if sb is not None else xb
            if bal is not None and abs(float(bal)) > 0.004:
                tide_db[name].append(dict(name=acct, balance=money(bal), basis="statement balance" if sb is not None else "Xero balance", as_at=str(sbd or "")[:10]))
        con.close()
    else:
        gaps.append(dict(company="Group", text="xero_finance.db not found: unreconciled bank lines not shown."))

    rows = []
    group_total = 0.0
    day_delta = 0.0
    week_delta = 0.0
    lfl_day = lfl_week = 0
    for c in companies:
        t = lc.get(c["tenant"])
        if not t:
            gaps.append(dict(company=c["code"], text=f"{c['tenant']} is not in today's cash snapshot (not connected to Xero or not synced)."))
            rows.append(dict(code=c["code"], tenant=c["tenant"], cluster=c["cluster"], cash=None, day=None, week=None, tide=[], unrec_n=None, unrec_gbp=None))
            continue
        cash = money(t["cash"])
        group_total += cash
        dd = ww = None
        if c["tenant"] in pc:
            dd = money(cash - pc[c["tenant"]]["cash"]); day_delta += dd; lfl_day += 1
        if c["tenant"] in wc:
            ww = money(cash - wc[c["tenant"]]["cash"]); week_delta += ww; lfl_week += 1
        tide = tide_db.get(c["tenant"], [])
        n, tot = unrec.get(c["tenant"], (None, None))
        rows.append(dict(code=c["code"], tenant=c["tenant"], cluster=c["cluster"], cash=cash, day=dd, week=ww, tide=tide, unrec_n=n, unrec_gbp=tot,
                         accounts=[(a["name"] if isinstance(a, dict) else str(a)).strip() for a in (t.get("operational_accounts") or [])]))
        if cash < 0:
            attention.append(dict(level="red", company=c["code"], text=f"Overdrawn: operational cash is {cash:,.2f}.", tab="today"))
        for a in tide:
            if a["balance"] < 0:
                attention.append(dict(level="amber", company=c["code"], text=f"Tide account {a['name']} is negative ({a['balance']:,.2f}). Not in the headline.", tab="today"))
    rows.sort(key=lambda r: (r["cash"] is None, -(r["cash"] or 0)))
    tide_total = money(sum(a["balance"] for r in rows for a in r["tide"]))
    alarm = cfg["cash_alarm_gbp"]
    cash_block = dict(
        as_at=latest.get("regenerated_at_utc") or latest["date"], snapshot_date=snap_date.isoformat(),
        group_total=money(group_total), snapshot_group_total=money(latest.get("group_total", 0)),
        day_delta=money(day_delta), day_lfl=lfl_day, prev_date=prev["date"] if prev else None,
        week_delta=money(week_delta), week_lfl=lfl_week, week_date=week["date"] if week else None,
        alarm=alarm, alarm_hit=group_total < alarm, shortfall=money(alarm - group_total) if group_total < alarm else 0.0,
        tide_total=tide_total, companies=rows, methodology=latest.get("methodology", ""),
        recon_backlog_total=money(latest.get("recon_backlog_total", 0)), recon_backlog_count=latest.get("recon_backlog_count"),
        source="cash-snapshots/{}.json (regen_cash_dashboard.py, the locked cash rule)".format(snap_date.isoformat()),
    )
    if cash_block["alarm_hit"]:
        attention.insert(0, dict(level="red", company="Group", text=f"Group cash {group_total:,.0f} is under the {alarm:,.0f} alarm. Shortfall {cash_block['shortfall']:,.0f}.", tab="today"))

    # ---- bank feed -> calendar ----------------------------------------------
    horizon, grace, lookback = cfg["calendar_horizon_days"], cfg["overdue_grace_days"], cfg["calendar_lookback_days"]
    feed_dir = root / cfg["feed_dir"]
    calendar: list[dict] = []
    patterns: dict[str, dict] = {}
    incoming: dict[str, dict] = {}
    inflow30: dict[str, float] = {}
    feed_as_at: dict[str, str] = {}
    for c in companies:
        code = c["code"]
        if not c.get("feed"):
            gaps.append(dict(company=code, text=f"No bank feed for {c['tenant']} (not on the FA pipeline). Nothing dated for it on the calendar."))
            continue
        fp = feed_dir / c["feed"]
        if not fp.exists():
            gaps.append(dict(company=code, text=f"Bank feed file {c['feed']} not found."))
            continue
        with fp.open(newline="") as fh:
            feed = list(csv.DictReader(fh))
        if not feed:
            gaps.append(dict(company=code, text=f"Bank feed {c['feed']} is empty."))
            continue
        last_row = max(d(r["Date"]) for r in feed)
        feed_as_at[code] = last_row.isoformat()
        if (today - last_row).days > 4:
            attention.append(dict(level="amber", company=code, text=f"Bank feed last row is {last_row.isoformat()}, {(today - last_row).days} days old.", tab="calendar"))
        src = f"{c['feed']} (bank feed, last row {last_row.isoformat()})"
        obs: dict[str, list] = defaultdict(list)
        uncoded = 0.0
        for r in feed:
            if r.get("Direction") != "OUT":
                continue
            cat = classify(r, c, cfg)
            dt, amt = d(r["Date"]), abs(float(r["Amount"]))
            if cat is None:
                if "TRANSFER" not in r.get("Type", "") and not (r.get("Contact") or "").strip() and dt >= today - timedelta(days=lookback):
                    uncoded += amt
                continue
            obs[cat].append((dt, amt, (r.get("Contact") or "").strip()))
        for cat in obs:
            obs[cat].sort()
        if uncoded > 0:
            gaps.append(dict(company=code, text=f"{uncoded:,.0f} of payments in the last {lookback} days have no contact in Xero, so they are not on the calendar."))
        p = {}
        items, s = vat_pattern([(a, b) for a, b, _ in obs["vat"]], today, horizon, grace, src, code)
        calendar += items; p["vat"] = s
        for cat in ("paye", "payroll", "nest", "rates", "rent"):
            note = ""
            if cat == "nest":
                note = "Nest pension contributions, paid a few days after the wages."
            if cat == "rent":
                note = "Paid to landlords (see rent register once wired)." if c.get("is_property_co") else "Paid to Maki Property."
            if cat == "rates":
                note = "Standing order to the council. Watched: a missed month goes red."
            if cat == "payroll":
                note = "Net pay as it left the bank. The Nest pension is counted separately."
            items, s = monthly_pattern([(a, b) for a, b, _ in obs[cat]], today, horizon, grace, CAT_LABELS[cat], src, code, note)
            calendar += items; p[cat] = s
        items, s = weekly_pattern(obs["supplier_run"], today, horizon, code, CAT_LABELS["supplier_run"], src)
        calendar += items; p["supplier_run"] = s
        p["intercompany_out_90d"] = money(sum(a for dt, a, _ in obs["intercompany"] if dt >= today - timedelta(days=90)))
        patterns[code] = p
        for cat in ("vat", "paye", "payroll", "rates", "rent"):
            if not obs[cat]:
                gaps.append(dict(company=code, text=f"No {CAT_LABELS[cat]} payment found in the 2026 bank feed, so none is projected."))

        # ---- coming in: Deliveroo pattern, franchise, intercompany ----------
        deliv, franchise, inter = [], [], []
        for r in feed:
            contact = (r.get("Contact") or "").strip()
            cl = contact.lower()
            is_deliveroo = "roofoods" in cl or "deliveroo" in cl
            # Deliveroo settlements land as PAYMENT-ACCPAYCREDIT with Direction "?" (a credit against the Deliveroo bill)
            inbound = r.get("Direction") == "IN" or (is_deliveroo and r.get("Direction") == "?" and "CREDIT" in r.get("Type", ""))
            if not inbound or "TRANSFER" in r.get("Type", ""):
                continue
            dt, amt = d(r["Date"]), abs(float(r["Amount"]))
            if is_deliveroo:
                deliv.append((dt, amt, contact))
            elif any(cl.startswith(f) for f in cfg["franchisee_contacts"]):
                franchise.append((dt, amt, contact))
            elif any(cl.startswith(pat) for pat in cfg["intercompany_contact_patterns"]) and cl != c["tenant"].lower():
                inter.append((dt, amt, contact))
        # everything that actually came in over the last 30 days, so the board can say what
        # the balance looks like at the end of the month rather than only what leaves it
        in30 = 0.0
        for r in feed:
            if "TRANSFER" in r.get("Type", ""):
                continue
            contact = (r.get("Contact") or "").strip().lower()
            is_deliveroo = "roofoods" in contact or "deliveroo" in contact
            inbound = r.get("Direction") == "IN" or (is_deliveroo and r.get("Direction") == "?" and "CREDIT" in r.get("Type", ""))
            if not inbound:
                continue
            if any(contact.startswith(pat) for pat in cfg["intercompany_contact_patterns"]):
                continue      # moving money between our own companies is not new money
            dt = d(r["Date"])
            if today - timedelta(days=30) <= dt <= today:
                in30 += abs(float(r["Amount"]))
        inflow30[code] = money(in30)

        inc = {}
        if deliv:
            _, ds = weekly_pattern(deliv, today, 28, code, "Deliveroo settlement", src)
            if ds:
                inc["deliveroo"] = dict(weekday=ds["run_day"], avg_week=ds["avg_week"], last_week=ds["last_week"], next=(today + timedelta(days=(WEEKDAYS.index(ds["run_day"]) - today.weekday()) % 7 or 7)).isoformat())
        if franchise:
            recent = [(dt, a, ct) for dt, a, ct in franchise if dt >= today - timedelta(days=180)]
            inc["franchise"] = dict(total_180d=money(sum(a for _, a, _ in recent)), last=max(franchise)[0].isoformat(), last_amount=money(max(franchise)[1]), from_=sorted({ct for _, _, ct in franchise}))
        if inter:
            recent = [(dt, a, ct) for dt, a, ct in inter if dt >= today - timedelta(days=90)]
            byc = Counter()
            for _, a, ct in recent:
                byc[ct] += a
            inc["intercompany"] = dict(total_90d=money(sum(byc.values())), by_counterparty=[dict(name=k, amount=money(v)) for k, v in byc.most_common(6)])
        if inc:
            incoming[code] = inc

    # corp tax + Companies House: not wired -> one named gap each, no invented dates
    for k, text in cfg["not_connected_yet"].items():
        gaps.append(dict(company="Group", text=text, key=k))

    # ---- calendar post-processing ------------------------------------------
    calendar = [i for i in calendar if d(i["date"]) >= today - timedelta(days=lookback)]
    calendar.sort(key=lambda i: (0 if i["status"] == "overdue" else 1, i["date"], -i["amount"]))
    for i in calendar:
        i["days"] = (d(i["date"]) - today).days
        if i["status"] == "overdue":
            attention.append(dict(level="red", company=i["company"], text=f"{i['category']} expected {i['date']} ({i['amount']:,.0f}) not seen in the bank feed.", tab="calendar"))
        elif i["status"] == "check" and i["days"] <= 0:
            attention.append(dict(level="amber", company=i["company"], text=f"{i['category']}: no payment since the last one on record. Confirm whether it still applies.", tab="calendar"))
    window = cfg["alert_window_days"]
    leaving = [i for i in calendar if i["status"] in ("due", "overdue") and 0 <= i["days"] <= window]
    by_cat = Counter()
    for i in leaving:
        by_cat[i["category"]] += i["amount"]
    leaving_block = dict(
        window_days=window, total=money(sum(by_cat.values())),
        by_category=[dict(category=k, amount=money(v)) for k, v in by_cat.most_common()],
        items=sorted(leaving, key=lambda i: -i["amount"])[:40],
        basis="Estimates from the bank-feed pattern of each company unless marked actual.",
    )
    # one gap line per company: "No VAT, PAYE or Rent payment found in the 2026 bank feed"
    feed_gaps: dict[str, list[str]] = defaultdict(list)
    kept: list[dict] = []
    for gp in gaps:
        m = re.match(r"No (.+?) payment found in the 2026 bank feed", gp["text"])
        if m:
            feed_gaps[gp["company"]].append(m.group(1))
        else:
            kept.append(gp)
    for code, whats in feed_gaps.items():
        joined = whats[0] if len(whats) == 1 else ", ".join(whats[:-1]) + " or " + whats[-1]
        kept.append(dict(company=code, text=f"No {joined} payment found in the 2026 bank feed, so none is projected on the calendar."))
    gaps[:] = kept

    # ---- projected closing cash, to the end of the month --------------------
    # Payroll leaves on the last day of the month, so a rolling 30-day window lands mid-month
    # and answers nothing anyone asks. The projection runs to month end instead, which always
    # has that month's payroll inside it. Within five days of month end it rolls to the next
    # one, so the horizon never shrinks to a day or two.
    def month_end(dt: date) -> date:
        return date(dt.year + (dt.month == 12), (dt.month % 12) + 1, 1) - timedelta(days=1)

    # 28-Sep-2026 (Michael): the headline is what is LEFT at the end of THIS month, never rolled
    # forward; the following month end is shown alongside as its own number.
    covered = [c["code"] for c in companies if c["code"] in inflow30]
    in_total = money(sum(inflow30.values()))

    def project(me: date) -> dict:
        me_days = (me - today).days
        me_items = [i for i in calendar if i["status"] in ("due", "overdue") and 0 <= i["days"] <= me_days]
        me_by_cat = Counter()
        for i in me_items:
            me_by_cat[i["category"]] += i["amount"]
        me_out = money(sum(me_by_cat.values()))
        me_in = money(in_total / 30 * me_days)
        return dict(
            window_days=window,
            month_end=me.isoformat(), days_to_month_end=me_days,
            opening=cash_block["group_total"],
            inflow=me_in, inflow_30d=in_total,
            outflow=me_out,
            closing=money(cash_block["group_total"] + me_in - me_out),
            out_by_category=[dict(category=k, amount=money(v)) for k, v in me_by_cat.most_common()],
            payroll_in_window=any("payroll" in (i["category"] or "").lower() or "wage" in (i["category"] or "").lower()
                                  for i in me_items),
            by_company=[dict(code=k, inflow=money(v / 30 * me_days)) for k, v in sorted(inflow30.items(), key=lambda kv: -kv[1])],
            companies_covered=len(covered), companies_total=len(companies),
            basis=(f"Runs to {me.strftime('%-d %B %Y')}, the last day of the month, so the month's payroll is inside it. "
                   f"Money in is what actually landed in the bank over the last 30 days across {len(covered)} companies "
                   f"(£{in_total:,.0f}, transfers between our own companies taken out), carried forward at the same daily "
                   f"rate for the {me_days} days left. Money out is everything the calendar expects between now and then. "
                   "Neither is a forecast of trading."),
        )

    me = month_end(today)
    projection = project(me)
    projection["next"] = project(month_end(me + timedelta(days=1)))
    if projection["closing"] < cfg["cash_alarm_gbp"]:
        attention.append(dict(level="amber", company="Group",
                              text=f"On last month's takings, cash at {me.strftime('%-d %B')} after payroll lands near "
                                   f"{projection['closing']:,.0f}, under the {cfg['cash_alarm_gbp']:,.0f} alarm.", tab="today"))

    # attention ordering: reds first, then ambers; dedupe
    seen = set(); att = []
    for a in attention:
        k = (a["company"], a["text"])
        if k not in seen:
            seen.add(k); att.append(a)
    att.sort(key=lambda a: (0 if a["level"] == "red" else 1, a["company"]))

    # ---- tab 3 company health + tab 4 management accounts --------------------
    store, alias = load_ma(cfg)
    if store is None:
        gaps.append(dict(company="Group", text="ma_history.json not built yet: run finance_control_centre/extract_ma_history.py. "
                                               "Company Health and Management Accounts show what they can without it."))
    analysis = load_analysis(cfg, (store or {}).get("latest_month"))
    prior_pl = load_pl_prior(cfg)
    if prior_pl is None:
        gaps.append(dict(company="Group", text="pl_2025.json not built yet: run finance_control_centre/extract_pl_2025.py "
                                               "so the board can show 2026 against 2025."))
    bep = load_bep(cfg)
    if bep is None:
        gaps.append(dict(company="Group", text="bep_data.json not built yet: run finance_control_centre/extract_analysis_docx.py "
                                               "after the monthly Financial Analysis document lands in the project folder."))
    health = health_block(cfg, companies, cash_block, calendar, feed_as_at, today, root, store, alias, gaps)
    ma = ma_block(cfg, store, alias, gaps, analysis)
    mayears = load_ma_years(cfg, store, alias, gaps)

    # health reds and the MA red flags join the Today attention list, once each
    for r in health["rows"]:
        for key, t in r["tests"].items():
            if t["state"] == "red" and key not in ("overdrawn", "vat"):   # those two already raised above
                att.append(dict(level="red", company=r["code"], text=t["text"], tab="health"))
    if ma:
        for f in ma["flags"]:
            if f["level"] == "red":
                att.append(dict(level="red", company=alias.get(f["code"], f["code"]), text=f["text"], tab="ma"))
    seen2 = set(); att2 = []
    for a in att:
        k = (a["company"], a["text"])
        if k not in seen2:
            seen2.add(k); att2.append(a)
    att2.sort(key=lambda a: (0 if a["level"] == "red" else 1, a["company"]))
    att = att2

    return dict(
        generated_at=datetime.now(timezone(timedelta(hours=cfg["as_of_timezone_offset_hours"]))).isoformat(timespec="seconds"),
        as_of=today.isoformat(),
        companies=[dict(code=c["code"], tenant=c["tenant"], cluster=c["cluster"]) for c in companies],
        cash=cash_block,
        leaving=leaving_block,
        incoming=incoming,
        attention=att,
        calendar=calendar,
        patterns=patterns,
        feed_as_at=feed_as_at,
        health=health,
        ma=ma,
        mayears=mayears,
        bep=bep,
        prior_year=prior_pl,
        target=revenue_target(cfg, store, prior_pl, gaps),
        projection=projection,
        gaps=gaps,
        tabs_live=["today", "calendar", "health", "ma", "bep"],
        tabs_next=["accounts_payable", "receivables", "fpa", "reconciliation"],
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--root", default=str(HERE.parent))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    cfg = json.loads(CONFIG_PATH.read_text())
    tz = timezone(timedelta(hours=cfg["as_of_timezone_offset_hours"]))
    today = d(a.as_of) if a.as_of else datetime.now(tz).date()
    if not TEMPLATE_PATH.exists():
        print(f"template missing: {TEMPLATE_PATH}", file=sys.stderr); return 2
    payload = build(root, today)
    out = SITE_DIR
    (out / "data").mkdir(parents=True, exist_ok=True)
    (out / "data" / "fcc.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    (out / "data" / "fcc.js").write_text("window.FCC = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    shutil.copyfile(TEMPLATE_PATH, out / "index.html")

    # a deploy copy with the data inlined: GitHub Pages gets two files at the repo root,
    # no data/ subfolder to fight the upload page with, and the page works from anywhere
    dep = out / "deploy"
    dep.mkdir(exist_ok=True)
    tpl = TEMPLATE_PATH.read_text()
    inline = "<script>window.FCC = " + json.dumps(payload, ensure_ascii=False) + ";</script>"
    tpl = tpl.replace('<script src="data/fcc.js"></script>', inline)
    (dep / "index.html").write_text(tpl)
    cd = root / cfg["cash_dashboard_html"]
    if cd.exists():
        shutil.copyfile(cd, out / "cash-dashboard.html")
        shutil.copyfile(cd, dep / "cash-dashboard.html")
    else:
        print("cash-dashboard.html missing; embed will 404", file=sys.stderr)
    c = payload["cash"]
    print(f"as of {payload['as_of']}: group cash {c['group_total']:,.2f} (snapshot {c['snapshot_date']}, {len([r for r in c['companies'] if r['cash'] is not None])}/{len(c['companies'])} companies) "
          f"| leaving {payload['leaving']['window_days']}d {payload['leaving']['total']:,.0f} | calendar {len(payload['calendar'])} items "
          f"({sum(1 for i in payload['calendar'] if i['status']=='overdue')} overdue) | attention {len(payload['attention'])} | gaps {len(payload['gaps'])}")
    hh = payload.get("health") or {}
    mm = payload.get("ma")
    print("health: " + ", ".join(f"{k} {v}" for k, v in sorted((hh.get('counts') or {}).items())) if hh else "health: none")
    if mm:
        g = mm["group"] or {}
        print(f"MA {mm['latest_label']}: group sales {g.get('sales'):,.0f} net profit {g.get('np'):,.0f} ({g.get('np_pct')}%), "
              f"{len(mm['sites'])} sites, {len(mm['losers'])} loss-making, {len(mm['over_food'])} over the {mm['food_target']}% food target")
    my = payload.get("mayears")
    if my:
        print(f"MA history: {len(my['sites'])} sites, {len(my['months'])} months "
              f"{my['months'][0]} to {my['months'][-1]}, {len(my['counted'])} counted as restaurants"
              + (f", {len(my['overrides'])} workbook figures overridden by the published dashboard"
                 if my['overrides'] else ""))
    bb = payload.get("bep")
    if bb:
        print(f"break-even: {bb['sites_with_capex']} sites, capex {bb['total_capex']:,.0f}, "
              f"recovered {bb['total_recovered']:,.0f} ({bb['percent_recovered']}%), outstanding {bb['outstanding']:,.0f}")
    pr = payload.get("projection") or {}
    if pr:
        print(f"projection to {pr['month_end']} ({pr['days_to_month_end']}d): opening {pr['opening']:,.0f} "
              f"+ in {pr['inflow']:,.0f} - out {pr['outflow']:,.0f} = {pr['closing']:,.0f}")
    tg = payload.get("target") or {}
    if tg:
        print(f"target {tg['target']:,.0f}: {tg['ytd']:,.0f} to {tg['through']} ({tg['pct']}%), "
              f"needs {tg['needed_per_month']:,.0f}/month for {tg['months_left']} months; "
              f"run-rate lands {tg['projection_run_rate']:,.0f}, seasonal {(tg['projection_seasonal'] or 0):,.0f}")
    print(f"wrote {out / 'index.html'} + data/fcc.json")
    print(f"deploy copy: {dep / 'index.html'} ({(dep / 'index.html').stat().st_size/1024:.0f} KB, data inlined) + cash-dashboard.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
