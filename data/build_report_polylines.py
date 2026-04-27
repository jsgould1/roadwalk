"""
Generate LineString geometries for GIP guardrails and WIP walls.

For each asset:
  - Project asset MP onto its section's alignment (giving STA → centerline lat/lng).
  - Offset perpendicular by side_offset_ft in the L or R direction from travel.
  - Walk along the road tangent for length_ft to produce the endpoint.
  - Emit a LineString with start, end, and metadata.

Length source:
  1. If a Tier 3 OCR extraction exists (gip-tier3-details.json / wip-tier3-details.json)
     AND contains length_ft for this asset → use real length, flag `length_source: 'ocr_verified'`.
  2. Else → use default estimate, flag `length_source: 'estimated_pending_ocr'`.

Re-run this script any time to refresh geometries once OCR produces new data.
"""
import json, math, os

DATA = os.path.dirname(os.path.abspath(__file__))

# Defaults when OCR length isn't available
DEFAULT_LENGTH_GIP_FT = 100.0   # guardrail
DEFAULT_LENGTH_WIP_FT = 50.0    # wall
# Lateral offset from road centerline (20 ft = ~one lane width past centerline,
# placing the symbol cleanly outside a 2-lane carriageway).
SIDE_OFFSET_FT = 20.0

R_FT = 20902231.0
def hav(a, b):
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R_FT*math.asin(math.sqrt(h))

def polyline_length_ft(coords):
    return sum(hav(coords[i], coords[i+1]) for i in range(len(coords)-1))

def point_and_tangent_at_distance(coords, dist_ft):
    """Return (lat, lng, tangent_radians) at arc-length `dist_ft` along the polyline."""
    cum = 0.0
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        seg = hav(a, b)
        if seg < 1e-9: continue
        if cum + seg >= dist_ft:
            t = (dist_ft - cum) / seg
            pt = (a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1]))
            # Tangent from a toward b
            lat1, lat2 = math.radians(a[0]), math.radians(b[0])
            dlon = math.radians(b[1] - a[1])
            x = math.sin(dlon) * math.cos(lat2)
            y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
            tangent = math.atan2(x, y)
            return pt[0], pt[1], tangent
        cum += seg
    # At or past end — use last segment's tangent
    a, b = coords[-2], coords[-1]
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return coords[-1][0], coords[-1][1], math.atan2(x, y)

def offset_latlng(pt, bearing_rad, dist_ft):
    R = R_FT
    lat1 = math.radians(pt[0])
    lon1 = math.radians(pt[1])
    d_over_R = dist_ft / R
    lat2 = math.asin(math.sin(lat1)*math.cos(d_over_R) + math.cos(lat1)*math.sin(d_over_R)*math.cos(bearing_rad))
    lon2 = lon1 + math.atan2(math.sin(bearing_rad)*math.sin(d_over_R)*math.cos(lat1),
                             math.cos(d_over_R) - math.sin(lat1)*math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))

def sta_fmt(sta_ft):
    return f'{int(sta_ft//100)}+{sta_ft%100:05.2f}'

# Section config — maps report route → section alignment + MP range
SECTIONS = {
    'A': {'file': 'section-A-gatlinburg-bypass.geojson',
          'report_routes': {'0012ZZ': (0.0, 3.747)},
          'mp_start': 0.0, 'mp_end': 3.747},
    'B': {'file': 'section-B-newfound-gap-NC-1.geojson',
          'report_routes': {'0010S': (14.64, 20.90)},
          'mp_start': 14.64, 'mp_end': 20.90},
    'C': {'file': 'section-C-newfound-gap-NC-2.geojson',
          'report_routes': {'0010S': (31.25, 31.96)},
          'mp_start': 31.25, 'mp_end': 31.96},
    'D': {'file': 'section-D-newfound-gap-TN.geojson',
          'report_routes': {'0010N': (1.86, 6.50)},
          'mp_start': 1.86, 'mp_end': 6.50},
}

# Load alignments
section_data = {}
for lab, cfg in SECTIONS.items():
    with open(os.path.join(DATA, cfg['file']), encoding='utf-8') as f: d = json.load(f)
    coords = [(c[1], c[0]) for c in d['features'][0]['geometry']['coordinates']]
    section_data[lab] = {'coords': coords, 'length_ft': polyline_length_ft(coords), 'cfg': cfg}

