"""Utilities tracker (12-Oct-2026, Michael): gas and electricity per site, from the Xero bank feeds.
Purpose: never get hit again the way M8 was (one gas payment of £37.8k covering 1-Oct-2025 to 6-Sep-2026).
For every site and fuel it shows who we pay, when we last paid, how far the bills go up to, and raises
a flag when a fuel has gone quiet (a catch-up bill is building), when a payment is a back-bill, or
when a payment is far above the site's normal.
Read-only. Register of accounts lives in utilities_register.json (seeded from the feeds, finance fills the rest)."""
from __future__ import annotations
import csv, json, re, statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTER_PATH = HERE / "utilities_register.json"

SUPPLIER_RE = re.compile(r"ecotricity|yu energy|octopus|british gas|scottish gas|jellyfish|yorkshire gas|\bygp\b|totalenergies|"
                         r"smartest ?energy|edf|uc energy|e\.?on\b|\bsse\b|npower|\bovo\b|opus energy|crown gas|haven power|"
                         r"pozitive|valda|drax|engie|corona energy|e e solutions|energy|gas and power|gas & power", re.I)
NOT_ENERGY_RE = re.compile(r"lynas|amazon|nisbets|flow hosp|hygiene|water|council|kobas", re.I)
PERIOD_RE = re.compile(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})\s*(?:to|-|–)\s*(\d{4})[.\-/](\d{2})[.\-/](\d{2})")
MONTH_RE = re.compile(r"(20\d{2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\w*", re.I)
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

QUIET_AMBER, QUIET_RED = 45, 75        # days since last payment for a fuel
BACKBILL_DAYS, BIG = 45, 3000.0         # a bill covering more than 45 days, over £3k, is a back-bill
SPIKE = 2.0                             # a payment more than 2x the site's typical bill


def _fuel(contact: str, desc: str) -> str:
    t = (desc or "") + " " + (contact or "")
    if re.search(r"electric", t, re.I): return "electricity"
    if re.search(r"\bgas\b", desc or "", re.I): return "gas"
    if re.search(r"british gas|scottish gas|crown gas|yorkshire gas|\bygp\b", contact or "", re.I): return "gas"
    if re.search(r"ecotricity|octopus|jellyfish|pozitive|edf|yu energy|smartest|drax|haven|opus", contact or "", re.I): return "electricity"
    return "energy"


def _period(desc: str):
    m = PERIOD_RE.search(desc or "")
    if m:
        try:
            a = date(int(m[1]), int(m[2]), int(m[3])); b = date(int(m[4]), int(m[5]), int(m[6]))
            if b >= a: return a, b
        except ValueError: pass
    m = MONTH_RE.search(desc or "")
    if m:
        y, mo = int(m[1]), MONTHS[m[2][:3].lower()]
        a = date(y, mo, 1); b = date(y + (mo == 12), (mo % 12) + 1, 1)
        return a, b
    return None, None


def _account(contact: str) -> str:
    m = re.search(r"A/C:?\s*([A-Za-z0-9\-]+)", contact or "")
    return m[1] if m else ""


def _supplier(contact: str) -> str:
    return re.sub(r"\s*-?\s*A/C.*$", "", contact or "").strip()


def load_register() -> dict:
    if REGISTER_PATH.exists():
        try: return json.loads(REGISTER_PATH.read_text())
        except Exception: return {}
    return {}


