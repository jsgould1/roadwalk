# NOTICE: This is copyrighted material. It is not to be reused, redistributed, or used in training datasets without explicit permission from the author.
"""
Consolidate all alignments + ghost pins into a single JSON bundle for RoadWalk.

Output: prewalk-bundle.json (one file, compact format for embedding or fetch).

Schema v2 (2026-05) — additions over v1:
- Every pin carries a `ulid` field — canonical, immutable, time-sortable id.
  `id` (e.g. "A-CUL-001") remains the human-readable label and may be
  renumbered when new features are inserted in stationing order; the ULID
  never changes, so cross-references and IDB keys stay stable across renames.
- Three new feature kinds added from park-wide NPS Esri exports:
    * pavement     — polygon, from grsm-pavement.geojson + GRSM_PAVEMENT csv
    * overlook     — point, from GRSM_SCENIC_OVERLOOKS csv
    * ngs_monument — point, from NGS_MONUMENTS csv
  Each is spatial-filtered to within a per-kind corridor of one of the four
  section alignments (60 m for pavement/overlook, 200 m for monuments).
- Culvert attributes are enriched from GRSM_ROAD_CULVERTS csv by lat/lon
  proximity match (<=30 m). Pulls CULVERTMATERIAL, FMSS_ASSET, FMSS_LOC,
  FMSS_CULVERT_TYPE, ROAD, NOTES, OBJECTID, GlobalID.
- Bridge attributes are stripped to {name, short_name, road, facility_type}
  only. Per project scope, ratings / FCI / load tables are dropped --
  road-walk inspections don't act on those.

Bundle structure:
{
  "version": "v2",
  "generated_at": ISO timestamp,
  "sections": [
    {
      "id": "A",
      "name": "Gatlinburg Bypass (MP 0.00-4.64)",
      "project_code": "TN NP GRSM 12(4)",
      "type": "linear",
      "pathweb_refs": [...],
      "mp_start": 0.0,
      "mp_end": 4.64,
      "alignment": [[lng, lat], ...],
      "sub_alignments": [...ramps...],
      "pins": [
        {
          "id":   "A-CUL-001",                       # mutable label
          "ulid": "01HF8XK7M3QZJP2N4VBR5S6T7W",      # canonical id (v2)
          "kind": "culvert" | "sign" | "pavement" | ...
          "source": "nps-gis" | "gip" | "wip" | "ngs",
          "geometry": {...},
          "sta_ft": ..., "sta": "1+23",
          "status": "pending",
          "attrs": {...},
        }, ...
      ]
    }, ...
  ]
}
"""
import json, os, datetime, csv, math, secrets

DATA    = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(DATA, 'Reports')


# -- ULID minting -----------------------------------------------------------
# 26-char Crockford base32 (10 chars timestamp + 16 chars secure-random).
# Lexicographic sort = chronological order. Compatible with the JS makeULID
# implementation in roadwalk.html.
_ULID_CHARS = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

def make_ulid():
    t = int(datetime.datetime.utcnow().timestamp() * 1000)
    time_str = ''
    for _ in range(10):
        time_str = _ULID_CHARS[t % 32] + time_str
        t //= 32
    rand_bytes = secrets.token_bytes(16)
    rand_str = ''.join(_ULID_CHARS[b % 32] for b in rand_bytes)
    return time_str + rand_str


