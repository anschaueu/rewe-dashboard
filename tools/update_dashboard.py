#!/usr/bin/env python3
"""
REWE Dashboard updater / regenerator.

Parses new REWE eBon PDFs (in-store receipts) and REWE online-order invoice PDFs,
merges them into the data embedded in ``report.html`` (the receipt list ``rL`` and the
product catalogue ``P``) and then recomputes every derived chart/stat from that data so
the whole dashboard stays internally consistent.

Why this exists
---------------
``report.html`` is a single self-contained file with ~25 pre-computed JS data structures.
There was never a generator in the repo, so earlier updates edited ``P`` and ``rL`` by hand
and left every aggregate (monthly spend, categories, stores, trends, inflation, ...) frozen
at ~April 2026 (and even corrupted ``bV``). This script makes updates deterministic and
complete: drop new PDFs in a folder, run it, commit.

Usage
-----
    python3 tools/update_dashboard.py \
        --report report.html \
        --ebons  "/path/to/Deine Rewe eBons" \
        --online "/path/to/online_invoice1.pdf" "/path/to/online_invoice2.pdf"

By default it only ingests receipts dated AFTER the latest receipt already present
(``--since`` overrides). Run with ``--dry-run`` to preview without writing.

See FULL_PICTURE.md for the data model, every formula and all design decisions.
"""
import argparse, datetime, json, os, re, sys

# ----------------------------------------------------------------------------- helpers
MONTHS_DE = ['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez']
MONTHS_EN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

STORE_NAMES = {'5626':'REWE Q6','5526':'REWE S6','0250':'REWE Lindenhof','5693':'REWE City N1',
               '5455':'REWE 5455','2620':'REWE Frankfurt Airport','6009':'REWE Kurfürstenstr.',
               '5899':'REWE Berlin','7203':'REWE Freiburg','0000':'REWE Lieferservice'}

def de_num(s):
    return float(s.replace('.', '').replace(',', '.'))

def D(ds):
    return datetime.date(*map(int, ds.split('-')))

def decode_escapes(s):
    """Fix the legacy bug where some category strings carry a literal ``\\uXXXX``."""
    if not isinstance(s, str) or '\\u' not in s:
        return s
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)

# ---------------------------------------------------------------- report.html (de)serialise
def extract(src, name):
    """Return the literal text of ``name=<array|object>`` using balanced-bracket scan."""
    for m in re.finditer(r'(?<![\w$])' + re.escape(name) + r'\s*=\s*', src):
        i = m.end()
        if i >= len(src) or src[i] not in '[{':
            continue
        depth = 0; instr = False; esc = False; q = ''
        j = i
        while j < len(src):
            c = src[j]
            if instr:
                if esc: esc = False
                elif c == '\\': esc = True
                elif c == q: instr = False
            else:
                if c in '"\'': instr = True; q = c
                elif c in '[{': depth += 1
                elif c in ']}':
                    depth -= 1
                    if depth == 0:
                        return i, j + 1, src[i:j + 1]
            j += 1
    raise KeyError(name)

def load(src, name):
    return json.loads(extract(src, name)[2])

def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

def replace_literal(src, name, obj):
    i, j, _ = extract(src, name)
    return src[:i] + dump(obj) + src[j:]

def replace_scalar(src, name, value):
    return re.sub(r'(?<![\w$])' + re.escape(name) + r'=\d+(?:\.\d+)?', f'{name}={value}', src, count=1)

# ----------------------------------------------------------------------------- PDF parsing
def _pdf_lines(path):
    import fitz
    doc = fitz.open(path)
    return "\n".join(doc[i].get_text() for i in range(doc.page_count)).split('\n')

