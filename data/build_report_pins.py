"""
Convert GIP/WIP asset IDs to ghost-pin GeoJSON by projecting MP onto section alignments.
Also enrich existing GIS-sourced per-section files with _reported_in tags based on
C5/C6 page mentions of the route.

Output files:
  nps-gip-guardrails-section-{A,B,C,D}.geojson   New ghost pins for guardrails
  nps-wip-walls-section-{A,B,C,D}.geojson        New ghost pins for walls
  (also rewrites the 28 existing per-section files with enriched _reported_in fields)
  pin-totals-by-section.json                     Final summary counts
"""
import json, math, os, glob

DATA = os.path.dirname(os.path.abspath(__file__))
R_FT = 20902231.0

def hav(a, b):
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R_FT*math.asin(math.sqrt(h))

def polyline_length_ft(coords):
    return sum(hav(coords[i], coords[i+1]) for i in range(len(coords)-1))

def point_at_distance(coords, dist_ft):
    """Return (lat, lng) at arc-length `dist_ft` along the polyline."""
    cum = 0.0
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        seg = hav(a, b)
        if cum + seg >= dist_ft:
            t = (dist_ft - cum) / seg if seg > 0 else 0.0
            return (a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1]))
        cum += seg
    return coords[-1]

def sta_fmt(sta_ft):
    return f'{int(sta_ft//100)}+{sta_ft%100:05.2f}'

# Section configuration — maps report route IDs to section alignments and MP ranges.
# 0012ZZ = Gatlinburg Bypass cumulative (mainline + ramps) per project MP 0-4.64.
# Our Section A alignment is mainline only (MP 0-3.747), so 0012ZZ assets with MP
# > 3.747 go to an out-of-section bucket (ramps need ramp-specific alignments we don't have).
SECTIONS = {
    'A': {
        'file': 'section-A-gatlinburg-bypass.geojson',
        'report_route_match': {'0012ZZ': (0.0, 3.747)},  # only plot MPs within mainline range
        'project_route': '0012Z',
        'mp_start': 0.0,
        'mp_end': 3.747,
        'project_scope_mp': (0.0, 4.64),
    },
    'B': {
        'file': 'section-B-newfound-gap-NC-1.geojson',
        'report_route_match': {'0010S': (14.64, 20.90)},
        'project_route': '0010S',
        'mp_start': 14.64,
        'mp_end': 20.90,
        'project_scope_mp': (14.64, 20.90),
    },
    'C': {
        'file': 'section-C-newfound-gap-NC-2.geojson',
        'report_route_match': {'0010S': (31.25, 31.96)},
        'project_route': '0010S',
        'mp_start': 31.25,
        'mp_end': 31.96,
        'project_scope_mp': (31.25, 31.96),
    },
    'D': {
        'file': 'section-D-newfound-gap-TN.geojson',
        'report_route_match': {'0010N': (1.86, 6.50)},
        'project_route': '0010N',
        'mp_start': 1.86,
        'mp_end': 6.50,
        'project_scope_mp': (1.86, 6.50),
    },
}

# Load each section's alignment polyline into (lat, lng) coords
section_data = {}
for label, cfg in SECTIONS.items():
    with open(os.path.join(DATA, cfg['file']), encoding='utf-8') as f:
        d = json.load(f)
    coords = [(c[1], c[0]) for c in d['features'][0]['geometry']['coordinates']]
    section_data[label] = {
        'coords': coords,
        'length_ft': polyline_length_ft(coords),
        'cfg': cfg,
    }

def mp_to_latlng(label, mp_along_section):
    """Interpolate lat/lng on the section's alignment at the given MP within the section."""
    sd = section_data[label]
    cfg = sd['cfg']
    # mp_along_section is MP on the original route. Compute fraction within section.
    frac = (mp_along_section - cfg['mp_start']) / (cfg['mp_end'] - cfg['mp_start'])
    frac = max(0.0, min(1.0, frac))
    sta_ft = frac * sd['length_ft']
    lat, lng = point_at_distance(sd['coords'], sta_ft)
    return lat, lng, sta_ft