# -- Spatial helpers --------------------------------------------------------
def _haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters."""
    R = 6371000.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _point_to_segment_m(plat, plon, alat, alon, blat, blon):
    """Min distance (m) from point P to segment AB using local-tangent
    meters (good for sub-km segments at GRSM latitude)."""
    lat0_rad = math.radians((alat + blat) / 2.0)
    mx = 111320.0 * math.cos(lat0_rad)
    my = 111320.0
    px = (plon - alon) * mx; py = (plat - alat) * my
    bx = (blon - alon) * mx; by = (blat - alat) * my
    seg_sq = bx*bx + by*by
    if seg_sq < 1e-9:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px*bx + py*by) / seg_sq))
    return math.hypot(px - bx*t, py - by*t)


def _point_to_alignment_m(plat, plon, alignment_lnglat):
    """Min distance (m) from point P to a polyline (list of [lng, lat])."""
    if not alignment_lnglat or len(alignment_lnglat) < 2:
        return float('inf')
    best = float('inf')
    for i in range(len(alignment_lnglat) - 1):
        a_lng, a_lat = alignment_lnglat[i]
        b_lng, b_lat = alignment_lnglat[i+1]
        d = _point_to_segment_m(plat, plon, a_lat, a_lng, b_lat, b_lng)
        if d < best: best = d
    return best


def _bearing_deg(lng1, lat1, lng2, lat2):
    """Forward azimuth in degrees (0=N, 90=E) from (lng1,lat1) to (lng2,lat2)."""
    lat1r = math.radians(lat1); lat2r = math.radians(lat2)
    dlng  = math.radians(lng2 - lng1)
    x = math.sin(dlng) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlng)
    return math.degrees(math.atan2(x, y)) % 360


def _road_bearing_at(lat, lng, alignment_lnglat):
    """Forward bearing of the alignment at the point nearest (lat, lng)."""
    if not alignment_lnglat or len(alignment_lnglat) < 2:
        return 0.0
    best_d = float('inf'); best_i = 0
    for i in range(len(alignment_lnglat) - 1):
        a_lng, a_lat = alignment_lnglat[i]
        b_lng, b_lat = alignment_lnglat[i + 1]
        d = _point_to_segment_m(lat, lng, a_lat, a_lng, b_lat, b_lng)
        if d < best_d:
            best_d = d; best_i = i
    a_lng, a_lat = alignment_lnglat[best_i]
    b_lng, b_lat = alignment_lnglat[best_i + 1]
    return _bearing_deg(a_lng, a_lat, b_lng, b_lat)


def _orient_linestring_with_road(coords_lnglat, alignment_lnglat):
    """Return coords_lnglat (possibly reversed) so the LineString runs in the
    same direction as the road (low-STA → high-STA).

    The JS viewer's _perpOffsetCoords derives the perpendicular direction from
    the feature's own coordinate order.  If the GIS feature was digitised
    high-STA→low-STA the perpendicular flips and the feature lands on the
    wrong side of the road.  This function corrects the coordinate ORDER only;
    the `side` attribute (L/R from NPS data) is NOT changed.

    coords_lnglat: list of [lng, lat] pairs (GeoJSON order)
    alignment_lnglat: section alignment list of [lng, lat] pairs
    Returns the original list unchanged, or a new reversed list.
    """
    if not coords_lnglat or len(coords_lnglat) < 2 or not alignment_lnglat:
        return coords_lnglat
    # Feature midpoint (for road-bearing lookup)
    mid_idx = max(1, len(coords_lnglat) // 2)
    mid = coords_lnglat[mid_idx]
    mid_lat, mid_lng = mid[1], mid[0]
    # Feature bearing: start → end vertex
    a, z = coords_lnglat[0], coords_lnglat[-1]
    if a == z:          # degenerate (single unique point)
        return coords_lnglat
    feat_bear = _bearing_deg(a[0], a[1], z[0], z[1])
    # Road bearing at the feature's midpoint
    road_bear = _road_bearing_at(mid_lat, mid_lng, alignment_lnglat)
    diff = (feat_bear - road_bear) % 360
    if 90 < diff < 270:
        return list(reversed(coords_lnglat))
    return coords_lnglat


def _project_sta_ft(plat, plon, alignment_lnglat):
    """Return arc-length (feet) along polyline of the closest projection."""
    if not alignment_lnglat or len(alignment_lnglat) < 2:
        return None
    M_TO_FT = 3.28084
    best_d = float('inf'); best_sta = None
    cum_m = 0.0
    for i in range(len(alignment_lnglat) - 1):
        a_lng, a_lat = alignment_lnglat[i]
        b_lng, b_lat = alignment_lnglat[i+1]
        lat0_rad = math.radians((a_lat + b_lat) / 2.0)
        mx = 111320.0 * math.cos(lat0_rad)
        my = 111320.0
        px = (plon - a_lng) * mx; py = (plat - a_lat) * my
        bx = (b_lng - a_lng) * mx; by = (b_lat - a_lat) * my
        seg_sq = bx*bx + by*by
        seg_len = math.sqrt(seg_sq) if seg_sq > 0 else 0.0
        if seg_sq < 1e-9:
            d = math.hypot(px, py); t = 0.0
        else:
            t = max(0.0, min(1.0, (px*bx + py*by) / seg_sq))
            d = math.hypot(px - bx*t, py - by*t)
        if d < best_d:
            best_d = d
            best_sta = (cum_m + seg_len * t) * M_TO_FT
        cum_m += seg_len
    return best_sta


def _fmt_sta(sta_ft):
    """Format arc-length in feet as a railroad-style station label."""
    if sta_ft is None: return None
    sta_int  = int(sta_ft // 100)
    sta_frac = sta_ft - sta_int * 100
    return '%d+%05.2f' % (sta_int, sta_frac)


# -- CSV loading ------------------------------------------------------------
def _load_csv(filename):
    """Load a CSV exported from NPS Esri. Uses utf-8-sig so the leading BOM
    that ArcGIS embeds doesn't poison the first column header (X→\\ufeffX)."""
    path = os.path.join(REPORTS, filename)
    if not os.path.exists(path):
        print('  ! CSV not found: ' + filename)
        return []
    with open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def _f(v):
    """Parse CSV string -> float; blank/invalid -> None."""
    if v is None or v == '': return None
    try: return float(v)
    except (ValueError, TypeError): return None


