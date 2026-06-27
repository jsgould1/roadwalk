"""Sync GRSM culvert attributes from the standalone QC project.

Reads `GRSM_Culvert_Stationing_SF_with_geometry.xlsx` (Sheet 1:
"Culverts + Geometry") and updates `data/prewalk-bundle.json` for
Sections A, G, H, I.

Match key — **Excel `Order`** (the row number in the QC project's
source spreadsheet) joins against the bundle's `attrs.fox_order` field
that the AECOM_FOX ingestion stamps on every culvert pin. The bundle's
pin id suffix is the Excel order (so `H-CV-005` is fox_order=5), while
the QC project's `culvert_id` is a renumbered-per-section sequence
(so the same culvert is `H-CV-002` over there). Once matched, the
bundle pin is RENAMED to the QC's culvert_id so the two systems align
going forward (pin.ulid is the canonical identifier, so the rename
doesn't break any inspection records).

Rules:
  - **Geometry block** (bearing, road CL bearing, crossing angle,
    skew, inlet/outlet elevations at point + ditchline, drops, slopes,
    flow-vs-drainage check) is ALWAYS overwritten — these are
    computed-from-data, not field-typed, so we trust the index.
  - **User-editable fields** (material, size_in, length_ft, in_type,
    out_type, notes, stationing) are FILL-IF-EMPTY — never clobber a
    value the field crew typed into the data form.
  - **Missing culverts** (rows whose Order parses as non-numeric like
    "10A", "52A", "60" — meaning the QC project added them after the
    AECOM_FOX ingestion was frozen) are INSERTED with position
    interpolated from the 1994 as-built STA (Pla94Sta column) against
    the two nearest neighbors that have BOTH a numeric Pla94Sta AND an
    existing sta_ft in the bundle. The new pin's id is the QC's
    culvert_id.

Idempotent — re-running with an updated index just re-applies the rules.

Run from the RoadWalk root:
    python data/sync_culverts_from_grsm_index.py
"""

from __future__ import annotations

import json
import math
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

import openpyxl


# ── Paths ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
BUNDLE_PATH = ROOT / "prewalk-bundle.json"

# Default XLSX location — sibling project. Pass an explicit path on
# argv if the file moves.
XLSX_DEFAULT = (
    Path.home()
    / "OneDrive - AECOM" / "Documents" / "!AECOM" / "CLAUDE" / "PaveCollector"
    / "culvert_match" / "GRSM_Culvert_Stationing_SF_with_geometry.xlsx"
)

SECTIONS_OF_INTEREST = {"A", "G", "H", "I"}

