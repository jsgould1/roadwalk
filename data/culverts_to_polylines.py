"""
Regenerate culvert features as perpendicular LineStrings.

For each culvert Point:
  1. Find its STA along the section's alignment polyline.
  2. Compute the road's local tangent (bearing) at that STA.
  3. Emit a LineString perpendicular to the tangent, centered on the point.
     - Endpoints become endpoint_1 (tangent_side = +90°) and endpoint_2 (−90°).
  4. Store the original GIS point in properties for reference.

Total perpendicular length defaults to 40 ft (20 ft each side of centerline).
This covers a typical 2-lane road + shoulder; wider roads can be overridden
per section if needed.

Output overwrites nps-culverts-section-{A,B,C,D}.geojson with LineString features.
"""
import json, math, os, glob

DATA = os.path.dirname(os.path.abspath(__file__))
HALF_WIDTH_FT = 20.0  # 20 ft each side = 40 ft total span

R_FT = 20902231.0
def hav(a, b):
    la1, lo1 = math.radians(a[0]), math.radians(a[1])
    la2, lo2 = math.radians(b[0]), math.radians(b[1])
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2*R_FT*math.asin(math.sqrt(h))

def polyline_length_ft(coords):
    return sum(hav(coords[i], coords[i+1]) for i in range(len(coords)-1))

def nearest_segment(pt, coords):
    """Return (i, t) where (i,i+1) is the nearest segment and t∈[0,1] the projection position."""
    lat0 = pt[0]; cos_lat = math.cos(math.radians(lat0))
    def xy(p): return ((p[1]-pt[1])*cos_lat*364000.0, (p[0]-pt[0])*364000.0)
    best = (0, 0.0, float('inf'))
    for i in range(len(coords)-1):
        a, b = coords[i], coords[i+1]
        ax, ay = xy(a); bx, by = xy(b)
        vx, vy = bx-ax, by-ay
        denom = vx*vx + vy*vy
        if denom < 1e-9: continue
        t = max(0.0, min(1.0, (-ax*vx + -ay*vy)/denom))
        proj_lat = a[0] + t*(b[0]-a[0])
        proj_lng = a[1] + t*(b[1]-a[1])
        d = hav(pt, (proj_lat, proj_lng))
        if d < best[2]:
            best = (i, t, d)
    return best

def segment_bearing(a, b):
    """Forward bearing in radians from a (lat,lng) toward b (lat,lng)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return math.atan2(x, y)

def offset_latlng(pt, bearing_rad, dist_ft):
    """Move `dist_ft` from pt in direction bearing_rad. Returns (lat, lng)."""
    R = R_FT
    lat1 = math.radians(pt[0])
    lon1 = math.radians(pt[1])
    d_over_R = dist_ft / R
    lat2 = math.asin(math.sin(lat1)*math.cos(d_over_R) + math.cos(lat1)*math.sin(d_over_R)*math.cos(bearing_rad))
    lon2 = lon1 + math.atan2(math.sin(bearing_rad)*math.sin(d_over_R)*math.cos(lat1),
                             math.cos(d_over_R) - math.sin(lat1)*math.sin(lat2))
    return (math.degrees(lat2), math.degrees(lon2))

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

# Process each section's culvert file
totals = {}
for label, align_coords in sections.items():
    in_file = os.path.join(DATA, f'nps-culverts-section-{label}.geojson')
    with open(in_file, encoding='utf-8') as f: src = json.load(f)

    new_features = []
    for ft in src['features']:
        orig_coord = ft['geometry']['coordinates']  # [lng, lat]
        pt = (orig_coord[1], orig_coord[0])  # (lat, lng)
        # Find nearest alignment segment + tangent bearing
        i, t, _ = nearest_segment(pt, align_coords)
        a = align_coords[i]
        b = align_coords[i+1]
        tangent = segment_bearing(a, b)
        # Perpendicular bearings: +π/2 to one side, -π/2 to the other
        bearing_side1 = tangent + math.pi/2
        bearing_side2 = tangent - math.pi/2
        # Endpoints at perpendicular offset from the culvert's projected point
        proj_pt = (a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1]))
        endpoint_1 = offset_latlng(proj_pt, bearing_side1, HALF_WIDTH_FT)
        endpoint_2 = offset_latlng(proj_pt, bearing_side2, HALF_WIDTH_FT)
        # New props: preserve original + annotate endpoint metadata
        new_props = dict(ft['properties'])
        new_props['_geometry_source'] = 'synthesized_perpendicular_from_point'
        new_props['_perpendicular_length_ft'] = round(HALF_WIDTH_FT * 2, 1)
        new_props['_tangent_bearing_deg'] = round(math.degrees(tangent) % 360, 1)
        new_props['_original_point_lat'] = orig_coord[1]
        new_props['_original_point_lng'] = orig_coord[0]
        new_props['endpoint_1'] = {
            'lat': round(endpoint_1[0], 7),
            'lng': round(endpoint_1[1], 7),
            'role': 'unassigned (inlet or outlet)',
            'headwall': None,   # inspector fills in during walk
            'flange_end_section': None,
        }
        new_props['endpoint_2'] = {
            'lat': round(endpoint_2[0], 7),
            'lng': round(endpoint_2[1], 7),
            'role': 'unassigned (inlet or outlet)',
            'headwall': None,
            'flange_end_section': None,
        }
        new_features.append({
            'type': 'Feature',
            'properties': new_props,
            'geometry': {
                'type': 'LineString',
                'coordinates': [[endpoint_1[1], endpoint_1[0]], [endpoint_2[1], endpoint_2[0]]],
            },
        })

    out = dict(src)
    out['features'] = new_features
    out['metadata'] = {**src.get('metadata', {}),
                       'geometry_note': 'Culverts rendered as perpendicular LineStrings synthesized from GIS points + road tangent.',
                       'perpendicular_length_ft': HALF_WIDTH_FT * 2,
                       'endpoint_role_assignment': 'unassigned — field walk determines inlet vs outlet'}
    with open(in_file, 'w', encoding='utf-8') as f: json.dump(out, f, indent=2)
    totals[label] = len(new_features)

print('Culverts regenerated as LineStrings:')
for lab, n in totals.items():
    print(f'  Section {lab}: {n}')
print(f'  Total: {sum(totals.values())}')
