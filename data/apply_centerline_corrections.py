"""Merge a user-exported centerlines JSON back into prewalk-bundle.json.

Input format is the `roadwalk-centerlines-v1` export shape — one
{section_id, alignment: [[lat,lng]...]} object per section in the project.
This script:

  1. Loads the user's export from the path passed in.
  2. For every section in the export, finds the matching bundle section
     by id. Skips sections whose alignment is unchanged from the bundle.
  3. For changed sections: applies the new alignment (flipped to the
     [lng,lat] convention the bundle uses), updates mp_start/mp_end if
     the export carried different values, then re-projects every pin
     in that section onto the new alignment — sta_ft, sta string, and
     attrs.mp_start/mp_end refreshed.
  4. Writes the bundle back with a fresh generated_at stamp.

Pins past the new endpoints clamp to 0 / total_ft (consistent with how
the in-app importer handles them).
"""
import datetime
import json
import math
import os
import shutil
import sys

DATA = os.path.dirname(os.path.abspath(__file__))
BUNDLE_PATH = os.path.join(DATA, 'prewalk-bundle.json')
R_FT = 20902231.0

EXPORT_PATH = (
    sys.argv[1] if len(sys.argv) > 1
    else r'C:\Users\gouldj\Downloads\GRSM_Pavement_Preservation_2026__centerlines_2026-06-01T13-10-24.json'
)


def hav_ft(a, b):
    la1, lo1 = math.radians(a[1]), math.radians(a[0])
    la2, lo2 = math.radians(b[1]), math.radians(b[0])
    h = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_FT * math.asin(math.sqrt(h))