def _csv_to_points(rows, lng_col='X', lat_col='Y'):
    """Extract (lng, lat, row) tuples from CSV rows that have valid coordinates."""
    out = []
    for r in rows:
        lat = _f(r.get(lat_col)); lng = _f(r.get(lng_col))
        if lat is not None and lng is not None and (lat != 0 or lng != 0):
            out.append((lng, lat, r))
    return out


# -- Culvert enrichment -----------------------------------------------------
# Match a bundle culvert pin to the nearest GRSM_ROAD_CULVERTS row within
# 30 m and copy in the columns the existing GeoJSON doesn't already carry.
CULVERT_FIELD_MAP = {
    'CULVERTMATERIAL':   'material',
    'FMSS_ASSET':        'fmss_asset',
    'FMSS_LOC':          'fmss_loc',
    'FMSS_CULVERT_TYPE': 'fmss_type',
    'ROAD':              'road',
    'NOTES':             'csv_notes',  # keep separate from existing notes
    'GlobalID':          'global_id',
    'OBJECTID':          'object_id',
}


def enrich_culvert_attrs(attrs, geom, csv_points, threshold_m=30):
    """Look up the nearest CSV culvert to this pin's representative point
    and copy in FMSS / material / road / notes attrs.

    Culverts in the per-section GeoJSONs are LineString features (across-road
    barrels) — we use the midpoint of the line as the representative point
    for CSV matching."""
    if not geom: return attrs
    gtype = geom.get('type')
    coords = geom.get('coordinates') or []
    if not coords: return attrs

    # Compute a representative (lng, lat) for the match
    if gtype == 'Point' and len(coords) >= 2:
        lng, lat = coords[0], coords[1]
    elif gtype == 'LineString' and len(coords) >= 2:
        # Use the segment midpoint — CSV culverts are point features, and the
        # GIS-Esri report point usually lands on the culvert's centerline.
        mid_idx = len(coords) // 2
        if mid_idx == 0: lng, lat = coords[0][0], coords[0][1]
        else:
            a, b = coords[mid_idx - 1], coords[mid_idx]
            lng = (a[0] + b[0]) / 2.0
            lat = (a[1] + b[1]) / 2.0
    else:
        return attrs

    best = None; best_d = threshold_m
    for clng, clat, row in csv_points:
        d = _haversine_m(lat, lng, clat, clng)
        if d < best_d:
            best_d = d; best = row
    if not best: return attrs
    for src, dst in CULVERT_FIELD_MAP.items():
        v = (best.get(src) or '').strip() if best.get(src) is not None else ''
        if v and v != 'None' and not attrs.get(dst):
            attrs[dst] = v
    attrs['_csv_match_dist_m'] = round(best_d, 2)
    return attrs


