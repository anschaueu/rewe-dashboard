# REWE Dashboard — Full Picture & Update Guide

This document explains **everything** about this project so the dashboard can be updated
directly next time: what the files are, how `report.html` is structured, every embedded data
structure and its formula, and the exact steps to ingest new receipts.

> TL;DR to update: put new PDFs in a folder and run
> `python3 tools/update_dashboard.py --ebons "<folder>" --online <invoice.pdf> ...`
> then commit. The script ingests only receipts **after the latest one already present**,
> recomputes every chart/stat, and rewrites `report.html`.

---

## 1. What this project is

A single-page, self-contained personal analytics dashboard for REWE (German supermarket)
purchases, deployed as static files (Netlify / GitHub Pages).

| File | Purpose |
|------|---------|
| `report.html` | **The whole app.** ~800 KB single file: HTML + CSS + Chart.js charts + all data embedded as JS literals. This is what gets updated. |
| `index.html` / `login.html` | Password gate (SHA-256 cookie `rAuth`) that redirects to `report.html`. |
| `tools/update_dashboard.py` | **The generator/updater** (added so updates are repeatable). Parses new PDFs, merges them, recomputes everything. |
| `_headers`, `_redirects`, `robots.txt`, `deploy.sh`, `.netlify/`, `.github/workflows/pages.yml` | Hosting/deploy config. |

External runtime dependencies (loaded from CDN by `report.html`): **Chart.js 4.4.7** and
**Firebase 10.12.0** (used for cross-device sync of the shopping cart + budget target). These
need internet at view-time; they are unrelated to data updates.

---

## 2. How `report.html` works

The file has three parts:

1. **HTML body + CSS** (top): the tabbed layout (Overview, Shopping List, Products, Stores,
   Receipts, Report, Trends, Basket Index, KPIs, Export). Some headline KPIs are written as
   static text but **overridden at load** by the i18n dictionaries (see §4).
2. **Data block** (`const P=[...]`, `const rL=[...]`, …): ~25 pre-computed JS literals.
3. **Render JS** (bottom): builds charts and lists from the data block. A `[data-i18n]` pass
   (PT/EN/DE) localizes labels.

Two kinds of data structures:

- **Pre-computed** (must be regenerated on update): `P, rL, mL, mV, sM, cL, cV, sts, tS, sC,
  cpd, shD, bL, bV, bIdx, gCPI, trd, pAlerts, volP, discD, wVi, wAv, pU, pSt, pDn` + the HTML KPIs.
- **Computed live in the browser** from `P`/`rL` (no action needed): the products table,
  product price chart, smart-cart search, inactivity/price alerts (`P.var`, `P.ds`), receipt
  timeline, and the category-evolution chart (sums `P.h[].p` per month using `sM` as the axis).

---

## 3. The data model

### `rL` — receipts (the raw transaction list; source of truth for totals)
```json
{"date":"01.06.2026","date_sort":"2026-06-01","total":199.13,
 "store":"REWE Q6","store_id":"5626","num_items":43,"products":["RFW DUROC SCHIN.",...]}
```
`total` = the receipt **SUMME** (what was paid, incl. deposit/discounts). `products` lists only
real product lines (deposit/fee/discount lines excluded). `num_items` = `len(products)`.