def process_report(report_key, report_label, icon_code):
    """Load {report}-asset-ids.json, convert to per-section GeoJSON."""
    path = os.path.join(DATA, f'{report_key}-asset-ids.json')
    with open(path, encoding='utf-8') as f:
        src = json.load(f)
    per_section = {k: [] for k in SECTIONS}
    out_of_section = []
    for aid, a in src['assets'].items():
        route = a['route']
        mp = a['mp']
        side = a['side']
        # Find which section this asset belongs to
        matched = False
        for label, cfg in SECTIONS.items():
            if route in cfg['report_route_match']:
                mp_lo, mp_hi = cfg['report_route_match'][route]
                if mp_lo <= mp <= mp_hi:
                    lat, lng, sta_ft = mp_to_latlng(label, mp)
                    per_section[label].append({
                        'type': 'Feature',
                        'properties': {
                            'asset_id': aid,
                            'route': route,
                            'side': side,
                            'mp': mp,
                            '_feature_code': icon_code,
                            '_report_source': report_label,
                            '_section': label,
                            '_sta_ft': round(sta_ft, 1),
                            '_sta': sta_fmt(sta_ft),
                            '_reported_in': [report_label],
                            '_report_cross_refs': {
                                report_label: {
                                    'asset_id': aid,
                                    'pages_in_report': a['pages'],
                                    'source_pdf': src['source'],
                                },
                            },
                            '_position_source': 'interpolated_from_mp',
                            '_note': 'Position is linear-interpolated along section alignment from MP. Ground-truth during field walk.',
                        },
                        'geometry': {'type': 'Point', 'coordinates': [lng, lat]},
                    })
                    matched = True
                    break
        if not matched:
            # Flag assets whose MP is on the route but outside any section's project scope
            out_of_section.append({'asset_id': aid, 'route': route, 'mp': mp, 'side': side})

    # Sort by STA within each section and write
    for label, rows in per_section.items():
        rows.sort(key=lambda f: f['properties']['_sta_ft'])
        out_name = f'nps-{report_key}-{icon_code.lower()}s-section-{label}.geojson'
        with open(os.path.join(DATA, out_name), 'w', encoding='utf-8') as f:
            json.dump({
                'type': 'FeatureCollection',
                'metadata': {
                    'source': src['source'],
                    'report': report_label,
                    'section': label,
                    'count': len(rows),
                    'note': 'Ghost pins derived from NPS inventory report asset IDs. Positions interpolated from MP.',
                },
                'features': rows,
            }, f, indent=2)
    return {k: len(v) for k, v in per_section.items()}, out_of_section

gip_counts, gip_oos = process_report('gip', 'GIP', 'GR')
wip_counts, wip_oos = process_report('wip', 'WIP', 'RW')

print('=== Ghost pins from reports (new) ===')
print(f'{"Type":<20} {"Sec A":>6} {"Sec B":>6} {"Sec C":>6} {"Sec D":>6} {"Total":>6}')
print(f'{"GIP (guardrails)":<20} {gip_counts["A"]:>6} {gip_counts["B"]:>6} {gip_counts["C"]:>6} {gip_counts["D"]:>6} {sum(gip_counts.values()):>6}')
print(f'{"WIP (walls)":<20} {wip_counts["A"]:>6} {wip_counts["B"]:>6} {wip_counts["C"]:>6} {wip_counts["D"]:>6} {sum(wip_counts.values()):>6}')
print(f'Out-of-section: GIP {len(gip_oos)}, WIP {len(wip_oos)}')
print()

# --- Enrich existing GIS per-section files with RIP cross-reference ---
# For each GIS asset, tag with RIP-C5 / RIP-C6 if the route has page mentions in that report.
# (Coarse: route-level presence, not per-asset lookup. Later refinement possible.)
with open(os.path.join(DATA, 'rip-c5-route-pages.json'), encoding='utf-8') as f:
    c5_pages = json.load(f)['route_pages']
with open(os.path.join(DATA, 'rip-c6-route-pages.json'), encoding='utf-8') as f:
    c6_pages = json.load(f)['route_pages']

