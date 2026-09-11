#!/usr/bin/env python3
"""Read Monica's monthly Financial Analysis .docx into the board's data stores.

Stdlib only (a .docx is a zip of XML), so it runs in the VM with plain python3 and
needs no packages. Run it each month after the new analysis lands in the project root.

Reads   Financial Analysis <Mon> <Year>.docx  (newest matching file wins)
Writes  finance_control_centre/analysis_<YYYY-MM>.json
        finance_control_centre/bep_data.json      break-even by site, the board's tab 5

What it takes out:
  - per site: total fit-out cost or capex, cumulative management-accounts profit to
    date, Monica's own break-even percentage, and the trading start date
  - per site: the notes bullets Monica wrote (missed payments, large payments, noted)
  - the final group table: revenue, rent, wage, cost of sales, variable, tax, net profit
    in pounds for every site
  - the per-site comparison tables, including the August 2025 column where she has it

Nothing is invented. A field she left blank stays null and the board says so.
Usage: python3 finance_control_centre/extract_analysis_docx.py [--root PATH] [--file NAME]
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Monica's headings -> the board's company codes
SITE_HEADINGS = [
    (r"^m1too", "M1"), (r"^fountainbridge", "M3"), (r"^ikigai south bridge", "IKI 2"),
    (r"^maki 1hw", "M6"), (r"^sjq", "M7"), (r"^renfield good food", "M8"),
    (r"^maki manchester", "M9"), (r"^maki leeds", "M10"), (r"^maki leicester", "M11"),
    (r"^maki newcastle", "M12"), (r"^maki aberdeen", "M13"), (r"^maki yorkshire", "M14"),
    (r"^maki metro", "M15"), (r"^maki nottingham", "M16"), (r"^maki lakeside", "M17"),
    (r"^maki soho", "M18"), (r"^maki nori", "NORI"), (r"^maki shoreditch", "M19"),
    (r"^maki southampton", "M20"), (r"^maki birmingham", "M21"),
]
# the group table's row labels -> codes
TABLE_NAMES = {
    "maki 1": "M1", "maki 3": "M3", "maki 6": "M6", "maki 7": "M7", "maki 8": "M8",
    "maki 9": "M9", "maki 10": "M10", "maki 11": "M11", "maki 12": "M12", "maki 13": "M13",
    "maki 14": "M14", "maki 15": "M15", "maki 16": "M16", "maki 17": "M17", "maki 18": "M18",
    "maki 19": "M19", "maki 20": "M20", "maki 21": "M21", "maki nori": "NORI", "ikigai 2": "IKI 2",
}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def money(s: str):
    """'£ 886,922' / '- £ 168,644' / '£886922' -> float, or None."""
    if s is None:
        return None
    s = s.replace("−", "-").replace("–", "-").replace("—", "-")
    neg = "-" in s.split("£")[0] if "£" in s else s.strip().startswith("-")
    m = re.search(r"£\s*([\d,]+(?:\.\d+)?)", s) or re.search(r"(-?[\d,]+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def pct(s: str):
    m = re.search(r"(-?[\d.]+)\s*%", s or "")
    return float(m.group(1)) if m else None


def parse_start(s: str):
    """'21/08/2026' -> ('2026-08-21','day'); 'July 2025' -> ('2025-07-01','month');
    '2023' -> ('2023-01-01','year'). The precision matters: a bare year must not be
    turned into a months-trading count."""
    s = (s or "").strip()
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{2,4})", s)
    if m:
        dd, mm, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yy += 2000 if yy < 100 else 0
        try:
            return date(yy, mm, dd).isoformat(), "day"
        except ValueError:
            return None, None
    m = re.search(r"([A-Za-z]{3,})\s+(\d{4})", s)
    if m and m.group(1)[:3].lower() in MONTHS:
        return date(int(m.group(2)), MONTHS[m.group(1)[:3].lower()], 1).isoformat(), "month"
    m = re.search(r"\b(20\d\d)\b", s)
    return (f"{m.group(1)}-01-01", "year") if m else (None, None)


def read_docx(path: Path):
    """Return (paragraph texts in order, list of tables as list-of-rows-of-cells)."""
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{NS}body")

    def text_of(el):
        return "".join(t.text or "" for t in el.iter(f"{NS}t")).strip()

    paras, tables = [], []
    for child in body:
        if child.tag == f"{NS}p":
            t = text_of(child)
            if t:
                paras.append(t)
        elif child.tag == f"{NS}tbl":
            rows = []
            for tr in child.findall(f"{NS}tr"):
                rows.append([text_of(tc) for tc in tr.findall(f"{NS}tc")])
            tables.append(rows)
    return paras, tables


def site_of(line: str):
    low = line.strip().lower()
    for pat, code in SITE_HEADINGS:
        if re.match(pat, low) and ("note" in low or low.endswith(code.lower())):
            return code
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(HERE.parent))
    ap.add_argument("--file", default=None)
    a = ap.parse_args()
    root = Path(a.root).resolve()
    if a.file:
        docs = [root / a.file]
    else:
        docs = sorted(root.glob("Financial Analysis *.docx"), key=lambda p: p.stat().st_mtime)
    docs = [p for p in docs if p.exists()]
    if not docs:
        print("no 'Financial Analysis *.docx' in the project root")
        return 2
    doc = docs[-1]

    paras, tables = read_docx(doc)
    title = paras[0] if paras else doc.stem
    mm = re.search(r"([A-Za-z]{3,})\s*(\d{4})", title) or re.search(r"([A-Za-z]{3,})\s*(\d{4})", doc.stem)
    period = f"{mm.group(2)}-{MONTHS[mm.group(1)[:3].lower()]:02d}" if mm and mm.group(1)[:3].lower() in MONTHS else None

    # ---- walk the paragraphs, attaching fields to the site heading above them ----
    sites: dict[str, dict] = {}
    cur = None
    closing: list[str] = []
    in_closing = False
    for line in paras:
        code = site_of(line)
        if code:
            cur, in_closing = code, False
            sites.setdefault(code, dict(code=code, notes=[], capex=None, capex_label=None,
                                        profit_to_date=None, bep_pct=None, start=None, start_precision=None, recovered_note=None))
            continue
        low = line.lower()
        if low.startswith("summary") or low.startswith("closing notes"):
            in_closing, cur = True, None
            continue
        if in_closing:
            closing.append(line)
            continue
        if not cur:
            continue
        s = sites[cur]
        # one line can carry both figures: "Total fit out cost: £886922 Current MA profit: £728,661"
        has_capex = "fit out cost" in low or "fit-out cost" in low or "total capex" in low
        has_profit = "ma profit" in low
        if has_capex or has_profit:
            cap_part, prof_part = line, line
            m = re.search(r"(current\s+)?ma\s+profit", line, re.I)
            if m and has_capex and has_profit:
                cap_part, prof_part = line[:m.start()], line[m.start():]
            if has_capex:
                s["capex"] = money(cap_part)
                s["capex_label"] = "Total capex" if "capex" in low else "Total fit-out cost"
                if "recover" in low:
                    s["recovered_note"] = line.strip()
            if has_profit:
                s["profit_to_date"] = money(prof_part)
        elif "bep" in low and "%" in line:
            s["bep_pct"] = pct(line)
        elif "start da" in low or "trading start" in low:
            s["start"], s["start_precision"] = parse_start(line)
        elif low.startswith(("missed payment", "variable payment", "large payment", "note", "link to")):
            if not re.search(r"n/?a\s*$", low) and not low.rstrip(": ").endswith(("payments", "note", "noted")):
                s["notes"].append(line)
        else:
            s["notes"].append(line)

    # ---- the group table: pounds per line per site ----------------------------
    group_table = {}
    group_total = None
    for rows in tables:
        head = " ".join(rows[0]).lower() if rows else ""
        if "revenue" in head and "net profit" in head and "cost of sales" in head:
            cols = [c.strip().lower() for c in rows[0]]
            for r in rows[1:]:
                name = r[0].strip().lower()
                vals = dict(zip(cols[1:], r[1:]))
                rec = dict(revenue=money(vals.get("revenue")), rent=money(vals.get("rent")),
                           wage=money(vals.get("wage")), cost_of_sales=money(vals.get("cost of sales")),
                           variable=money(vals.get("variable costs")), tax=money(vals.get("tax")),
                           np=money(vals.get("net profit £")), np_pct=pct(vals.get("net profit %", "")))
                if name in TABLE_NAMES:
                    group_table[TABLE_NAMES[name]] = rec
                elif name.startswith("total"):
                    group_total = rec
            break

    # ---- the per-site comparison tables, for the prior-year column -------------
    compare = {}
    for rows in tables:
        if not rows or len(rows) < 3:
            continue
        code_cell = rows[1][0].strip().lower() if len(rows) > 1 and rows[1] else ""
        code = {"iki 2": "IKI 2", "m. nori": "NORI"}.get(code_cell, code_cell.upper())
        if not re.fullmatch(r"M\d{1,2}|IKI 2|NORI", code):
            continue
        header = [c.strip() for c in rows[0]]
        ly_idx = [i for i, c in enumerate(header) if re.search(r"[A-Za-z]{3}-2\d", c) and "26" not in c]
        rec = {}
        for r in rows[2:]:
            line = r[0].strip().lower()
            if line in ("sales", "wages", "food") and ly_idx:
                rec[line] = money(r[ly_idx[0]]) if ly_idx[0] < len(r) else None
        if rec:
            compare[code] = dict(prior_year=rec, prior_year_label=header[ly_idx[0]] if ly_idx else None)

    # ---- write the two stores --------------------------------------------------
    analysis = dict(schema=1, period=period, title=title, source=doc.name,
                    sites=sites, group_table=group_table, group_total=group_total,
                    prior_year=compare, closing_notes=closing)
    (HERE / f"analysis_{period or 'latest'}.json").write_text(json.dumps(analysis, indent=1, ensure_ascii=False))

    # Monica occasionally types a minus in front of a profit. The management-accounts
    # store settles it: where the year-to-date net profit agrees in size but not in sign,
    # the MA sign wins and the correction is recorded on the row.
    ma_ytd = {}
    ma_path = HERE / "ma_history.json"
    if ma_path.exists():
        try:
            mh = json.loads(ma_path.read_text())
            al = mh.get("ma_code_aliases") or {"M5": "IKI 2", "Nori": "NORI"}
            latest = mh.get("latest_month")
            for k, v in (mh.get("months", {}).get(latest, {}).get("sites", {}) or {}).items():
                if k != "ALL" and v.get("ytd_np") is not None:
                    ma_ytd[al.get(k, k)] = v["ytd_np"]
        except Exception:
            pass

    bep_rows = []
    for code, s in sites.items():
        capex, prof, bep = s["capex"], s["profit_to_date"], s["bep_pct"]
        sign_fixed = None
        y = ma_ytd.get(code)
        if prof is not None and y is not None and prof < 0 < y and abs(abs(prof) - y) < max(50.0, abs(y) * 0.02):
            sign_fixed = f"The analysis types this as a loss of {abs(prof):,.0f}, but the {latest} management account has the same figure as a profit. Read as a profit."
            prof = abs(prof)
        # Monica writes the percentage as size over capex, so a loss-making site can show a
        # positive percentage. Recompute from her two figures and keep hers alongside.
        derived = None if not capex else round((prof or 0) / capex * 100, 2)
        # her sign is unreliable where the percentage and the profit disagree
        sign_conflict = (prof is not None and bep is not None and capex and
                         abs(abs(derived) - bep) < 0.6 and derived < 0 < bep)
        bep_rows.append(dict(
            code=code, capex=capex, capex_label=s["capex_label"], profit_to_date=prof,
            bep_pct_monica=bep, bep_pct=derived, start=s["start"], start_precision=s["start_precision"],
            recovered_note=s["recovered_note"], sign_conflict=sign_conflict,
            sign_corrected=sign_fixed, notes=s["notes"],
            fully_recovered=bool(s["recovered_note"] and "recover" in s["recovered_note"].lower()),
        ))
    bep_rows.sort(key=lambda r: (r["bep_pct"] is None, -(r["bep_pct"] or -1e9)))
    known = [r for r in bep_rows if r["capex"]]
    bep = dict(
        schema=1, period=period, source=doc.name,
        as_at=date.today().isoformat(),
        rows=bep_rows,
        total_capex=round(sum(r["capex"] for r in known), 2),
        total_recovered=round(sum(r["profit_to_date"] or 0 for r in known), 2),
        sites_with_capex=len(known),
        sites_without_capex=[r["code"] for r in bep_rows if not r["capex"]],
        note=("Break-even is the cumulative management-accounts profit a site has made since it opened, "
              "against what it cost to build. Both figures are Monica's, from the monthly analysis."),
    )
    bep["percent_recovered"] = round(bep["total_recovered"] / bep["total_capex"] * 100, 2) if bep["total_capex"] else None
    bep["outstanding"] = round(bep["total_capex"] - bep["total_recovered"], 2)
    (HERE / "bep_data.json").write_text(json.dumps(bep, indent=1, ensure_ascii=False))

    print(f"read {doc.name} (period {period}): {len(sites)} sites, group table {len(group_table)} rows, "
          f"prior-year column for {len(compare)}")
    print(f"break-even: capex {bep['total_capex']:,.0f} across {bep['sites_with_capex']} sites, "
          f"recovered {bep['total_recovered']:,.0f} ({bep['percent_recovered']}%), "
          f"outstanding {bep['outstanding']:,.0f}; no capex for {bep['sites_without_capex']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
