"""One-shot dedupe for culvert pins with duplicate attrs.fox_order.

The AECOM_FOX ingestion + merge sequence pre-dating idempotency
checks occasionally inserted the same Excel row twice (different
ulids, identical Order/photo/notes). The Phase 1 GRSM sync surfaces
the duplicate via a warning. This script collapses each duplicate
group to a single pin — keeps the FIRST encountered (lowest sta_ft
typically, which matches the original ingestion sweep), drops the
rest.

Idempotent. Reports what it removed.

Run from the RoadWalk root:
    python data/dedupe_culvert_fox_orders.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUNDLE_PATH = ROOT / "prewalk-bundle.json"


def main() -> int:
    bundle = json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    total_dropped = 0
    drop_log: list[tuple[str, int, str, str]] = []
    for sec in bundle.get("sections", []):
        seen: dict[int, dict] = {}
        kept: list[dict] = []
        for pin in sec.get("pins", []):
            if not pin or pin.get("kind") != "culvert":
                kept.append(pin)
                continue
            fo = (pin.get("attrs") or {}).get("fox_order")
            if not isinstance(fo, int):
                kept.append(pin)
                continue
            if fo in seen:
                drop_log.append((
                    sec.get("id", "?"), fo, pin.get("id", "?"), pin.get("ulid", "?")
                ))
                total_dropped += 1
                continue
            seen[fo] = pin
            kept.append(pin)
        sec["pins"] = kept

    if total_dropped:
        print(f"Dropped {total_dropped} duplicate pin(s):")
        for sec_id, fo, pid, ulid in drop_log:
            print(f"  section {sec_id}  fox_order {fo:>3}  id={pid}  ulid={ulid}")
    else:
        print("No duplicates found — nothing to do.")

    BUNDLE_PATH.write_text(json.dumps(bundle, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"\nWrote {BUNDLE_PATH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
