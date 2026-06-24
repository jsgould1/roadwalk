"""Merge AECOM_FOX culvert pins into prewalk-bundle.json's sections[A].pins.

Idempotent — re-running drops any existing source=AECOM_FOX pins on Section A
before appending the fresh set, so the script can be rerun whenever the
culvert ingestion is regenerated.

Generates a 26-char Crockford-base32 ULID per pin so they merge cleanly
with the existing pin schema (kind, source, ulid, sta_ft, attrs, …).

Run from the RoadWalk root:
    python data/merge_culvert_aecom_fox_into_bundle.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

ROOT       = Path(__file__).resolve().parent
BUNDLE     = ROOT / "prewalk-bundle.json"
CULVERT    = ROOT / "culvert-aecom-fox-bundle.json"
SECTION_ID = "A"
SOURCE_TAG = "AECOM_FOX"

# Crockford base32 — same alphabet ULID v0 uses; uppercase, no I/L/O/U.
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    """48-bit ms timestamp + 80-bit random, encoded as 26 base32 chars."""
    ts_ms = int(time.time() * 1000)
    rand  = int.from_bytes(secrets.token_bytes(10), "big")
    n = (ts_ms << 80) | rand                         # 128-bit value
    out = []
    for _ in range(26):
        out.append(_B32[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def main() -> int:
    bundle  = json.loads(BUNDLE.read_text(encoding="utf-8"))
    culvert = json.loads(CULVERT.read_text(encoding="utf-8"))

    sec_a = next((s for s in bundle["sections"] if s.get("id") == SECTION_ID), None)
    if sec_a is None:
        print(f"ERROR: Section {SECTION_ID} not found in bundle", file=sys.stderr)
        return 1
    pins = sec_a.setdefault("pins", [])

    # Drop any prior AECOM_FOX culvert pins so re-runs don't duplicate.
    before = len(pins)
    pins[:] = [p for p in pins if (p.get("source") or "") != SOURCE_TAG]
    dropped = before - len(pins)
    if dropped:
        print(f"Dropped {dropped} prior {SOURCE_TAG} pin(s) before merge")

    # Append fresh pins. ULIDs are minted here so the bundle stays the
    # single source of truth for stable pin identity.
    fresh = []
    for p in culvert["pins"]:
        clone = dict(p)
        clone["ulid"] = _ulid()
        fresh.append(clone)
    pins.extend(fresh)

    # Stable sort so the bundle diff is readable across reruns.
    pins.sort(key=lambda x: (x.get("sta_ft") or 0, x.get("id") or ""))

    BUNDLE.write_text(
        json.dumps(bundle, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    new_size_mb = os.path.getsize(BUNDLE) / (1024 * 1024)
    print(f"Merged {len(fresh)} {SOURCE_TAG} pins into Section {SECTION_ID}.")
    print(f"  Section {SECTION_ID} pin count: {len(pins)}")
    print(f"  prewalk-bundle.json: {new_size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