def block(root: Path, cfg: dict, companies: list[dict], today: date, gaps: list) -> dict:
    feed_dir = root / cfg["feed_dir"]
    reg = load_register()
    reg.setdefault("accounts", {})
    sites: list[dict] = []
    flags: list[dict] = []
    months = [f"{y}-{m:02d}" for y in range(today.year, today.year + 1) for m in range(1, today.month + 1)]
    group_ytd = 0.0
    backbill_ytd = 0.0
    biggest = None

    for c in companies:
        code = c["code"]
        fp = feed_dir / c["feed"] if c.get("feed") else None
        if not fp or not fp.exists():
            continue
        with fp.open(newline="") as fh:
            feed = list(csv.DictReader(fh))
        if not feed: continue
        feed_start = min(r["Date"] for r in feed)
        pays = []
        for r in feed:
            if r.get("Direction") != "OUT": continue
            contact, desc, names = r.get("Contact", ""), r.get("InvoiceLineDescriptions", ""), r.get("AccountNames", "")
            is_energy = bool(SUPPLIER_RE.search(contact)) or "445" in (r.get("AccountCodes") or "") or "Light, Power" in names
            if not is_energy or NOT_ENERGY_RE.search(contact): continue
            amt = float(r["Amount"] or 0)
            if amt < 20: continue    # £5 Octopus admin lines etc.
            a, b = _period(desc)
            pays.append(dict(date=r["Date"], amount=round(amt, 2), supplier=_supplier(contact), account=_account(contact),
                             fuel=_fuel(contact, desc), period_from=a.isoformat() if a else None, period_to=b.isoformat() if b else None,
                             period_days=(b - a).days if a and b else None, desc=(desc or "")[:80], ref=r.get("InvoiceNumber") or r.get("Reference") or ""))
        pays.sort(key=lambda p: p["date"])
        by_fuel: dict[str, list] = defaultdict(list)
        for p in pays: by_fuel[p["fuel"]].append(p)
        site_ytd = sum(p["amount"] for p in pays)
        group_ytd += site_ytd
        monthly = defaultdict(float)
        for p in pays: monthly[p["date"][:7]] += p["amount"]
        fuels = []
        site_state = "green"
        rank = {"green": 0, "amber": 1, "red": 2}
        for fuel in ("electricity", "gas", "energy"):
            ps = by_fuel.get(fuel)
            if not ps:
                if fuel == "energy": continue
                # nothing paid for this fuel all year
                st = "amber"
                txt = f"No {fuel} payment seen since the feed starts ({feed_start}). Confirm whether the site has {fuel}; if it does, a catch-up bill is building."
                fuels.append(dict(fuel=fuel, state=st, suppliers=[], last_paid=None, days_since=None, billed_to=None, ytd=0.0, typical=None, text=txt, payments=[]))
                flags.append(dict(level=st, code=code, fuel=fuel, text=txt))
                site_state = max(site_state, st, key=rank.get)
                continue
            last = ps[-1]
            days_since = (today - date.fromisoformat(last["date"])).days
            billed_to = max((p["period_to"] for p in ps if p["period_to"]), default=None)
            normal = [p["amount"] for p in ps if not (p["period_days"] and p["period_days"] > BACKBILL_DAYS)]
            typical = round(statistics.median(normal), 2) if normal else None
            ytd = round(sum(p["amount"] for p in ps), 2)
            suppliers = sorted({p["supplier"] for p in ps})
            st, notes = "green", []
            # 1. back-bills (the M8 case)
            bb: dict[tuple, list] = defaultdict(list)
            for p in ps:
                if p["period_days"] and p["period_days"] > BACKBILL_DAYS and p["amount"] >= BIG:
                    backbill_ytd += p["amount"]
                    bb[(p["supplier"], p["period_from"], p["period_to"], p["period_days"])].append(p)
            for (sup, pf, pt, pd), grp in bb.items():
                tot = sum(p["amount"] for p in grp); last_bb = grp[-1]
                lvl = "red" if (today - date.fromisoformat(last_bb["date"])).days <= 60 else "amber"
                how = f"on {last_bb['date']}" if len(grp) == 1 else f"in {len(grp)} instalments ({grp[0]['date']} to {last_bb['date']})"
                t = f"Back-bill: £{tot:,.0f} to {sup} {how} covered {pd} days ({pf} to {pt})."
                notes.append(t); flags.append(dict(level=lvl, code=code, fuel=fuel, text=t)); st = max(st, lvl, key=rank.get)
            # 2. spikes against the site's typical bill
            if typical and typical > 0:
                for p in ps[-3:]:
                    if p["amount"] >= BIG and p["amount"] > SPIKE * typical and not (p["period_days"] and p["period_days"] > BACKBILL_DAYS):
                        t = f"Spike: £{p['amount']:,.0f} to {p['supplier']} on {p['date']} is {p['amount']/typical:.1f}x the site's typical {fuel} bill (£{typical:,.0f})."
                        notes.append(t); flags.append(dict(level="amber", code=code, fuel=fuel, text=t)); st = max(st, "amber", key=rank.get)
            # 3. gone quiet: a bill is building
            if days_since > QUIET_RED or days_since > QUIET_AMBER:
                lvl = "red" if days_since > QUIET_RED else "amber"
                est = f" Roughly £{typical * days_since / 30:,.0f} is building up." if typical else ""
                t = f"Quiet: no {fuel} payment for {days_since} days (last {last['date']}, {last['supplier']}).{est}"
                notes.append(t); flags.append(dict(level=lvl, code=code, fuel=fuel, text=t)); st = max(st, lvl, key=rank.get)
            # 4. two suppliers billing the same fuel in overlapping periods
            if len(suppliers) > 1:
                spans = [(p["supplier"], p["period_from"], p["period_to"], p["amount"]) for p in ps if p["period_from"]]
                pairs: dict[tuple, dict] = {}
                for i in range(len(spans)):
                    for j in range(i + 1, len(spans)):
                        s1, s2 = spans[i], spans[j]
                        if s1[0] != s2[0] and s1[1] < s2[2] and s2[1] < s1[2]:
                            k = tuple(sorted((s1[0], s2[0])))
                            e = pairs.setdefault(k, dict(lo=min(s1[1], s2[1]), hi=max(s1[2], s2[2]), amt=0.0, n=0))
                            e["lo"] = min(e["lo"], s1[1], s2[1]); e["hi"] = max(e["hi"], s1[2], s2[2]); e["n"] += 1
                for (a1, a2), e in pairs.items():
                    t = f"Two suppliers for {fuel}: {a1} and {a2} both billed periods inside {e['lo']} to {e['hi']} ({e['n']} overlapping bills). Check for double billing."
                    notes.append(t); flags.append(dict(level="amber", code=code, fuel=fuel, text=t)); st = max(st, "amber", key=rank.get)
            for p in ps:
                if biggest is None or p["amount"] > biggest["amount"]:
                    biggest = dict(p, code=code)
            fuels.append(dict(fuel=fuel, state=st, suppliers=suppliers, last_paid=last["date"], days_since=days_since, billed_to=billed_to,
                              ytd=ytd, typical=typical, text=" ".join(notes) if notes else "Paying regularly.", payments=ps[-12:]))
            site_state = max(site_state, st, key=rank.get)
            # seed the register
            for p in ps:
                key = f"{code}|{fuel}|{p['supplier']}"
                reg["accounts"].setdefault(key, dict(site=code, fuel=fuel, supplier=p["supplier"], account=p["account"], payment_method="", contract_end="", monthly_budget=None, notes=""))
                if p["account"] and not reg["accounts"][key].get("account"):
                    reg["accounts"][key]["account"] = p["account"]
        sites.append(dict(code=code, cluster=c.get("cluster"), state=site_state, ytd=round(site_ytd, 2), fuels=fuels,
                          monthly={m: round(monthly.get(m, 0.0), 2) for m in months}, feed_start=feed_start))

    try:
        REGISTER_PATH.write_text(json.dumps(reg, indent=1, ensure_ascii=False))
    except Exception as e:
        gaps.append(dict(company="Group", text=f"Could not write utilities_register.json: {e}"))
    order = {"red": 0, "amber": 1, "green": 2}
    sites.sort(key=lambda s: (order[s["state"]], -s["ytd"]))
    flags.sort(key=lambda f: (order[f["level"]], f["code"]))
    return dict(
        as_at=today.isoformat(), months=months, sites=sites, flags=flags,
        group_ytd=round(group_ytd, 2), backbill_ytd=round(backbill_ytd, 2), biggest=biggest,
        red=sum(1 for s in sites if s["state"] == "red"), amber=sum(1 for s in sites if s["state"] == "amber"),
        register=reg["accounts"],
        rules=dict(quiet_amber_days=QUIET_AMBER, quiet_red_days=QUIET_RED, backbill_days=BACKBILL_DAYS, big=BIG, spike=SPIKE),
        source="Xero bank feeds (daily_bank_*.csv), account 445 Light, Power, Heating and known energy suppliers. Water, waste and £5 admin lines left out.",
    )