def parse_ebon(path):
    """Parse one in-store eBon PDF -> dict(date, date_sort, store_id, store, items, summe).

    Items carry kind in {'product','pfand','discount'}. Product line shows the LINE TOTAL
    plus a tax letter; an optional 'N Stk x unit' or 'w kg x EUR/kg' line follows.
    """
    lines = _pdf_lines(path)
    m = re.search(r'vom (\d{2})\.(\d{2})\.(\d{4})', os.path.basename(path))
    date = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    date_sort = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    store_id = None
    for ln in lines:
        mm = re.search(r'Markt:\s*(\d+)', ln)
        if mm: store_id = mm.group(1); break
    if not store_id:
        for ln in lines[:8]:
            if 'Q6' in ln: store_id = '5626'; break
    start = 0
    for k, ln in enumerate(lines):
        if ln.rstrip().endswith('EUR') and not any(x in ln for x in ('SUMME', 'Geg.', 'Gesamt', 'Netto', 'Guthaben')):
            start = k + 1; break
    items = []; i = start; summe = None
    while i < len(lines):
        ln = lines[i]
        if re.search(r'-{5,}', ln) or re.search(r'\bSUMME\b', ln):
            break
        raw = re.sub(r'\s*\*\s*$', '', ln.rstrip())
        mm = re.match(r'^(.*\S)\s+(-?\d{1,3},\d{2})\s+([AB])$', raw)
        if mm:
            name = re.sub(r'\s+X\d+$', '', mm.group(1).strip()).strip()
            line_total = de_num(mm.group(2)); tax = mm.group(3)
            qty = 1; unit = line_total; pkg = None
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                mq = re.match(r'^\s*(\d+)\s*Stk\s*x\s*([\d.,]+)\s*$', nxt)
                mw = re.match(r'^\s*([\d.,]+)\s*kg\s*x\s*([\d.,]+)', nxt)
                if mq: qty = int(mq.group(1)); unit = de_num(mq.group(2)); i += 1
                elif mw: pkg = de_num(mw.group(2)); unit = line_total; i += 1
            up = name.upper()
            kind = ('pfand' if up.startswith(('PFAND', 'LEERGUT'))
                    else 'discount' if ('RABATT' in up or 'COUPON' in up or line_total < 0)
                    else 'product')
            items.append(dict(name=name, line_total=round(line_total, 2), qty=qty,
                              unit=round(unit, 4), tax=tax, pkg=pkg, kind=kind))
        i += 1
    for ln in lines:
        mm = re.search(r'SUMME\s+EUR\s+(-?\d{1,4},\d{2})', ln)
        if mm: summe = round(de_num(mm.group(1)), 2); break
    return dict(date=date, date_sort=date_sort, store_id=store_id,
                store=STORE_NAMES.get(store_id, 'REWE ' + str(store_id)),
                items=items, summe=summe, source='ebon', path=path)

def parse_online(path):
    """Parse one REWE online-order invoice PDF (delivery)."""
    lines = [l.rstrip() for l in _pdf_lines(path)]
    date = None
    for j, l in enumerate(lines):
        if 'Rechnungsdatum' in l:
            for k in (j, j + 1):
                if k < len(lines):
                    mm = re.search(r'(\d{2}\.\d{2}\.\d{4})', lines[k])
                    if mm: date = mm.group(1); break
        if date: break
    if not date:
        for j, l in enumerate(lines):
            if 'Lieferdatum' in l or 'Liefertermin' in l:
                for k in (j, j + 1):
                    if k < len(lines):
                        mm = re.search(r'(\d{2}\.\d{2}\.\d{4})', lines[k])
                        if mm: date = mm.group(1); break
            if date: break
    hi = None
    for j, l in enumerate(lines):
        if l.strip() in ('Summe Pos.', 'Gesamt') and any('Menge' in lines[k] for k in range(max(0, j - 4), j)):
            hi = j; break
    items = []; total = None; namebuf = []; i = hi + 1
    is_qty = lambda s: bool(re.match(r'^-?\d+$', s.strip())) or bool(re.match(r'^\d+\s*g$', s.strip()))
    while i < len(lines):
        s = lines[i].strip()
        if s in ('Summe', 'Gesamtsumme'):
            for k in range(i + 1, min(i + 4, len(lines))):
                mm = re.search(r'(-?\d+,\d{2})\s*€', lines[k])
                if mm: total = round(de_num(mm.group(1)), 2); break
            break
        if s.startswith('Für die mit') or s == 'Steuersatz':
            break
        if is_qty(s) and i + 3 < len(lines) and lines[i + 1].strip() in ('A', 'B', 'A/B'):
            me = re.search(r'(-?\d+,\d{2})\s*€', lines[i + 2])
            ms = re.search(r'(-?\d+,\d{2})\s*€', lines[i + 3])
            if me and ms:
                name = re.sub(r'\*$', '', ' '.join(x.strip() for x in namebuf if x.strip()).strip()).strip()
                items.append(dict(name=name, menge=s, mwst=lines[i + 1].strip(),
                                  einzel=round(de_num(me.group(1)), 4), summe=round(de_num(ms.group(1)), 2),
                                  perkg='/kg' in lines[i + 2]))
                namebuf = []; i += 4; continue
        namebuf.append(lines[i]); i += 1
    ds = None
    if date:
        d, mo, y = date.split('.'); ds = f"{y}-{mo}-{d}"
    return dict(date=date, date_sort=ds, items=items, total=total, source='online', path=path)

