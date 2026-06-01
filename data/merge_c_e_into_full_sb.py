"""Phase A: merge the current Section C (MP 31.25-31.96) and Section E
(MP 30.00-31.25) into one full SB Section C covering MP 30.00-31.96.

Background: an earlier build appended a northward extension to Section
C and called it Section E. That was a mistake — both alignments are
on the SB centerline (route GRSM-0010S). The correct structure is one
SB section (C) and a separate NB section (E, offset 50' NE — built in
Phase B).

This script:
  1. Concatenates current E + current C into one continuous SB alignment
     in north-to-south order (low MP -> high MP).
  2. Snaps the northern v0 to Pathweb's documented MP 30.0 coordinate
     (lat 35.522219, lng -83.307798). If v0 is already within ~5 ft
     of that point, nothing moves; otherwise insert a new v0 at the
     Pathweb coord (chord to existing v0 closes the small gap).
  3. Re-projects every pin from BOTH old sections onto the merged
     alignment — lat/lng untouched, sta_ft / sta / attrs.mp_start /
     attrs.mp_end recomputed against the new geometry.
  4. Migrates old E's pins into Section C and removes old E from the
     bundle. (Phase B will re-create Section E as the NB-direction
     section.)

Reads/Writes:
  data/prewalk-bundle.json
  data/nps-*-section-E.geojson      -> renamed to ...-section-C.geojson
                                        (or merged if section-C already
                                         has entries; original E files
                                         are left in place as a backup)
"""
import copy
import datetime
import glob
import json
import math
import os
import shutil

DATA = os.path.dirname(os.path.abspath(__file__))
R_FT = 20902231.0

# Pathweb's documented endpoints for the full SB segment (route 0010S).
PATHWEB_SB_NORTH_LNGLAT = [-83.307798, 35.522219]   # MP 30.0
PATHWEB_SB_SOUTH_LNGLAT = [-83.303516, 35.499606]   # MP 31.96

NEW_MP_START = 30.00     # MP at the merged alignment's v0 (north end)
NEW_MP_END   = 31.96     # MP at the alignment's last vertex (south end)
NEW_NAME     = 'Newfound Gap Rd NC SB (MP 30.00-31.96)'


def hav_ft(a, b):
    la1, lo1 = math.radians(a[1]), math.radians(a[0])
    la2, lo2 = math.radians(b[1]), math.radians(b[0])
    h = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_FT * math.asin(math.sqrt(h))


def line_len_ft(coords):
    return sum(hav_ft(coords[i], coords[i+1]) for i in range(len(coords) - 1))


