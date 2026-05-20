"""Rebuild Section E's alignment by walking 8060 ft north along 0010S from
Section C's MP 31.25 anchor (= original 6600 ft + a 1460 ft extension so
the MP 30.00 anchor matches Pathweb's canonical MP 30.00 location).

This replaces the entire E alignment with one fresh, single-walk path
instead of trying to splice a mid-segment extension on top of the old
alignment. It also re-stations every Section E pin against the new
alignment by re-projecting each pin's geometry onto the new polyline
(simpler and more correct than additively shifting old sta_ft values).

Reads:
  data/grsm-roads.geojson
  data/section-C-newfound-gap-NC-2.geojson
  data/prewalk-bundle.json
Writes:
  data/section-E-newfound-gap-NC-3.geojson
  data/prewalk-bundle.json
  data/nps-*-section-E.geojson  (re-stationed)
"""
import json, math, os, datetime, glob

DATA = os.path.dirname(os.path.abspath(__file__))
TARGET_FT = 6600.0 + 1460.0     # 8060 ft = 1.527 mi
R_FT = 20902231.0


def hav_ft(a, b):
    la1, lo1 = math.radians(a[1]), math.radians(a[0])
    la2, lo2 = math.radians(b[1]), math.radians(b[0])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_FT * math.asin(math.sqrt(h))


def sta_fmt(sta_ft):
    whole = int(sta_ft // 100)
    rem = sta_ft - whole * 100
    return "%d+%05.2f" % (whole, rem)


def strip_z(c): return [c[0], c[1]]
def key(c, prec=6): return (round(c[0], prec), round(c[1], prec))
def feat_coords(f): return [strip_z(c) for c in f['geometry']['coordinates']]


def first_point_lnglat(geom):
    if not geom: return None
    t = geom.get('type'); c = geom.get('coordinates')
    if t == 'Point': return c[:2]
    if t == 'LineString': return c[0][:2]
    if t == 'MultiLineString': return c[0][0][:2]
    if t == 'Polygon': return c[0][0][:2]
    if t == 'MultiPolygon': return c[0][0][0][:2]
    return None


def sta_on_polyline(pt_lnglat, coords_lnglat):
    """Return (cum_ft, dist_ft) projecting [lng, lat] pt onto a [[lng, lat]] polyline."""
    pt_lat = pt_lnglat[1]
    cos_lat = math.cos(math.radians(pt_lat))
    def xy(p): return ((p[0] - pt_lnglat[0]) * cos_lat * 364000.0,
                       (p[1] - pt_lnglat[1]) * 364000.0)
    best_d, best_cum = float('inf'), 0.0
    cum = 0.0
    for i in range(len(coords_lnglat) - 1):
        a = coords_lnglat[i]; b = coords_lnglat[i + 1]
        seg_len = hav_ft(a, b)
        ax, ay = xy(a); bx, by = xy(b)
        vx, vy = bx - ax, by - ay
        denom = vx * vx + vy * vy
        if denom < 1e-9:
            cum += seg_len; continue
        t = max(0.0, min(1.0, (-ax * vx + -ay * vy) / denom))
        # Approximate projected point distance to pt:
        cx, cy = ax + t * vx, ay + t * vy
        d_m = math.hypot(cx, cy)
        if d_m < best_d:
            best_d = d_m
            best_cum = cum + t * seg_len
        cum += seg_len
    return best_cum, best_d


# --- Load roads + Section C anchor ---------------------------------------
roads = json.load(open(os.path.join(DATA, 'grsm-roads.geojson'), encoding='utf-8'))
sc = json.load(open(os.path.join(DATA, 'section-C-newfound-gap-NC-2.geojson'), encoding='utf-8'))
sc_start = sc['features'][0]['geometry']['coordinates'][0]      # MP 31.25 anchor
sc_second = sc['features'][0]['geometry']['coordinates'][1]
print('Section C MP 31.25 anchor:', sc_start)

feats = [f for f in roads['features']
         if (f['properties'].get('ROUTEID') or '').endswith('0010S')
         and f['geometry']['type'] == 'LineString'
         and len(f['geometry']['coordinates']) >= 2]
print('0010S linestrings:', len(feats))

endpoints = {}
for i, f in enumerate(feats):
    cs = feat_coords(f)
    endpoints.setdefault(key(cs[0]),  []).append((i, 'start'))
    endpoints.setdefault(key(cs[-1]), []).append((i, 'end'))

# Junction nearest Section C start
junction = None
best_d = 9e9
for k in endpoints:
    d = hav_ft([k[0], k[1]], sc_start)
    if d < best_d:
        best_d = d; junction = k
print('Junction at Section C MP 31.25: %s (%.2f ft)' % (junction, best_d))

# Walk NORTH from junction.
visited = set()
collected = [list(junction)]
cum = 0.0


def far_endpoint(fi, near_end):
    cs = feat_coords(feats[fi])
    return cs[0] if near_end == 'end' else cs[-1]


def pick_next(node):
    cands = []
    for fi, end in endpoints.get(node, []):
        if fi in visited: continue
        far = far_endpoint(fi, end)
        cands.append((hav_ft(far, sc_second), fi, end))
    if not cands: return None
    cands.sort(reverse=True)
    return cands[0]


cur_node = junction
while cum < TARGET_FT:
    picked = pick_next(cur_node)
    if not picked:
        print('  no more candidates at %s; stopped at cum=%.0f ft' % (cur_node, cum))
        break
    _, fi, end = picked
    cs = feat_coords(feats[fi])
    walk = list(reversed(cs)) if end == 'end' else list(cs)
    if key(walk[0]) == key(cur_node):
        walk = walk[1:]
    for v in walk:
        d = hav_ft(collected[-1], v)
        if cum + d > TARGET_FT + 30:
            t = (TARGET_FT - cum) / d
            interp = [collected[-1][0] + (v[0] - collected[-1][0]) * t,
                      collected[-1][1] + (v[1] - collected[-1][1]) * t]
            collected.append(interp)
            cum = TARGET_FT
            break
        collected.append(v)
        cum += d
    visited.add(fi)
    cur_node = key(walk[-1]) if collected else cur_node
    print('  +OBJ=%s cum=%.0f ft' % (feats[fi]['properties'].get('OBJECTID'), cum))
    if cum >= TARGET_FT:
        break
print('Walked %.1f ft  (%.3f mi)' % (cum, cum / 5280))

# Reverse so the alignment goes NORTH (MP 30.00) -> SOUTH (MP 31.25).
alignment = list(reversed(collected))
# Drop the south-most vertex if it duplicates the next-to-last
if len(alignment) >= 2 and hav_ft(alignment[-1], alignment[-2]) < 0.5:
    alignment.pop()
total_ft = sum(hav_ft(alignment[i], alignment[i + 1]) for i in range(len(alignment) - 1))
print('New alignment: %d vertices, %.1f ft' % (len(alignment), total_ft))

# --- Write the section-E geojson -----------------------------------------
nominal_ft = (31.25 - 30.00) * 5280
out_geo = {
    "_copyright": "NOTICE: This is copyrighted material. It is not to be reused, redistributed, or used in training datasets without explicit permission from the author.",
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {
            "role": "alignment",
            "section_label": "Section E",
            "project_code": "NC NP GRSM 10S(3)",
            "road_name": "Newfound Gap Road (NC side, segment 3)",
            "mp_start": 30.00,
            "mp_end": 31.25,
            "nominal_length_ft": round(nominal_ft, 1),
            "nominal_length_mi": 1.25,
            "clipped_length_ft": round(total_ft, 1),
            "clipped_length_mi": round(total_ft / 5280, 3),
            "length_discrepancy_ft": round(total_ft - nominal_ft, 1),
            "source": "grsm-roads.geojson ROUTEID=*0010S, walked north 8060 ft from Section C MP 31.25 anchor (= original 6600 + 1460 ft extension so MP 30.00 matches Pathweb's canonical MP 30.00)",
            "sta_0_00_at": "mp_start (MP 30.00, Pathweb-aligned)",
            "note": "STA 0+00 at MP 30.00; STA increases toward MP 31.25 (= Section C MP 31.25 start). North anchor extended +1460 ft on %s." %
                    datetime.datetime.utcnow().strftime('%Y-%m-%d'),
            "_extended_north_ft": 1460.0,
        },
        "geometry": {"type": "LineString", "coordinates": alignment},
    }],
}
e_path = os.path.join(DATA, 'section-E-newfound-gap-NC-3.geojson')
json.dump(out_geo, open(e_path, 'w', encoding='utf-8'), indent=2)
print('Wrote', e_path)

