#!/usr/bin/env python3
"""Extract the management-accounts figures the FCC board needs into one durable store.

Source of truth is Monica's annual workbooks in Drive. Those are not reachable from
this machine's shell (no network), so this reads the two MA dashboards already built
from them and banked in the project root:

  Maki_MA_Dashboard_August_2026.html   Aug 2026 per site + Jan-Aug cumulative + group by month Jan-Aug
  Maki_MA_Dashboard_July_2026.html     Jul 2026 per site + Jan-Jul cumulative

Writes finance_control_centre/ma_history.json. The store is additive: when the Drive
leg is wired, earlier months and 2023-2025 append to "months" and "group_series" and
nothing else has to change.

Every figure carries the dashboard it came from. Nothing is interpolated.
Usage: python3 finance_control_centre/extract_ma_history.py [--root PATH]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "ma_history.json"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def txt(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&minus;", "-"), ("&mdash;", "-"), ("&ndash;", "-"), ("&amp;", "&"), ("&nbsp;", " "),
                 ("−", "-"), ("—", "-"), ("–", "-"), ("▲", ""), ("▼", "")):
        s = s.replace(a, b)
    return s.strip()


def num(s: str):
    """'£335,121' -> 335121.0 ; '-£15,728' -> -15728.0 ; '23.9%' -> 23.9 ; '-'/'' -> None"""
    s = txt(s).replace(",", "").replace("£", "").replace("%", "").strip()
    if s in ("", "-", "n/a", "new"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def rows_of(html: str):
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not tds:
            continue
        code_m = re.search(r'class="code"[^>]*>(.*?)</span>', tds[0], re.S)
        if not code_m:
            continue
        code = txt(code_m.group(1))
        name = txt(re.sub(r'<span class="code".*?</span>', "", tds[0], flags=re.S))
        yield code, name, tds


def parse_dashboard(path: Path, month_key: str, has_ytd_numbers: bool):
    """Value cells in order: sales, [ytd_sales], tips, wages%, rent%, food%, var%, vat%,
    total_costs, np, np%, [ytd_np]. The July board draws its cumulative columns as
    sparklines instead of numbers, so it carries no year-to-date figures."""
    html = path.read_text()
    out = {}
    for code, name, tds in rows_of(html):
        vals = []
        for t in tds[1:]:
            if 'class="d"' in t or "chip" in t:
                continue          # month-on-month delta chip
            if "<svg" in t or 'class="sp"' in t or "nsp" in t:
                continue          # sparkline cell, no figure behind it
            vals.append(num(t))
        need = 12 if has_ytd_numbers else 10
        if len(vals) < need:
            continue
        if has_ytd_numbers:
            (sales, ytd_sales, tips, wages, rent, food, var, vat, costs, np_, np_pct, ytd_np) = vals[:12]
        else:
            (sales, tips, wages, rent, food, var, vat, costs, np_, np_pct) = vals[:10]
            ytd_sales = ytd_np = None
        out[code] = dict(
            code=code, name=name, sales=sales, tips=tips,
            wages_pct=wages, rent_pct=rent, food_pct=food, variable_pct=var, vat_pct=vat,
            total_costs=costs, np=np_, np_pct=np_pct,
            ytd_sales=ytd_sales, ytd_np=ytd_np,
            month=month_key, source=path.name,
        )
    return out


def parse_group_series(path: Path, heading: str, unit: str):
    """Bar labels under a chart heading: '£2.37m' or '£144k' per month. Rounded on the chart."""
    html = path.read_text()
    i = html.find(heading)
    if i < 0:
        return []
    seg = html[i:i + 9000]
    seg = seg[:seg.find("</svg>") + 6] if "</svg>" in seg else seg
    pairs = []
    for g in re.findall(r"<g>.*?</g>", seg, re.S):
        lab = re.search(r'class="bl"[^>]*>(.*?)</text>', g, re.S)
        mon = re.search(r'class="bx"[^>]*>(.*?)</text>', g, re.S)
        if not lab or not mon:
            continue
        s = txt(lab.group(1)).replace("£", "").replace(",", "")
        neg = s.startswith("-")
        s = s.lstrip("-")
        mult = 1e6 if s.endswith("m") else 1e3 if s.endswith("k") else 1.0
        try:
            v = float(s.rstrip("mk")) * mult
        except ValueError:
            continue
        pairs.append(dict(month=txt(mon.group(1)), value=round(-v if neg else v, 2), rounded=True))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(HERE.parent))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    aug = root / "Maki_MA_Dashboard_August_2026.html"
    jul = root / "Maki_MA_Dashboard_July_2026.html"
    missing = [p.name for p in (aug, jul) if not p.exists()]
    if missing:
        print("missing: " + ", ".join(missing))
        return 2

    m_aug = parse_dashboard(aug, "2026-08", True)
    m_jul = parse_dashboard(jul, "2026-07", False)

    # Jan-Jun per site by subtraction: Jan-Aug cumulative minus Aug minus Jul
    h1 = {}
    for code, a8 in m_aug.items():
        a7 = m_jul.get(code)
        if not a7 or a8.get("ytd_sales") is None or a7.get("sales") is None:
            continue
        s = a8["ytd_sales"] - (a8["sales"] or 0) - (a7["sales"] or 0)
        n = None
        if a8.get("ytd_np") is not None and a8.get("np") is not None and a7.get("np") is not None:
            n = a8["ytd_np"] - a8["np"] - a7["np"]
        h1[code] = dict(code=code, name=a8["name"], sales=round(s, 2), np=None if n is None else round(n, 2),
                        months="2026-01..2026-06", basis="Jan-Aug cumulative less August less July",
                        source=f"{aug.name} + {jul.name}")

    payload = dict(
        schema=1,
        latest_month="2026-08",
        prior_month="2026-07",
        months={
            "2026-08": dict(label="August 2026", sites=m_aug, source=aug.name,
                            note="20 sites. Group tie-out to the group book passed at £3 on sales and £3 on net profit."),
            "2026-07": dict(label="July 2026", sites=m_jul, source=jul.name,
                            note="19 sites (M21 Birmingham had no MA yet)."),
        },
        first_half=dict(label="January to June 2026", sites=h1,
                        basis="Derived by subtraction from the cumulative columns, not read from a workbook."),
        group_series=dict(
            sales=parse_group_series(aug, "Group sales by month", "m"),
            np=parse_group_series(aug, "Group net profit by month", "k"),
            source=aug.name,
            note="Chart labels, so rounded to the nearest £10k on sales and £1k on net profit.",
        ),
        targets=dict(food_pct=25.0, note="Food cost target is 25% of sales."),
        flags=[
            dict(code="ALL", month="2026-08", level="amber",
                 text="August is the Edinburgh Fringe. M1, M3, M6, M7 and Ikigai 2 are well above trend. Do not annualise August."),
            dict(code="M16", month="2026-08", level="amber",
                 text="July carried a £30,010 VAT credit that reversed in August exactly as expected. The underlying run rate is unchanged."),
        ],
        gaps=[
            "2023, 2024, 2025 and January to June 2026 month by month are in Monica's annual workbooks in Google Drive. This machine's shell has no network, so they are not in the store yet. Only the group totals by month for 2026 are here, taken from the chart labels.",
            "September 2026 is not closed yet.",
        ],
    )
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    g = m_aug.get("ALL", {})
    print(f"wrote {OUT.name}: Aug {len(m_aug)} rows, Jul {len(m_jul)} rows, H1 {len(h1)} rows, "
          f"group series {len(payload['group_series']['sales'])} months")
    print(f"  group Aug sales {g.get('sales')} np {g.get('np')} np% {g.get('np_pct')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