# -- Section metadata - canonical config (unchanged from v1) ---------------
SECTIONS_META = [
    {
        'id': 'A', 'name': 'Gatlinburg Bypass (MP 0.00-4.64)',
        'project_code': 'TN NP GRSM 12(4)', 'type': 'linear',
        'mp_start': 0.0, 'mp_end': 4.64,
        'alignment_file': 'section-A-gatlinburg-bypass.geojson',
        'sub_alignments_file': 'gatlinburg-bypass.geojson',
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
        'id': 'B', 'name': 'Newfound Gap Rd NC #1 (MP 14.64-20.90)',
        'project_code': 'NC NP GRSM 10S(2)', 'type': 'linear',
        'mp_start': 14.64, 'mp_end': 20.90,
        'alignment_file': 'section-B-newfound-gap-NC-1.geojson',
        'pathweb_refs': [
            {'id': 6407, 'role': 'mainline_S', 'mp_start': 14.98, 'mp_end': 31.96, 'note': 'full 0010S Pathweb section; this RoadWalk section covers MP 14.64-20.90'},
        ],
    },
    {
        'id': 'C', 'name': 'Newfound Gap Rd NC #2 (MP 31.25-31.96)',
        'project_code': 'NC NP GRSM 10S(2)', 'type': 'linear',
        'mp_start': 31.25, 'mp_end': 31.96,
        'alignment_file': 'section-C-newfound-gap-NC-2.geojson',
        'pathweb_refs': [
            {'id': 6407, 'role': 'mainline_S', 'mp_start': 14.98, 'mp_end': 31.96, 'note': 'same Pathweb section as B; RoadWalk section covers MP 31.25-31.96'},
        ],
    },
    {
        'id': 'D', 'name': 'Newfound Gap Rd TN (MP 1.86-6.50)',
        'project_code': 'TN NP GRSM 10N(4)', 'type': 'linear',
        'mp_start': 1.86, 'mp_end': 6.50,
        'alignment_file': 'section-D-newfound-gap-TN.geojson',
        'pathweb_refs': [
            {'id': 6406, 'role': 'mainline_N', 'mp_start': 0.0, 'mp_end': 14.98, 'note': 'full 0010N Pathweb section; this RoadWalk section covers MP 1.86-6.50'},
        ],
    },
]

# Per-kind per-section feature sources (unchanged from v1)
PIN_SOURCES = [
    ('nps-signs-section-{lab}.geojson',       'sign',        'nps-gis'),
    ('nps-mile-markers-section-{lab}.geojson','mile_marker', 'nps-gis'),
    ('nps-bridge-pt-section-{lab}.geojson',   'bridge',      'nps-gis'),
    ('nps-bridge-ln-section-{lab}.geojson',   'bridge_line', 'nps-gis'),
    ('nps-gates-section-{lab}.geojson',       'gate',        'nps-gis'),
    ('nps-parking-section-{lab}.geojson',     'parking',     'nps-gis'),
    ('nps-culverts-section-{lab}.geojson',    'culvert',     'nps-gis'),
    ('nps-gip-grs-section-{lab}.geojson',     'guardrail',   'gip'),
    ('nps-wip-rws-section-{lab}.geojson',     'wall',        'wip'),
]


def load_alignment(path):
    with open(path, encoding='utf-8') as f: d = json.load(f)
    return d['features'][0]['geometry']['coordinates']


