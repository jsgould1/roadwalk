"""Phase B: build a new Section E (NB direction of the divided Newfound
Gap Rd) by offsetting Section C's SB centerline 50' to the NE and
clipping the result to Pathweb's documented NB MP range (30.445-31.59).

Why offset instead of using a real NB centerline:
  The user confirmed no NB-direction centerline exists in any GIS file
  we have. The two lanes are physically separated by a grass median;
  50' total separation (median + lane offset) is the user-supplied
  estimate. The geometry is good enough for surveying and asset
  tracking; future runs can replace with a real NB centerline by
  swapping the alignment field if better data becomes available.

Mechanics:
  1. Walk Section C's alignment vertex-by-vertex. At each, compute the
     forward bearing (segment to next vertex) and the perpendicular-left
     bearing (forward - 90 degrees compass). For a southbound road that
     left-perpendicular IS the northeast direction.
  2. Offset each vertex 50 ft in that perpendicular direction. The result
     is a parallel polyline ~50 ft northeast of SB.
  3. Project Pathweb's NB START (MP 31.59) and NB END (MP 30.445) coords
     onto the offset alignment. Those projections define where the
     real NB segment begins and ends on the offset line.
  4. Clip the offset polyline between those two stations and use the
     result as Section E's alignment.

Reads/Writes:
  data/prewalk-bundle.json   (Phase A has already merged C; this script
                              adds a fresh Section E)
"""
import datetime
import json
import math
import os

DATA = os.path.dirname(os.path.abspath(__file__))
R_FT = 20902231.0

# Offset distance from SB centerline to NB centerline (~50 ft including
# the grass median per user's estimate).
OFFSET_FT = 50.0

# Pathweb-documented NB endpoints (lat first in xls, normalized to
# [lng, lat] here).
NB_START_LNGLAT = [-83.299036, 35.502421]   # MP 31.59 (south, high MP)
NB_END_LNGLAT   = [-83.306835, 35.516103]   # MP 30.445 (north, low MP)

NB_MP_START = 30.445
NB_MP_END   = 31.59
NB_NAME     = 'Newfound Gap Rd NC NB (MP 30.45-31.59)'
NB_PROJECT  = 'NC NP GRSM 10S NB'


def hav_ft(a, b):
    la1, lo1 = math.radians(a[1]), math.radians(a[0])
    la2, lo2 = math.radians(b[1]), math.radians(b[0])
    h = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_FT * math.asin(math.sqrt(h))


def line_len_ft(coords):
    return sum(hav_ft(coords[i], coords[i+1]) for i in range(len(coords) - 1))


def project_onto(pt, coords):
    pt_lat = pt[1]
    cos_lat = math.cos(math.radians(pt_lat))
    def xy(p):
        return ((p[0] - pt[0]) * cos_lat * 364000.0,
                (p[1] - pt[1]) * 364000.0)
    best_d = float('inf'); best_cum = 0.0; cum = 0.0
    for i in range(len(coords) - 1):
        a = coords[i]; b = coords[i + 1]
        seg_len = hav_ft(a, b)
        ax, ay = xy(a); bx, by = xy(b)
        vx, vy = bx - ax, by - ay
        denom = vx * vx + vy * vy
        if denom < 1e-9:
            cum += seg_len; continue
        t = max(0.0, min(1.0, (-ax * vx + -ay * vy) / denom))
        cx, cy = ax + t * vx, ay + t * vy
        d = math.hypot(cx, cy)
        if d < best_d:
            best_d = d; best_cum = cum + t * seg_len
        cum += seg_len
    return best_cum, best_d


def point_at_sta(sta_ft, coords):
    """Return [lng, lat] of the point that's sta_ft from coords[0] along
    the polyline."""
    cum = 0.0
    for i in range(len(coords) - 1):
        a = coords[i]; b = coords[i + 1]
        seg_len = hav_ft(a, b)
        if cum + seg_len >= sta_ft:
            t = (sta_ft - cum) / seg_len if seg_len > 0 else 0
            return [a[0] + t * (b[0] - a[0]),
                    a[1] + t * (b[1] - a[1])]
        cum += seg_len
    return list(coords[-1])


def forward_bearing_deg(a, b):
    """Compass bearing from a to b (0 = North, 90 = East, clockwise).
    Treats short distances as a flat plane (good for vertex spacing here)."""
    lat0 = math.radians(a[1])
    cos_lat = math.cos(lat0)
    dx = (b[0] - a[0]) * cos_lat   # eastward in (deg)
    dy = (b[1] - a[1])             # northward
    # atan2(east, north) gives clockwise-from-north
    bearing = math.degrees(math.atan2(dx, dy))
    return (bearing + 360) % 360


def offset_point(p, perp_bearing_deg, dist_ft):
    """Move p by dist_ft in the given compass bearing direction."""
    br = math.radians(perp_bearing_deg)
    dx_ft = dist_ft * math.sin(br)        # east component
    dy_ft = dist_ft * math.cos(br)        # north component
    dlat = dy_ft / 364000.0
    cos_lat = math.cos(math.radians(p[1]))
    dlng = dx_ft / (364000.0 * cos_lat)
    return [p[0] + dlng, p[1] + dlat]


# ── Load bundle + merged Section C ───────────────────────────────────────
bundle = json.load(open(os.path.join(DATA, 'prewalk-bundle.json'),
                        encoding='utf-8'))
