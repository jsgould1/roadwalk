"""Convert the legacy `culvert_photos_manifest.json` to streaming
`culvert_photos_manifest.jsonl`.

The big-array format choked the importer because Edge's
`FileReader.readAsText` + `JSON.parse` peak at ~3× the file size in
memory (UTF-16 string + parsed graph). JSONL writes one record per
line so the importer can stream-read with a fixed peak.

Output format:
  • Line 1: header object — { format, exported_at, total, culvert_ids,
    image_settings }
  • Line 2…N: one photo record per line — same keys as the existing
    JSON, dataUrl included.

Run from anywhere (no args):
    python data/convert_manifest_to_jsonl.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime

QC = (
    Path.home()
    / "OneDrive - AECOM" / "Documents" / "!AECOM" / "CLAUDE" / "PaveCollector"
    / "culvert_match"
)
IN_PATH  = QC / "culvert_photos_manifest.json"
OUT_PATH = QC / "culvert_photos_manifest.jsonl"


def main() -> int:
    if not IN_PATH.exists():
        print(f"ERROR: {IN_PATH} not found.", file=sys.stderr)
        return 1
    print(f"Reading  {IN_PATH}  ({IN_PATH.stat().st_size / 1048576:.1f} MB)…")
    manifest = json.loads(IN_PATH.read_text(encoding="utf-8"))
    photos = manifest.get("photos") or []
    if not photos:
        print("ERROR: input manifest has no photos[] array.", file=sys.stderr)
        return 1

    culvert_ids = sorted({p.get("culvert_id") for p in photos if p.get("culvert_id")})
    header = {
        "format":         "culvert_photos_manifest_jsonl_v1",
        "exported_at":    manifest.get("exported_at") or datetime.now().isoformat(timespec="seconds"),
        "source_csv":     manifest.get("source_csv", ""),
        "total":          len(photos),
        "culvert_ids":    culvert_ids,
        "image_settings": manifest.get("image_settings", {}),
    }
    print(f"  {len(photos)} photo(s) covering {len(culvert_ids)} culvert(s).")
    print(f"Writing  {OUT_PATH} …")
    with OUT_PATH.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, separators=(",", ":")))
        fh.write("\n")
        for ph in photos:
            fh.write(json.dumps(ph, separators=(",", ":")))
            fh.write("\n")
    size_mb = OUT_PATH.stat().st_size / 1048576
    print(f"  wrote {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