# -- compact_pin: bundle pin extractor --------------------------------------
# Translates a per-section GeoJSON feature into the compact pin dict that
# lives in prewalk-bundle.json. Schema v2 adds the ULID, strips bridge data
# to name only, and runs culvert enrichment from the park-wide CSV.

# Tally of LineStrings reversed to match road direction (logged at end of run)
_reorient_count = {}


def compact_pin(ft, kind, source, section_id, pin_id, culvert_csv_points,
                alignment_lnglat=None):
    props = ft.get('properties', {})
    geom  = ft.get('geometry', {})

    # Normalise coordinate direction for guardrail/wall so the JS viewer's
    # _perpOffsetCoords pushes the feature to the correct side of the road.
    # The `side` attribute (L/R) is NOT changed — only vertex ORDER is fixed.
    if kind in ('guardrail', 'wall') and alignment_lnglat:
        if geom.get('type') == 'LineString':
            coords = geom.get('coordinates', [])
            oriented = _orient_linestring_with_road(coords, alignment_lnglat)
            if oriented is not coords:          # actually reversed
                geom = dict(geom)               # shallow copy — don't mutate original
                geom['coordinates'] = oriented
                _reorient_count[kind] = _reorient_count.get(kind, 0) + 1

    pin = {
        'id':       pin_id,
        'ulid':     make_ulid(),
        'kind':     kind,
        'source':   source,
        'status':   'pending',
        'geometry': geom,
    }

    # Bridge override: name + road only, drop ratings/FCI per project scope.
    if kind in ('bridge', 'bridge_line'):
        if '_sta_ft' in props: pin['sta_ft'] = props['_sta_ft']
        if '_sta'    in props: pin['sta']    = props['_sta']
        attrs = {}
        for src_key, dst_key in [('NAME', 'name'), ('SHORTNAME', 'short_name'),
                                 ('ROAD', 'road'), ('FACILITYTYPE', 'facility_type')]:
            if src_key in props and props[src_key] not in (None, '', 'None'):
                attrs[dst_key] = props[src_key]
        if '_reported_in' in props: pin['reported_in'] = props['_reported_in']
        if attrs:
            pin['attrs'] = attrs
        return pin

    # Common station / source-tracking fields
    if '_sta_ft' in props:           pin['sta_ft'] = props['_sta_ft']
    elif '_sta_start_ft' in props:   pin['sta_ft'] = props['_sta_start_ft']
    if '_sta' in props:              pin['sta']    = props['_sta']
    elif '_sta_start' in props:      pin['sta']    = props['_sta_start']
    if '_reported_in' in props:      pin['reported_in']  = props['_reported_in']
    if '_report_cross_refs' in props: pin['report_refs'] = props['_report_cross_refs']
    if 'asset_id' in props:          pin['asset_id'] = props['asset_id']

    # Type-specific compact attrs (same field map as v1)
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
        ('wall_material', 'wall_material'), ('wall_function', 'wall_function'),
        ('rating', 'rating'), ('repair_cost', 'repair_cost'),
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

    # Culvert enrichment from CSV (FMSS, material, type, notes)
    if kind == 'culvert' and culvert_csv_points:
        enrich_culvert_attrs(attrs, geom, culvert_csv_points)

    if attrs:
        pin['attrs'] = attrs
    return pin


# -- New-kind builders ------------------------------------------------------
NEW_KIND_CORRIDOR_M = {
    'pavement':     60,    # road-adjacent paved areas
    'overlook':     80,    # turn-outs can sit a bit further off the alignment
    'ngs_monument': 200,   # surveyor marks often set off-road at land corners
}


def _best_section_for(lat, lng, sections, max_m):
    """Pick the section whose alignment is nearest the point, within max_m."""
    best = None; best_d = max_m
    for sec in sections:
        d = _point_to_alignment_m(lat, lng, sec['_align_lnglat'])
        if d < best_d:
            best_d = d; best = sec
    return best, best_d