existing_ids = {s['id'] for s in bundle['sections']}
if 'E' in existing_ids:
    raise SystemExit('Section E already exists in bundle. Phase A should '
                     'have removed it. Aborting to avoid double-creation.')
secC = next(s for s in bundle['sections'] if s['id'] == 'C')
print(f"Source SB alignment (Section C): {len(secC['alignment'])} vertices, "
      f"{line_len_ft(secC['alignment']):.1f} ft")

# ── Compute the 50' NE offset polyline ───────────────────────────────────
sb_align = secC['alignment']
offset_align = []
for i, v in enumerate(sb_align):
    # Forward bearing uses the segment AHEAD of this vertex, except at
    # the last vertex where we re-use the previous segment's bearing.
    if i < len(sb_align) - 1:
        fwd = forward_bearing_deg(v, sb_align[i + 1])
    else:
        fwd = forward_bearing_deg(sb_align[i - 1], v)
    # Left-perpendicular of "forward" = forward - 90 deg (compass).
    # For a SB road that's NE — exactly what the user asked for.
    perp = (fwd - 90.0) % 360.0
    offset_align.append(offset_point(v, perp, OFFSET_FT))

print(f"Offset polyline (full length, before clipping): "
      f"{len(offset_align)} verts, {line_len_ft(offset_align):.1f} ft")

# ── Project Pathweb NB endpoints onto the offset polyline ────────────────
sta_north, d_north = project_onto(NB_END_LNGLAT,   offset_align)  # MP 30.445
sta_south, d_south = project_onto(NB_START_LNGLAT, offset_align)  # MP 31.59
print(f"\nPathweb NB endpoints projected onto offset polyline:")
print(f"  MP 30.445 (north): sta {sta_north:8.1f}, "
      f"{d_north:.1f} ft from offset polyline")
print(f"  MP 31.59  (south): sta {sta_south:8.1f}, "
      f"{d_south:.1f} ft from offset polyline")

# Ensure north < south
if sta_north > sta_south:
    sta_north, sta_south = sta_south, sta_north

# ── Clip the offset polyline to the NB station window ────────────────────
def clip_polyline(coords, sta_lo, sta_hi):
    """Return the polyline between sta_lo and sta_hi (inclusive) along
    coords, adding interpolated boundary vertices."""
    out = []
    cum = 0.0
    inserted_start = False
    for i in range(len(coords) - 1):
        a = coords[i]; b = coords[i + 1]
        seg_len = hav_ft(a, b)
        seg_start = cum
        seg_end = cum + seg_len
        if seg_end < sta_lo:
            cum = seg_end
            continue
        if seg_start > sta_hi:
            break
        # Interpolate boundary entry if this is the first segment overlapping
        if not inserted_start:
            if seg_start <= sta_lo <= seg_end:
                t = (sta_lo - seg_start) / seg_len if seg_len > 0 else 0
                out.append([a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1])])
            else:
                # sta_lo is before seg_start (only possible at very first
                # iteration when sta_lo == 0); use a as-is
                out.append(list(a))
            inserted_start = True
        # Push b unless we're going to interpolate exit
        if seg_end <= sta_hi:
            out.append(list(b))
        else:
            t = (sta_hi - seg_start) / seg_len if seg_len > 0 else 0
            out.append([a[0] + t*(b[0]-a[0]), a[1] + t*(b[1]-a[1])])
            break
        cum = seg_end
    return out

clipped = clip_polyline(offset_align, sta_north, sta_south)
print(f"\nClipped NB alignment: {len(clipped)} vertices, "
      f"{line_len_ft(clipped):.1f} ft  "
      f"(nominal MP-range = {(NB_MP_END-NB_MP_START)*5280:.1f} ft, "
      f"diff = {line_len_ft(clipped) - (NB_MP_END-NB_MP_START)*5280:+.1f})")

# ── Build Section E ──────────────────────────────────────────────────────
new_E = {
    'id': 'E',
    'name': NB_NAME,
    'type': 'linear',
    'project_code': NB_PROJECT,
    'mp_start': NB_MP_START,
    'mp_end':   NB_MP_END,
    'alignment': clipped,
    'pathweb_refs': [{
        'id': 12508,   # 0010N companion (placeholder; user can update)
        'role': 'mainline_NB',
        'mp_start': NB_MP_START,
        'mp_end':   NB_MP_END,
        'note': 'Synthetic offset 50 ft NE of Section C (SB) — no NB '
                'centerline available in source data.',
    }],
    'sub_alignments': [],
    'pins': [],
    '_aerial_choice': 'esri_aerial',
    '_synthetic_alignment': {
        'method': 'perpendicular_offset',
        'source_section_id': 'C',
        'offset_ft': OFFSET_FT,
        'offset_side': 'NE (left of SB direction of travel)',
    },
}
bundle['sections'].append(new_E)

# Sort sections back to A-Z order for cleanliness
bundle['sections'].sort(key=lambda s: s['id'])

# ── Save ──────────────────────────────────────────────────────────────────
bundle['generated_at'] = (datetime.datetime.now(datetime.UTC)
                          .strftime('%Y-%m-%dT%H:%M:%SZ'))
json.dump(bundle, open(os.path.join(DATA, 'prewalk-bundle.json'), 'w',
                       encoding='utf-8'),
          ensure_ascii=False, indent=1)

print(f"\nWrote Section E. Bundle now has {len(bundle['sections'])} sections.")
print(f"Done.")
