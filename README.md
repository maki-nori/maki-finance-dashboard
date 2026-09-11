# Maki & Ramen — Finance Control Centre

Static finance board for the finance team. Five tabs: Today, Calendar, Company Health,
Management Accounts, Break-even.

Live: https://maki-nori.github.io/maki-finance-dashboard/

| Path | What |
| --- | --- |
| `index.html` | the board, data inlined, works from any host |
| `cash-dashboard.html` | the daily cash view, embedded in the Today tab |
| `src/` | the builders, the page template and the registry |
| `data/` | the stores the builders write |

## Rebuilding

Offline and stdlib only. Nothing here touches Xero.

```
python3 src/extract_ma_history.py      # -> data/ma_history.json
python3 src/extract_analysis_docx.py   # -> data/analysis_<period>.json, data/bep_data.json
python3 src/extract_pl_2025.py         # -> data/pl_2025.json
python3 src/build_fcc.py               # -> the board
```

On the finance Mac all four run from `Build Finance Control Centre.command`.

## Reading the numbers

Every figure carries an as-at date and a source. Missing inputs are named, never filled
with zero. Calendar dates come from each company's bank-feed pattern: `actual` was seen in
the bank, `estimate` is projected, `check` means no recent payment on record. Break-even is
cumulative profit since a site opened against what it cost to build; five older sites have
no build cost on record and sit outside the totals. The 2025 book stops at EBITDA and covers
fewer sites, so only revenue compares like for like against 2026.