def _new_kind_point_pin(kind, source, lng, lat, attrs, sec):
    sta_ft = _project_sta_ft(lat, lng, sec['_align_lnglat'])
    return {
        'ulid':     make_ulid(),
        'kind':     kind,
        'source':   source,
        'status':   'pending',
        'geometry': {'type': 'Point', 'coordinates': [lng, lat]},
        'sta_ft':   round(sta_ft, 1) if sta_ft is not None else None,
        'sta':      _fmt_sta(sta_ft),
        'attrs':    attrs,
    }


def build_overlook_pins(overlook_pts, sections):
    pins_per_sec = {sec['id']: [] for sec in sections}
    for lng, lat, row in overlook_pts:
        sec, d = _best_section_for(lat, lng, sections, NEW_KIND_CORRIDOR_M['overlook'])
        if not sec: continue
        attrs = {}
        for src_key, dst_key in [('LOC_NAME', 'loc_name'), ('ALT_NAME', 'alt_name'),
                                  ('ROAD', 'road'), ('FACILITYTYPE', 'facility_type'),
                                  ('NOTES', 'notes'), ('ELEVATION', 'elevation'),
                                  ('OBJECTID', 'object_id'), ('GlobalID', 'global_id')]:
            v = (row.get(src_key) or '').strip() if row.get(src_key) is not None else ''
            if v: attrs[dst_key] = v
        attrs['_dist_from_alignment_ft'] = round(d * 3.28084, 1)
        pins_per_sec[sec['id']].append(
            _new_kind_point_pin('overlook', 'nps-gis', lng, lat, attrs, sec))
    return pins_per_sec


def build_monument_pins(monument_pts, sections):
    pins_per_sec = {sec['id']: [] for sec in sections}
    for lng, lat, row in monument_pts:
        sec, d = _best_section_for(lat, lng, sections, NEW_KIND_CORRIDOR_M['ngs_monument'])
        if not sec: continue
        attrs = {}
        for src_key, dst_key in [('PID', 'pid'), ('NAME', 'name'), ('MARKER', 'marker'),
                                  ('STABILITY', 'stability'), ('SETTING', 'setting'),
                                  ('STAMPING', 'stamping'), ('COUNTY', 'county'),
                                  ('ORTHO_HT', 'ortho_ht'), ('VERT_DATUM', 'vert_datum'),
                                  ('LAST_RECV', 'last_recv'), ('LAST_COND', 'last_cond'),
                                  ('STATE', 'state'), ('GlobalID', 'global_id')]:
            v = (row.get(src_key) or '').strip() if row.get(src_key) is not None else ''
            if v: attrs[dst_key] = v
        attrs['_dist_from_alignment_ft'] = round(d * 3.28084, 1)
        pins_per_sec[sec['id']].append(
            _new_kind_point_pin('ngs_monument', 'ngs', lng, lat, attrs, sec))
    return pins_per_sec


