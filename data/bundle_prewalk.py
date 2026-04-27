"""
Consolidate all alignments + ghost pins into a single JSON bundle for RoadWalk.

Output: prewalk-bundle.json (one file, compact format for embedding or fetch).

Bundle structure:
{
  "version": "v1",
  "generated_at": ISO timestamp,
  "sections": [
    {
      "id": "A",
      "name": "Gatlinburg Bypass (MP 0.00–4.64)",
      "project_code": "TN NP GRSM 12(4)",
      "type": "linear",
      "pathweb_primary": 6416,
      "pathweb_refs": [{ "id": 6416, "role": "mainline", "mp_start": 0.0, "mp_end": 3.747 }, ...],
      "mp_start": 0.0,
      "mp_end": 4.64,
      "alignment": [[lng, lat], ...],
      "pins": [
        {
          "id": "A-si-001",            # local stable id for this section
          "kind": "sign" | "culvert" | "guardrail" | ...,
          "source": "nps-gis" | "gip" | "wip",
          "geometry": { "type": "Point"|"LineString", "coordinates": [...] },
          "asset_id": original report asset id if available,
          "sta_ft": stationing in feet from 0+00,
          "status": "pending",  # pending | confirmed | discarded | relocated
          "reported_in": ["NPS-GIS"] or ["GIP"] or ["NPS-GIS","RIP-C6"],
          "attrs": {...per-type fields, compact},
        }, ...
      ]
    }, ...
  ]
}
"""
import json, os, glob, datetime

DATA = os.path.dirname(os.path.abspath(__file__))

# Section metadata — canonical config
SECTIONS_META = [
    {
        'id': 'A', 'name': 'Gatlinburg Bypass (MP 0.00–4.64)',
        'project_code': 'TN NP GRSM 12(4)', 'type': 'linear',
        'mp_start': 0.0, 'mp_end': 4.64,
        'alignment_file': 'section-A-gatlinburg-bypass.geojson',
        'pathweb_refs': [
            {'id': 6416, 'role': 'mainline', 'mp_start': 0.0, 'mp_end': 3.747},
            {'id': 6410, 'role': 'ramp_NB_AZ', 'mp_start': 0.0, 'mp_end': 0.231},
            {'id': 6411, 'role': 'ramp_NB_BZ', 'mp_start': 0.0, 'mp_end': 0.088},
            {'id': 6412, 'role': 'ramp_SB_CZ', 'mp_start': 0.0, 'mp_end': 0.093},
            {'id': 6413, 'role': 'ramp_NB_DZ', 'mp_start': 0.0, 'mp_end': 0.404},
            {'id': 6414, 'role': 'ramp_SB_EZ', 'mp_start': 0.0, 'mp_end': 0.044},
            {'id': 6415, 'role': 'ramp_NB_FZ', 'mp_start': 0.0, 'mp_end': 0.038},
        ],
    },
    {
        'id': 'B', 'name': 'Newfound Gap Rd NC #1 (MP 14.64–20.90)',
        'project_code': 'NC NP GRSM 10S(2)', 'type': 'linear',
        'mp_start': 14.64, 'mp_end': 20.90,
        'alignment_file': 'section-B-newfound-gap-NC-1.geojson',
        'pathweb_refs': [
            {'id': 6407, 'role': 'mainline_S', 'mp_start': 14.98, 'mp_end': 31.96, 'note': 'full 0010S Pathweb section; this RoadWalk section covers MP 14.64-20.90'},
        ],
    },
    {
        'id': 'C', 'name': 'Newfound Gap Rd NC #2 (MP 31.25–31.96)',
        'project_code': 'NC NP GRSM 10S(2)', 'type': 'linear',
        'mp_start': 31.25, 'mp_end': 31.96,
        'alignment_file': 'section-C-newfound-gap-NC-2.geojson',
        'pathweb_refs': [
            {'id': 6407, 'role': 'mainline_S', 'mp_start': 14.98, 'mp_end': 31.96, 'note': 'same Pathweb section as B; RoadWalk section covers MP 31.25-31.96'},
        ],
    },
    {
        'id': 'D', 'name': 'Newfound Gap Rd TN (MP 1.86–6.50)',
        'project_code': 'TN NP GRSM 10N(4)', 'type': 'linear',
        'mp_start': 1.86, 'mp_end': 6.50,
        'alignment_file': 'section-D-newfound-gap-TN.geojson',
        'pathweb_refs': [
            {'id': 6406, 'role': 'mainline_N', 'mp_start': 0.0, 'mp_end': 14.98, 'note': 'full 0010N Pathweb section; this RoadWalk section covers MP 1.86-6.50'},
        ],
    },
]

# Pin source files per section: (pattern, kind, source)
# Kind: semantic type; source: where it came from
PIN_SOURCES = [
    ('nps-signs-section-{lab}.geojson',       'sign',      'nps-gis'),
    ('nps-mile-markers-section-{lab}.geojson','mile_marker','nps-gis'),
    ('nps-bridge-pt-section-{lab}.geojson',   'bridge',    'nps-gis'),
    ('nps-bridge-ln-section-{lab}.geojson',   'bridge_line','nps-gis'),
    ('nps-gates-section-{lab}.geojson',       'gate',      'nps-gis'),
    ('nps-parking-section-{lab}.geojson',     'parking',   'nps-gis'),
    ('nps-culverts-section-{lab}.geojson',    'culvert',   'nps-gis'),
    ('nps-gip-grs-section-{lab}.geojson',     'guardrail', 'gip'),
    ('nps-wip-rws-section-{lab}.geojson',     'wall',      'wip'),
]

