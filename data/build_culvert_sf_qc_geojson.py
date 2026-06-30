"""Convert the EFL SF Culvert KMZ to GeoJSON for the RoadWalk QC layer.

Source - the team's reviewed KMZ at:
  !DATA/EFL/2026 GRSM/GRSM_Culvert/04_Steven Updates after Review 062626/
  GRSM_Culvert 1 (3).kmz

The KMZ is a ZIP carrying doc.kml. Attributes ride in the Placemark's
HTML <description> CDATA (not ExtendedData), as a two-column key/value
table; we parse that table to pull FID, Id, Num, In_Type, Out_Type,
Material, Coating, Length, Size, Notes, Stationing, Pla94Sta.

For each LineString we also compute drawn_length_ft = haversine across
the polyline so the RoadWalk QC tooltip can show drawn vs surveyed
lengths in the same hover.

Output - data/culverts-sf-qc.geojson under the RoadWalk repo, ready to
ship to GitHub Pages via the existing fetch path.

Run from anywhere:
    python data/build_culvert_sf_qc_geojson.py
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
import zipfile
from pathlib import Path

KMZ_PATH = (
    Path.home()
    / "OneDrive - AECOM" / "Documents" / "!DATA" / "EFL"
    / "2026 GRSM" / "GRSM_Culvert"
    / "04_Steven Updates after Review 062626"
    / "GRSM_Culvert 1 (3).kmz"
)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "culverts-sf-qc.geojson"

R_EARTH_M = 6371000.0
M2FT = 3.280839895


def _hav_ft(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_M * math.asin(math.sqrt(a)) * M2FT


def _polyline_ft(coords: list[list[float]]) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i][0], coords[i][1]
        lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
        total += _hav_ft(lat1, lon1, lat2, lon2)
    return total


def _parse_html_table(desc: str) -> dict:
    """Pull <td>key</td><td>value</td> rows out of the description HTML."""
    if not desc:
        return {}
    text = html.unescape(desc)
    out: dict = {}
    for m in re.finditer(
        r"<td[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>\s*([^<]*?)\s*</td>",
        text,
        flags=re.IGNORECASE,
    ):
        key = m.group(1).strip()
        val = m.group(2).strip()
        if not key or key.lower() in ("0", "fid"):
            if key.lower() == "fid":
                out["FID"] = val
            continue
        out.setdefault(key, val)
    return out


def _parse_coords(raw: str) -> list[list[float]]:
    """KML coordinate string -> list of [lon, lat] pairs (drop altitude)."""
    pts: list[list[float]] = []
    for tok in (raw or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0]); lat = float(parts[1])
        except ValueError:
            continue
        pts.append([lon, lat])
    return pts


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    if not KMZ_PATH.exists():
        print(f"ERROR: KMZ not found at {KMZ_PATH}", file=sys.stderr)
        return 1

    with zipfile.ZipFile(KMZ_PATH) as z:
        kml_name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
        if not kml_name:
            print("ERROR: KMZ has no .kml file", file=sys.stderr)
            return 1
        kml_bytes = z.read(kml_name)
    kml = kml_bytes.decode("utf-8", errors="replace")

    # Pull every Placemark block; for each grab name (often blank), the
    # HTML description (for attribute keys), and the first LineString.
    features: list[dict] = []
    skipped_no_geom = 0
    for pm in re.finditer(r"<Placemark[\s\S]*?</Placemark>", kml):
        body = pm.group(0)
        name_match = re.search(r"<name>([\s\S]*?)</name>", body)
        desc_match = re.search(r"<description>([\s\S]*?)</description>", body)
        ls_match   = re.search(r"<LineString[\s\S]*?<coordinates>([\s\S]*?)</coordinates>", body)
        if not ls_match:
            skipped_no_geom += 1
            continue
        coords = _parse_coords(ls_match.group(1))
        if len(coords) < 2:
            skipped_no_geom += 1
            continue
        # Strip CDATA wrappers from description
        desc_raw = desc_match.group(1) if desc_match else ""
        desc_clean = re.sub(r"^<!\[CDATA\[", "", desc_raw)
        desc_clean = re.sub(r"\]\]>$", "", desc_clean)
        attrs = _parse_html_table(desc_clean)
        attrs["_kml_name"] = (name_match.group(1).strip() if name_match else "")
        drawn = round(_polyline_ft(coords), 1)
        attrs["drawn_length_ft"] = drawn
        # XLSX length comparison if Length present
        xlen = _fnum(attrs.get("Length"))
        if xlen is not None:
            attrs["xlsx_length_ft"] = xlen
            attrs["length_delta_ft"] = round(drawn - xlen, 1)
            if xlen > 0:
                attrs["length_ratio"] = round(drawn / xlen, 2)
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": attrs,
        })

    geo = {
        "type": "FeatureCollection",
        "metadata": {
            "source_kmz": str(KMZ_PATH),
            "feature_count": len(features),
            "skipped_no_geometry": skipped_no_geom,
        },
        "features": features,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(geo, separators=(",", ":")) + "\n", encoding="utf-8")

    # Summary
    print(f"Read    : {KMZ_PATH}")
    print(f"Wrote   : {OUT}")
    print(f"Features: {len(features)}  (skipped {skipped_no_geom} w/o geometry)")
    if features:
        with_x = [f for f in features if "length_delta_ft" in f["properties"]]
        if with_x:
            deltas = [abs(f["properties"]["length_delta_ft"]) for f in with_x]
            deltas.sort()
            n = len(deltas)
            print(f"|drawn - xlsx| (ft):  n={n}  "
                  f"median={deltas[n // 2]:.1f}  "
                  f"p90={deltas[int(n * 0.9)]:.1f}  "
                  f"max={deltas[-1]:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