def build_pavement_pins(pavement_csv_rows, sections):
    """Pavement polygons live in grsm-pavement.geojson (park-wide). Match
    each polygon to the GRSM_PAVEMENT csv by OBJECTID for any attrs the
    GeoJSON properties don't already carry, then assign to the nearest
    section via polygon centroid."""
    pavement_geojson_path = os.path.join(DATA, 'grsm-pavement.geojson')
    pins_per_sec = {sec['id']: [] for sec in sections}
    if not os.path.exists(pavement_geojson_path):
        print('  ! grsm-pavement.geojson missing -- pavement kind skipped')
        return pins_per_sec
    with open(pavement_geojson_path, encoding='utf-8') as f:
        pav_geo = json.load(f)
    csv_by_oid = {}
    for r in pavement_csv_rows:
        oid = (r.get('OBJECTID') or '').strip()
        if oid: csv_by_oid[oid] = r

    for ft in pav_geo.get('features', []):
        geom = ft.get('geometry') or {}
        gtype = geom.get('type')
        coords = geom.get('coordinates') or []
        if gtype == 'Polygon':
            rings = [coords[0]] if coords else []
        elif gtype == 'MultiPolygon':
            rings = [c[0] for c in coords if c]
        else:
            continue
        if not rings or not rings[0]: continue

        # Pavement polygons (parking lots, turnouts) are large — their
        # centroid often sits 100s of meters from the road. Use min vertex
        # distance to the alignment instead, so any polygon whose EDGE
        # touches the corridor counts as belonging to that section.
        best_sec = None
        best_vert_d = NEW_KIND_CORRIDOR_M['pavement']
        for sec_cand in sections:
            for ring in rings:
                for lng, lat in ring:
                    d = _point_to_alignment_m(lat, lng, sec_cand['_align_lnglat'])
                    if d < best_vert_d:
                        best_vert_d = d; best_sec = sec_cand
                        if best_vert_d < 1: break
                if best_vert_d < 1: break
        if not best_sec: continue
        sec = best_sec; d = best_vert_d

        # STA gets computed from the polygon centroid (for SLD strip placement)
        # but assignment uses the vertex-edge distance (more accurate for big polys).
        clng = sum(p[0] for p in rings[0]) / len(rings[0])
        clat = sum(p[1] for p in rings[0]) / len(rings[0])
        sta_ft = _project_sta_ft(clat, clng, sec['_align_lnglat'])
        props = ft.get('properties', {})
        oid = str(props.get('OBJECTID', '')).strip()
        csv_row = csv_by_oid.get(oid, {})
        attrs = {}
        for src_key, dst_key in [('LOC_NAME', 'loc_name'), ('ROAD', 'road'),
                                  ('SURFACE', 'surface'), ('ACRES', 'acres'),
                                  ('NOTES', 'notes'), ('Shape__Area', 'shape_area'),
                                  ('Shape__Length', 'shape_length'),
                                  ('OBJECTID', 'object_id'), ('GlobalID', 'global_id')]:
            v = props.get(src_key)
            if v in (None, ''): v = csv_row.get(src_key)
            v = str(v).strip() if v is not None else ''
            if v: attrs[dst_key] = v
        attrs['_dist_from_alignment_ft'] = round(d * 3.28084, 1)
        pins_per_sec[sec['id']].append({
            'ulid':     make_ulid(),
            'kind':     'pavement',
            'source':   'nps-gis',
            'status':   'pending',
            'geometry': geom,
            'sta_ft':   round(sta_ft, 1) if sta_ft is not None else None,
            'sta':      _fmt_sta(sta_ft),
            'attrs':    attrs,
        })
    return pins_per_sec


# -- Main build -------------------------------------------------------------
print('Loading CSVs from Reports/ ...')
csv_culvert = _load_csv('GRSM_ROAD_CULVERTS (1).csv')
csv_pavement = _load_csv('GRSM_PAVEMENT (2).csv')
csv_overlook = _load_csv('GRSM_SCENIC_OVERLOOKS.csv')
csv_monument = _load_csv('NGS_MONUMENTS.csv')
print('  culverts=%d  pavement=%d  overlook=%d  monument=%d' %
      (len(csv_culvert), len(csv_pavement), len(csv_overlook), len(csv_monument)))

culvert_pts  = _csv_to_points(csv_culvert)
overlook_pts = _csv_to_points(csv_overlook)
monument_pts = _csv_to_points(csv_monument, lng_col='DEC_LON', lat_col='DEC_LAT')

# Pre-load section alignments so new-kind builders can spatial-filter against them
sections_alignments = []
for meta in SECTIONS_META:
    align = load_alignment(os.path.join(DATA, meta['alignment_file']))
    sections_alignments.append(dict(list(meta.items()) + [('_align_lnglat', align)]))

print('')
print('Building new-kind pins...')
overlook_per_sec = build_overlook_pins(overlook_pts, sections_alignments)
monument_per_sec = build_monument_pins(monument_pts, sections_alignments)
pavement_per_sec = build_pavement_pins(csv_pavement, sections_alignments)
for sec_id in [m['id'] for m in SECTIONS_META]:
    print('  %s: pavement=%d overlook=%d monument=%d' % (
        sec_id, len(pavement_per_sec[sec_id]),
        len(overlook_per_sec[sec_id]), len(monument_per_sec[sec_id])))

