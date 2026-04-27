"""
Render unrolled aerial road strips for each RoadWalk section.

Hybrid sources:
  - Sections A & D : Nearmap Vert tiles, one strip per Nearmap survey/date
                      (5 winter "disaster" captures 2022-2026).
  - Sections B & C : Esri World Imagery (no Nearmap coverage), single baseline strip.

Output: data/aerial-strips/
  - {section}-{source}-{date}.jpg  (rectified strip; centerline runs horizontally)
  - manifest.json                  (lists strips per section + metadata)

Strip geometry:
  ALONG_PX_PER_FT controls along-track scale (image width).
  CROSS_PX_PER_FT controls cross-track scale (image height).
  Strip image width  = total_alignment_ft * ALONG_PX_PER_FT.
  Strip image height = STRIP_HALF_WIDTH_FT * 2 * CROSS_PX_PER_FT.
  Centerline sits at row = height/2; row 0 = LEFT of travel, row height-1 = RIGHT.
  Decoupled densities so source aspect matches the browser display aspect.

Pipeline per (section × capture):
  1. Walk alignment in arc-length steps.
  2. For each step, compute centerline lat/lng + tangent bearing.
  3. Sample N_CROSS pixels perpendicular to tangent, ±STRIP_HALF_WIDTH_FT.
  4. Look up each sample in a precomputed Web-Mercator tile mosaic for the corridor.
  5. Write the column into the output strip; advance to next step.

Re-run any time. Skips strips that already exist (delete the file to refresh).
"""
import json, os, math, sys, urllib.request, urllib.parse, ssl, time
from io import BytesIO
from collections import defaultdict
from PIL import Image

DATA = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DATA, 'aerial-strips')
os.makedirs(OUT, exist_ok=True)

# --- Config -----------------------------------------------------------------
"""
Strip resolution — decoupled along-track and cross-track densities.

The browser displays the strip at very different px/ft on each axis:
  along-track : ~4-5 px/ft  (250-ft window in a ~1100 px desktop canvas)
  cross-track : ~1 px/ft    (100-ft corridor in a 100 px tall aerial band)

Rendering source at uniform px/ft therefore stretches one axis and squashes
the other. Match each axis to the display:
  ALONG_PX_PER_FT = 5  → mild oversample of Nearmap z=20 native (~2.5 px/ft)
  CROSS_PX_PER_FT = 1  → matches the 100 px display height of the aerial band
"""
ALONG_PX_PER_FT = 5.0
CROSS_PX_PER_FT = 1.0
# Back-compat handle: tile-mosaic and lookup math still want a single density,
# but we're done with the "single px/ft" assumption everywhere else.
STRIP_PX_PER_FT = ALONG_PX_PER_FT
STRIP_HALF_WIDTH_FT = 50.0   # ±50 ft from centerline → 100 ft corridor
NEARMAP_ZOOM = 20            # ~0.6 ft/px → ample headroom over 2 px/ft strip
ESRI_ZOOM = 19               # Esri World Imagery typically maxes at z19 in this region
TILE_PX = 256
NEARMAP_KEY_PATH = os.environ.get('NEARMAP_KEY_PATH', r'C:\Dev\nearmapkey.txt')

SECTIONS = [
    ('A', 'section-A-gatlinburg-bypass.geojson'),
    ('B', 'section-B-newfound-gap-NC-1.geojson'),
    ('C', 'section-C-newfound-gap-NC-2.geojson'),
    ('D', 'section-D-newfound-gap-TN.geojson'),
]

# Coverage: which sections use which captures (from nearmap-coverage-discovery.json)
NEARMAP_CAPTURES = [
    ('2026-01-21', 'd5e3196b-2d7c-5ef2-ae02-adbeb8bd6ca7'),
    ('2025-02-23', '8ed0b6d6-001c-11f0-9bbb-ef7fa58adbb2'),
    ('2024-02-05', 'a9aae5ce-c9ec-11ee-be5a-f705530ead1c'),
    ('2023-01-28', 'd03e4eec-aca6-11ed-9ad2-0768e0ccbaf5'),
    ('2022-02-08', '57fe53b6-9675-11ec-aae5-8be525a8f6a2'),
]
NEARMAP_SECTIONS = {'A', 'D'}
ESRI_SECTIONS = {'B', 'C'}