def load_ocr_lengths(ocr_file):
    """Return dict of asset_id -> {length_ft, type, material, rating, ...} if file exists."""
    path = os.path.join(DATA, ocr_file)
    if not os.path.exists(path): return {}
    with open(path, encoding='utf-8') as f: d = json.load(f)
    return d.get('assets', {})

def load_photo_index():
    """Return dict of asset_id -> [photo_filename, ...] if index exists."""
    path = os.path.join(DATA, 'asset-photos-index.json')
    if not os.path.exists(path): return {}
    with open(path, encoding='utf-8') as f: d = json.load(f)
    return d.get('assets', {})

PHOTO_INDEX = load_photo_index()

def build_polylines(report_key, default_length, feature_code, ocr_file):
    """Build per-section polyline GeoJSON for a report's target-route assets."""
    with open(os.path.join(DATA, f'{report_key}-asset-ids.json'), encoding='utf-8') as f:
        src = json.load(f)
    ocr_data = load_ocr_lengths(ocr_file)
    per_section = {lab: [] for lab in SECTIONS}
    length_sources = {'pdf_text_layer': 0, 'estimated_pending_ocr': 0}

    for aid, a in src['assets'].items():
        route = a['route']
        mp = a['mp']
        side = a['side']   # L / R / B / C
        # Find section
        section_label = None
        for lab, cfg in SECTIONS.items():
            if route in cfg['report_routes']:
                lo, hi = cfg['report_routes'][route]
                if lo <= mp <= hi:
                    section_label = lab
                    break
        if not section_label: continue

        sd = section_data[section_label]
        cfg = sd['cfg']
        # STA within section
        frac = (mp - cfg['mp_start']) / (cfg['mp_end'] - cfg['mp_start'])
        frac = max(0.0, min(1.0, frac))
        start_sta_ft = frac * sd['length_ft']

        # Length: extracted-from-PDF if available, else default estimate.
        # The extraction script (extract_report_details.py) writes the same
        # asset-keyed JSON shape this script already consumed, so no rename.
        ocr_rec = ocr_data.get(aid, {})
        if 'length_ft' in ocr_rec:
            length_ft = float(ocr_rec['length_ft'])
            length_source = 'pdf_text_layer'
        else:
            length_ft = default_length
            length_source = 'estimated_pending_ocr'
        length_sources[length_source] = length_sources.get(length_source, 0) + 1

        # --- Build curved polyline that follows road centerline curvature ---
        # Sample the centerline in small steps, project each sample to the
        # roadside offset, then walk along the OFFSET curve summing haversine
        # distances until we hit the feature's true length_ft. The result is
        # a multi-point LineString whose rendered arc length equals length_ft
        # — not a straight chord between projected start/end stations.
        #
        # Side sign in COMPASS-bearing space (0 = north, +π/2 = east, −π/2 = west):
        #   bearing − π/2  →  LEFT of direction of travel
        #   bearing + π/2  →  RIGHT of direction of travel
        # Reports define L/R relative to a driver facing the direction of
        # increasing MP. All four section alignments here run in the
        # increasing-MP direction (verified against NPS milepost markers),
        # so we can use the side directly.
        STEP_FT = 2.0
        if side == 'L':
            side_sign = -1
        elif side == 'R':
            side_sign = +1
        else:
            side_sign = 0  # both / center → no perpendicular offset

        def offset_at(sta_ft):
            lat_c, lng_c, tan = point_and_tangent_at_distance(sd['coords'], sta_ft)
            if side_sign == 0:
                return (lat_c, lng_c)
            bearing = tan + side_sign * math.pi / 2
            return offset_latlng((lat_c, lng_c), bearing, SIDE_OFFSET_FT)

        section_max_sta = sd['length_ft']
        offset_points = [offset_at(start_sta_ft)]
        accumulated = 0.0
        sta = start_sta_ft
        end_sta_ft = start_sta_ft

        while accumulated < length_ft and sta < section_max_sta:
            next_sta = min(sta + STEP_FT, section_max_sta)
            next_off = offset_at(next_sta)
            seg_len = hav(offset_points[-1], next_off)
            if seg_len < 1e-9:
                sta = next_sta
                end_sta_ft = next_sta
                continue
            if accumulated + seg_len >= length_ft:
                # Interpolate the final segment so total arc length is exact
                remaining = length_ft - accumulated
                t = remaining / seg_len
                last = offset_points[-1]
                interp = (last[0] + t * (next_off[0] - last[0]),
                          last[1] + t * (next_off[1] - last[1]))
                offset_points.append(interp)
                end_sta_ft = sta + (next_sta - sta) * t
                accumulated = length_ft
                break
            offset_points.append(next_off)
            accumulated += seg_len
            sta = next_sta
            end_sta_ft = next_sta

        # Degenerate guard: if the feature sits right at the section end and
        # we couldn't accumulate any length, force at least 2 distinct points
        # so the output is still a valid LineString.
        if len(offset_points) < 2:
            tail_sta = min(start_sta_ft + STEP_FT, section_max_sta)
            offset_points.append(offset_at(tail_sta))
            end_sta_ft = tail_sta

        start_pt = offset_points[0]
        end_pt = offset_points[-1]

        # Build properties
        props = {
            'asset_id': aid,
            'route': route,
            'mp_start': mp,
            'mp_end': round(mp + length_ft/5280.0, 4),
            'side': side,
            'length_ft': round(length_ft, 1),
            'length_source': length_source,
            '_feature_code': feature_code,
            '_section': section_label,
            '_sta_start_ft': round(start_sta_ft, 1),
            '_sta_start': sta_fmt(start_sta_ft),
            '_sta_end_ft': round(end_sta_ft, 1),
            '_sta_end': sta_fmt(end_sta_ft),
            '_side_offset_ft': SIDE_OFFSET_FT,
            '_reported_in': [report_key.upper()],
            '_report_cross_refs': {
                report_key.upper(): {
                    'asset_id': aid,
                    'index_pages': a['pages'],
                    'source_pdf': src['source'],
                }
            },
        }
        # If we have OCR fields, merge them
        for k, v in ocr_rec.items():
            if k in ('asset_id','route','mp','side','length_ft','_source_page'): continue
            props[k] = v
        if 'length_ft' in ocr_rec:
            props['_report_cross_refs'][report_key.upper()]['detail_page'] = ocr_rec.get('_source_page')

        # Attach condition-photo references (extracted from the PDF). Each
        # asset can have 1+ photos; the canonical one (no -N suffix) is
        # what the field UI shows by default.
        photo_files = PHOTO_INDEX.get(aid)
        if photo_files:
            props['photo'] = photo_files[0]
            if len(photo_files) > 1:
                props['photos'] = photo_files

        per_section[section_label].append({
            'type': 'Feature',
            'properties': props,
            'geometry': {
                'type': 'LineString',
                'coordinates': [[p[1], p[0]] for p in offset_points],
            },
        })

    # Write per-section files
    for lab, rows in per_section.items():
        rows.sort(key=lambda f: f['properties']['_sta_start_ft'])
        out_name = f'nps-{report_key}-{feature_code.lower()}s-section-{lab}.geojson'
        with open(os.path.join(DATA, out_name), 'w', encoding='utf-8') as f:
            json.dump({
                'type': 'FeatureCollection',
                'metadata': {
                    'source': src['source'],
                    'report': report_key.upper(),
                    'section': lab,
                    'count': len(rows),
                    'default_length_ft': default_length,
                    'side_offset_ft': SIDE_OFFSET_FT,
                    'length_source_counts': {
                        'pdf_text_layer': sum(1 for r in rows if r['properties']['length_source']=='pdf_text_layer'),
                        'estimated_pending_ocr': sum(1 for r in rows if r['properties']['length_source']=='estimated_pending_ocr'),
                    },
                },
                'features': rows,
            }, f, indent=2)
    return length_sources, per_section

print('Building GIP (guardrails) polylines...')
gip_sources, gip_sec = build_polylines('gip', DEFAULT_LENGTH_GIP_FT, 'GR', 'gip-tier3-details.json')
print(f'  length sources: {gip_sources}')
for lab, rows in gip_sec.items():
    print(f'  Section {lab}: {len(rows)} guardrail polylines')

print()
print('Building WIP (walls) polylines...')
wip_sources, wip_sec = build_polylines('wip', DEFAULT_LENGTH_WIP_FT, 'RW', 'wip-tier3-details.json')
print(f'  length sources: {wip_sources}')
for lab, rows in wip_sec.items():
    print(f'  Section {lab}: {len(rows)} wall polylines')

print()
print('Done. Re-run this script any time to refresh polylines from updated OCR data.')
