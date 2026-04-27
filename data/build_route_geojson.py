"""
Stitch raw Overpass JSON into RoadWalk route GeoJSON files,
clipping each section's polyline to user-supplied NPS milepost endpoints.

Algorithm:
  1. Chain all ways of the requested road into a continuous polyline.
     (Includes ramps on bypass-style roads so MP endpoints that land on
     a ramp are still reachable.)
  2. For each section, snap its start and end coords to the nearest point
     on that polyline (snap = perpendicular foot of coord onto the closest
     polyline segment, not just the nearest vertex).
  3. Extract the polyline subsequence between the two snaps, inserting
     the snap points as new vertices at the start and end of the clip.
  4. Orient the clip so it flows from start->end in the same direction
     the user's coords imply.
  5. Emit a FeatureCollection with the clipped alignment + any ramps
     (for the bypass) + metadata (length, match distances, MP range).
"""

import json, math, os, sys

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- geo primitives ----------
R_FT = 20902231.0  # Earth radius in feet

def hav_ft(a, b):
    """Haversine distance in feet between (lat, lng) tuples."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2-lat1, lon2-lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 2 * R_FT * math.asin(math.sqrt(h))

def polyline_length_ft(coords):
    return sum(hav_ft(coords[i], coords[i+1]) for i in range(len(coords)-1))

def snap_to_polyline(pt, coords):
    """
    Return (nearest_lat, nearest_lng, seg_index, t, dist_ft).
    seg_index = index i such that the snap lies on segment coords[i] -> coords[i+1].
    t in [0, 1] along that segment (0 = at coords[i], 1 = at coords[i+1]).
    """
    # Project on each segment in a local equirectangular tangent plane.
    # Earth curvature is negligible over ~100 ft segments.
    best = None
    lat0 = pt[0]  # use point's latitude for approx
    cos_lat = math.cos(math.radians(lat0))
    def ll_to_xy(p):
        return ((p[1] - pt[1]) * cos_lat * 364000.0,   # ft per deg lon at this lat (approx)
                (p[0] - pt[0]) * 364000.0)             # ft per deg lat (approx)
    target_xy = (0.0, 0.0)
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        ax, ay = ll_to_xy(a)
        bx, by = ll_to_xy(b)
        vx, vy = bx-ax, by-ay
        wx, wy = target_xy[0]-ax, target_xy[1]-ay
        denom = vx*vx + vy*vy
        if denom <= 1e-9:
            continue
        t = max(0.0, min(1.0, (wx*vx + wy*vy) / denom))
        proj_lat = a[0] + t*(b[0]-a[0])
        proj_lng = a[1] + t*(b[1]-a[1])
        d = hav_ft(pt, (proj_lat, proj_lng))
        if best is None or d < best[4]:
            best = (proj_lat, proj_lng, i, t, d)
    return best

def clip_polyline(coords, pt_start, pt_end):
    """
    Clip polyline between the two points. Returns (clipped_coords, meta_dict).
    The returned polyline starts at pt_start's snap, ends at pt_end's snap.
    """
    s = snap_to_polyline(pt_start, coords)
    e = snap_to_polyline(pt_end, coords)
    if s is None or e is None:
        raise RuntimeError('Could not snap endpoint to polyline')
    # Determine traversal direction — from lower (seg_i, t) to higher
    def key(snap):
        return (snap[2], snap[3])
    if key(s) > key(e):
        s, e = e, s
        reversed_flag = True
    else:
        reversed_flag = False
    s_i, s_t = s[2], s[3]
    e_i, e_t = e[2], e[3]
    # Build the clip:
    #   - start with the snapped start point
    #   - walk through intermediate vertices (coords[s_i+1 .. e_i])
    #   - end with the snapped end point
    clip = [(s[0], s[1])]
    for i in range(s_i+1, e_i+1):
        if i <= e_i:
            clip.append(coords[i])
    clip.append((e[0], e[1]))
    if reversed_flag:
        clip = list(reversed(clip))
    meta = {
        'start_match_ft': round(s[4] if not reversed_flag else e[4], 1),
        'end_match_ft': round(e[4] if not reversed_flag else s[4], 1),
        'clipped_from_full_length_ft': round(polyline_length_ft(coords), 1),
        'clipped_length_ft': round(polyline_length_ft(clip), 1),
    }
    return clip, meta

# ---------- OSM chaining ----------
def load_ways(raw_path, name_substr=None, exclude_highways=('service',)):
    with open(raw_path) as f:
        raw = json.load(f)
    nodes = {n['id']: (n['lat'], n['lon']) for n in raw['elements'] if n['type']=='node'}
    ways = []
    for w in raw['elements']:
        if w['type'] != 'way': continue
        tags = w.get('tags', {})
        if tags.get('highway') in exclude_highways: continue
        nm = tags.get('name', '')
        if name_substr and name_substr.lower() not in nm.lower(): continue
        ways.append({
            'id': w['id'],
            'name': nm,
            'highway': tags.get('highway', ''),
            'lanes': tags.get('lanes', ''),
            'oneway': tags.get('oneway', ''),
            'node_ids': w['nodes'],
            'coords': [nodes[n] for n in w['nodes'] if n in nodes],
        })
    return ways

def chain_ways(ways, seed_coord):
    """
    Chain ways end-to-end, starting from the way whose endpoint is nearest
    to seed_coord. Returns (chained_coords, used_ids, unused_ways).
    Only ways that can be topologically linked are chained; the rest are returned.
    """
    if not ways:
        return [], [], []
    # Pick seed way and orientation
    best_d, seed_i, seed_at_head = float('inf'), 0, True
    for i, w in enumerate(ways):
        for at_head, idx in ((True, 0), (False, -1)):
            d = hav_ft(seed_coord, w['coords'][idx])
            if d < best_d:
                best_d, seed_i, seed_at_head = d, i, at_head
    seed = ways[seed_i]
    chained_node_ids = list(seed['node_ids']) if seed_at_head else list(reversed(seed['node_ids']))
    nodes_lookup = {}  # node_id -> coord
    for w in ways:
        for nid, c in zip(w['node_ids'], w['coords']):
            nodes_lookup[nid] = c
    used = {seed_i}
    # Walk forward from tail
    while True:
        tail_nid = chained_node_ids[-1]
        next_i, rev = None, False
        for i, w in enumerate(ways):
            if i in used: continue
            if w['node_ids'][0] == tail_nid:
                next_i, rev = i, False; break
            if w['node_ids'][-1] == tail_nid:
                next_i, rev = i, True; break
        if next_i is None: break
        used.add(next_i)
        add = list(reversed(ways[next_i]['node_ids'])) if rev else list(ways[next_i]['node_ids'])
        chained_node_ids.extend(add[1:])
    # Walk backward from head (unless we seeded at head of the correct-orientation way)
    while True:
        head_nid = chained_node_ids[0]
        prev_i, rev = None, False
        for i, w in enumerate(ways):
            if i in used: continue
            if w['node_ids'][-1] == head_nid:
                prev_i, rev = i, False; break
            if w['node_ids'][0] == head_nid:
                prev_i, rev = i, True; break
        if prev_i is None: break
        used.add(prev_i)
        add = list(reversed(ways[prev_i]['node_ids'])) if rev else list(ways[prev_i]['node_ids'])
        chained_node_ids = add[:-1] + chained_node_ids
    coords = [nodes_lookup[nid] for nid in chained_node_ids]
    used_ids = [ways[i]['id'] for i in sorted(used)]
    unused = [ways[i] for i in range(len(ways)) if i not in used]
    return coords, used_ids, unused

# ---------- section builder ----------
def build_section(raw_path, out_path, road_name, section_label,
                  start_coord, end_coord, mp_start, mp_end,
                  project_code, name_substr=None, include_ramps=True):
    ways = load_ways(raw_path, name_substr=name_substr)
    mainline_ways = [w for w in ways if w['oneway'] != 'yes' and w['highway'] != 'trunk_link']
    ramp_ways = [w for w in ways if w not in mainline_ways]

    # Chain the mainline relative to start_coord (seed) so the polyline runs
    # in the geographic direction we expect. Include ramps in the chaining
    # pool when include_ramps=True so MP endpoints that sit on ramps are
    # reachable.
    pool = (mainline_ways + ramp_ways) if include_ramps else mainline_ways
    full_coords, used_ids, unused = chain_ways(pool, start_coord)

    # Clip to the two user-supplied endpoints
    clip_coords, clip_meta = clip_polyline(full_coords, start_coord, end_coord)
    clip_len = polyline_length_ft(clip_coords)
    nominal_len_mi = abs(mp_end - mp_start)
    nominal_len_ft = nominal_len_mi * 5280

    features = []
    features.append({
        'type': 'Feature',
        'properties': {
            'role': 'alignment',
            'section_label': section_label,
            'project_code': project_code,
            'road_name': road_name,
            'mp_start': mp_start,
            'mp_end': mp_end,
            'nominal_length_ft': round(nominal_len_ft, 1),
            'nominal_length_mi': nominal_len_mi,
            'clipped_length_ft': round(clip_len, 1),
            'clipped_length_mi': round(clip_len/5280, 3),
            'length_discrepancy_ft': round(clip_len - nominal_len_ft, 1),
            'osm_ways_used': used_ids,
            'ways_used_count': len(used_ids),
            'start_match_ft': clip_meta['start_match_ft'],
            'end_match_ft': clip_meta['end_match_ft'],
            'sta_0_00_at': 'user_start_coord',
            'note': f'STA 0+00 at MP {mp_start}; STA increases toward MP {mp_end}',
        },
        'geometry': {
            'type': 'LineString',
            'coordinates': [[c[1], c[0]] for c in clip_coords],
        },
    })

    with open(out_path, 'w') as f:
        json.dump({
            'type': 'FeatureCollection',
            'features': features,
            'metadata': {
                'source': 'OpenStreetMap via Overpass API',
                'license': 'ODbL — OpenStreetMap contributors',
                'section_label': section_label,
                'project_code': project_code,
                'road_name': road_name,
                'mp_range': f'MP {mp_start} → {mp_end}',
                'user_start_coord': list(start_coord),
                'user_end_coord': list(end_coord),
                'unused_ways_in_fetch': len(unused),
            },
        }, f, indent=2)

    print(f'\n=== {section_label}  |  {project_code}  |  {road_name} ===')
    print(f'  MP {mp_start} → {mp_end}  (nominal {nominal_len_mi:.2f} mi)')
    print(f'  Clipped length: {clip_len:.0f} ft  ({clip_len/5280:.3f} mi)')
    print(f'  Discrepancy from nominal: {clip_len - nominal_len_ft:+.0f} ft')
    print(f'  Start coord match: {clip_meta["start_match_ft"]} ft off polyline')
    print(f'  End   coord match: {clip_meta["end_match_ft"]} ft off polyline')
    print(f'  OSM ways joined: {len(used_ids)}')
    print(f'  Wrote: {os.path.basename(out_path)}')

# ---------- run all four sections ----------
if __name__ == '__main__':
    # Gatlinburg Bypass — Section A
    build_section(
        raw_path=os.path.join(DATA_DIR, 'gatlinburg-bypass-raw.json'),
        out_path=os.path.join(DATA_DIR, 'section-A-gatlinburg-bypass.geojson'),
        road_name='Gatlinburg Bypass Road',
        section_label='Section A',
        project_code='TN NP GRSM 12(4)',
        start_coord=(35.72596, -83.51609),   # MP 0.00
        end_coord=(35.69161, -83.53264),     # MP 4.64
        mp_start=0.00, mp_end=4.64,
        name_substr='Gatlinburg Bypass Road',
        include_ramps=True,
    )

    # Section D — Newfound Gap Road, TN side
    build_section(
        raw_path=os.path.join(DATA_DIR, 'newfound-gap-raw.json'),
        out_path=os.path.join(DATA_DIR, 'section-D-newfound-gap-TN.geojson'),
        road_name='Newfound Gap Road (TN side)',
        section_label='Section D',
        project_code='TN NP GRSM 10N(4)',
        start_coord=(35.68303, -83.53397),   # MP 1.86
        end_coord=(35.63979, -83.49568),     # MP 6.50
        mp_start=1.86, mp_end=6.50,
        name_substr='Newfound Gap',
        include_ramps=False,
    )

    # Section B — Newfound Gap Road, NC side segment 1 (MP 14.64 → 20.90)
    build_section(
        raw_path=os.path.join(DATA_DIR, 'newfound-gap-raw.json'),
        out_path=os.path.join(DATA_DIR, 'section-B-newfound-gap-NC-1.geojson'),
        road_name='Newfound Gap Road (NC side, segment 1)',
        section_label='Section B',
        project_code='NC NP GRSM 10S(2)',
        start_coord=(35.61073, -83.42801),   # MP 14.64
        end_coord=(35.59818, -83.40199),     # MP 20.90
        mp_start=14.64, mp_end=20.90,
        name_substr='Newfound Gap',
        include_ramps=False,
    )

    # Section C — Newfound Gap Road, NC side segment 2 (MP 31.25 → 31.96)
    build_section(
        raw_path=os.path.join(DATA_DIR, 'newfound-gap-raw.json'),
        out_path=os.path.join(DATA_DIR, 'section-C-newfound-gap-NC-2.geojson'),
        road_name='Newfound Gap Road (NC side, segment 2)',
        section_label='Section C',
        project_code='NC NP GRSM 10S(2)',
        start_coord=(35.50575, -83.30234),   # MP 31.25
        end_coord=(35.49958, -83.30366),     # MP 31.96
        mp_start=31.25, mp_end=31.96,
        name_substr='Newfound Gap',
        include_ramps=False,
    )
