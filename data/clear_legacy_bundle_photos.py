"""Drop legacy bundle-embedded photos from every culvert the KML+XLSX
pipeline touched, so the newly-imported XLSX-cell manifest photos
have a clean slate to link into.

Rules:
  - Only touches culvert pins.
  - Only touches pins tagged attrs.source == "SF_KML_070126"
    (i.e., the pipeline is the current source of truth for them).
  - Wipes attrs.photos entirely (embedded "Culvert N" style byte
    arrays from the pre-Fox era).
  - Wipes attrs.report_hidden_photos too — those hide keys point at
    array indices ("emb:0", "emb:1") that no longer make sense once
    attrs.photos is empty; if they're stale the report card's
    "Restore (N hidden)" chip would show ghost counts.
  - Leaves attrs.source == "AECOM_FOX" / anything else alone — those
    are the orphans we're deliberately preserving (A-CV-052 today,
    plus any user-flagged review pins).
  - Doesn't touch linked geophotos (those live in IDB, not in the
    bundle, and the XLSX manifest import will handle those directly).

Run right after import_culvert_kml_and_xlsx.py finishes:
    python data/clear_legacy_bundle_photos.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_PATH = ROOT / "data" / "prewalk-bundle.json"

SECTIONS_OF_INTEREST = {"A", "G", "H", "I"}
NEW_SOURCE_LABEL = "SF_KML_070126"


def main() -> int:
    if not BUNDLE_PATH.exists():
        print(f"ERROR: bundle not found at {BUNDLE_PATH}", file=sys.stderr)
        return 1
    print(f"Reading bundle: {BUNDLE_PATH}")
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))

    total_pins = 0
    cleared_pins = 0
    photos_dropped = 0
    hides_dropped = 0
    skipped_orphan = 0
    for sec in bundle.get("sections") or []:
        if sec.get("id") not in SECTIONS_OF_INTEREST:
            continue
        sec_id = sec["id"]
        for pin in sec.get("pins") or []:
            if (pin.get("kind") or "") != "culvert":
                continue
            total_pins += 1
            attrs = pin.get("attrs") or {}
            src = attrs.get("source")
            if src != NEW_SOURCE_LABEL:
                # Orphan or other-source — leave alone.
                if attrs.get("photos") or attrs.get("report_hidden_photos"):
                    skipped_orphan += 1
                continue
            n_photos = len(attrs.get("photos") or [])
            n_hides  = len(attrs.get("report_hidden_photos") or [])
            if n_photos or n_hides:
                cleared_pins += 1
                photos_dropped += n_photos
                hides_dropped += n_hides
                attrs.pop("photos", None)
                attrs.pop("report_hidden_photos", None)
            # Always stamp the flag on every SF_KML_070126 pin, even
            # if THIS run didn't have anything to drop. Rationale: the
            # bundle-side photos may already be empty (a previous run
            # of this script cleared them) while a user's IDB working
            # copy still holds the old embeds. The flag is what
            # applyBundle Pass 1b keys off to wipe the working copy.
            # Idempotent — later reloads see the flag, find nothing to
            # clear, and no-op.
            attrs["_legacy_photos_cleared"] = True

    BUNDLE_PATH.write_text(json.dumps(bundle, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    print()
    print(f"  Culvert pins in target sections:        {total_pins}")
    print(f"  Pins with legacy embeds cleared:        {cleared_pins}")
    print(f"    attrs.photos entries dropped:         {photos_dropped}")
    print(f"    attrs.report_hidden_photos dropped:   {hides_dropped}")
    print(f"  Orphan pins preserved (source != {NEW_SOURCE_LABEL}) "
          f"that had photos or hides: {skipped_orphan}")
    print(f"\nWrote {BUNDLE_PATH}.")
    print("Linked geophotos (from Import matched photos) are IDB-only "
          "and untouched by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