# Assemble bundle
bundle = {
    'version':      'v2',
    'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
    'description':  'RoadWalk pre-walk dataset for GRSM -- schema v2 (ULID + new kinds)',
    'sections':     [],
}
totals = {}

for meta in SECTIONS_META:
    sec_id = meta['id']
    align  = next(s['_align_lnglat'] for s in sections_alignments if s['id'] == sec_id)

    sub_alignments = []
    if 'sub_alignments_file' in meta:
        with open(os.path.join(DATA, meta['sub_alignments_file']), encoding='utf-8') as f:
            sa_data = json.load(f)
        for ft in sa_data.get('features', []):
            p = ft.get('properties', {})
            if p.get('role') == 'ramp':
                sub_alignments.append({
                    'role':       'ramp',
                    'name':       p.get('name', 'Ramp'),
                    'length_ft':  round(p.get('length_ft', 0), 1),
                    'coordinates': ft['geometry']['coordinates'],
                })

    # Existing per-section kinds (v1 flow + ULID + culvert enrich + bridge strip)
    pins = []
    pin_counter = {}
    for pattern, kind, source in PIN_SOURCES:
        path = os.path.join(DATA, pattern.format(lab=sec_id))
        if not os.path.exists(path): continue
        with open(path, encoding='utf-8') as f: d = json.load(f)
        for ft in d.get('features', []):
            pin_counter.setdefault(kind, 0)
            pin_counter[kind] += 1
            kind_prefix = kind[:3].upper()
            pin_id = '%s-%s-%03d' % (sec_id, kind_prefix, pin_counter[kind])
            pins.append(compact_pin(ft, kind, source, sec_id, pin_id, culvert_pts, align))

    # New kinds: assign sequential pin_ids in their own counter buckets
    for new_kind, per_sec_dict, kind_prefix in [
        ('pavement',     pavement_per_sec, 'PAV'),
        ('overlook',     overlook_per_sec, 'OVE'),
        ('ngs_monument', monument_per_sec, 'NGS'),
    ]:
        bucket = per_sec_dict[sec_id]
        bucket.sort(key=lambda p: (p.get('sta_ft') if p.get('sta_ft') is not None else 1e18))
        for i, pin in enumerate(bucket, start=1):
            pin['id'] = '%s-%s-%03d' % (sec_id, kind_prefix, i)
            pin_counter[new_kind] = i
            pins.append(pin)

    by_kind = {}
    for p in pins:
        k = p.get('kind', '?')
        by_kind[k] = by_kind.get(k, 0) + 1
    totals[sec_id] = {'pins': len(pins), 'by_kind': by_kind}

    bundle['sections'].append({
        'id':            sec_id,
        'name':          meta['name'],
        'project_code':  meta['project_code'],
        'type':          meta['type'],
        'mp_start':      meta['mp_start'],
        'mp_end':        meta['mp_end'],
        'pathweb_refs':  meta['pathweb_refs'],
        'alignment':     align,
        'sub_alignments': sub_alignments,
        'pins':          pins,
    })

# Write output
out_path = os.path.join(DATA, 'prewalk-bundle.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(bundle, f, separators=(',', ':'))
size_kb = os.path.getsize(out_path) / 1024
print('')
print('Wrote %s  (%.1f KB)' % (out_path, size_kb))
print('')
print('Per-section totals:')
for sec_id, t in totals.items():
    print('  Section %s: %d pins  %s' % (sec_id, t['pins'], t['by_kind']))
grand = sum(t['pins'] for t in totals.values())
print('')
print('Grand total: %d pins across %d sections' % (grand, len(SECTIONS_META)))
if _reorient_count:
    print('')
    print('Geometry orientation corrections (coord order reversed to match road direction):')
    for k, n in sorted(_reorient_count.items()):
        print('  %-12s %d reversed' % (k, n))
else:
    print('(no geometry orientation corrections applied)')
