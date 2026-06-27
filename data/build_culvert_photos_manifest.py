"""Build the culvert photo manifest for RoadWalk import.

Reads `MASTER_all_photos.csv` from the standalone QC project, walks
the three photo source folders (SF / JSG / MAY) listed in
`build_kmz.py`, converts each photo to a watermarked 1200-px JPEG at
quality 82, and emits a single JSON manifest the in-app importer
ingests.

Output is written next to the source CSV (NOT committed to the repo
— ~100 MB+ for 387 photos). The user picks it from disk via the
import modal's file input.

Watermark spec mirrors `drawWatermark()` in roadwalk.html:
  bottom band · 58% black bg · white monospace · font = max(12, w/48)
  line 1: lat, lng
  line 2: yyyy-mm-dd  hh:mm:ss
  line 3: <culvert_id> · <end>

Skips:
  - Rows whose `end` is "(video~)" (QC project's video marker) or
    whose file extension says video.
  - Rows whose culvert_id or section_id is empty.
  - Files that don't exist on disk OR fail to open / convert.

Requires:
    pip install pillow pillow-heif

Run from anywhere:
    python data/build_culvert_photos_manifest.py
"""

from __future__ import annotations

import base64
import csv
import io
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image, ImageOps, ImageDraw, ImageFont
except ImportError:
    print("ERROR: install Pillow — `pip install pillow pillow-heif`", file=sys.stderr)
    sys.exit(1)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    print("WARNING: pillow_heif not installed — .HEIC files will be skipped.")
    print("         Install with `pip install pillow-heif` to include them.")


# ── Paths ──────────────────────────────────────────────────────────
# Locations mirror build_kmz.py so the manifest pipeline + the legacy
# KMZ pipeline read the same source folders.
QC = (
    Path.home()
    / "OneDrive - AECOM" / "Documents" / "!AECOM" / "CLAUDE" / "PaveCollector"
    / "culvert_match"
)
CSV_PATH = QC / "MASTER_all_photos.csv"

DATA_ROOT = Path.home() / "OneDrive - AECOM" / "Documents" / "!DATA" / "NPS" / "GRSM"
PHOTO_DIRS = {
    "SF":  DATA_ROOT / "SF_Culverts_060226",
    "JSG": DATA_ROOT / "052926-060326JSG Scoping" / "Culverts",
    "MAY": DATA_ROOT / "051826-052126_JSG Scoping" / "Culverts",
}

# Manifest output — alongside the source CSV, NOT in the RoadWalk repo.
# Size: ~300 KB per photo × 387 photos ≈ 100 MB → not commit-friendly.
OUT_PATH = QC / "culvert_photos_manifest.json"

# Image processing — match the sign report's makeThumb defaults.
MAX_SIZE = 1200
JPEG_QUALITY = 82


# ── Watermark ─────────────────────────────────────────────────────
def _wm_font(size_px: int):
    """Pick a monospace face Pillow can resolve; fall back to default."""
    candidates = [
        "C:/Windows/Fonts/consolab.ttf",  # bold consolas, sharpest
        "C:/Windows/Fonts/consola.ttf",
        "consolab.ttf",
        "consola.ttf",
        "DejaVuSansMono-Bold.ttf",
        "DejaVuSansMono.ttf",
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size_px)
        except Exception:
            continue
    return ImageFont.load_default()


def burn_watermark(img: Image.Image, lines: list[str]) -> Image.Image:
    """Composite a bottom-band watermark over an RGB image. Spec
    matches the in-app drawWatermark function so the photos in the
    culvert report look identical to the sign report's."""
    if not lines:
        return img
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    fs = max(12, round(w / 48))
    font = _wm_font(fs)
    lh = round(fs * 1.5)
    pad_x = round(fs * 0.6)
    pad_y = round(fs * 0.45)
    band_h = len(lines) * lh + pad_y * 2
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([(0, h - band_h), (w, h)], fill=(0, 0, 0, int(0.58 * 255)))
    for i, ln in enumerate(lines):
        draw.text((pad_x, h - band_h + pad_y + i * lh), ln, font=font, fill=(255, 255, 255, 255))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


# ── Conversions ───────────────────────────────────────────────────
def to_jpeg_dataurl(src_path: Path, wm_lines: list[str]) -> str:
    """Read src_path with any pillow-supported decoder, apply EXIF
    rotation, resize to MAX_SIZE on the longest edge, burn the
    watermark, encode JPEG at JPEG_QUALITY and return as a data: URL."""
    img = Image.open(src_path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_SIZE:
        s = MAX_SIZE / max(w, h)
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    img = burn_watermark(img, wm_lines)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ── Parsing helpers ───────────────────────────────────────────────
_VIDEO_MARKERS = ("(video~)", "video", "vid", "(video)")
_VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "3gp"}