### `P` — product catalogue (one entry per distinct product name)
```json
{"n":"PROTEINPUDD. SCH","cat":"Laticínios & Ovos","tb":77,"tq":401,"lp":0.89,
 "mn":0.89,"mx":0.99,"var":0.0,"tr":"stable","ld":"2026-06-25","ds":1,"kg":false,
 "ts":380.73,"af":3.1,"sb":{"REWE Q6":58,...},
 "h":[{"date":"02.08.2024","ds":"2024-08-02","p":0.99,"s":"REWE Q6"},...],
 "desc":"","iu":"<image url>","fn":"<full name>","pl":"<product link>","tg":[]}
```
| field | meaning | how computed on update |
|-------|---------|------------------------|
| `n` | short name (the eBon line text) | identity key |
| `cat` | category (PT) | kept; new products guessed (see §6) |
| `h` | purchase history; `p`=unit price, `s`=store, `pkg`=€/kg for weighed items | append one entry per purchase event |
| `tb` | times bought | `len(h)` |
| `tq` | total quantity (units) | `+= qty` (qty not stored per event, so kept cumulative) |
| `ts` | total spent | `+= line_total` (kept cumulative) |
| `lp/mn/mx` | last / min / max price | from `h` (uses `pkg` for kg items) |
| `ld` | last purchase date (`YYYY-MM-DD`) | max date in `h` |
| `ds` | days since last purchase | `REF_DATE − ld` where **REF_DATE = latest receipt date** |
| `af` | avg gap (days) between purchase dates | mean of consecutive-date gaps |
| `sb` | per-store purchase counts | from `h` |
| `var` | price trend % = `(lp − avg(prices)) / avg(prices) × 100` | recomputed |
| `tr` | `up` if `var>2`, `down` if `var<-2`, else `stable` | recomputed |
| `iu,fn,pl,desc,tg` | enrichment (image, full name, shop link, …) from REWE catalogue | **preserved**, cannot be regenerated from PDFs |

### Other structures (all recomputed from `P`+`rL`)
- `mL` / `mV` / `sM` — months with data: German labels / receipt-total sum / `YYYY-MM` keys (parallel).
- `cL` / `cV` — categories sorted by spend (`Σ P.ts` per `cat`) — labels / values.
- `sts` — per store: `{id,name,receipt_count,total_spent,avg_ticket}` (sorted by spend).
- `tS` — top 10 products by `ts` (full `P` objects).
- `sC` — Q6-vs-S6 comparison per product: `{q6_avg,q6_count,s6_avg,s6_count,exclusive_to,cheaper_at,diff_pct}`.
- `cpd` — per category (products with `tb≥2`): `{up,dn,st,ct,av}` where `av`=avg `|var|`; top 14 by `av`.
- `volP` — top 10 volatile (`tb≥3`) by amplitude `vol=(mx−mn)/mn×100`.
- `discD` — new products per month `{month,count,label}` (first-purchase month).
- `shD` — repurchase suggestions (`tb≥3`, regular cadence): `od=ds−af`, `urg` overdue/soon; top 40.
- `pAlerts` — products on the **most recent receipt** whose price moved >5% vs the prior purchase.
- `bL`/`bV`/`bIdx` — fixed-basket index over the last 12 months: basket = top-25 most-bought
  non-kg products; `bV`= sum of each product's latest-known unit price each month; `bIdx`=`bV/bV[0]×100`.
- `gCPI` — official German food CPI (Trends tab). **External data, not available offline** — it is
  extended from the prior series by its average monthly step. Replace with real Destatis values if wanted.
- `trd` — `{price_up, price_down, reconsider, cat_trends, personal_inflation, german_inflation,
  last_3_avg, overall_avg, ticket_diff}`.
- `wVi`/`wAv` — per weekday (Mon→Sun) visit count / avg ticket.
- `pU`/`pSt`/`pDn` — KPI bar: count of purchase events priced above / at / below each product's mean.
- `xD` — only `budget_target` is used by the app (kept = 500); other keys are dead data, left as-is.

---

## 4. Headline KPIs in the HTML body

These are updated by the script. The **i18n dictionaries override the static text at load**, so
the dict entries are the ones that actually show:
- `header_sub` (4 places): `DD.MM.YYYY - DD.MM.YYYY · N compras · N produtos · N meses`
- `items_count` (3 dicts): `"N itens/items/Artikel"`
- KPI cards (values live in non-`data-i18n` `.vl` divs, so they are NOT overridden): Total Gasto,
  Média Mensal (`total / month-span`), Ticket Médio (`total / receipts`), Compras, Produtos,
  Últimas 3 Compras, vs Média Geral.
- Written report period header in `rpTexts` (PT/EN/DE).

`meses`/month-span = **calendar months inclusive** between first and last receipt (e.g. Jul 2024 →
Jun 2026 = 24), which differs from the count of months that actually have receipts (`len(mL)`).

---

## 5. How to update (next time)