# ------------------------------------------------------------------- category guessing
CAT_RULES = [
    ('Carnes & Peixes', ['LACHS','HAEHN','HUHN','HACK','STEAK','BURGER','FILET','BRUST','NACKEN','SCHNITZEL','FISCH','HERING','THUN','GARNEL','PUTE','RIND','SCHWEIN','DUROC','CABANOSSI','BRUEH','HÄHN','BRUSTFIL']),
    ('Frios & Embutidos', ['SCHINKEN','SALAMI','WURST','PROSCIUTTO','SPECK','BACON','AUFSCHNITT','METTWURST','CRUDO']),
    ('Laticínios & Ovos', ['MILCH','MILK','JOGHURT','JOGURT','QUARK','KAESE','KÄSE','GOUDA','EMMENTAL','GRANA','MOZZAR','BUTTER','SAHNE','EIER','EI ','PUDD','SKYR','FRISCHKAESE','GALBANI','OBAZDA','MILRAM','MILKANA','SPEISEQ','SCHLAGSAHNE','MILCHMAED']),
    ('Frutas', ['BANANE','APFEL','TRAUBE','MELON','ORANGE','ZITRON','BEERE','MANGO','AVOCADO','PFLAUME','BIRNE','KIWI','ANANAS','WASSERMELON']),
    ('Legumês & Verduras', ['ZWIEBEL','KNOBLAUCH','KAROTTE','PORREE','GEMUESE','GEMÜSE','SALAT','SPINAT','TOMATE','GURKE','PAPRIKA','KARTOFFEL','CHAMP','PILZ','BROKKOLI','SUPPENGRUEN','FELDSALAT','BABYSPINAT','INGWER']),
    ('Padaria & Massas', ['BROT','BROETCHEN','BRIOCHE','NUDEL','PASTA','PENNE','SPAGHET','MEHL','TEIG','BUNS','TOAST','PANIERMEHL','BASMATI','REIS']),
    ('Doces & Snacks', ['SCHOKO','CHOCO','KEKS','WAFER','TORTE','BROWNIE','NUTELLA','RIEGEL','BONBON','GUMMI','CHIPS','SNACK','DAIM','KITKAT','MILKA','ZUCKER','TIRAMISU','TOERTCHEN','GOLDSCHATZ','HONIG']),
    ('Bebidas', ['COLA','WASSER','SAFT','LIMO','BIONADE','APPLE','ORANGE','TEE','KAFFEE','BIER','WEIN','DRINK','ISOCLEAR','WHEY','SPORT','KOKOSNUSSWASSER','TRIPLE']),
    ('Higiene & Limpeza', ['DUSCHE','DUSCH','SEIFE','SHAMPOO','ZAHN','WASCH','REINIG','TEMPO','DOVE','ULTIMATE','SENSITIVE','SPUEL','WC','ARIEL','OXI','COLORWASCH']),
    ('Ração & Pets', ['PURINA','KATZE','HUND','PET','GRILLIES','SCHLECKS','FELIX','SHEBA','KA ','GEFL LA','HAEHNCHEN & ENTE']),
    ('Molhos & Condimentos', ['KETCHUP','MAYO','SENF','SAUCE','SOJA','DRESSING','ESSIG','OEL','ÖL','CURRY','PESTO','FOND']),
    ('Temperos & Especiarias', ['SALZ','PFEFFER','KRAEUTER','KRÄUTER','GEWUERZ','PAPRIKA GEM','MEERSALZ','SCHALEN']),
    ('Congelados & Prontos', ['PIZZA','TIEFKUEHL','FROST','FLAMMKUCHEN','STAEBCHEN','BACKFISCH','PROTEINPUDD']),
]

def guess_category(name):
    up = name.upper()
    for cat, kws in CAT_RULES:
        if any(k in up for k in kws):
            return cat
    return 'Outros'

def short_name(full):
    """Make a compact, eBon-style name from an online full product name."""
    up = re.sub(r'\s+', ' ', full).strip().upper()
    up = re.sub(r'\b\d+[,\.]?\d*\s?(ML|L|G|KG|STÜCK|STUECK|WL)\b', '', up).strip()
    return up[:16].strip()

# ----------------------------------------------------------------------------- merge
def price_of(prod, h):
    if prod['kg'] and h.get('pkg') is not None:
        return h['pkg']
    return h['p']