# --- Nearmap key ------------------------------------------------------------
def read_key():
    if not os.path.exists(NEARMAP_KEY_PATH):
        sys.exit(f'Nearmap key not found at {NEARMAP_KEY_PATH}')
    with open(NEARMAP_KEY_PATH, encoding='utf-8') as f:
        return f.read().strip()
NEARMAP_KEY = read_key()

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# --- Geo math ---------------------------------------------------------------
R_FT = 20902231.0
def hav(a, b):
    """a, b: (lat, lng) in degrees. Returns distance in feet."""
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R_FT*math.asin(math.sqrt(h))

def polyline_length_ft(coords):
    return sum(hav(coords[i], coords[i+1]) for i in range(len(coords)-1))

def point_and_tangent_at_distance(coords, dist_ft):
    """coords: list of (lat, lng). dist_ft: arc length from start.
    Returns (lat, lng, tangent_bearing_radians)."""
    cum = 0.0
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        seg = hav(a, b)
        if seg < 1e-9: continue
        if cum + seg >= dist_ft:
            t = (dist_ft - cum) / seg
            lat = a[0] + t*(b[0]-a[0])
            lng = a[1] + t*(b[1]-a[1])
            lat1, lat2 = math.radians(a[0]), math.radians(b[0])
            dlon = math.radians(b[1] - a[1])
            x = math.sin(dlon) * math.cos(lat2)
            y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
            return lat, lng, math.atan2(x, y)
        cum += seg
    a, b = coords[-2], coords[-1]
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return coords[-1][0], coords[-1][1], math.atan2(x, y)

def offset_latlng(pt, bearing_rad, dist_ft):
    """pt: (lat, lng) deg. bearing_rad: 0 = north, +π/2 = east. Returns (lat, lng) deg."""
    R = R_FT
    lat1 = math.radians(pt[0])
    lon1 = math.radians(pt[1])
    d_over_R = dist_ft / R
    lat2 = math.asin(math.sin(lat1)*math.cos(d_over_R) +
                     math.cos(lat1)*math.sin(d_over_R)*math.cos(bearing_rad))
    lon2 = lon1 + math.atan2(math.sin(bearing_rad)*math.sin(d_over_R)*math.cos(lat1),
                             math.cos(d_over_R) - math.sin(lat1)*math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))

# --- Web Mercator (XYZ tile) projection -------------------------------------
def lnglat_to_pixel(lng, lat, zoom):
    """Web Mercator px coords at given zoom (origin top-left of world)."""
    n = 2.0 ** zoom
    x = (lng + 180.0) / 360.0 * n * TILE_PX
    s = math.sin(math.radians(lat))
    s = max(-0.9999, min(0.9999, s))
    y = (0.5 - math.log((1+s)/(1-s)) / (4*math.pi)) * n * TILE_PX
    return x, y

# --- Tile fetch + mosaic ----------------------------------------------------
def fetch_tile(url, retries=3, sleep=1.0):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'RoadWalk/1.0'})
            with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                return resp.read()
        except Exception as e:
            last_err = e
            time.sleep(sleep * (attempt + 1))
    raise last_err

