"""Convert an Avenza-export GeoPackage of culvert data into the chunked
RoadWalk AECOM features format for in-app import.

Reads:
  - Layer "FHWA _ EFL RFP _ GRSM Pave Pres 20251" — all 111 culverts.
  - Layer "Limits" — only rows whose name/description mentions culvert,
    headwall, pipe, etc. (skips MP marker pins).
  - Table "avenza_media" — pulls every photo whose (layer_id, feature_id)
    matches one of the culvert pins above.

Writes:
  - <basename>.main.json — pin metadata in AECOM features v1 format.
  - <basename>.geophotos.NNN.ndjson — base64 photo chunks of ~250 MB each.

Every pin and photo is stamped with an _import_batch_id so the in-app
Manage Imports panel can roll the whole import back with one tap.

Usage:
    python import_culverts_gpkg.py "C:\\path\\to\\file.gpkg"
"""
import argparse
import base64
import datetime
import json
import math
import os
import re
import secrets
import sqlite3
import struct
import sys
import time

# ── Constants ──────────────────────────────────────────────────────────────
SECTION_ID = 'A'                         # force every culvert onto Section A
TARGET_CHUNK_BYTES = 250 * 1024 * 1024   # ~250 MB per NDJSON chunk
WARN_PERP_FT = 500                       # >500 ft off Section A → log a warning

CULVERT_TABLES = ['FHWA _ EFL RFP _ GRSM Pave Pres 20251']
LIMITS_TABLE   = 'Limits'
LIMITS_CULVERT_RE = re.compile(
    r'(culvert|headwall|pipe|catch.basin|inlet|outlet|head\s*wall)',
    re.IGNORECASE
)

BUNDLE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'prewalk-bundle.json'
)
ULID_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'


# ── Helpers ────────────────────────────────────────────────────────────────
def make_ulid():
    """26-char Crockford base32 ULID, same shape the app's makeULID() emits."""
    ts_ms = int(time.time() * 1000)
    rand = secrets.token_bytes(10)
    val = (ts_ms << 80) | int.from_bytes(rand, 'big')
    return ''.join(ULID_ALPHABET[(val >> (i * 5)) & 0x1F] for i in range(25, -1, -1))


def parse_gpkg_point(blob):
    """Parse a GeoPackage Binary blob containing a Point or PointZ.
    Returns (lng, lat) or None on parse error."""
    if not blob or len(blob) < 16 or blob[0:2] != b'GP':
        return None
    offset = 8                           # GPB header w/ no envelope = 8 bytes
    byte_order = blob[offset]; offset += 1
    fmt = '<' if byte_order == 1 else '>'
    geom_type = struct.unpack(fmt + 'I', blob[offset:offset+4])[0]; offset += 4
    if geom_type == 1:                  # Point (2D)
        lng, lat = struct.unpack(fmt + 'dd', blob[offset:offset+16])
    elif geom_type == 1001:             # PointZ (3D — the Avenza shape)
        lng, lat, _z = struct.unpack(fmt + 'ddd', blob[offset:offset+24])
    else:
        return None
    return lng, lat