def find_product(P, by_name, by_fn, name, fn=None):
    if name in by_name:
        return by_name[name]
    if fn:
        key = re.sub(r'\s+', ' ', fn).strip().lower()
        if key in by_fn:
            return by_fn[key]
    return None

def new_product(name, cat, kg, fn=''):
    return dict(n=name, cat=cat, tb=0, tq=0, lp=0.0, mn=0.0, mx=0.0, var=0.0, tr='stable',
                ld='', ds=0, kg=kg, ts=0.0, af=0, sb={}, h=[], desc='', iu='', fn=fn, pl='', tg=[])

def add_event(prod, date, ds, price, store, qty, line_total, pkg=None):
    h = dict(date=date, ds=ds, p=round(price, 2), s=store)
    if pkg is not None:
        h['pkg'] = round(pkg, 2)
    prod['h'].append(h)
    prod['tq'] += qty
    prod['ts'] = round(prod['ts'] + line_total, 2)

def merge_ebon(rec, P, by_name, by_fn, new_products):
    prods = [it for it in rec['items'] if it['kind'] == 'product']
    for it in prods:
        prod = by_name.get(it['name'])
        if prod is None:
            prod = new_product(it['name'], guess_category(it['name']), it['pkg'] is not None)
            P.append(prod); by_name[it['name']] = prod; new_products.append(it['name'])
        add_event(prod, rec['date'], rec['date_sort'], it['unit'], rec['store'],
                  it['qty'], it['line_total'], pkg=it['pkg'])
    return dict(date=rec['date'], date_sort=rec['date_sort'], total=rec['summe'],
                store=rec['store'], store_id=rec['store_id'], num_items=len(prods),
                products=[it['name'] for it in prods])

def merge_online(rec, P, by_name, by_fn, new_products):
    prods = [it for it in rec['items']
             if not re.search(r'PFAND|LIEFERGEB|PFANDTASCHE', it['name'].upper())]
    names = []
    for it in prods:
        prod = find_product(P, by_name, by_fn, it['name'], fn=it['name'])
        if prod is None:
            sn = short_name(it['name']); base = sn[:14]; k = 2
            while sn in by_name:
                sn = f"{base}{k}"[:16]; k += 1
            prod = new_product(sn, guess_category(it['name']), it['perkg'], fn=it['name'])
            P.append(prod); by_name[sn] = prod
            by_fn[re.sub(r'\s+', ' ', it['name']).strip().lower()] = prod
            new_products.append(sn + '  <= ' + it['name'])
        names.append(prod['n'])
        if it['perkg']:
            add_event(prod, rec['date'], rec['date_sort'], it['einzel'], 'REWE Lieferservice',
                      1, it['summe'], pkg=it['einzel'])
        else:
            qty = int(re.sub(r'\D', '', it['menge']) or '1')
            add_event(prod, rec['date'], rec['date_sort'], it['einzel'], 'REWE Lieferservice',
                      qty, it['summe'])
    return dict(date=rec['date'], date_sort=rec['date_sort'], total=rec['total'],
                store='REWE Lieferservice', store_id='0000', num_items=len(prods), products=names)

# --------------------------------------------------------------- per-product recompute
def recompute_product(p, ref_today):
    h = sorted(p['h'], key=lambda x: x['ds'])
    p['h'] = h
    if not h:
        return
    prices = [price_of(p, x) for x in h]
    p['lp'] = round(price_of(p, h[-1]), 2)
    p['mn'] = round(min(prices), 2)
    p['mx'] = round(max(prices), 2)
    p['ld'] = h[-1]['ds']
    p['tb'] = len(h)
    sb = {}
    for x in h:
        sb[x['s']] = sb.get(x['s'], 0) + 1
    p['sb'] = sb
    dates = sorted({x['ds'] for x in h})
    if len(dates) > 1:
        gaps = [(D(dates[i + 1]) - D(dates[i])).days for i in range(len(dates) - 1)]
        p['af'] = round(sum(gaps) / len(gaps), 1)
    else:
        p['af'] = 0
    p['ds'] = (ref_today - D(p['ld'])).days
    avg = sum(prices) / len(prices)
    p['var'] = round((p['lp'] - avg) / avg * 100, 1) if avg else 0.0
    p['tr'] = 'up' if p['var'] > 2 else 'down' if p['var'] < -2 else 'stable'

# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--report', default='report.html')
    ap.add_argument('--ebons', default=None, help='folder with "Dein REWE eBon vom *.pdf" files')
    ap.add_argument('--online', nargs='*', default=[], help='online invoice PDF files')
    ap.add_argument('--since', default=None, help='only ingest receipts strictly AFTER this YYYY-MM-DD')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    src = open(args.report, encoding='utf-8').read()
    P = load(src, 'P')
    rL = load(src, 'rL')
    for p in P:
        p['cat'] = decode_escapes(p['cat'])

    cutoff = args.since or max(r['date_sort'] for r in rL)
    print(f"Latest receipt already present: {max(r['date_sort'] for r in rL)}  -> ingesting after {cutoff}")

    # collect candidate receipts
    candidates = []
    if args.ebons:
        for f in sorted(os.listdir(args.ebons)):
            if not f.lower().endswith('.pdf'):
                continue
            m = re.search(r'vom (\d{2})\.(\d{2})\.(\d{4})', f)
            if not m:
                continue
            ds = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            if ds > cutoff:
                candidates.append(parse_ebon(os.path.join(args.ebons, f)))
    for f in args.online:
        rec = parse_online(f)
        if rec['date_sort'] and rec['date_sort'] > cutoff:
            candidates.append(rec)
        else:
            print(f"  skip online {os.path.basename(f)} (date {rec['date_sort']} <= {cutoff})")
    candidates.sort(key=lambda r: (r['date_sort'], r.get('store_id', ''), r.get('source')))

    # validate + merge
    by_name = {p['n']: p for p in P}
    by_fn = {re.sub(r'\s+', ' ', p['fn']).strip().lower(): p for p in P if p.get('fn')}
    new_products = []
    added = 0
    for rec in candidates:
        if rec['source'] == 'ebon':
            tot = round(sum(it['line_total'] for it in rec['items']), 2)
            if rec['summe'] is None or abs(tot - rec['summe']) > 0.02:
                print(f"  !! eBon {rec['date']} sum mismatch ({tot} vs {rec['summe']}) — SKIPPED"); continue
            rL.append(merge_ebon(rec, P, by_name, by_fn, new_products))
            print(f"  + {rec['date']} {rec['store']:20s} €{rec['summe']:.2f}")
        else:
            tot = round(sum(it['summe'] for it in rec['items']), 2)
            if rec['total'] is None or abs(tot - rec['total']) > 0.02:
                print(f"  !! online {rec['date']} sum mismatch ({tot} vs {rec['total']}) — SKIPPED"); continue
            rL.append(merge_online(rec, P, by_name, by_fn, new_products))
            print(f"  + {rec['date']} REWE Lieferservice (online) €{rec['total']:.2f}")
        added += 1

    if not added:
        print("Nothing new to add. Done."); return

    rL.sort(key=lambda r: r['date_sort'])
    ref_today = D(max(r['date_sort'] for r in rL))
    for p in P:
        recompute_product(p, ref_today)

    src = recompute_and_write(src, P, rL, ref_today)
    if new_products:
        print(f"\n{len(new_products)} new product(s) created:")
        for n in new_products:
            print("   ", n)

    if args.dry_run:
        print("\n[dry-run] not writing report.html")
        return
    open(args.report, 'w', encoding='utf-8').write(src)
    print(f"\nWrote {args.report}  ({len(rL)} receipts, {len(P)} products)")