def _is_video(end: str, kind: str) -> bool:
    e = (end or "").strip().lower()
    k = (kind or "").strip().lower()
    return e in _VIDEO_MARKERS or k in _VIDEO_EXT


def _parse_dt(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace(" ", "T")).isoformat()
    except Exception:
        return s


def _fmt_dt_for_wm(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d  %H:%M:%S")
    except Exception:
        return iso


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Main ──────────────────────────────────────────────────────────
def main() -> int:
    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}", file=sys.stderr)
        return 1
    for name, p in PHOTO_DIRS.items():
        if not p.exists():
            print(f"WARNING: {name} folder missing: {p}")

    print(f"Reading manifest CSV: {CSV_PATH}")
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8-sig")))
    print(f"  {len(rows)} CSV row(s)")

    manifest = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "source_csv":  str(CSV_PATH),
        "image_settings": {
            "max_size_px": MAX_SIZE,
            "jpeg_quality": JPEG_QUALITY,
            "watermark": "bottom-band black 58% / white mono / lat-lng + datetime + culvert_id·end",
        },
        "photos": [],
    }

    skipped_video = 0
    skipped_missing_culvert = 0
    skipped_no_file: list[str] = []
    failed_decode: list[str] = []

    for i, r in enumerate(rows):
        cset = (r.get("photo_set") or "").strip()
        file = (r.get("file") or "").strip()
        kind = (r.get("kind") or "").strip()
        end = (r.get("end") or "").strip()
        cul_id = (r.get("culvert_id") or "").strip()
        sec_id = (r.get("section_id") or "").strip()

        if _is_video(end, kind):
            skipped_video += 1
            continue
        if not cul_id or not sec_id:
            skipped_missing_culvert += 1
            continue
        photo_dir = PHOTO_DIRS.get(cset)
        if not photo_dir:
            continue
        src = photo_dir / file
        if not src.exists():
            skipped_no_file.append(f"{cset}/{file}")
            continue

        iso = _parse_dt(r.get("datetime", ""))
        lat = _fnum(r.get("lat"))
        lon = _fnum(r.get("lon"))

        # Watermark lines.
        wm_lines = []
        wm_lines.append(f"{lat:.6f}, {lon:.6f}" if (lat is not None and lon is not None) else "")
        wm_lines.append(_fmt_dt_for_wm(iso))
        end_pretty = (end or "photo").capitalize()
        wm_lines.append(f"{cul_id} · {end_pretty}")

        try:
            data_url = to_jpeg_dataurl(src, wm_lines)
        except Exception as e:
            failed_decode.append(f"{cset}/{file}: {e}")
            continue

        manifest["photos"].append({
            "id":            f"{cset}_{Path(file).stem}",
            "culvert_id":    cul_id,
            "section_id":    sec_id,
            "end":           end,
            "captured_at":   iso,
            "lat":           lat,
            "lon":           lon,
            "photo_set":     cset,
            "original_file": file,
            "dist_m":        _fnum(r.get("dist_m")),
            "dataUrl":       data_url,
        })

        if (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(rows)}  (kept {len(manifest['photos'])})")

    print()
    print(f"Final manifest entries: {len(manifest['photos'])}")
    print(f"  skipped (video):           {skipped_video}")
    print(f"  skipped (missing cul/sec): {skipped_missing_culvert}")
    print(f"  skipped (file not found):  {len(skipped_no_file)}")
    for f in skipped_no_file[:10]:
        print(f"     ! {f}")
    if len(skipped_no_file) > 10:
        print(f"     … and {len(skipped_no_file) - 10} more")
    print(f"  failed (decode):           {len(failed_decode)}")
    for f in failed_decode[:10]:
        print(f"     ! {f}")

    print(f"\nWriting {OUT_PATH} …")
    OUT_PATH.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    size_mb = OUT_PATH.stat().st_size / 1048576
    print(f"  wrote {size_mb:.1f} MB")

    # Per-culvert coverage report.
    from collections import Counter
    by_pin = Counter(p["culvert_id"] for p in manifest["photos"])
    print(f"\nPer-culvert coverage:")
    for cid in sorted(by_pin):
        print(f"   {cid:<12}  {by_pin[cid]:>3} photo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