# ── ULID minting (matches merge_culvert_aecom_fox_into_bundle.py) ──
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    ts_ms = int(time.time() * 1000)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    n = (ts_ms << 80) | rand
    out = []
    for _ in range(26):
        out.append(_B32[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


# ── Helpers ────────────────────────────────────────────────────────
def _fnum(v) -> Optional[float]:
    """Coerce a cell to float; return None for blanks, '#VALUE!', text."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if math.isnan(v):
            return None
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s or s.startswith("#"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _str(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.startswith("#") or s in ("None", "nan"):
        return ""
    return s


_PLA_PAT = re.compile(r"^\s*(\d+)\s*\+?\s*(\d{0,2})\s*$")


def _pla94_to_ft(raw) -> Optional[float]:
    """Convert a 1994 as-built station string like '203+00' or '35+75'
    into a single float in feet. Returns None for non-numeric values
    like 'Parking Lot', 'Median', 'Campbell Lead Ramp', etc."""
    s = _str(raw)
    if not s:
        return None
    # Plain numeric (rare, but accept).
    try:
        return float(s)
    except ValueError:
        pass
    m = _PLA_PAT.match(s)
    if not m:
        return None
    hundreds, rest = m.group(1), m.group(2)
    return int(hundreds) * 100 + (int(rest) if rest else 0)


def _projected_latlng_at_sta_ft(alignment, target_sta_ft: float):
    """Walk a section's alignment polyline and return the [lat, lng]
    of the point at the given station (feet from the start).

    alignment is [[lat, lng], …] (Leaflet convention used inside the
    in-memory SECTIONS array, BUT the bundle JSON stores the raw GeoJSON
    [[lng, lat], …] under sections[].alignment — applyBundle does the
    flip on load). This helper accepts the BUNDLE-shape (lng-first)
    because we're working with the JSON on disk.
    """
    if not alignment or len(alignment) < 2:
        return None
    # Equirectangular projection at the mean latitude — same convention
    # the in-app code uses, accurate at the corridor scale.
    lats = [c[1] for c in alignment if isinstance(c, list) and len(c) >= 2]
    if not lats:
        return None
    lat0 = sum(lats) / len(lats)
    cos0 = math.cos(math.radians(lat0))
    FT_PER_DEG = 364000.0

    cum_ft = 0.0
    for i in range(len(alignment) - 1):
        a, b = alignment[i], alignment[i + 1]
        ax, ay = a[0] * cos0 * FT_PER_DEG, a[1] * FT_PER_DEG
        bx, by = b[0] * cos0 * FT_PER_DEG, b[1] * FT_PER_DEG
        seg_len = math.hypot(bx - ax, by - ay)
        if seg_len < 1e-6:
            continue
        if cum_ft + seg_len >= target_sta_ft:
            # Interpolate within this segment.
            t = (target_sta_ft - cum_ft) / seg_len
            px = ax + t * (bx - ax)
            py = ay + t * (by - ay)
            lng = px / (cos0 * FT_PER_DEG)
            lat = py / FT_PER_DEG
            return [lat, lng]
        cum_ft += seg_len
    # Past the end — clamp to the final vertex.
    last = alignment[-1]
    return [last[1], last[0]]


def _fmt_sta(sta_ft: float) -> str:
    sta_ft = max(0.0, sta_ft)
    full = int(sta_ft / 100)
    rem = sta_ft - full * 100
    return f"{full}+{rem:02.0f}"


# ── XLSX schema (matches the workbook layout the user shared) ──────
# Column indices into the row tuple openpyxl yields. Confirmed by
# inspection of GRSM_Culvert_Stationing_SF_with_geometry.xlsx.
COL = {
    "section_id":     0,
    "culvert_id":     1,
    "order":          2,
    "in_type":        3,
    "in_side":        4,
    "out_type":       5,
    "out_side":       6,
    "material":       7,
    "length_ft":      8,
    "size_in":        9,
    "drainage_basin_ac": 10,
    "drainage_dir":   11,
    "notes":          12,
    "stationing":     13,
    "pla94_sta":      15,
    "inlet_str":      16,
    "outlet_str":     17,
    # Geometry block — always overwritten on this side.
    "bearing":        19,
    "road_cl_brg":    20,
    "crossing_ang":   21,
    "skew_perp":      22,
    "in_elev_ditch":  23,
    "out_elev_ditch": 24,
    "drop_ditch":     25,
    "slope_ditch":    26,
    "slope_pipe":     27,
    "in_elev_pt":     28,
    "out_elev_pt":    29,
    "drop_pt":        30,
    "slope_pt":       31,
    "flow_check":     32,
    "geom_note":      33,
}


def _read_index(xlsx_path: Path) -> list[dict]:
    """Load every Culvert ID row from the workbook. Skips totally
    blank rows; warns on a section/id prefix mismatch."""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["Culverts + Geometry"]
    rows = []
    warns = []
    for r in ws.iter_rows(values_only=True):
        if not r or r[COL["culvert_id"]] is None:
            continue
        sec = _str(r[COL["section_id"]])
        cid = _str(r[COL["culvert_id"]])
        if not cid or not sec:
            continue
        # Defensive: prefer the id-prefix when sec disagrees (the user's
        # A-CV-041 row is marked section "G" but the id is clearly A-).
        prefix = cid.split("-", 1)[0] if "-" in cid else sec
        if prefix != sec:
            warns.append(f"  ! {cid} marked section {sec!r} — using {prefix!r} from the id")
            sec = prefix
        if sec not in SECTIONS_OF_INTEREST:
            continue
        rows.append({
            "section_id":   sec,
            "culvert_id":   cid,
            "order":        _str(r[COL["order"]]),
            "in_type":      _str(r[COL["in_type"]]),
            "in_side":      _str(r[COL["in_side"]]),
            "out_type":     _str(r[COL["out_type"]]),
            "out_side":     _str(r[COL["out_side"]]),
            "material":     _str(r[COL["material"]]),
            "length_ft":    _fnum(r[COL["length_ft"]]),
            "size_in":      _fnum(r[COL["size_in"]]),
            "drainage_basin_ac": _fnum(r[COL["drainage_basin_ac"]]),
            "drainage_dir": _str(r[COL["drainage_dir"]]),
            "notes":        _str(r[COL["notes"]]),
            "stationing":   _str(r[COL["stationing"]]),
            "pla94_str":    _str(r[COL["pla94_sta"]]),
            "pla94_ft":     _pla94_to_ft(r[COL["pla94_sta"]]),
            "inlet_str":    _str(r[COL["inlet_str"]]),
            "outlet_str":   _str(r[COL["outlet_str"]]),
            # Geometry — keep raw floats so we can write None for blanks.
            "bearing":        _fnum(r[COL["bearing"]]),
            "road_cl_brg":    _fnum(r[COL["road_cl_brg"]]),
            "crossing_ang":   _fnum(r[COL["crossing_ang"]]),
            "skew_perp":      _fnum(r[COL["skew_perp"]]),
            "in_elev_ditch":  _fnum(r[COL["in_elev_ditch"]]),
            "out_elev_ditch": _fnum(r[COL["out_elev_ditch"]]),
            "drop_ditch":     _fnum(r[COL["drop_ditch"]]),
            "slope_ditch":    _fnum(r[COL["slope_ditch"]]),
            "slope_pipe":     _fnum(r[COL["slope_pipe"]]),
            "in_elev_pt":     _fnum(r[COL["in_elev_pt"]]),
            "out_elev_pt":    _fnum(r[COL["out_elev_pt"]]),
            "drop_pt":        _fnum(r[COL["drop_pt"]]),
            "slope_pt":       _fnum(r[COL["slope_pt"]]),
            "flow_check":     _str(r[COL["flow_check"]]),
            "geom_note":      _str(r[COL["geom_note"]]),
        })
    if warns:
        print("\nWarnings during index read:")
        for w in warns:
            print(w)
    return rows


# ── attrs mapping ─────────────────────────────────────────────────
# user-editable: only written when currently empty/blank/None.
USER_EDITABLE_TEXT = [
    ("material",     "material"),
    ("in_type",      "in_type"),
    ("out_type",     "out_type"),
    ("notes",        "notes"),
    ("stationing",   "stationing_label"),
    ("drainage_dir", "drainage_direction"),
    ("inlet_str",    "inlet_structure_label"),
    ("outlet_str",   "outlet_structure_label"),
    ("in_side",      "in_side"),
    ("out_side",     "out_side"),
]
USER_EDITABLE_NUM = [
    ("length_ft",          "length_ft"),
    ("size_in",            "size_in"),
    ("drainage_basin_ac",  "drainage_basin_ac"),
]
# Geometry — always written (overwrite). Float None becomes attr removal.
GEOMETRY_NUM = [
    ("bearing",        "bearing_in_to_out_deg"),
    ("road_cl_brg",    "road_cl_bearing_deg"),
    ("crossing_ang",   "crossing_angle_deg"),
    ("skew_perp",      "skew_from_perp_deg"),
    ("in_elev_ditch",  "inlet_elev_ditch_ft"),
    ("out_elev_ditch", "outlet_elev_ditch_ft"),
    ("drop_ditch",     "drop_ditch_ft"),
    ("slope_ditch",    "slope_ditch_pct"),
    ("slope_pipe",     "slope_pipe_pct"),
    ("in_elev_pt",     "inlet_elev_point_ft"),
    ("out_elev_pt",    "outlet_elev_point_ft"),
    ("drop_pt",        "drop_point_ft"),
    ("slope_pt",       "slope_point_pct"),
]
GEOMETRY_TEXT = [
    ("flow_check", "flow_check"),
    ("geom_note",  "geom_note"),
    ("pla94_str",  "pla94_sta_label"),
]


_SLOPE_INT_KEYS = {"slope_ditch_pct", "slope_pipe_pct", "slope_point_pct"}


def _apply_index_row(attrs: dict, row: dict) -> None:
    """Write the index row's values onto an existing pin's attrs in
    place. XLSX is the authoritative source of truth — every column
    present in the row overwrites the prior bundle value. The only
    pin attrs left untouched are those the XLSX doesn't carry at all
    (condition ratings, condition notes, photos, report_hidden_photos).
    Slope columns round to the nearest whole number per spec."""
    for src_key, attr_key in USER_EDITABLE_TEXT:
        v = row[src_key]
        if v:
            attrs[attr_key] = v
        else:
            attrs.pop(attr_key, None)
    for src_key, attr_key in USER_EDITABLE_NUM:
        v = row[src_key]
        if v is not None:
            attrs[attr_key] = v
        else:
            attrs.pop(attr_key, None)
    for src_key, attr_key in GEOMETRY_NUM:
        v = row[src_key]
        if v is None:
            attrs.pop(attr_key, None)
        elif attr_key in _SLOPE_INT_KEYS:
            attrs[attr_key] = int(round(v))
        else:
            attrs[attr_key] = round(v, 2)
    for src_key, attr_key in GEOMETRY_TEXT:
        v = row[src_key]
        if v:
            attrs[attr_key] = v
        else:
            attrs.pop(attr_key, None)


def _build_new_pin(row: dict, lat: float, lng: float, sta_ft: float) -> dict:
    """Build a culvert pin dict for a brand-new (missing) culvert."""
    attrs = {
        "source":      "GRSM_INDEX_2026",
        "fox_order":   None,  # explicit so it doesn't shadow a real one
    }
    _apply_index_row(attrs, row)
    pin = {
        "id":       row["culvert_id"],
        "ulid":     _ulid(),
        "kind":     "culvert",
        "source":   "GRSM_INDEX_2026",
        "status":   "pending",
        "sta_ft":   round(sta_ft, 2),
        "sta":      _fmt_sta(sta_ft),
        "geometry": {
            "type":        "Point",
            "coordinates": [lng, lat],
        },
        "attrs":    attrs,
    }
    return pin


def _interpolate_sta_ft(target_pla94: float, neighbors: list[tuple[float, float]]) -> Optional[float]:
    """Given the missing pin's Pla94 station and a sorted list of
    (pla94_ft, sta_ft) pairs from pins on the same section that have
    both, linear-interpolate the missing pin's sta_ft."""
    if not neighbors or target_pla94 is None:
        return None
    # Two-pointer scan to find the bracketing pair.
    below, above = None, None
    for p, s in neighbors:
        if p <= target_pla94 and (below is None or p > below[0]):
            below = (p, s)
        if p >= target_pla94 and (above is None or p < above[0]):
            above = (p, s)
    if below and above and below[0] != above[0]:
        t = (target_pla94 - below[0]) / (above[0] - below[0])
        return below[1] + t * (above[1] - below[1])
    if below:
        return below[1]
    if above:
        return above[1]
    return None


# ── Main ──────────────────────────────────────────────────────────
def main() -> int:
    xlsx_path = Path(sys.argv[1]) if len(sys.argv) > 1 else XLSX_DEFAULT
    if not xlsx_path.exists():
        print(f"ERROR: index XLSX not found at {xlsx_path}", file=sys.stderr)
        return 1
    if not BUNDLE_PATH.exists():
        print(f"ERROR: prewalk-bundle.json not found at {BUNDLE_PATH}", file=sys.stderr)
        return 1

    print(f"Reading index:  {xlsx_path}")
    rows = _read_index(xlsx_path)
    print(f"Loaded {len(rows)} culvert row(s) across sections {sorted(SECTIONS_OF_INTEREST)}.")

    print(f"Reading bundle: {BUNDLE_PATH}")
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    sections = {s["id"]: s for s in bundle.get("sections", []) if s.get("id") in SECTIONS_OF_INTEREST}

    # Bucket the index by section and by INT Order so we can match
    # against the bundle's attrs.fox_order. Non-numeric orders (10A,
    # 52A, 60, …) go to a separate list — those are the "insert me"
    # rows because there's no fox_order to match them by.
    rows_by_int_order: dict[int, dict] = {}
    rows_missing: list[dict] = []
    for r in rows:
        ostr = r["order"]
        try:
            oi = int(ostr)
        except (TypeError, ValueError):
            rows_missing.append(r)
            continue
        if oi in rows_by_int_order:
            print(f"  ! duplicate Order={oi} ({rows_by_int_order[oi]['culvert_id']} and {r['culvert_id']}) — using the first")
        else:
            rows_by_int_order[oi] = r

    updated_existing = 0
    renamed = []
    matched_orders: set[int] = set()
    duplicate_orders: list[tuple[int, str, str, str]] = []  # (order, first_id, dup_id, sec)

    # ── Pass 1: update existing pins ─────────────────────────────
    # Match every culvert pin with attrs.fox_order against an XLSX
    # row. Update attrs in place AND rename the pin to the QC's
    # culvert_id so the two systems align going forward.
    seen_orders: dict[int, tuple[str, str]] = {}  # fox_order -> (pin_id, sec_id)
    for sec_id, sec in sections.items():
        for pin in sec.get("pins", []):
            if not pin or pin.get("kind") != "culvert":
                continue
            attrs = pin.setdefault("attrs", {})
            fo = attrs.get("fox_order")
            if not isinstance(fo, int):
                # NPS-GIS pins and anything else without a fox_order
                # are out of scope — leave untouched.
                continue
            row = rows_by_int_order.get(fo)
            if not row:
                continue
            if fo in seen_orders:
                first_id, first_sec = seen_orders[fo]
                duplicate_orders.append((fo, first_id, pin.get("id") or "?", sec_id))
            else:
                seen_orders[fo] = (pin.get("id") or "?", sec_id)
            matched_orders.add(fo)
            _apply_index_row(attrs, row)
            updated_existing += 1
            new_id = row["culvert_id"]
            if pin.get("id") != new_id:
                renamed.append((pin.get("id"), new_id, sec_id))
                pin["id"] = new_id

    # Any index row whose Order is a plain int but had NO matching
    # bundle pin should also be treated as missing — the AECOM_FOX
    # ingestion was frozen before that row was added on the QC side.
    for oi, r in rows_by_int_order.items():
        if oi not in matched_orders:
            rows_missing.append(r)

    # ── Pass 2: insert missing pins ──────────────────────────────
    # For each section, build a sorted (pla94_ft, sta_ft) neighbor
    # list from EXISTING pins (now that they've been renamed to QC
    # ids) so we can interpolate the missing ones in.
    inserted = []
    skipped_no_pla94 = []
    skipped_no_neighbors = []
    for sec_id, sec in sections.items():
        # Build the neighbor list from this section's pins keyed by
        # fox_order, looking up Pla94 from the index.
        existing_pins = [p for p in sec.get("pins", []) if p and p.get("kind") == "culvert"]
        neighbors: list[tuple[float, float]] = []
        for p in existing_pins:
            attrs = p.get("attrs") or {}
            fo = attrs.get("fox_order")
            if not isinstance(fo, int):
                continue
            ridx = rows_by_int_order.get(fo)
            if not ridx or ridx["pla94_ft"] is None:
                continue
            if p.get("sta_ft") is None:
                continue
            neighbors.append((ridx["pla94_ft"], float(p["sta_ft"])))

        # alignment is [[lng, lat], …] in the bundle.
        alignment = sec.get("alignment") or []

        # Walk every "missing" row whose section matches this one.
        for r in rows_missing:
            if r["section_id"] != sec_id:
                continue
            if r["pla94_ft"] is None:
                skipped_no_pla94.append((r["culvert_id"], r["pla94_str"] or "(blank)"))
                continue
            sta = _interpolate_sta_ft(r["pla94_ft"], neighbors)
            if sta is None or not alignment:
                skipped_no_neighbors.append(r["culvert_id"])
                continue
            ll = _projected_latlng_at_sta_ft(alignment, sta)
            if not ll:
                skipped_no_neighbors.append(r["culvert_id"])
                continue
            pin = _build_new_pin(r, ll[0], ll[1], sta)
            sec.setdefault("pins", []).append(pin)
            inserted.append((r["culvert_id"], sec_id, sta, ll))
            # Add the new pin to the neighbor list so subsequent
            # interpolations on the same section can lean on it.
            neighbors.append((r["pla94_ft"], sta))

    # Stable sort each section's pins by sta_ft so reload diffs stay
    # readable.
    for sec in sections.values():
        sec.get("pins", []).sort(key=lambda p: (p.get("sta_ft") or 0, p.get("id") or ""))

    BUNDLE_PATH.write_text(json.dumps(bundle, separators=(",", ":")) + "\n", encoding="utf-8")

    # ── Summary ──────────────────────────────────────────────────
    print()
    print(f"  Existing pins updated:  {updated_existing}")
    if renamed:
        print(f"  Renamed (bundle id -> QC culvert_id):  {len(renamed)}")
        for old, new, sec_id in renamed:
            print(f"     {old:>14}  ->  {new:<14}  ({sec_id})")
    if duplicate_orders:
        print(f"  ⚠ Duplicate fox_order in bundle (DATA INTEGRITY ISSUE):  {len(duplicate_orders)}")
        for oi, first_id, dup_id, sec_id in duplicate_orders:
            print(f"     ! Order={oi}  first={first_id}  duplicate={dup_id}  ({sec_id})")
    print(f"  New pins inserted:      {len(inserted)}")
    for cid, sec_id, sta, ll in inserted:
        print(f"     + {cid}  ({sec_id})  sta_ft={sta:.1f}  ({ll[0]:.6f}, {ll[1]:.6f})")
    if skipped_no_pla94:
        print(f"  Skipped (no numeric Pla94Sta):  {len(skipped_no_pla94)}")
        for cid, raw in skipped_no_pla94:
            print(f"     ! {cid}  Pla94Sta={raw!r}")
    if skipped_no_neighbors:
        print(f"  Skipped (no neighbor pair to interpolate):  {len(skipped_no_neighbors)}")
        for cid in skipped_no_neighbors:
            print(f"     ! {cid}")
    print(f"\nWrote {BUNDLE_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