```bash
pip install pymupdf            # one-time; the parser needs PyMuPDF (fitz)

python3 tools/update_dashboard.py \
    --report report.html \
    --ebons  "/path/to/Deine Rewe eBons"            # folder of "Dein REWE eBon vom DD.MM.YYYY.pdf"
    --online "/path/to/invoiceA.pdf" "/path/to/invoiceB.pdf"   # optional online-order invoices

# preview first:  add --dry-run
```
- It auto-detects the cutoff = latest `date_sort` in `rL` and ingests **only newer** receipts, so
  you can safely point it at the full eBons folder every time.
- Each receipt is validated: the sum of parsed line items must equal the printed `SUMME`/total,
  otherwise it is skipped with a warning.
- After it writes `report.html`, commit and push.

### Receipt formats handled
- **In-store eBon**: product line shows the *line total* + tax letter (A/B); an optional
  `N Stk x unit` (quantity) or `w kg x €/kg` (weighed) line follows. `Markt:NNNN` → store.
  Multi-page receipts are concatenated. `PFAND`/`LEERGUT` (deposit) and `Frischerabatt`/coupon
  (discount) lines are excluded from products but remain in the receipt total.
- **Online order invoice** (delivery): rows of `name / Menge / MwSt / Einzelpreis / Summe`.
  Mapped to store **REWE Lieferservice** (`store_id 0000`). Weighed rows (`592g`, `€/kg`) become
  kg products. `Pfandtasche`/`Liefergebühr` excluded from products. Items are matched to existing
  products by full name (`fn`); unmatched ones become new products.

---

## 6. Decisions & conventions
- **Cutoff**: only transactions strictly after the last logged receipt are added. The provided
  online invoice dated **11.08.2025** was **skipped** (its date is already covered); only the
  **03.06.2026** online order was added. To add a retroactive invoice, ingest it deliberately and
  re-verify monthly totals (use `--since` carefully to avoid re-adding eBons).
- **REF_DATE for "days since"** = the latest receipt date (deterministic, not wall-clock).
- **New-product category** is keyword-guessed (`CAT_RULES` in the script), defaulting to `Outros`.
- **Numbers**: prices rounded to 2 dp, percentages to 1 dp. JSON written compact, UTF-8 (`ä/ü/ç` as
  real characters — this fixes the old `\uXXXX`-in-category bug).

---

## 7. Bugs fixed in this regeneration (were caused by earlier hand-edits)
1. **Frozen aggregates** — monthly/category/stores/trends/inflation had been stuck at ~April 2026
   while only `P` and `rL` were hand-appended. E.g. May 2026 monthly showed €172.73 but real spend
   was €505.61. All aggregates are now recomputed.
2. **Corrupted `bV`** — 8 receipt objects had been accidentally appended to the basket-index array
   (mixed numbers + receipts). `bV` is now a clean numeric series.
3. **Duplicate categories** — `cL` had both `Laticínios & Ovos` and a mis-encoded
   `Laticínios & Ovos` (and similar). Categories are now decoded and de-duplicated.
4. **Stale `sts`/`sM`** — REWE Freiburg was missing from the store list; `sM` was one month short of
   `mL`. Both fixed.

---

## 8. Known limitations / data-quality notes
- **`gCPI` (official inflation)** is approximated offline — substitute real Destatis food-CPI if precision matters.
- **Online product naming**: online full names rarely match the short eBon names exactly. Confident
  matches reuse the existing product (e.g. *High Protein Pudding* → `PROTEINPUDD. SCH`); the rest
  become new entries with a derived short name (this created one `NUTELLA2` duplicate of `NUTELLA`,
  and the weighed *Hähnchenbrustfilets* appears as repeated `WILHELM BRANDENB` lines). Harmless;
  rename in the data if desired.
- **`Outros`** holds products the keyword guesser couldn't classify — extend `CAT_RULES` to improve.
- **`rpTexts`** (the written Report tab) only has its period header refreshed; the narrative prose is
  not regenerated and may cite older figures.
- **`P.ts`/`P.tq`** are cumulative (per-event quantity isn't stored in `h`), so they can't be
  rebuilt from scratch — the updater only ever *adds* to them.
