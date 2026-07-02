"""Compute the geometry block (bearing / crossing / skew / elevations /
drop / slope) for every culvert in the bundle and write it back in
place. Replaces the old KMZ→CSV→XLSX→sync chain now that the bundle
IS the source of truth for culvert geometry.

Elevation source: USGS 3DEP getSamples endpoint. Each culvert endpoint
gets a 3x3 grid at 1 m spacing (~±3.3 ft) sampled; the min cell in the
grid is the "ditchline" elevation, the center cell is the "at-point"
elevation.

Attrs written to each culvert pin:
    bearing_in_to_out_deg    (0..360, inlet → outlet great-circle bearing)
    road_cl_bearing_deg      (bearing of the section alignment at the pipe midpoint)
    crossing_angle_deg       (0..90, acute angle between pipe and road)
    skew_from_perp_deg       (0..90, how far off perpendicular)
    inlet_elev_ditch_ft      (min of the 3x3 grid at the inlet)
    outlet_elev_ditch_ft
    drop_ditch_ft
    slope_ditch_pct
    slope_pipe_pct
    inlet_elev_point_ft      (center of the 3x3 grid — surveyed spot)
    outlet_elev_point_ft
    drop_point_ft
    slope_point_pct
    flow_check               ("OK" / "CHECK opposed" / "flat/uncertain")

Preserved: everything else on the pin.

Run from anywhere:
    python data/apply_flow_geometry.py
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = ROOT / "data" / "prewalk-bundle.json"

SECTIONS_OF_INTEREST = {"A", "G", "H", "I"}
URL = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/getSamples"
M2FT = 3.280839895

# ── Geometry helpers ─────────────────────────────────────────────
def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def line_orient(b): return b % 180.0


def acute(b1, b2):
    d = abs(line_orient(b1) - line_orient(b2)) % 180.0
    return min(d, 180.0 - d)


def offset(lat, lon, de, dn):
    """de = metres east, dn = metres north → (lat, lon) offset."""
    return (
        lat + dn / 110574.0,
        lon + de / (111320.0 * math.cos(math.radians(lat))),
    )


def hav_ft(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h)) * M2FT


# ── Section alignment sampling (bearing at a projected point) ────
def _flat_xy(alignment_lng_lat):
    """Approximate flat cartesian projection around GRSM latitude."""
    LAT0 = 35.71
    MX = 111320.0 * math.cos(math.radians(LAT0))
    MY = 110574.0
    return [(lng * MX, lat * MY) for (lng, lat) in alignment_lng_lat]


def _cumulative_lengths(xy_pts):
    cum = [0.0]
    for i in range(1, len(xy_pts)):
        dx = xy_pts[i][0] - xy_pts[i - 1][0]
        dy = xy_pts[i][1] - xy_pts[i - 1][1]
        cum.append(cum[-1] + math.hypot(dx, dy))
    return cum


def _nearest_s(xy_pts, cum, px, py):
    """Return (dist_m to nearest, arc-length s at nearest)."""
    best = None
    for i in range(len(xy_pts) - 1):
        ax, ay = xy_pts[i]; bx, by = xy_pts[i + 1]
        dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        s = cum[i] + t * math.sqrt(L2)
        if best is None or d < best[0]:
            best = (d, s)
    return best


def _point_at_s(alignment_lng_lat, cum, s):
    s = max(0.0, min(cum[-1], s))
    for i in range(len(cum) - 1):
        if cum[i] <= s <= cum[i + 1]:
            seg = cum[i + 1] - cum[i]
            t = 0.0 if seg == 0 else (s - cum[i]) / seg
            lng = alignment_lng_lat[i][0] + t * (alignment_lng_lat[i + 1][0] - alignment_lng_lat[i][0])
            lat = alignment_lng_lat[i][1] + t * (alignment_lng_lat[i + 1][1] - alignment_lng_lat[i][1])
            return (lat, lng)
    lng, lat = alignment_lng_lat[-1]
    return (lat, lng)


def road_bearing(alignment_lng_lat, xy_cache, mid_lat, mid_lon, window_m=20.0):
    """Local road-centerline bearing at the projection of (mid_lat, mid_lon).
    Returns (bearing_deg, perp_dist_m) or (None, None) when unusable."""
    if not alignment_lng_lat or len(alignment_lng_lat) < 2:
        return None, None
    xy_pts, cum = xy_cache
    LAT0 = 35.71
    MX = 111320.0 * math.cos(math.radians(LAT0))
    MY = 110574.0
    px, py = mid_lon * MX, mid_lat * MY
    r = _nearest_s(xy_pts, cum, px, py)
    if r is None:
        return None, None
    d, s = r
    a_lat, a_lng = _point_at_s(alignment_lng_lat, cum, s - window_m)
    b_lat, b_lng = _point_at_s(alignment_lng_lat, cum, s + window_m)
    return bearing(a_lat, a_lng, b_lat, b_lng), d


# ── USGS 3DEP sampling ───────────────────────────────────────────
def sample_batch(points_lonlat):
    """Send a chunk of [lon, lat] points to 3DEP, return {index: elevation_ft}."""
    body = urllib.parse.urlencode({
        "geometry": json.dumps({"points": points_lonlat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryMultipoint",
        "returnFirstValueOnly": "true",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "json",
    }).encode()
    req = urllib.request.Request(URL, data=body)
    with urllib.request.urlopen(req, timeout=90) as fh:
        data = json.loads(fh.read())
    out = {}
    for s in data.get("samples", []):
        try:
            out[int(s["locationId"])] = float(s["value"]) * M2FT
        except (TypeError, ValueError, KeyError):
            pass
    return out


def add_3x3_grid(sample_pts, sample_owner, lat, lon, tag):
    """Add 9 sample points forming a 3x3 grid at 1 m spacing around
    (lat, lon). Returns the list of indices into sample_pts for this
    grid (indexed 0..8; center is index 4)."""
    idx = []
    for de in (-1, 0, 1):
        for dn in (-1, 0, 1):
            la, lo = offset(lat, lon, de, dn)
            idx.append(len(sample_pts))
            sample_pts.append([lo, la])
            sample_owner.append(tag)
    return idx


# ── Main ─────────────────────────────────────────────────────────
_FLOW_INT_KEYS = {"slope_ditch_pct", "slope_pipe_pct", "slope_point_pct"}


def main() -> int:
    if not BUNDLE_PATH.exists():
        print(f"ERROR: bundle not found at {BUNDLE_PATH}", file=sys.stderr)
        return 1
    print(f"Reading bundle: {BUNDLE_PATH}")
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))

    # Cache each section's alignment in flat XY.
    align_cache = {}
    for sec in bundle.get("sections") or []:
        if sec.get("id") not in SECTIONS_OF_INTEREST:
            continue
        align = sec.get("alignment") or []
        if len(align) < 2:
            continue
        xy_pts = _flat_xy(align)
        cum = _cumulative_lengths(xy_pts)
        align_cache[sec["id"]] = (align, (xy_pts, cum))

    # Walk every culvert pin, batching 3DEP sample points.
    sample_pts = []           # [[lon, lat], ...]
    sample_owner = []         # parallel — the tag we used at add-time (unused later)
    records = []              # per-culvert bookkeeping
    for sec in bundle.get("sections") or []:
        if sec.get("id") not in SECTIONS_OF_INTEREST:
            continue
        sec_id = sec["id"]
        for pin in sec.get("pins") or []:
            if (pin.get("kind") or "") != "culvert":
                continue
            g = pin.get("geometry") or {}
            coords = g.get("coordinates") or []
            if not (isinstance(coords, list) and len(coords) >= 2):
                continue
            try:
                inlet_lon, inlet_lat = float(coords[0][0]),  float(coords[0][1])
                outlet_lon, outlet_lat = float(coords[-1][0]), float(coords[-1][1])
            except (ValueError, TypeError):
                continue
            gin  = add_3x3_grid(sample_pts, sample_owner, inlet_lat,  inlet_lon,
                                ("in",  sec_id, pin.get("id")))
            gout = add_3x3_grid(sample_pts, sample_owner, outlet_lat, outlet_lon,
                                ("out", sec_id, pin.get("id")))
            records.append({
                "pin": pin,
                "sec_id": sec_id,
                "inlet_lat": inlet_lat, "inlet_lon": inlet_lon,
                "outlet_lat": outlet_lat, "outlet_lon": outlet_lon,
                "gin": gin, "gout": gout,
            })

    print(f"  {len(records)} culvert(s) to sample "
          f"({len(sample_pts)} elevation point(s) total).")

    # Batch 3DEP requests at CHUNK sample points per call.
    CHUNK = 100
    elev: dict[int, float] = {}
    t0 = time.time()
    for i in range(0, len(sample_pts), CHUNK):
        res = sample_batch(sample_pts[i:i + CHUNK])
        for j, v in res.items():
            elev[i + j] = v
        done = min(i + CHUNK, len(sample_pts))
        print(f"  elevation {done}/{len(sample_pts)}"
              f"  ({time.time() - t0:.1f} s)")

    # Compute the geometry block for each culvert.
    flow_ok = 0; flow_check = 0; flow_flat = 0; swapped = 0
    for r in records:
        pin = r["pin"]; attrs = pin.setdefault("attrs", {})
        # Cell values from the grids.
        iv = [elev[k] for k in r["gin"]  if k in elev]
        ov = [elev[k] for k in r["gout"] if k in elev]
        i_pt = elev.get(r["gin"][4])            # center of 3x3
        o_pt = elev.get(r["gout"][4])
        i_dl = min(iv) if iv else None
        o_dl = min(ov) if ov else None
        # KML endpoint order is arbitrary — the first-clicked vertex
        # doesn't necessarily equal the physical inlet. Water flows
        # downhill: the higher-elevation end is the inlet. Swap if
        # the ditchline mins say we've got it backwards. Uses ditch
        # (not point) because that's the flowline sample the report
        # already displays. Threshold of 1 ft keeps flat / noisy
        # pipes from bouncing endpoints on rounding.
        if (i_dl is not None and o_dl is not None
                and (o_dl - i_dl) > 1.0):
            # outlet is higher than inlet → swap.
            r["inlet_lat"], r["outlet_lat"] = r["outlet_lat"], r["inlet_lat"]
            r["inlet_lon"], r["outlet_lon"] = r["outlet_lon"], r["inlet_lon"]
            i_dl, o_dl = o_dl, i_dl
            i_pt, o_pt = o_pt, i_pt
            iv,   ov   = ov,   iv
            r["gin"],  r["gout"]  = r["gout"], r["gin"]
            swapped += 1
            # Flip the geometry coordinates too so the report + SLD
            # inlet/outlet markers line up with the sampled elevations.
            coords = (pin.get("geometry") or {}).get("coordinates") or []
            if coords:
                pin["geometry"]["coordinates"] = list(reversed(coords))
        # Culvert bearing (in → out) via great-circle.
        brg_io = bearing(r["inlet_lat"], r["inlet_lon"], r["outlet_lat"], r["outlet_lon"])
        # Road bearing + crossing / skew at pipe midpoint.
        mlat = (r["inlet_lat"]  + r["outlet_lat"])  / 2
        mlon = (r["inlet_lon"] + r["outlet_lon"]) / 2
        rb, rd = None, None
        if r["sec_id"] in align_cache:
            align, xy_cache = align_cache[r["sec_id"]]
            rb, rd = road_bearing(align, xy_cache, mlat, mlon, window_m=20.0)
        cross = acute(brg_io, rb) if rb is not None else None
        skew  = (90.0 - cross)   if cross is not None else None
        # Horizontal run + drop + slope (ditchline and at-point sets).
        run_ft = hav_ft(r["inlet_lat"], r["inlet_lon"], r["outlet_lat"], r["outlet_lon"])
        drop_dl  = (i_dl - o_dl) if (i_dl is not None and o_dl is not None) else None
        slope_dl = (drop_dl / run_ft * 100) if (drop_dl is not None and run_ft > 0.5) else None
        drop_pt  = (i_pt - o_pt) if (i_pt is not None and o_pt is not None) else None
        slope_pt = (drop_pt / run_ft * 100) if (drop_pt is not None and run_ft > 0.5) else None
        # slope_pipe_pct = drop / length_ft (uses field-measured pipe length).
        try:
            plen = float(attrs.get("length_ft") or 0)
        except (TypeError, ValueError):
            plen = 0
        slope_pipe = (drop_dl / plen * 100) if (drop_dl is not None and plen > 0.5) else None
        # Flow-vs-drainage-direction sanity flag.
        NOISE_FT = 1.0
        if drop_dl is None:
            fc = "flat/uncertain"; flow_flat += 1
        elif abs(drop_dl) < NOISE_FT:
            fc = "flat/uncertain"; flow_flat += 1
        elif drop_dl > 0:
            fc = "OK"; flow_ok += 1
        else:
            fc = "CHECK opposed"; flow_check += 1
        # Write attrs. Slopes round to integers per project convention;
        # other numerics get 2 decimals; None → drop the key.
        pairs = {
            "bearing_in_to_out_deg": brg_io,
            "road_cl_bearing_deg":   rb,
            "crossing_angle_deg":    cross,
            "skew_from_perp_deg":    skew,
            "inlet_elev_ditch_ft":   i_dl,
            "outlet_elev_ditch_ft":  o_dl,
            "drop_ditch_ft":         drop_dl,
            "slope_ditch_pct":       slope_dl,
            "slope_pipe_pct":        slope_pipe,
            "inlet_elev_point_ft":   i_pt,
            "outlet_elev_point_ft":  o_pt,
            "drop_point_ft":         drop_pt,
            "slope_point_pct":       slope_pt,
            "flow_check":            fc,
        }
        for k, v in pairs.items():
            if v is None or (isinstance(v, str) and not v):
                attrs.pop(k, None)
            elif isinstance(v, float):
                attrs[k] = int(round(v)) if k in _FLOW_INT_KEYS else round(v, 2)
            else:
                attrs[k] = v

    # Write bundle.
    BUNDLE_PATH.write_text(json.dumps(bundle, separators=(",", ":")) + "\n",
                           encoding="utf-8")

    print()
    print(f"  Endpoint swaps (KML order was reversed):  {swapped}")
    print(f"  Flow direction: OK={flow_ok}  CHECK={flow_check}  flat/uncertain={flow_flat}")
    print(f"\nWrote {BUNDLE_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