# Map section -> routes covered
section_routes = {
    'A': ['0012Z','0012ZZ','0012AZ','0012BZ','0012CZ','0012DZ','0012EZ','0012FZ'],
    'B': ['0010S'],
    'C': ['0010S'],
    'D': ['0010N'],
}

gis_pattern = 'nps-*-section-*.geojson'
rip_enrichment_count = 0
for path in glob.glob(os.path.join(DATA, 'nps-*-section-*.geojson')):
    filename = os.path.basename(path)
    # Only process GIS-sourced files (exclude report-derived files we just wrote)
    if any(k in filename for k in ['-gip-','-wip-']):
        continue
    # Parse section label from filename
    # Pattern: nps-{dataset}-section-{A|B|C|D}.geojson
    parts = filename[:-len('.geojson')].split('-')
    if len(parts) < 4: continue
    section_label = parts[-1]
    if section_label not in SECTIONS: continue
    routes = section_routes[section_label]
    c5_hits = any(c5_pages.get(r) for r in routes)
    c6_hits = any(c6_pages.get(r) for r in routes)
    if not (c5_hits or c6_hits): continue
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    modified = False
    for ft in d.get('features', []):
        p = ft.setdefault('properties', {})
        reported = p.setdefault('_reported_in', ['NPS-GIS'])
        refs = p.setdefault('_report_cross_refs', {})
        if c5_hits and 'RIP-C5' not in reported:
            reported.append('RIP-C5')
            refs['RIP-C5'] = {'pages_by_route': {r: c5_pages.get(r, []) for r in routes if c5_pages.get(r)}}
            modified = True
        if c6_hits and 'RIP-C6' not in reported:
            reported.append('RIP-C6')
            refs['RIP-C6'] = {'pages_by_route': {r: c6_pages.get(r, []) for r in routes if c6_pages.get(r)}}
            modified = True
    if modified:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=2)
        rip_enrichment_count += len(d.get('features', []))

print(f'GIS assets enriched with RIP-C5/C6 cross-refs: {rip_enrichment_count}')
print()

# --- Final tally ---
totals = {lab: {} for lab in SECTIONS}
for path in glob.glob(os.path.join(DATA, 'nps-*-section-*.geojson')):
    filename = os.path.basename(path)
    parts = filename[:-len('.geojson')].split('-')
    label = parts[-1]
    if label not in SECTIONS: continue
    # Dataset name = 'nps' + parts between 'nps' and 'section'
    # e.g. 'nps-signs-section-A' -> dataset 'signs'
    # 'nps-gip-grs-section-A' -> dataset 'gip-grs'
    try:
        sec_i = parts.index('section')
        ds = '-'.join(parts[1:sec_i])
    except ValueError:
        ds = '?'
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    totals[label][ds] = len(d.get('features', []))

print('=== Final pin totals per section ===')
all_ds = set()
for s in totals.values():
    all_ds.update(s.keys())
all_ds = sorted(all_ds)
print(f'{"Dataset":<20}' + ''.join(f'{lab:>7}' for lab in SECTIONS) + f'{"Total":>8}')
print('-' * (20 + 7*4 + 8))
grand = {lab: 0 for lab in SECTIONS}
for ds in all_ds:
    row_sum = 0
    row_str = f'{ds:<20}'
    for lab in SECTIONS:
        n = totals[lab].get(ds, 0)
        row_sum += n
        grand[lab] += n
        row_str += f'{n:>7}'
    row_str += f'{row_sum:>8}'
    print(row_str)
print('-' * (20 + 7*4 + 8))
row_str = f'{"TOTAL":<20}' + ''.join(f'{grand[lab]:>7}' for lab in SECTIONS) + f'{sum(grand.values()):>8}'
print(row_str)

# Write final manifest
with open(os.path.join(DATA, 'pin-totals-by-section.json'), 'w', encoding='utf-8') as f:
    json.dump({'totals': totals, 'grand': grand, 'gip_out_of_section': gip_oos, 'wip_out_of_section': wip_oos}, f, indent=2)