def line_len_ft(coords):
    return sum(hav_ft(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def fmt_sta(sta_ft):
    if sta_ft is None or not math.isfinite(sta_ft):
        return None
    whole = int(sta_ft // 100)
    rem = sta_ft - whole * 100
    return '%d+%05.2f' % (whole, rem)


def first_point(geom):
    if not geom: return None
    t = geom.get('type'); c = geom.get('coordinates')
    if c is None: return None
    if t == 'Point': return c[:2]
    if t == 'LineString' and c: return c[0][:2]
    if t == 'MultiLineString' and c and c[0]: return c[0][0][:2]
    if t == 'Polygon' and c and c[0]: return c[0][0][:2]
    return None


def last_point(geom):
    if not geom: return None
    t = geom.get('type'); c = geom.get('coordinates')
    if c is None: return None
    if t == 'LineString' and c: return c[-1][:2]
    if t == 'MultiLineString' and c and c[-1]: return c[-1][-1][:2]
    return None


def project_onto(pt, coords):
    """Project pt onto polyline coords (clamped). pt and coords in [lng,lat]."""
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


def alignments_equal(a, b, eps_ft=0.5):
    """True if both polylines have the same vertex count and every
    corresponding vertex pair is within eps_ft. Cheap diff to skip
    no-op sections."""
    if len(a) != len(b):
        return False
    for i in range(len(a)):
        if hav_ft(a[i], b[i]) > eps_ft:
            return False
    return True


# ── Load both files ──────────────────────────────────────────────────────
print(f'Reading export: {EXPORT_PATH}')
with open(EXPORT_PATH, encoding='utf-8') as f:
    export = json.load(f)
if export.get('_format') != 'roadwalk-centerlines-v1':
    raise SystemExit(
        'Unexpected _format: %r (need roadwalk-centerlines-v1)'
        % export.get('_format'))

print(f'Reading bundle: {BUNDLE_PATH}')
with open(BUNDLE_PATH, encoding='utf-8') as f:
    bundle = json.load(f)
sections_by_id = {s['id']: s for s in bundle['sections']}

# ── Walk every section in the export, apply if changed ──────────────────
applied  = []
skipped_same = []
skipped_missing = []
total_pins_reprojected = 0
total_pins_clamped = 0

for cl in export.get('centerlines', []):
    sid = cl.get('section_id')
    if not sid:
        continue
    sec = sections_by_id.get(sid)
    if not sec:
        skipped_missing.append(sid)
        continue
    new_align_latlng = cl.get('alignment') or []
    if len(new_align_latlng) < 2:
        skipped_missing.append(sid + ' (alignment too short)')
        continue

    # Export uses [lat,lng]; bundle uses [lng,lat]. Flip on the way in.
    new_align = [[v[1], v[0]] for v in new_align_latlng]
    old_align = sec.get('alignment') or []

    if alignments_equal(old_align, new_align):
        skipped_same.append(sid)
        continue

    old_total = line_len_ft(old_align) if len(old_align) >= 2 else 0
    new_total = line_len_ft(new_align)
    new_mp_start = cl.get('mp_start', sec.get('mp_start'))
    new_mp_end   = cl.get('mp_end',   sec.get('mp_end'))
    mp_range = (new_mp_end - new_mp_start) if (new_mp_start is not None and new_mp_end is not None) else None

    # Apply alignment + MP range
    sec['alignment'] = new_align
    if new_mp_start is not None: sec['mp_start'] = new_mp_start
    if new_mp_end   is not None: sec['mp_end']   = new_mp_end

    # Re-project every pin in this section.
    n_pins = 0; n_clamped = 0
    SLOP_FT = 1.0
    for p in sec.get('pins', []):
        g = p.get('geometry')
        pt = first_point(g)
        if pt is None:
            continue
        sta_ft, perp = project_onto(pt, new_align)
        p['sta_ft'] = round(sta_ft, 1)
        p['sta']    = fmt_sta(sta_ft)
        a = p.setdefault('attrs', {})
        if 'mp_start' in a and mp_range is not None and new_total > 0:
            a['mp_start'] = round(new_mp_start + (sta_ft / new_total) * mp_range, 6)
        end_pt = last_point(g)
        if end_pt is not None and 'mp_end' in a and mp_range is not None and new_total > 0:
            end_sta, _ = project_onto(end_pt, new_align)
            a['mp_end'] = round(new_mp_start + (end_sta / new_total) * mp_range, 6)
        n_pins += 1
        if sta_ft < SLOP_FT or sta_ft > new_total - SLOP_FT:
            n_clamped += 1
    # Sort pins by sta_ft so downstream renderers see them in monotonic order.
    sec['pins'].sort(key=lambda x: x.get('sta_ft') if x.get('sta_ft') is not None else 0)

    applied.append({
        'section_id':  sid,
        'name':        sec.get('name', ''),
        'old_verts':   len(old_align),
        'new_verts':   len(new_align),
        'old_total':   old_total,
        'new_total':   new_total,
        'delta_ft':    new_total - old_total,
        'pins':        n_pins,
        'clamped':     n_clamped,
    })
    total_pins_reprojected += n_pins
    total_pins_clamped += n_clamped

# ── Summary ──────────────────────────────────────────────────────────────
print(f'\n── Summary ──')
print(f'Sections in export: {len(export.get("centerlines", []))}')
print(f'Applied:            {len(applied)}')
print(f'Unchanged:          {len(skipped_same)}  ({", ".join(skipped_same) if skipped_same else "-"})')
print(f'Not in bundle:      {len(skipped_missing)}  ({", ".join(skipped_missing) if skipped_missing else "-"})')

for a in applied:
    print(f'\n  [{a["section_id"]}] {a["name"]!r}')
    print(f'    verts:     {a["old_verts"]} -> {a["new_verts"]}')
    print(f'    length:    {a["old_total"]:.1f} -> {a["new_total"]:.1f} ft  '
          f'(delta {a["delta_ft"]:+.1f} ft)')
    print(f'    pins:      {a["pins"]} re-projected, {a["clamped"]} clamped to an endpoint')

print(f'\nTotal pins re-projected: {total_pins_reprojected}')
print(f'Total pins clamped:      {total_pins_clamped}')

if not applied:
    print('\nNo changes to apply. Bundle untouched.')
    sys.exit(0)

# ── Backup + write ──────────────────────────────────────────────────────
backup_path = BUNDLE_PATH + '.bak'
shutil.copy(BUNDLE_PATH, backup_path)
print(f'\nBacked up old bundle to {os.path.basename(backup_path)}')

bundle['generated_at'] = (datetime.datetime.now(datetime.UTC)
                          .strftime('%Y-%m-%dT%H:%M:%SZ'))
with open(BUNDLE_PATH, 'w', encoding='utf-8') as f:
    json.dump(bundle, f, ensure_ascii=False, indent=1)
print(f'Wrote updated {os.path.basename(BUNDLE_PATH)}.')
print('Done.')