def recompute_and_write(src, P, rL, ref_today):
    """Recompute every derived structure from P + rL and splice it back into report.html."""
    from collections import defaultdict

    months = sorted({r['date_sort'][:7] for r in rL})
    def lbl_de(m): y, mo = m.split('-'); return f"{MONTHS_DE[int(mo)-1]} {y}"
    def lbl_en(m): y, mo = m.split('-'); return f"{MONTHS_EN[int(mo)-1]} {y}"

    # monthly spend
    msum = defaultdict(float)
    for r in rL:
        msum[r['date_sort'][:7]] += r['total']
    mL = [lbl_de(m) for m in months]
    mV = [round(msum[m], 2) for m in months]
    sM = list(months)

    # categories (spend = sum of product ts)
    csum = defaultdict(float)
    for p in P:
        csum[p['cat']] += p['ts']
    cats_sorted = sorted(csum.items(), key=lambda kv: -kv[1])
    cL = [c for c, _ in cats_sorted]
    cV = [round(v, 2) for _, v in cats_sorted]

    # stores
    st = {}
    for r in rL:
        s = st.setdefault(r['store_id'], dict(id=r['store_id'], name=r['store'], address='',
                                              receipt_count=0, total_spent=0.0, avg_ticket=0.0))
        s['receipt_count'] += 1
        s['total_spent'] += r['total']
    sts = sorted(st.values(), key=lambda s: -s['total_spent'])
    for s in sts:
        s['total_spent'] = round(s['total_spent'], 2)
        s['avg_ticket'] = round(s['total_spent'] / s['receipt_count'], 2)

    # top spending
    tS = sorted(P, key=lambda p: -p['ts'])[:10]

    # weekday (Mon..Sun)
    wv = [0]*7; ws = [0.0]*7
    for r in rL:
        wd = D(r['date_sort']).weekday(); wv[wd] += 1; ws[wd] += r['total']
    wVi = wv
    wAv = [round(ws[i]/wv[i], 2) if wv[i] else 0 for i in range(7)]

    # price-movement counter (each purchase event vs that product's mean price)
    pU = pSt = pDn = 0
    for p in P:
        pr = [price_of(p, x) for x in p['h']]
        if not pr: continue
        avg = sum(pr)/len(pr)
        for v in pr:
            if v > avg + 1e-9: pU += 1
            elif v < avg - 1e-9: pDn += 1
            else: pSt += 1

    # category price-change breakdown
    cbd = defaultdict(lambda: dict(up=0, dn=0, st=0, ct=0, vsum=0.0))
    for p in P:
        if p['tb'] < 2: continue
        d = cbd[p['cat']]
        d['ct'] += 1; d['vsum'] += abs(p['var'])
        d['up'] += p['tr'] == 'up'; d['dn'] += p['tr'] == 'down'; d['st'] += p['tr'] == 'stable'
    cpd = []
    for cat, d in cbd.items():
        cpd.append(dict(cat=cat, av=round(d['vsum']/d['ct'], 1) if d['ct'] else 0,
                        up=d['up'], dn=d['dn'], st=d['st'], ct=d['ct']))
    cpd.sort(key=lambda c: -c['av'])
    cpd = cpd[:14]

    # volatile products
    volP = []
    for p in P:
        if p['tb'] >= 3 and p['mn'] > 0:
            vol = (p['mx'] - p['mn']) / p['mn'] * 100
            if vol > 0:
                volP.append(dict(n=p['n'], mn=p['mn'], mx=p['mx'], vol=round(vol, 1), tb=p['tb']))
    volP.sort(key=lambda x: -x['vol']); volP = volP[:10]

    # discovery (new products per month)
    firstm = defaultdict(int)
    for p in P:
        if p['h']:
            firstm[min(x['ds'] for x in p['h'])[:7]] += 1
    discD = [dict(month=m, count=firstm[m], label=lbl_en(m)) for m in months]

    # repurchase suggestions
    shD = []
    for p in P:
        if p['tb'] >= 3 and p['af'] and p['af'] > 0:
            od = p['ds'] - round(p['af'])
            urg = 'overdue' if p['ds'] > p['af'] else 'soon' if p['ds'] > p['af']*0.6 else 'ok'
            if urg in ('overdue', 'soon'):
                shD.append(dict(n=p['n'], cat=p['cat'], af=p['af'], ds=p['ds'], od=od,
                                lp=p['lp'], kg=p['kg'], tb=p['tb'], urg=urg,
                                iu=p.get('iu', ''), fn=p.get('fn', ''), desc=p.get('desc', '')))
    shD.sort(key=lambda s: -s['od']); shD = shD[:40]

    # price alerts from the most recent receipt
    last = sorted(rL, key=lambda r: r['date_sort'])[-1]
    pAlerts = []
    for name in set(last['products']):
        p = next((x for x in P if x['n'] == name), None)
        if not p: continue
        evs = [h for h in p['h'] if h['ds'] == last['date_sort']]
        prior = [h for h in p['h'] if h['ds'] < last['date_sort']]
        if not evs or not prior: continue
        curr = price_of(p, evs[-1]); prev = price_of(p, prior[-1])
        if prev and abs(curr - prev) / prev > 0.05:
            pAlerts.append(dict(n=name, prev=round(prev, 2), curr=round(curr, 2),
                                chg=round((curr - prev)/prev*100, 1)))
    pAlerts.sort(key=lambda a: -abs(a['chg'])); pAlerts = pAlerts[:12]

    # store comparison Q6 vs S6
    sC = []
    for p in P:
        q = [price_of(p, h) for h in p['h'] if h['s'] == 'REWE Q6']
        s = [price_of(p, h) for h in p['h'] if h['s'] == 'REWE S6']
        if not q and not s: continue
        qa = round(sum(q)/len(q), 2) if q else None
        sa = round(sum(s)/len(s), 2) if s else None
        excl = 'Q6' if q and not s else 'S6' if s and not q else None
        cheaper = diff = None
        if qa is not None and sa is not None:
            cheaper = 'Q6' if qa < sa else 'S6' if sa < qa else None
            base = min(qa, sa)
            diff = round(abs(qa - sa)/base*100, 1) if base else None
        sC.append(dict(name=p['n'], category=p['cat'], q6_avg=qa, q6_count=len(q),
                       s6_avg=sa, s6_count=len(s), exclusive_to=excl,
                       cheaper_at=cheaper, diff_pct=diff))
    sC.sort(key=lambda x: x['name'])

    # basket index (fixed basket of top-25 most-bought non-kg products) + inflation
    basket = [p for p in sorted(P, key=lambda p: -p['tb']) if not p['kg']][:25]
    bmonths = months[-12:]
    bL = [lbl_en(m) for m in bmonths]
    bV = []
    for m in bmonths:
        end = m + '-31'
        cost = 0.0
        for p in basket:
            past = [price_of(p, h) for h in p['h'] if h['ds'][:7] <= m]
            if past:
                cost += past[-1]
        bV.append(round(cost, 2))
    base = bV[0] if bV and bV[0] else 1
    bIdx = [round(v/base*100, 2) for v in bV]
    # official German food CPI: external data is unavailable here, so extend the prior
    # series by its historical average monthly step (documented approximation).
    try:
        old_gcpi = load(src, 'gCPI')
        step = ((old_gcpi[-1]-old_gcpi[0])/(len(old_gcpi)-1)) if len(old_gcpi) > 1 else 0.2
    except Exception:
        step = 0.2
    gCPI = [round(100 + step*i, 2) for i in range(len(bL))]

    # trends
    def recent_change(p):
        pr = [price_of(p, x) for x in p['h']]
        if len(pr) < 2: return None
        avg = sum(pr)/len(pr); last_p = pr[-1]
        n = max(1, len(pr)//3); recent_avg = sum(pr[-n:])/n
        return dict(n=p['n'], cat=p['cat'], tb=p['tb'], avg=round(avg, 2), last=round(last_p, 2),
                    recent_avg=round(recent_avg, 2), chg=round((last_p-avg)/avg*100, 1),
                    rchg=round((recent_avg-avg)/avg*100, 1), ts=round(p['ts'], 2), kg=p['kg'])
    changes = [c for c in (recent_change(p) for p in P if p['tb'] >= 3) if c]
    price_up = sorted([c for c in changes if c['rchg'] > 5], key=lambda c: -c['rchg'])[:15]
    price_down = sorted([c for c in changes if c['rchg'] < -5], key=lambda c: c['rchg'])[:15]
    reconsider = sorted([c for c in changes if c['chg'] > 10 and c['ts'] > 25], key=lambda c: -c['ts'])[:8]
    catprev = defaultdict(lambda: [0.0, 0.0, 0, 0])
    cutidx = bmonths[len(bmonths)//2] if bmonths else '9999'
    for p in P:
        for h in p['h']:
            pr = price_of(p, h); rec = h['ds'][:7] >= cutidx
            d = catprev[p['cat']]
            if rec: d[0] += pr; d[2] += 1
            else: d[1] += pr; d[3] += 1
    cat_trends = []
    for cat, d in catprev.items():
        if d[2] and d[3]:
            recent = round(d[0], 2); prev = round(d[1], 2)
            cat_trends.append(dict(cat=cat, recent=recent, prev=prev,
                                   chg=round((recent-prev)/prev*100, 1) if prev else 0))
    cat_trends.sort(key=lambda c: c['chg'])
    last3 = sorted(rL, key=lambda r: r['date_sort'])[-3:]
    last_3_avg = round(sum(r['total'] for r in last3)/len(last3), 2)
    overall_avg = round(sum(r['total'] for r in rL)/len(rL), 2)
    trd = dict(price_up=price_up, price_down=price_down, reconsider=reconsider,
               cat_trends=cat_trends, personal_inflation=round(bIdx[-1]-100, 1) if bIdx else 0,
               german_inflation=round(gCPI[-1]-100, 1) if gCPI else 0,
               last_3_avg=last_3_avg, overall_avg=overall_avg,
               ticket_diff=round((last_3_avg/overall_avg-1)*100, 1) if overall_avg else 0)

    # ----------------------------------------------------------------- splice back
    for name, obj in [('mL', mL), ('mV', mV), ('sM', sM), ('cL', cL), ('cV', cV),
                      ('sts', sts), ('tS', tS), ('wVi', wVi), ('wAv', wAv),
                      ('cpd', cpd), ('volP', volP), ('discD', discD), ('shD', shD),
                      ('pAlerts', pAlerts), ('sC', sC), ('bL', bL), ('bV', bV),
                      ('bIdx', bIdx), ('gCPI', gCPI), ('trd', trd), ('rL', rL), ('P', P)]:
        src = replace_literal(src, name, obj)
    for name, val in [('pU', pU), ('pSt', pSt), ('pDn', pDn)]:
        src = replace_scalar(src, name, val)

    # ----------------------------------------------------------------- HTML KPIs
    total = sum(r['total'] for r in rL)
    n = len(rL)
    nprod = len(P)
    items = sum(r['num_items'] for r in rL)
    first_d = D(min(r['date_sort'] for r in rL)); last_d = D(max(r['date_sort'] for r in rL))
    span = (last_d.year-first_d.year)*12 + (last_d.month-first_d.month) + 1
    first_s = first_d.strftime('%d.%m.%Y'); last_s = last_d.strftime('%d.%m.%Y')
    def eur(x): return f"{x:,.2f}".replace(',', '@').replace('.', ',').replace('@', '.')

    header = f"{first_s} - {last_s} · {n} compras · {nprod} produtos · {span} meses"
    src = re.sub(r'\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4} · \d+ compras · \d+ produtos · \d+ meses',
                 header, src)
    src = re.sub(r'\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4} &middot; \d+ compras &middot; \d+ produtos &middot; \d+ meses',
                 header.replace(' · ', ' &middot; '), src)
    # item count lives in the i18n dicts (overrides the static value at load)
    src = re.sub(r"items_count:'\d+ (itens|items|Artikel)'",
                 lambda m: f"items_count:'{items} {m.group(1)}'", src)
    # written-report period header (only matches inside rpTexts, not product dates)
    src = re.sub(r'01\.07\.2024 - 04\.04\.2026 \(21 (meses|months|Monate)\)',
                 lambda m: f'{first_s} - {last_s} ({span} {m.group(1)})', src)
    # KPI cards (overview)
    src = re.sub(r'(<div class="lb" data-i18n="total_spent">[^<]*</div><div class="vl">&euro;)[\d.,]+',
                 r'\g<1>' + eur(total), src)
    src = re.sub(r'(<div class="lb" data-i18n="monthly_avg">[^<]*</div><div class="vl">&euro;)[\d.,]+',
                 r'\g<1>' + eur(total/span), src)
    src = re.sub(r'(<div class="lb" data-i18n="avg_receipt">[^<]*</div><div class="vl">&euro;)[\d.,]+',
                 r'\g<1>' + eur(total/n), src)
    src = re.sub(r'(<div class="lb" data-i18n="purchases">[^<]*</div><div class="vl">)\d+(</div><div class="dt" data-i18n="items_count">)\d+( itens)',
                 r'\g<1>' + str(n) + r'\g<2>' + str(items) + r'\g<3>', src)
    src = re.sub(r'(<div class="lb" data-i18n="products">[^<]*</div><div class="vl">)\d+',
                 r'\g<1>' + str(nprod), src)
    # last-3 / vs-average cards
    src = re.sub(r'(data-i18n="last_3_purchases">[^<]*</div><div class="vl sm">&euro;)[\d.,]+',
                 r'\g<1>' + f"{last_3_avg:.1f}", src)
    diff = round((last_3_avg/overall_avg-1)*100, 1)
    src = re.sub(r'(data-i18n="vs_avg">[^<]*</div><div class="vl sm">)[-\d.,]+%(</div><div class="dt">&euro;)[\d.,]+',
                 r'\g<1>' + f"{diff}%" + r'\g<2>' + f"{overall_avg:.2f}", src)

    print(f"\nKPIs: total €{eur(total)} | {n} receipts | {nprod} products | {items} items | "
          f"{span} months | monthly €{total/span:.2f} | ticket €{total/n:.2f}")
    print(f"Range: {first_s} - {last_s} | last-3 avg €{last_3_avg} ({diff}% vs €{overall_avg})")
    return src


if __name__ == '__main__':
    main()
