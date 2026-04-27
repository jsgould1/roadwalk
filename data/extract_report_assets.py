"""
Full-text extract of the four GRSM PDFs, pulling structured asset mentions.

Outputs:
  gip-asset-ids.json     Every {route}-{mp}-{side} asset ID in GIP + the page(s) it appears on
  wip-asset-ids.json     Same for WIP
  rip-c5-route-pages.json / rip-c6-route-pages.json  Page ranges per route
  reports-cross-ref-index.json  Consolidated: for each target route, which reports reference it and on which pages
"""
import pypdf, os, re, json, sys, time
from collections import defaultdict

BASE = r'C:\Users\gouldj\OneDrive - AECOM\Documents\!DATA\NPS'
OUT = r'C:\Users\gouldj\OneDrive - AECOM\Documents\!AECOM\CLAUDE\RoadWalk\data'

# Target routes for this trial
TARGET_ROUTES = {'0010N','0010S','0012Z','0012ZZ','0012AZ','0012BZ','0012CZ','0012DZ','0012EZ','0012FZ'}

# Asset-ID pattern: ROUTE-MP.MP-SIDE where MP is decimal, SIDE is L/R/B/C
# Example: 0010N-1.471-R, 0012Z-3.512-L, 0010S-27.687-L
ASSET_RE = re.compile(r'\b(00\d{2}[A-Z]{0,2})-(\d+\.\d+)-([LRBC])\b')

def extract_from_pdf(pdf_name, out_key):
    print(f'--- {pdf_name} ---', flush=True)
    path = os.path.join(BASE, pdf_name)
    assets = defaultdict(lambda: {'pages': [], 'mp': None, 'side': None, 'route': None})
    route_pages = defaultdict(list)  # route -> list of page numbers it appears on
    t0 = time.time()
    with open(path, 'rb') as f:
        r = pypdf.PdfReader(f)
        n = len(r.pages)
        for i in range(n):
            if i % 100 == 0:
                print(f'  page {i+1}/{n}  ({time.time()-t0:.1f}s elapsed)', flush=True)
            try:
                text = r.pages[i].extract_text() or ''
            except Exception as e:
                print(f'  page {i+1}: extract error: {e}', flush=True)
                continue
            # All asset IDs on this page
            for m in ASSET_RE.finditer(text):
                route, mp, side = m.group(1), m.group(2), m.group(3)
                aid = f'{route}-{mp}-{side}'
                a = assets[aid]
                a['pages'].append(i+1)
                a['mp'] = float(mp)
                a['side'] = side
                a['route'] = route
            # Track which routes appear on which pages (broader than just asset IDs)
            for route in TARGET_ROUTES:
                if route in text:
                    route_pages[route].append(i+1)
    print(f'  -> {len(assets)} unique asset IDs, {sum(len(v) for v in route_pages.values())} route hits', flush=True)
    # Deduplicate page lists
    for aid, a in assets.items():
        a['pages'] = sorted(set(a['pages']))
    for route in route_pages:
        route_pages[route] = sorted(set(route_pages[route]))
    # Write
    with open(os.path.join(OUT, f'{out_key}-asset-ids.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'source': pdf_name,
            'total_unique_assets': len(assets),
            'assets': assets,
        }, f, indent=2)
    with open(os.path.join(OUT, f'{out_key}-route-pages.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'source': pdf_name,
            'target_routes': sorted(TARGET_ROUTES),
            'route_pages': route_pages,
        }, f, indent=2)
    print(f'  wrote {out_key}-asset-ids.json and {out_key}-route-pages.json', flush=True)
    return assets, route_pages

# Process all four PDFs
gip_assets, gip_routes = extract_from_pdf('GRSM_GIPReport.pdf', 'gip')
wip_assets, wip_routes = extract_from_pdf('GRSM_WIPReport.pdf', 'wip')
c5_assets, c5_routes   = extract_from_pdf('GRSM_C5_RipReport.pdf', 'rip-c5')
c6_assets, c6_routes   = extract_from_pdf('GRSM_C6_RipReport.pdf', 'rip-c6')

# Summary — per target route, asset count from GIP and WIP
summary = {}
for route in sorted(TARGET_ROUTES):
    summary[route] = {
        'gip_assets_on_route': sum(1 for aid, a in gip_assets.items() if a['route'] == route),
        'wip_assets_on_route': sum(1 for aid, a in wip_assets.items() if a['route'] == route),
        'gip_page_mentions':   len(gip_routes.get(route, [])),
        'wip_page_mentions':   len(wip_routes.get(route, [])),
        'c5_page_mentions':    len(c5_routes.get(route, [])),
        'c6_page_mentions':    len(c6_routes.get(route, [])),
    }

print()
print('=== Summary: asset/mention counts per target route ===')
print(f'{"Route":<8} {"GIP#":>5} {"WIP#":>5} {"GIP pg":>7} {"WIP pg":>7} {"C5 pg":>6} {"C6 pg":>6}')
for route, s in summary.items():
    print(f'{route:<8} {s["gip_assets_on_route"]:>5} {s["wip_assets_on_route"]:>5} {s["gip_page_mentions"]:>7} {s["wip_page_mentions"]:>7} {s["c5_page_mentions"]:>6} {s["c6_page_mentions"]:>6}')

# Write the consolidated index
with open(os.path.join(OUT, 'reports-cross-ref-index.json'), 'w', encoding='utf-8') as f:
    json.dump({
        'per_route_summary': summary,
        'gip_asset_count': len(gip_assets),
        'wip_asset_count': len(wip_assets),
    }, f, indent=2)
print()
print('Wrote reports-cross-ref-index.json')
