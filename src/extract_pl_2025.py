#!/usr/bin/env python3
"""Pull the 2025 group profit and loss into the board so 2026 has something to sit against.

Stdlib only (an .xlsx is a zip of XML), so it runs in the VM with plain python3.
Reads   Maki_Ramen_Consolidated_PL_2025.xlsx  (tabs "Group P&L 2025", "Summary by Site")
Writes  finance_control_centre/pl_2025.json

⚠ The 2025 book stops at EBITDA. The 2026 management accounts go further and deduct tax,
so only REVENUE is compared like for like on the board. The 2025 book also covers sixteen
sites against 2026's twenty-one, which is recorded here and shown wherever the two years
are put side by side.

Usage: python3 finance_control_centre/extract_pl_2025.py [--root PATH]
"""
from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def sheet_rows(z: zipfile.ZipFile, name: str):
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in ss.findall(NS + "si")]
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    target = None
    for s in wb.iter(NS + "sheet"):
        if s.get("name") == name:
            target = "xl/" + rels[s.get(RNS + "id")].lstrip("/")
    if not target:
        return []
    sh = ET.fromstring(z.read(target))

    def val(c):
        v = c.find(NS + "v")
        if v is None:
            return ""
        if c.get("t") == "s":
            try:
                return shared[int(v.text)]
            except (ValueError, IndexError):
                return ""
        return v.text
    return [[val(c) for c in row.findall(NS + "c")] for row in sh.iter(NS + "row")]


def num(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(HERE.parent))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    fp = root / "Maki_Ramen_Consolidated_PL_2025.xlsx"
    if not fp.exists():
        print("Maki_Ramen_Consolidated_PL_2025.xlsx not in the project root")
        return 2

    with zipfile.ZipFile(fp) as z:
        rows = sheet_rows(z, "Group P&L 2025")
        site_rows = sheet_rows(z, "Summary by Site")

    want = {"revenue": "revenue", "wages": "wages", "rent": "rent", "food costs": "food",
            "variable costs": "variable", "total operating costs": "total_costs", "ebitda": "ebitda"}
    lines: dict[str, list] = {}
    for r in rows:
        if not r:
            continue
        key = (r[0] or "").strip().lower()
        if key in want and len(r) >= 13:
            vals = [num(x) for x in r[1:13]]
            lines[want[key]] = [dict(month=MONTHS[i], value=v) for i, v in enumerate(vals) if v is not None]

    sites = {}
    if site_rows:
        head = [(c or "").strip().lower() for c in site_rows[0]]
        for r in site_rows[1:]:
            if not r or not (r[0] or "").strip():
                continue
            code = (r[0] or "").strip()
            rec = {}
            for i, c in enumerate(head[1:], start=1):
                if i < len(r) and c:
                    rec[c] = num(r[i])
            if rec:
                sites[code] = rec

    rev = lines.get("revenue", [])
    payload = dict(
        schema=1, year=2025, source=fp.name,
        lines=lines,
        total_revenue=round(sum(x["value"] for x in rev), 2) if rev else None,
        sites=sites,
        caveats=[
            "The 2025 book stops at EBITDA. The 2026 management accounts deduct tax as well, so only revenue is compared like for like.",
            "2025 covers the sites trading that year. 2026 has more of them, so part of any increase is simply new sites rather than the same shops doing better.",
        ],
    )
    (HERE / "pl_2025.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print(f"wrote pl_2025.json: {len(lines)} lines, revenue {payload['total_revenue']:,.0f}, "
          f"{len(rev)} months, {len(sites)} site rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