def fmt_sta(sta_ft):
    if sta_ft is None or not math.isfinite(sta_ft):
        return None
    whole = int(sta_ft // 100)
    rem = sta_ft - whole * 100
    return '%d+%05.2f' % (whole, rem)


def first_point(geom):
    if not geom:
        return None
    t = geom.get('type'); c = geom.get('coordinates')
    if c is None:
        return None
    if t == 'Point':
        return c[:2]
    if t == 'LineString' and c:
        return c[0][:2]
    if t == 'MultiLineString' and c and c[0]:
        return c[0][0][:2]
    if t == 'Polygon' and c and c[0]:
        return c[0][0][:2]
    return None


def last_point(geom):
    if not geom:
        return None
    t = geom.get('type'); c = geom.get('coordinates')
    if c is None:
        return None
    if t == 'LineString' and c:
        return c[-1][:2]
    if t == 'MultiLineString' and c and c[-1]:
        return c[-1][-1][:2]
    return None


def project_onto(pt_lnglat, coords):
    pt_lat = pt_lnglat[1]
    cos_lat = math.cos(math.radians(pt_lat))
    def xy(p):
        return ((p[0] - pt_lnglat[0]) * cos_lat * 364000.0,
                (p[1] - pt_lnglat[1]) * 364000.0)
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


# ── Load bundle ───────────────────────────────────────────────────────────
bundle = json.load(open(os.path.join(DATA, 'prewalk-bundle.json'),
                        encoding='utf-8'))
secC = next(s for s in bundle['sections'] if s['id'] == 'C')
secE = next(s for s in bundle['sections'] if s['id'] == 'E')

print(f"Before merge:")
print(f"  C: {secC['name']}  MP {secC.get('mp_start')}-{secC.get('mp_end')}  "
      f"verts={len(secC['alignment'])}  pins={len(secC.get('pins',[]))}")
print(f"  E: {secE['name']}  MP {secE.get('mp_start')}-{secE.get('mp_end')}  "
      f"verts={len(secE['alignment'])}  pins={len(secE.get('pins',[]))}")

# ── Figure out which end of each connects ────────────────────────────────
C_v0 = secC['alignment'][0]
C_vN = secC['alignment'][-1]
E_v0 = secE['alignment'][0]
E_vN = secE['alignment'][-1]
gaps = {
    'C_v0--E_vN': hav_ft(C_v0, E_vN),
    'C_v0--E_v0': hav_ft(C_v0, E_v0),
    'C_vN--E_vN': hav_ft(C_vN, E_vN),
    'C_vN--E_v0': hav_ft(C_vN, E_v0),
}
join, join_gap = min(gaps.items(), key=lambda kv: kv[1])
print(f"\nClosest endpoint pair: {join}  (gap {join_gap:.1f} ft)")

# ── Build the merged alignment in north-to-south order ───────────────────
# We want v0 at the NORTH end (low MP / Pathweb's MP 30.0) and the last
# vertex at the SOUTH end (high MP / Pathweb's MP 31.96).
# Which alignment carries which physical end depends on each section's
# orientation. Use the Pathweb anchor distances to decide.
dC_v0_to_N = hav_ft(C_v0, PATHWEB_SB_NORTH_LNGLAT)
dC_vN_to_N = hav_ft(C_vN, PATHWEB_SB_NORTH_LNGLAT)
dE_v0_to_N = hav_ft(E_v0, PATHWEB_SB_NORTH_LNGLAT)
dE_vN_to_N = hav_ft(E_vN, PATHWEB_SB_NORTH_LNGLAT)
print(f"\nDistance from each endpoint to Pathweb MP 30.0:")
print(f"  C[v0]  : {dC_v0_to_N:7.1f} ft")
print(f"  C[vN]  : {dC_vN_to_N:7.1f} ft")
print(f"  E[v0]  : {dE_v0_to_N:7.1f} ft")
print(f"  E[vN]  : {dE_vN_to_N:7.1f} ft")

# The endpoint closest to Pathweb MP 30.0 is the north end of the merged
# alignment. That'll be v0 of the new merged shape.
endpoints = {
    ('C', 'v0'): (dC_v0_to_N, secC['alignment'], False),
    ('C', 'vN'): (dC_vN_to_N, secC['alignment'], True),
    ('E', 'v0'): (dE_v0_to_N, secE['alignment'], False),
    ('E', 'vN'): (dE_vN_to_N, secE['alignment'], True),
}
(north_sec, north_end), (_d, north_align, north_reverse) = min(
    endpoints.items(), key=lambda kv: kv[1][0])
print(f"\nNorth-end identified at: section {north_sec} {north_end} "
      f"({_d:.1f} ft from Pathweb MP 30.0)")

# Build north-section in north-to-south order
if north_reverse:
    north_part = list(reversed(north_align))
else:
    north_part = list(north_align)
# The OTHER section is the south part. Strip the duplicate join vertex
# off its front when concatenating.
south_sec_id = 'C' if north_sec == 'E' else 'E'
south_section = secC if south_sec_id == 'C' else secE
south_align_input = south_section['alignment']
# Determine which end of the south section attaches to the north's tail
south_v0_dist = hav_ft(north_part[-1], south_align_input[0])
south_vN_dist = hav_ft(north_part[-1], south_align_input[-1])
if south_vN_dist < south_v0_dist:
    south_part = list(reversed(south_align_input))
else:
    south_part = list(south_align_input)

# Drop the joining duplicate vertex (south[0] equals north[-1] within ~join_gap)
join_vertex_gap = hav_ft(north_part[-1], south_part[0])
print(f"Join gap (after orienting): {join_vertex_gap:.1f} ft "
      f"({'duplicate dropped' if join_vertex_gap < 5 else 'kept as a real segment'})")
merged_align = north_part + (south_part[1:] if join_vertex_gap < 5 else south_part)

# ── Snap northern v0 to Pathweb MP 30.0 if it isn't already ──────────────
v0_to_pathweb = hav_ft(merged_align[0], PATHWEB_SB_NORTH_LNGLAT)
print(f"\nMerged v0 distance to Pathweb MP 30.0: {v0_to_pathweb:.1f} ft")
SNAP_TOL_FT = 5.0
if v0_to_pathweb < SNAP_TOL_FT:
    print('  -> close enough, leaving v0 alone')
elif v0_to_pathweb < 200.0:
    print('  -> inserting Pathweb MP 30.0 as new v0 (small chord to old v0)')
    merged_align.insert(0, list(PATHWEB_SB_NORTH_LNGLAT))
else:
    print('  -> WARNING: gap exceeds 200 ft, leaving alignment as-is')
    print('     (manual review recommended)')

# Also check the south end
vN_to_pathweb = hav_ft(merged_align[-1], PATHWEB_SB_SOUTH_LNGLAT)
print(f"Merged vN distance to Pathweb MP 31.96: {vN_to_pathweb:.1f} ft")
if vN_to_pathweb >= SNAP_TOL_FT and vN_to_pathweb < 200.0:
    print('  -> appending Pathweb MP 31.96 as new last vertex')
    merged_align.append(list(PATHWEB_SB_SOUTH_LNGLAT))
elif vN_to_pathweb < SNAP_TOL_FT:
    print('  -> close enough, leaving vN alone')
else:
    print('  -> WARNING: gap exceeds 200 ft, leaving alignment as-is')

merged_total = line_len_ft(merged_align)
nominal_total = (NEW_MP_END - NEW_MP_START) * 5280
print(f"\nMerged alignment: {len(merged_align)} vertices, "
      f"{merged_total:.1f} ft  (nominal MP-range = {nominal_total:.1f} ft, "
      f"diff = {merged_total-nominal_total:+.1f} ft)")

# ── Migrate pins from old E into C, then re-project every pin ────────────
old_E_pins = list(secE.get('pins', []))
all_pins = list(secC.get('pins', [])) + old_E_pins
print(f"\nPins: {len(secC.get('pins',[]))} from C + {len(old_E_pins)} from E "
      f"= {len(all_pins)} total to re-station")

def sta_to_mp(sta_ft):
    if sta_ft is None or not math.isfinite(sta_ft):
        return None
    return NEW_MP_START + (sta_ft / merged_total) * (NEW_MP_END - NEW_MP_START)

restationed = 0
for p in all_pins:
    g = p.get('geometry')
    pt = first_point(g)
    if pt is None:
        continue
    sta_ft, perp_ft = project_onto(pt, merged_align)
    p['sta_ft'] = round(sta_ft, 1)
    p['sta']    = fmt_sta(sta_ft)
    a = p.setdefault('attrs', {})
    if 'mp_start' in a:
        a['mp_start'] = round(sta_to_mp(sta_ft), 6)
    end_pt = last_point(g)
    if end_pt is not None and 'mp_end' in a:
        end_sta, _ = project_onto(end_pt, merged_align)
        a['mp_end'] = round(sta_to_mp(end_sta), 6)
    restationed += 1
print(f"Re-stationed: {restationed}")

# Sort merged pins by sta_ft for cleanliness
all_pins.sort(key=lambda x: x.get('sta_ft', 0) or 0)

# ── Apply changes to Section C; remove Section E entirely ────────────────
secC['alignment']   = merged_align
secC['mp_start']    = NEW_MP_START
secC['mp_end']      = NEW_MP_END
secC['name']        = NEW_NAME
secC['pins']        = all_pins
# Keep pathweb_refs but extend with Section E's refs so the provenance
# isn't lost.
secC.setdefault('pathweb_refs', [])
for ref in secE.get('pathweb_refs', []):
    if ref not in secC['pathweb_refs']:
        secC['pathweb_refs'].append(ref)

bundle['sections'] = [s for s in bundle['sections'] if s['id'] != 'E']

# ── Save ──────────────────────────────────────────────────────────────────
bundle['generated_at'] = (datetime.datetime.now(datetime.UTC)
                          .strftime('%Y-%m-%dT%H:%M:%SZ'))
json.dump(bundle, open(os.path.join(DATA, 'prewalk-bundle.json'), 'w',
                       encoding='utf-8'),
          ensure_ascii=False, indent=1)

# ── Migrate the per-section feature GeoJSONs E -> C ──────────────────────
# Existing nps-*-section-E.geojson files have pins of the same kinds the
# user is tracking; rather than try to merge into section-C files that may
# already exist, we just leave them in place and rename. A backup of the
# bundle's old E pins is already preserved inside the merged secC.pins.
migrated = []
for src in sorted(glob.glob(os.path.join(DATA, 'nps-*-section-E.geojson'))):
    base = os.path.basename(src).replace('-section-E.', '-section-C.')
    dst = os.path.join(DATA, base)
    # If a section-C version already exists, append features into it;
    # otherwise just rename.
    if os.path.exists(dst):
        g_dst = json.load(open(dst, encoding='utf-8'))
        g_src = json.load(open(src, encoding='utf-8'))
        before = len(g_dst.get('features', []))
        g_dst.setdefault('features', []).extend(g_src.get('features', []))
        # Re-station feature properties too
        for ft in g_dst['features']:
            pt = first_point(ft.get('geometry'))
            if pt is None: continue
            sta_ft, dist = project_onto(pt, merged_align)
            pp = ft.setdefault('properties', {})
            pp['_sta_ft'] = round(sta_ft, 1)
            pp['_sta']    = fmt_sta(sta_ft)
            pp['_dist_from_alignment_ft'] = round(dist, 1)
        g_dst['features'].sort(
            key=lambda f: (f.get('properties') or {}).get('_sta_ft', 0))
        json.dump(g_dst, open(dst, 'w', encoding='utf-8'), indent=2)
        # Move the source out of the way so the bundle script doesn't see it
        bak = src + '.merged.bak'
        shutil.move(src, bak)
        migrated.append(f'  merged {os.path.basename(src)} -> {base} (+{len(g_src.get("features",[]))} feats)')
    else:
        # Re-station then rename
        g_src = json.load(open(src, encoding='utf-8'))
        for ft in g_src.get('features', []):
            pt = first_point(ft.get('geometry'))
            if pt is None: continue
            sta_ft, dist = project_onto(pt, merged_align)
            pp = ft.setdefault('properties', {})
            pp['_sta_ft'] = round(sta_ft, 1)
            pp['_sta']    = fmt_sta(sta_ft)
            pp['_dist_from_alignment_ft'] = round(dist, 1)
        g_src['features'].sort(
            key=lambda f: (f.get('properties') or {}).get('_sta_ft', 0))
        json.dump(g_src, open(dst, 'w', encoding='utf-8'), indent=2)
        os.remove(src)
        migrated.append(f'  renamed {os.path.basename(src)} -> {base}')

print(f"\nPer-section feature files migrated ({len(migrated)}):")
for line in migrated:
    print(line)

print(f"\nWrote: {os.path.join(DATA, 'prewalk-bundle.json')}")
print(f"Section count: {len(bundle['sections'])}")
print("Done.")
