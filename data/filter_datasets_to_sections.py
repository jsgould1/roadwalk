"""
Filter every useful GRSM GIS dataset to per-section GeoJSON files.

For each (dataset, section) pair, emit a GeoJSON FeatureCollection of
features within TOLERANCE_FT of the section's alignment polyline,
annotated with computed STA.
"""
import json, math, os

DATA = os.path.dirname(os.path.abspath(__file__))
TOLERANCE_FT = 300.0  # include anything within 300 ft of alignment

R_FT = 20902231.0
def hav(a, b):
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R_FT*math.asin(math.sqrt(h))

def sta_on_polyline(pt, coords):
    lat0 = pt[0]; cos_lat = math.cos(math.radians(lat0))
    def xy(p): return ((p[1]-pt[1])*cos_lat*364000.0, (p[0]-pt[0])*364000.0)
    best_d = float('inf'); best_cum = 0.0
    cum = 0.0
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        seg_len = hav(a, b)
        ax, ay = xy(a); bx, by = xy(b)
        vx, vy = bx-ax, by-ay
        denom = vx*vx + vy*vy
        if denom < 1e-9: cum += seg_len; continue
        t = max(0.0, min(1.0, (-ax*vx + -ay*vy)/denom))
        proj = (a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1]))
        d = hav(pt, proj)
        if d < best_d:
            best_d = d
            best_cum = cum + t*seg_len
        cum += seg_len
    return best_cum, best_d

def first_point(geom):
    if not geom: return None
    t = geom.get('type')
    c = geom.get('coordinates')
    if t == 'Point': return c[:2]
    if t == 'LineString': return c[0][:2]
    if t == 'MultiLineString': return c[0][0][:2]
    if t == 'Polygon': return c[0][0][:2]
    if t == 'MultiPolygon': return c[0][0][0][:2]
    return None

def sta_fmt(sta_ft):
    return f'{int(sta_ft//100)}+{sta_ft%100:05.2f}'

# Load section alignments
sections = {}
for label, file in [
    ('A', 'section-A-gatlinburg-bypass.geojson'),
    ('B', 'section-B-newfound-gap-NC-1.geojson'),
    ('C', 'section-C-newfound-gap-NC-2.geojson'),
    ('D', 'section-D-newfound-gap-TN.geojson'),
]:
    with open(os.path.join(DATA, file), encoding='utf-8') as f: d = json.load(f)
    coords = [(c[1], c[0]) for c in d['features'][0]['geometry']['coordinates']]
    sections[label] = coords

# Datasets to filter. Skip barriers (trailhead bollards, not road guardrails)
# and pavement/roads (too sparse or not useful as pins).
datasets = [
    ('signs',        'grsm-signs.geojson'),
    ('mile-markers', 'grsm-mile-markers.geojson'),
    ('bridge-pt',    'grsm-bridge-pt.geojson'),
    ('bridge-ln',    'grsm-bridge-ln.geojson'),
    ('gates',        'grsm-gates.geojson'),
    ('parking',      'grsm-parking.geojson'),
]

manifest = {'sections': {k: {} for k in sections}, 'meta': {
    'tolerance_ft': TOLERANCE_FT,
    'source': 'NPS GRSM ArcGIS Hub opendata',
}}

for name, fn in datasets:
    with open(os.path.join(DATA, fn), encoding='utf-8') as f: src = json.load(f)
    per_section = {k: [] for k in sections}
    for ft in src['features']:
        g = ft.get('geometry')
        coord = first_point(g) if g else None
        if coord is None: continue
        lng, lat = coord
        pt = (lat, lng)
        # Assign to the section with smallest distance under tolerance
        best_label, best_dist, best_sta = None, TOLERANCE_FT + 1, None
        for label, coords in sections.items():
            sta, d = sta_on_polyline(pt, coords)
            if d <= TOLERANCE_FT and d < best_dist:
                best_label, best_dist, best_sta = label, d, sta
        if best_label is None: continue
        # Build enriched properties
        props = dict(ft.get('properties', {}))
        props['_section'] = best_label
        props['_sta_ft'] = round(best_sta, 1)
        props['_sta'] = sta_fmt(best_sta)
        props['_dist_from_alignment_ft'] = round(best_dist, 1)
        props['_reported_in'] = ['NPS-GIS']
        props['_report_cross_refs'] = {}
        per_section[best_label].append({
            'type': 'Feature',
            'properties': props,
            'geometry': g,
        })

    # Write one file per section
    for label, feats in per_section.items():
        feats.sort(key=lambda f: f['properties']['_sta_ft'])
        out_name = f'nps-{name}-section-{label}.geojson'
        with open(os.path.join(DATA, out_name), 'w', encoding='utf-8') as f:
            json.dump({
                'type': 'FeatureCollection',
                'metadata': {
                    'dataset': name,
                    'section': label,
                    'count': len(feats),
                    'tolerance_ft': TOLERANCE_FT,
                    'source': 'NPS GRSM ArcGIS Hub opendata',
                },
                'features': feats,
            }, f, indent=2)
        manifest['sections'][label][name] = {
            'file': out_name,
            'count': len(feats),
        }

with open(os.path.join(DATA, 'nps-assets-manifest.json'), 'w', encoding='utf-8') as f:
    json.dump(manifest, f, indent=2)

# Summary table
print(f'{"Dataset":<15}' + ''.join(f'{"Sec "+k:>8}' for k in ['A','B','C','D']) + f'{"Total":>8}')
print('-' * 55)
for name, _ in datasets:
    row = [manifest['sections'][k][name]['count'] for k in ['A','B','C','D']]
    print(f'{name:<15}' + ''.join(f'{n:>8}' for n in row) + f'{sum(row):>8}')
# Include culverts totals from already-written files
cul_counts = {}
for k in ['A','B','C','D']:
    with open(os.path.join(DATA, f'nps-culverts-section-{k}.geojson'), encoding='utf-8') as f:
        cul_counts[k] = len(json.load(f)['features'])
    manifest['sections'][k]['culverts'] = {'file': f'nps-culverts-section-{k}.geojson', 'count': cul_counts[k]}
# Rewrite manifest with culverts merged in
with open(os.path.join(DATA, 'nps-assets-manifest.json'), 'w', encoding='utf-8') as f:
    json.dump(manifest, f, indent=2)
print(f'{"culverts":<15}' + ''.join(f'{cul_counts[k]:>8}' for k in ['A','B','C','D']) + f'{sum(cul_counts.values()):>8}')
print()
print(f'Manifest written: nps-assets-manifest.json')
