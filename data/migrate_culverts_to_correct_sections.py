"""Move specific AECOM_FOX culverts off Section A onto the ramp/spur
sections they actually live on.

The KMZ + Excel ingestion dropped every AECOM_FOX culvert on Section A
because the source Excel didn't carry a section column. Field knowledge
overrides that — these orders belong elsewhere:

    Excel Order 1, 5, 6 → Section H  (NB Gatlinburg Bypass Ramp,  GRSM-0012AZ)
    Excel Order 2, 3, 4 → Section I  (SB Gatlinburg Bypass Ramp,  GRSM-0012BZ)
    Excel Order 32      → Section G  (Campbell Lead Road Access Ramp, GRSM-0163BZ)

For each pin moved we:
  * Re-project the LineString centroid onto the destination section's
    alignment to compute fresh sta_ft / sta / perp_offset_ft / perp_side
    so the report's MP / STA / Side row reads against the correct
    centerline.
  * Rename the pin id from A-CV-NNN to {DEST}-CV-NNN so the printed
    header row picks up the right section letter.
  * Pop the pin from Section A's pins[] and append to the destination.

Idempotent — re-running matches by attrs.fox_order rather than by id,
so the script can be re-run safely after the first rename.

Run from the RoadWalk root:
    python data/migrate_culverts_to_correct_sections.py
"""

import json
import math
import sys
from pathlib import Path

ROOT   = Path(__file__).resolve().parent
BUNDLE = ROOT / "prewalk-bundle.json"

# (fox_order, destination section id)
MIGRATIONS = [
    (1,  "H"),
    (5,  "H"),
    (6,  "H"),
    (2,  "I"),
    (3,  "I"),
    (4,  "I"),
    (32, "G"),
]


def project_pt_onto_alignment(pt, align):
    """pt: (lat, lng). align: list of (lat, lng) pairs.
    Returns (sta_ft, perp_offset_ft, side) — same convention the
    ingestion script uses (cross product → 'L' / 'R' / 'C')."""
    if not align or len(align) < 2:
        return (0.0, 0.0, "C")
    lat0 = sum(p[0] for p in align) / len(align)
    coslat0 = math.cos(math.radians(lat0))
    FT_PER_DEG = 364000.0
    px = pt[1] * coslat0 * FT_PER_DEG
    py = pt[0] * FT_PER_DEG
    best = None
    best_d2 = float("inf")
    cum_ft = 0.0
    for i in range(len(align) - 1):
        a, b = align[i], align[i + 1]
        ax = a[1] * coslat0 * FT_PER_DEG
        ay = a[0] * FT_PER_DEG
        bx = b[1] * coslat0 * FT_PER_DEG
        by = b[0] * FT_PER_DEG
        seg_dx = bx - ax
        seg_dy = by - ay
        seg_len2 = seg_dx * seg_dx + seg_dy * seg_dy
        if seg_len2 == 0:
            continue
        t = ((px - ax) * seg_dx + (py - ay) * seg_dy) / seg_len2
        t_c = max(0.0, min(1.0, t))
        proj_x = ax + t_c * seg_dx
        proj_y = ay + t_c * seg_dy
        d2 = (px - proj_x) ** 2 + (py - proj_y) ** 2
        if d2 < best_d2:
            seg_len = math.sqrt(seg_len2)
            sta_ft = cum_ft + t_c * seg_len
            cross = seg_dx * (py - ay) - seg_dy * (px - ax)
            side = "L" if cross > 0 else "R" if cross < 0 else "C"
            offset = math.sqrt(d2)
            best = (sta_ft, offset, side)
            best_d2 = d2
        cum_ft += math.sqrt(seg_len2)
    return best if best else (0.0, 0.0, "C")


def fmt_sta(sta_ft):
    sta_ft = max(0.0, sta_ft)
    full = int(sta_ft / 100)
    rem = sta_ft - full * 100
    return f"{full}+{rem:02.0f}"


def main():
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    sections_by_id = {s["id"]: s for s in bundle["sections"]}
    if "A" not in sections_by_id:
        print("ERROR: Section A not found in bundle", file=sys.stderr)
        return 1

    moved = 0
    notes = []
    for order, target_id in MIGRATIONS:
        dest = sections_by_id.get(target_id)
        if not dest:
            notes.append(f"Order {order} -> section {target_id}: section not found")
            continue

        # Match by fox_order so the script is idempotent — works whether
        # the pin still lives on Section A or has already been moved.
        pin = None
        cur_sec = None
        for s in bundle["sections"]:
            for p in s.get("pins", []):
                if (p.get("attrs") or {}).get("fox_order") == order:
                    pin = p
                    cur_sec = s
                    break
            if pin:
                break
        if not pin:
            notes.append(f"Order {order}: no pin with attrs.fox_order={order}")
            continue

        # Centroid of the LineString endpoints — same midpoint the
        # report renderer uses for MP / lat / lng.
        coords = (pin.get("geometry") or {}).get("coordinates") or []
        if not coords:
            notes.append(f"Order {order}: pin has no coords")
            continue
        c_lat = sum(c[1] for c in coords) / len(coords)
        c_lng = sum(c[0] for c in coords) / len(coords)

        # Destination alignment is [[lng, lat], …] (GeoJSON style).
        align_raw = dest.get("alignment") or []
        align_latlng = [(c[1], c[0]) for c in align_raw if len(c) >= 2]
        if len(align_latlng) < 2:
            notes.append(f"Order {order}: destination section {target_id} has no alignment")
            continue
        sta_ft, perp_offset, side = project_pt_onto_alignment(
            (c_lat, c_lng), align_latlng
        )

        # Update pin
        pin["sta_ft"] = round(sta_ft, 2)
        pin["sta"]    = fmt_sta(sta_ft)
        attrs = pin.setdefault("attrs", {})
        attrs["perp_offset_ft"] = round(perp_offset, 1)
        attrs["perp_side"]      = side
        attrs["sta_formatted"]  = fmt_sta(sta_ft)

        old_id = pin.get("id") or ""
        # Replace whatever prefix the id currently carries.
        # CV-001 always lives behind a dash; pick the segment after it.
        suffix = old_id.split("-", 1)[1] if "-" in old_id else f"CV-{order:03d}"
        if not suffix.startswith("CV-"):
            suffix = f"CV-{order:03d}"
        pin["id"] = f"{target_id}-{suffix}"

        # Move between sections only if the pin isn't already on dest.
        if cur_sec is not dest:
            cur_sec["pins"] = [p for p in cur_sec.get("pins", []) if p is not pin]
            dest.setdefault("pins", []).append(pin)
        moved += 1
        print(
            f"  {old_id:>15s} -> {pin['id']:<15s}  "
            f"sta {pin['sta']:<8s} side {side}  offset {perp_offset:6.1f} ft"
            + ("  (re-projected, already on dest)" if cur_sec is dest else "")
        )

    # Stable order on the sections we touched so the diff stays readable.
    for sid in {"A", "G", "H", "I"}:
        s = sections_by_id.get(sid)
        if not s:
            continue
        s.get("pins", []).sort(key=lambda p: (p.get("sta_ft") or 0, p.get("id") or ""))

    BUNDLE.write_text(json.dumps(bundle, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"\nMoved {moved} pin(s).")
    if notes:
        print("Notes:")
        for n in notes:
            print(" ", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