def build_sparse_mosaic(corridor_pts, zoom, tile_url_fn):
    """corridor_pts: list of (lat, lng) covering the area to mosaic.
    Only fetches tiles that contain at least one corridor point — sparse coverage
    along the road, not the full bbox.

    Returns (tiles_dict, mosaic_lookup) where:
      tiles_dict: {(tx, ty): PIL.Image} for fetched tiles only
      mosaic_lookup: callable(world_px_x, world_px_y) -> (r, g, b) or None
    """
    # Identify needed tiles
    needed = set()
    for lat, lng in corridor_pts:
        wpx, wpy = lnglat_to_pixel(lng, lat, zoom)
        tx = int(wpx // TILE_PX)
        ty = int(wpy // TILE_PX)
        # Add this tile and immediate neighbours so cross-track sampling near
        # tile edges still finds pixels.
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                needed.add((tx + dx, ty + dy))
    n_tiles = len(needed)
    print(f'    sparse mosaic z={zoom}: {n_tiles} tiles needed', flush=True)
    tiles = {}
    fetched = 0
    failed = 0
    for (tx, ty) in sorted(needed):
        url = tile_url_fn(zoom, tx, ty)
        try:
            data = fetch_tile(url)
            tiles[(tx, ty)] = Image.open(BytesIO(data)).convert('RGB').load()
            fetched += 1
        except Exception as e:
            failed += 1
            if failed <= 5:
                print(f'      tile {zoom}/{tx}/{ty} failed: {e}', flush=True)
        if fetched % 50 == 0 and fetched > 0:
            print(f'      fetched {fetched}/{n_tiles}', flush=True)
    print(f'    mosaic done: {fetched}/{n_tiles} tiles ({failed} failed)', flush=True)
    def lookup(wpx, wpy):
        tx = int(wpx // TILE_PX)
        ty = int(wpy // TILE_PX)
        tile = tiles.get((tx, ty))
        if tile is None:
            return None
        px = int(wpx) - tx * TILE_PX
        py = int(wpy) - ty * TILE_PX
        if 0 <= px < TILE_PX and 0 <= py < TILE_PX:
            return tile[px, py]
        return None
    return lookup

# --- Strip rendering --------------------------------------------------------
def render_strip(coords, mosaic_lookup, zoom):
    """coords: list of (lat, lng) for the alignment.
    mosaic_lookup: callable(world_px_x, world_px_y) -> (r, g, b) or None.
    Returns a PIL Image of the unrolled strip.

    Output is sized at ALONG_PX_PER_FT × CROSS_PX_PER_FT — matched to the
    way the browser actually displays the strip on each axis."""
    total_ft = polyline_length_ft(coords)
    strip_w = max(2, int(total_ft * ALONG_PX_PER_FT))
    strip_h = max(2, int(STRIP_HALF_WIDTH_FT * 2 * CROSS_PX_PER_FT))
    print(f'    strip {strip_w}x{strip_h} ({total_ft:.0f} ft, '
          f'{ALONG_PX_PER_FT} along × {CROSS_PX_PER_FT} cross)', flush=True)
    strip = Image.new('RGB', (strip_w, strip_h), (0, 0, 0))
    dst_px = strip.load()
    half_h_ft = STRIP_HALF_WIDTH_FT
    # Precompute centerline samples + tangents (one per output column)
    samples = []
    for x in range(strip_w):
        sta_ft = min((x + 0.5) / ALONG_PX_PER_FT, total_ft)
        lat_c, lng_c, tan = point_and_tangent_at_distance(coords, sta_ft)
        samples.append((lat_c, lng_c, tan))
    # For each output column, sample cross-track
    for x in range(strip_w):
        lat_c, lng_c, tan = samples[x]
        for y in range(strip_h):
            # y=0 is top of image → LEFT of travel.
            cross_ft = ((y + 0.5) / CROSS_PX_PER_FT) - half_h_ft
            if cross_ft < 0:
                bearing = tan + math.pi/2
                d = -cross_ft
            elif cross_ft > 0:
                bearing = tan - math.pi/2
                d = cross_ft
            else:
                bearing = None
            if bearing is None:
                lat_p, lng_p = lat_c, lng_c
            else:
                lat_p, lng_p = offset_latlng((lat_c, lng_c), bearing, d)
            wpx, wpy = lnglat_to_pixel(lng_p, lat_p, zoom)
            rgb = mosaic_lookup(wpx, wpy)
            if rgb is not None:
                dst_px[x, y] = rgb
        if x % 1000 == 0 and x > 0:
            print(f'      col {x}/{strip_w}', flush=True)
    return strip

# --- Tile URL builders ------------------------------------------------------
def nearmap_url_fn(survey_id):
    # IMPORTANT: Nearmap's tiles/v3/Vert/{z}/{x}/{y} endpoint does NOT honor
    # ?surveyID=... or ?survey_id=... query parameters — it silently returns
    # the latest survey regardless. Targeting a specific survey requires the
    # path form  /tiles/v3/surveys/{surveyID}/Vert/{z}/{x}/{y}.jpg .  Discovered
    # the hard way after every year's strip rendered byte-identical.
    def fn(z, x, y):
        return (f'https://api.nearmap.com/tiles/v3/surveys/{survey_id}/Vert/'
                f'{z}/{x}/{y}.jpg?apikey={urllib.parse.quote(NEARMAP_KEY)}')
    return fn

def esri_url_fn():
    def fn(z, x, y):
        return (f'https://server.arcgisonline.com/ArcGIS/rest/services/'
                f'World_Imagery/MapServer/tile/{z}/{y}/{x}')
    return fn

# --- Sample corridor for mosaic bounds --------------------------------------
def corridor_sample_points(coords, step_ft=50.0):
    """Return points along the corridor (centerline + ±half_width) for bbox."""
    total = polyline_length_ft(coords)
    pts = []
    d = 0.0
    while d <= total:
        lat_c, lng_c, tan = point_and_tangent_at_distance(coords, d)
        # Pad a bit beyond half-width so mosaic covers all sample points.
        pad_ft = STRIP_HALF_WIDTH_FT + 25.0
        pts.append(offset_latlng((lat_c, lng_c), tan + math.pi/2, pad_ft))
        pts.append((lat_c, lng_c))
        pts.append(offset_latlng((lat_c, lng_c), tan - math.pi/2, pad_ft))
        d += step_ft
    if d - step_ft < total:
        # Make sure the very end is included
        lat_c, lng_c, tan = point_and_tangent_at_distance(coords, total)
        pts.append((lat_c, lng_c))
    return pts

# --- Per-section orchestration ----------------------------------------------
def load_section_coords(filename):
    with open(os.path.join(DATA, filename), encoding='utf-8') as f:
        d = json.load(f)
    # GeoJSON is [lng, lat]; we want (lat, lng) tuples
    return [(c[1], c[0]) for c in d['features'][0]['geometry']['coordinates']]

def process_section(section_id, alignment_file):
    print(f'\n=== Section {section_id} ===', flush=True)
    coords = load_section_coords(alignment_file)
    total_ft = polyline_length_ft(coords)
    print(f'  alignment: {len(coords)} pts, {total_ft:.0f} ft', flush=True)
    corridor = corridor_sample_points(coords, step_ft=50.0)
    results = []
    if section_id in NEARMAP_SECTIONS:
        for date, survey_id in NEARMAP_CAPTURES:
            out_name = f'{section_id}-nearmap-{date}.jpg'
            out_path = os.path.join(OUT, out_name)
            if os.path.exists(out_path):
                print(f'  [skip] {out_name} (already rendered)', flush=True)
                results.append({'date': date, 'source': 'nearmap', 'survey_id': survey_id, 'file': out_name})
                continue
            print(f'  Nearmap {date} ({survey_id[:8]}…)', flush=True)
            try:
                lookup = build_sparse_mosaic(corridor, NEARMAP_ZOOM, nearmap_url_fn(survey_id))
                strip = render_strip(coords, lookup, NEARMAP_ZOOM)
                strip.save(out_path, 'JPEG', quality=85)
                print(f'    wrote {out_name} ({os.path.getsize(out_path)//1024} KB)', flush=True)
                results.append({'date': date, 'source': 'nearmap', 'survey_id': survey_id, 'file': out_name})
            except Exception as e:
                print(f'    FAILED: {e}', flush=True)
    if section_id in ESRI_SECTIONS:
        out_name = f'{section_id}-esri-baseline.jpg'
        out_path = os.path.join(OUT, out_name)
        if os.path.exists(out_path):
            print(f'  [skip] {out_name} (already rendered)', flush=True)
            results.append({'date': None, 'source': 'esri', 'file': out_name})
        else:
            print(f'  Esri World Imagery baseline', flush=True)
            try:
                lookup = build_sparse_mosaic(corridor, ESRI_ZOOM, esri_url_fn())
                strip = render_strip(coords, lookup, ESRI_ZOOM)
                strip.save(out_path, 'JPEG', quality=85)
                print(f'    wrote {out_name} ({os.path.getsize(out_path)//1024} KB)', flush=True)
                results.append({'date': None, 'source': 'esri', 'file': out_name})
            except Exception as e:
                print(f'    FAILED: {e}', flush=True)
    return {
        'section': section_id,
        'alignment_length_ft': round(total_ft, 1),
        'strip_along_px_per_ft': ALONG_PX_PER_FT,
        'strip_cross_px_per_ft': CROSS_PX_PER_FT,
        'strip_half_width_ft': STRIP_HALF_WIDTH_FT,
        'captures': results,
    }

# --- Main -------------------------------------------------------------------
if __name__ == '__main__':
    only = set(sys.argv[1:]) if len(sys.argv) > 1 else None
    manifest = {
        'version': 'v1',
        'strip_along_px_per_ft': ALONG_PX_PER_FT,
        'strip_cross_px_per_ft': CROSS_PX_PER_FT,
        'strip_half_width_ft': STRIP_HALF_WIDTH_FT,
        'sections': {},
    }
    for sid, fn in SECTIONS:
        if only and sid not in only: continue
        manifest['sections'][sid] = process_section(sid, fn)
    manifest_path = os.path.join(OUT, 'manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2)
    print(f'\nWrote {manifest_path}', flush=True)
