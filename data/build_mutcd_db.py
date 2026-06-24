"""Build data/mutcd_signs.json from data/mutcd_raw.txt.

The raw file is a pipe-delimited dump scraped from trafficsign.us
(W1-W25, R1-R16, M1-M6, D1-D17 minus D7+D16 which 404, OM).
Each non-blank, non-comment line is CODE|NAME|SIZE|IMAGE.

This script normalizes the dump and emits a compact JSON with one
entry per sign:

    { "c": "W6-3", "n": "Two Way Traffic", "cat": "W", "img": "w6-3.gif", "sz": "36x36" (optional) }

The PWA's existing window._RW.MUTCD_SIGNS array uses the same shape,
so this JSON can either be fetched at runtime or baked into roadwalk.html.

Run from the RoadWalk root: `python data/build_mutcd_db.py`
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW  = ROOT / "mutcd_raw.txt"
OUT  = ROOT / "mutcd_signs.json"

# Series prefix → category short code. Order matters for the bucket
# headings written to the JSON.
SERIES_ORDER = ["W", "R", "M", "D", "OM"]
CATEGORY_LABEL = {
    "W":  "warning",
    "R":  "regulatory",
    "M":  "route_marker",
    "D":  "guide",
    "OM": "object_marker",
}

# Codes with a 2-letter prefix come first so OMx-* never gets shoved
# into the M bucket.
SERIES_RE = re.compile(r"^(OM|[A-Z])(\d)")

def category_for(code: str) -> str:
    m = SERIES_RE.match(code)
    if not m:
        raise ValueError(f"unrecognized MUTCD code shape: {code!r}")
    return m.group(1)

def parse_raw(text: str):
    signs = []
    seen_codes = set()
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 4:
            raise ValueError(
                f"line {lineno}: expected 4 |-fields, got {len(parts)}: {raw_line!r}"
            )
        code, name, size, image = (p.strip() for p in parts)
        if code in seen_codes:
            # Duplicates are real on the scrape (e.g., two pages list the
            # same code) — keep the first one wins.
            continue
        seen_codes.add(code)
        entry = {
            "c":   code,
            "n":   name,
            "cat": category_for(code),
            "img": image,
        }
        if size and size != "—":
            entry["sz"] = size
        signs.append(entry)
    # Stable sort by category bucket, then by the numeric piece of the
    # code so W1-1 / W1-2 / W1-10 land in numeric order rather than
    # ASCII order (W1-1 / W1-10 / W1-2).
    def sort_key(s):
        cat_idx = SERIES_ORDER.index(s["cat"])
        m = re.match(r"^(OM|[A-Z])(\d+)-(\d+)([A-Za-z]*)", s["c"])
        if not m:
            return (cat_idx, 9999, 9999, s["c"])
        return (cat_idx, int(m.group(2)), int(m.group(3)), m.group(4) or "")
    signs.sort(key=sort_key)
    return signs

def main():
    text = RAW.read_text(encoding="utf-8")
    signs = parse_raw(text)
    payload = {
        "generated":  "2026-06-24",
        "source":     "https://www.trafficsign.us/",
        "categories": CATEGORY_LABEL,
        "count":      len(signs),
        "signs":      signs,
    }
    OUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    by_cat = {}
    for s in signs:
        by_cat[s["cat"]] = by_cat.get(s["cat"], 0) + 1
    print(f"Wrote {OUT.relative_to(ROOT.parent)} — {len(signs)} signs total")
    for cat in SERIES_ORDER:
        print(f"  {cat:>3} ({CATEGORY_LABEL[cat]:>13}): {by_cat.get(cat, 0)}")

if __name__ == "__main__":
    main()