def load_alignment(path):
    with open(path, encoding='utf-8') as f: d = json.load(f)
    # First feature is alignment
    return d['features'][0]['geometry']['coordinates']

def compact_pin(ft, kind, source, section_id, pin_id):
    """Extract essential fields into a compact pin dict."""
    props = ft.get('properties', {})
    geom = ft.get('geometry', {})
    pin = {
        'id': pin_id,
        'kind': kind,
        'source': source,
        'status': 'pending',
        'geometry': geom,
    }
    # Common fields
    if '_sta_ft' in props: pin['sta_ft'] = props['_sta_ft']
    elif '_sta_start_ft' in props: pin['sta_ft'] = props['_sta_start_ft']
    if '_sta' in props: pin['sta'] = props['_sta']
    elif '_sta_start' in props: pin['sta'] = props['_sta_start']
    if '_reported_in' in props: pin['reported_in'] = props['_reported_in']
    if '_report_cross_refs' in props: pin['report_refs'] = props['_report_cross_refs']
    if 'asset_id' in props: pin['asset_id'] = props['asset_id']
    # Type-specific compact attrs
    attrs = {}
    for src_key, dst_key in [
        ('LOC_NAME', 'loc_name'), ('ROAD', 'road'),
        ('NAME', 'name'), ('SHORTNAME', 'short_name'),
        ('MILE_LABEL', 'mile_label'),
        ('BARRIER_TYPE', 'barrier_type'),
        ('TYPE', 'type'),
        ('CULVERTMATERIAL', 'material'), ('FMSS_CULVERT_TYPE', 'fmss_type'),
        ('FMSS_LOC', 'fmss_loc'), ('FMSS_ASSET', 'fmss_asset'),
        ('KEY_', 'key_code'), ('NOTES', 'notes'),
        ('length_ft', 'length_ft'), ('length_source', 'length_source'),
        ('mp_start', 'mp_start'), ('mp_end', 'mp_end'),
        ('side', 'side'),
        ('type', 'rpt_type'), ('barrier_material', 'barrier_material'),
        ('wall_material', 'wall_material'), ('rating', 'rating'),
        ('inspection_date', 'inspection_date'), ('speed_limit', 'speed_limit'),
        ('road_grade_pct', 'road_grade_pct'), ('hazard_behind', 'hazard_behind'),
        ('crashworthy', 'crashworthy'), ('test_level', 'test_level'),
        ('repair_action', 'repair_action'),
        ('photo', 'photo'), ('photos', 'photos'),
    ]:
        if src_key in props and props[src_key] not in (None, '', 'None'):
            attrs[dst_key] = props[src_key]
    # Endpoint metadata for culverts
    if kind == 'culvert':
        for ep_key in ('endpoint_1', 'endpoint_2'):
            if ep_key in props:
                attrs[ep_key] = props[ep_key]
        if '_tangent_bearing_deg' in props:
            attrs['road_bearing_deg'] = props['_tangent_bearing_deg']
    if attrs:
        pin['attrs'] = attrs
    return pin

# Build the bundle
bundle = {
    'version': 'v1',
    'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
    'description': 'RoadWalk pre-walk dataset for GRSM trial — 4 sections, ~540 ghost pins',
    'sections': [],
}
totals = {}
for meta in SECTIONS_META:
    sec_id = meta['id']
    align = load_alignment(os.path.join(DATA, meta['alignment_file']))
    pins = []
    pin_counter = {}  # per-kind counter for stable IDs
    for pattern, kind, source in PIN_SOURCES:
        path = os.path.join(DATA, pattern.format(lab=sec_id))
        if not os.path.exists(path): continue
        with open(path, encoding='utf-8') as f: d = json.load(f)
        for ft in d.get('features', []):
            pin_counter.setdefault(kind, 0)
            pin_counter[kind] += 1
            pin_id = f'{sec_id}-{kind[:3].upper()}-{pin_counter[kind]:03d}'
            pins.append(compact_pin(ft, kind, source, sec_id, pin_id))
    totals[sec_id] = {'pins': len(pins), 'by_kind': {k: pin_counter.get(k, 0) for k in set(p['kind'] for p in pins)}}
    bundle['sections'].append({
        'id': sec_id,
        'name': meta['name'],
        'project_code': meta['project_code'],
        'type': meta['type'],
        'mp_start': meta['mp_start'],
        'mp_end': meta['mp_end'],
        'pathweb_refs': meta['pathweb_refs'],
        'alignment': align,  # [lng, lat] pairs (GeoJSON convention)
        'pins': pins,
    })

out_path = os.path.join(DATA, 'prewalk-bundle.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(bundle, f, separators=(',', ':'))  # compact, no indent
size_kb = os.path.getsize(out_path) / 1024
print(f'Wrote {out_path}  ({size_kb:.1f} KB)')
print()
print('Per-section totals:')
for sec_id, t in totals.items():
    print(f'  Section {sec_id}: {t["pins"]} pins  {t["by_kind"]}')
print()
grand = sum(t['pins'] for t in totals.values())
print(f'Grand total: {grand} pins across 4 sections')