R_FT = 20902231.0
def hav_ft(a, b):
    la1, lo1 = math.radians(a[1]), math.radians(a[0])
    la2, lo2 = math.radians(b[1]), math.radians(b[0])
    h = (math.sin((la2 - la1) / 2) ** 2 +
         math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 2 * R_FT * math.asin(math.sqrt(h))


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
        if denom < 1e-9: cum += seg_len; continue
        t = max(0.0, min(1.0, (-ax * vx + -ay * vy) / denom))
        cx, cy = ax + t * vx, ay + t * vy
        d = math.hypot(cx, cy)
        if d < best_d: best_d = d; best_cum = cum + t * seg_len
        cum += seg_len
    return best_cum, best_d


def fmt_sta(sta_ft):
    if sta_ft is None or not math.isfinite(sta_ft): return None
    whole = int(sta_ft // 100); rem = sta_ft - whole * 100
    return '%d+%05.2f' % (whole, rem)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('gpkg_path')
    ap.add_argument('--out-dir', default=None,
                    help='Where to write the output files (defaults to the gpkg directory)')
    args = ap.parse_args()

    if not os.path.exists(args.gpkg_path):
        print(f'File not found: {args.gpkg_path}'); sys.exit(1)
    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.gpkg_path))
    basename = re.sub(r'[^A-Za-z0-9._-]', '_',
                      os.path.splitext(os.path.basename(args.gpkg_path))[0])[:32]

    now = datetime.datetime.now(datetime.UTC)
    batch_id = 'gpkg-' + now.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    source_filename = os.path.basename(args.gpkg_path)

    # Load Section A alignment for sta_ft projection + perp-distance check.
    bundle = json.load(open(BUNDLE_PATH, encoding='utf-8'))
    sec = next((s for s in bundle['sections'] if s['id'] == SECTION_ID), None)
    if not sec:
        print(f'Section {SECTION_ID} not in bundle'); sys.exit(1)
    align = sec['alignment']
    sec_name = sec['name']
    mp_start = sec['mp_start']; mp_end = sec['mp_end']
    sec_total_ft = sum(hav_ft(align[i], align[i+1]) for i in range(len(align)-1))
    mp_per_ft = (mp_end - mp_start) / sec_total_ft if sec_total_ft > 0 else 0
    print(f'Section {SECTION_ID}: {sec_name}')
    print(f'  alignment: {len(align)} verts, {sec_total_ft:.0f} ft, MP {mp_start}-{mp_end}')
    print(f'Batch id: {batch_id}\n')

    conn = sqlite3.connect(args.gpkg_path)
    cur = conn.cursor()

    pins = []
    pin_ulid_by_lf = {}        # (layer_id, feature_id) -> pin ulid
    far_warnings = []          # culverts > 500 ft from Section A

    # Pull the Avenza icon name out of OGR_STYLE so we can carry the
    # pin color/style intent forward — e.g. "red_pin" likely meant
    # "impaired" in the inspector's mental model.
    OGR_ICON_RE = re.compile(r'avenza-(?:url-http[^,]+?/|file-)([A-Za-z0-9_-]+?)(?:[._]|,ogr-)')
    def _parse_icon(style):
        if not style: return None
        m = OGR_ICON_RE.search(style)
        return m.group(1) if m else None

    def add_culvert(name, desc, lng, lat, layer_id, feature_id, source_table,
                    collected_at=None, ogr_style=None):
        pin_ulid = make_ulid()
        sta_ft, perp_ft = project_onto([lng, lat], align)
        attrs = {
            'source': 'AECOM',
            'name': (name or '').strip() or None,
            'notes': (desc or '').strip() or None,
            'collected_at': (collected_at or '').strip() or None,
            'avenza_icon': _parse_icon(ogr_style),
            '_import_batch_id': batch_id,
            '_import_source': source_filename,
            '_import_at': now_iso,
            '_avenza_layer_id': layer_id,
            '_avenza_feature_id': feature_id,
            '_avenza_table': source_table,
            'mp_start': round(mp_start + sta_ft * mp_per_ft, 6) if mp_per_ft else None,
        }
        attrs = {k: v for k, v in attrs.items() if v is not None and v != ''}
        pins.append({
            'ulid': pin_ulid,
            'kind': 'culvert',
            'geometry': {'type': 'Point', 'coordinates': [lng, lat]},
            'attrs': attrs,
            'status': 'pending',
            '_hint_section_id': SECTION_ID,
            '_hint_sta_ft': round(sta_ft, 1),
        })
        pin_ulid_by_lf[(layer_id, feature_id)] = pin_ulid
        if perp_ft > WARN_PERP_FT:
            far_warnings.append((name, desc, round(perp_ft)))

    # FHWA layer — every row is a culvert.
    for t in CULVERT_TABLES:
        try:
            cur.execute(
                f'SELECT geom, avenza_name, avenza_description, '
                f'avenza_datetime, OGR_STYLE, '
                f'avenza_layer_id, avenza_feature_id FROM "{t}";'
            )
        except sqlite3.Error as e:
            print(f'Skipping {t}: {e}'); continue
        n = 0
        for geom_blob, name, desc, dt, style, layer_id, feature_id in cur.fetchall():
            pt = parse_gpkg_point(geom_blob)
            if pt is None: continue
            add_culvert(name, desc, pt[0], pt[1], layer_id, feature_id, t,
                        collected_at=dt, ogr_style=style)
            n += 1
        print(f'  {t}: {n} culverts')

    # Limits table — only culvert-keyword rows.
    try:
        cur.execute(
            f'SELECT geom, avenza_name, avenza_description, '
            f'avenza_datetime, OGR_STYLE, '
            f'avenza_layer_id, avenza_feature_id FROM "{LIMITS_TABLE}";'
        )
        n_limits = 0
        for geom_blob, name, desc, dt, style, layer_id, feature_id in cur.fetchall():
            if not LIMITS_CULVERT_RE.search(((name or '') + ' ' + (desc or ''))):
                continue
            pt = parse_gpkg_point(geom_blob)
            if pt is None: continue
            add_culvert(name, desc, pt[0], pt[1], layer_id, feature_id, LIMITS_TABLE,
                        collected_at=dt, ogr_style=style)
            n_limits += 1
        print(f'  {LIMITS_TABLE}: {n_limits} culverts (filtered by keyword)')
    except sqlite3.Error as e:
        print(f'Skipping {LIMITS_TABLE}: {e}')

    print(f'\nTotal culvert pins: {len(pins)}')
    if far_warnings:
        print(f'\nWARNING: {len(far_warnings)} culvert(s) sit >{WARN_PERP_FT} ft from Section A alignment:')
        for name, desc, perp in far_warnings[:10]:
            print(f'    {name!r:30}  {desc!r:30}  {perp} ft perp')
        if len(far_warnings) > 10:
            print(f'    ... and {len(far_warnings) - 10} more (see batch summary in app)')
        print('  These pins will still be imported and assigned to Section A, but their')
        print('  STAs will be off. Use the Manage Imports panel to delete the batch if')
        print('  this looks wrong.')

    # Main JSON.
    main_data = {
        '_format':       'roadwalk-aecom-features-v1',
        '_chunked':      True,
        '_batch_id':     batch_id,
        'exported_at':   now_iso,
        'project_name':  f'{sec_name} — culvert import from {source_filename}',
        'field_sections': [],
        'bundle_section_pins': pins,
    }
    main_path = os.path.join(out_dir, f'{basename}.main.json')
    with open(main_path, 'w', encoding='utf-8') as f:
        json.dump(main_data, f, ensure_ascii=False, indent=1)
    print(f'\nWrote {main_path}')
    print(f'  ({os.path.getsize(main_path) / 1024:.0f} KB, {len(pins)} pins)')

    # Stream photos to NDJSON chunks.
    print(f'\nStreaming photos to NDJSON chunks (target {TARGET_CHUNK_BYTES // (1024*1024)} MB each):')
    cur.execute(
        'SELECT layer_id, feature_id, data, content_type, datetime, '
        'PhotoLoc, PhotoOrien, PhotoName, PhotoAlt, DeviceType FROM avenza_media;'
    )
    chunks_written = 0; photos_written = 0; photos_skipped = 0
    current_chunk = []; current_bytes = 0

    def flush_chunk():
        nonlocal chunks_written, current_bytes
        if not current_chunk: return
        chunks_written += 1
        chunk_path = os.path.join(
            out_dir, f'{basename}.geophotos.{chunks_written:03d}.ndjson'
        )
        with open(chunk_path, 'w', encoding='utf-8') as f:
            for rec in current_chunk:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        print(f'  chunk {chunks_written:03d}: {len(current_chunk)} photos, '
              f'{os.path.getsize(chunk_path) / (1024*1024):.0f} MB')
        current_chunk.clear()

    for layer_id, feature_id, blob, content_type, dt, photo_loc, photo_orien, photo_name, photo_alt, device_type in cur:
        pin_ulid = pin_ulid_by_lf.get((layer_id, feature_id))
        if not pin_ulid or not blob:
            photos_skipped += 1; continue

        # PhotoLoc longitude sign fix — GRSM is at -83.x but Avenza writes 83.x
        lat = lng = None
        if photo_loc:
            try:
                parts = [p.strip() for p in str(photo_loc).split(',')]
                if len(parts) >= 2:
                    lat = float(parts[0]); lng = float(parts[1])
                    if lng > 0 and 80 < lng < 100:
                        lng = -lng
            except ValueError:
                pass
        try: bearing = float(photo_orien) if photo_orien else None
        except ValueError: bearing = None
        # PhotoAlt is "1370.41 ft" — strip the unit, keep the float.
        altitude_ft = None
        if photo_alt:
            m = re.match(r'([-+]?\d+(?:\.\d+)?)', str(photo_alt).strip())
            if m:
                try: altitude_ft = float(m.group(1))
                except ValueError: pass
        # DeviceType is often the literal string 'None' rather than a NULL
        # — drop those so we don't pollute the photo records.
        dev = (device_type or '').strip() if device_type else ''
        if dev.lower() in ('', 'none'): dev = None
        captured_at = None
        if dt:
            s = str(dt).strip()
            if 'T' in s and not s.endswith('Z'): s += 'Z'
            captured_at = s

        mime = content_type or 'image/jpeg'
        if mime == 'image/jpg': mime = 'image/jpeg'
        b64 = base64.b64encode(blob).decode('ascii')

        rec = {
            'id':              make_ulid(),
            'lat':             lat,
            'lng':             lng,
            'dataUrl':         'data:' + mime + ';base64,' + b64,
            'captured_at':     captured_at,
            '_link_pin_ulid':  pin_ulid,    # in-app importer resolves this to linked_pin_id
            'bearing':         bearing,
            'altitude_ft':     altitude_ft,
            'device_type':     dev,
            'description':     (photo_name or '').strip() or None,
            '_import_batch_id': batch_id,
        }
        rec = {k: v for k, v in rec.items() if v is not None}

        size = len(b64) + 256
        if current_bytes + size > TARGET_CHUNK_BYTES:
            flush_chunk(); current_bytes = 0
        current_chunk.append(rec); current_bytes += size
        photos_written += 1
    flush_chunk()

    print(f'\nPhotos: {photos_written} written across {chunks_written} chunk(s), '
          f'{photos_skipped} skipped (no pin match)')

    print(f'\n── DONE ──')
    print(f'Open RoadWalk → Dashboard → Import AECOM Features and pick ALL of:')
    print(f'    {basename}.main.json')
    for i in range(1, chunks_written + 1):
        print(f'    {basename}.geophotos.{i:03d}.ndjson')
    print(f'\nIf the import looks wrong: Dashboard → Manage Imports → 🗑 Delete batch')
    print(f'  Batch id: {batch_id}')

    conn.close()


if __name__ == '__main__':
    main()