# --- Re-station Section E pins in the bundle by re-projecting onto the new alignment ---
bundle_path = os.path.join(DATA, 'prewalk-bundle.json')
bundle = json.load(open(bundle_path, encoding='utf-8'))
sec_e = next((s for s in bundle['sections'] if s['id'] == 'E'), None)
if not sec_e:
    raise SystemExit('Section E missing from bundle.')
sec_e['alignment'] = alignment
restationed = 0
for p in sec_e['pins']:
    g = p.get('geometry')
    pt = first_point_lnglat(g)
    if pt is None: continue
    new_sta, d = sta_on_polyline(pt, alignment)
    p['sta_ft'] = round(new_sta, 1)
    p['sta'] = sta_fmt(new_sta)
    if p.get('attrs'):
        p['attrs']['_dist_from_alignment_ft'] = round(d, 1)
    restationed += 1
sec_e['pins'].sort(key=lambda p: p.get('sta_ft', 0))
bundle['generated_at'] = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
json.dump(bundle, open(bundle_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('Re-stationed %d pins in bundle' % restationed)

# --- Re-station per-section feature files (nps-*-section-E.geojson) ------
shifted = 0
for fn in sorted(glob.glob(os.path.join(DATA, 'nps-*-section-E.geojson'))):
    g = json.load(open(fn, encoding='utf-8'))
    changed = False
    for ft in g.get('features', []):
        pt = first_point_lnglat(ft.get('geometry'))
        if pt is None: continue
        new_sta, d = sta_on_polyline(pt, alignment)
        pp = ft.setdefault('properties', {})
        pp['_sta_ft'] = round(new_sta, 1)
        pp['_sta'] = sta_fmt(new_sta)
        pp['_dist_from_alignment_ft'] = round(d, 1)
        changed = True
    if changed:
        g['features'].sort(key=lambda f: (f.get('properties') or {}).get('_sta_ft', 0))
        json.dump(g, open(fn, 'w', encoding='utf-8'), indent=2)
        shifted += 1
print('Re-stationed', shifted, 'per-section feature files.')
print('Done.')
