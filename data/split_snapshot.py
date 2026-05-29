"""Split a RoadWalk snapshot JSON into main + chunked photos files.

Browsers can't hold a single JS string larger than ~500 MB (V8's
String::kMaxLength). A RoadWalk project with thousands of base64
field-photos can produce a snapshot well over that. The exporter
writes the file fine by streaming, but the importer's
`file.text()` call silently truncates oversized strings, after
which `JSON.parse` reports "Unexpected end of JSON input".

This tool splits one large snapshot into:

    <name>.main.json                 # bundle + field_data + legacy
                                     # photos (geophotos replaced
                                     # with null)
    <name>.geophotos.001.ndjson      # first ≤250 MB of geophoto
                                     # records, one per line
    <name>.geophotos.002.ndjson      # next chunk
    <name>.geophotos.NNN.ndjson      # ...

Chunked NDJSON makes large snapshots easier to transfer (USB,
cloud sync, email) and resumable on failure — each ~250 MB file
syncs independently. The streaming importer in RoadWalk reads
main.json normally, then iterates every NDJSON file in the picker
selection in order.

Reads the input line-by-line so multi-GB snapshots fit in modest
RAM.

Usage:
    python split_snapshot.py <snapshot.json> [chunk_mb]

  chunk_mb (optional, default 250) — target size per geophoto
  chunk file in MB. Smaller chunks → more files but easier transfer.

Writes all output files alongside the input. The RoadWalk
importer (file picker → select main.json + ALL .NNN.ndjson files
with Ctrl+A or Shift+click) restores them in one pass.

Constraints on the input:
    Must be the streamed output produced by `_streamExport` (one
    key per line at the top level, one geophoto record per line
    inside the `"geophotos":[ ... ]` array). Pretty-printed JSON
    or JSON written by older non-streaming code paths is not
    supported by this tool. Re-export from RoadWalk if needed.
"""
import os
import re
import sys


def split(in_path, chunk_mb=250):
    if not os.path.exists(in_path):
        raise SystemExit(f"Input file not found: {in_path}")

    chunk_bytes = chunk_mb * 1024 * 1024
    in_size = os.path.getsize(in_path)
    base = re.sub(r"\.json$", "", in_path, flags=re.I)
    main_path = base + ".main.json"

    print(f"Reading: {in_path}  ({in_size / 1048576:.1f} MB)")
    print(f"Chunk size: {chunk_mb} MB per geophoto file")
    print()

    # State machine driven by the line structure of the streamed export:
    #   0 = top-level (lines copied to main.json verbatim)
    #   1 = inside the geophotos array (each line is one record,
    #       trailing comma stripped, written to NDJSON; rotates to
    #       a new chunk file when current one exceeds chunk_bytes)
    #   2 = closed the array (top-level again until file end)
    state = 0
    photo_count = 0
    bytes_read = 0
    last_report = 0

    # NDJSON chunk rotation state
    chunk_idx = 0
    chunk_paths = []     # all chunk files written
    chunk_bytes_cur = 0
    fnd = None

    def open_next_chunk():
        nonlocal chunk_idx, fnd, chunk_bytes_cur
        if fnd is not None:
            fnd.close()
        chunk_idx += 1
        path = f"{base}.geophotos.{chunk_idx:03d}.ndjson"
        chunk_paths.append(path)
        chunk_bytes_cur = 0
        fnd = open(path, "w", encoding="utf-8")

    with open(in_path, "r", encoding="utf-8") as fin, \
            open(main_path, "w", encoding="utf-8") as fmain:

        for line in fin:
            bytes_read += len(line.encode("utf-8"))

            if state == 0:
                # Watch for `"geophotos":` — either inline null
                # ("geophotos":null) or the start of the array
                # ("geophotos":[ ).
                stripped = line.lstrip()
                if stripped.startswith('"geophotos":'):
                    if "null" in stripped:
                        # No photos in this snapshot — pass through
                        # and skip the NDJSON files.
                        fmain.write(line)
                    else:
                        # Replace the array with `null` in main.json so
                        # the structure stays valid; photos live in the
                        # companion NDJSON chunks.
                        fmain.write('"geophotos":null\n')
                        state = 1
                        open_next_chunk()
                else:
                    fmain.write(line)

            elif state == 1:
                stripped = line.strip()
                if stripped in ("]", "],"):
                    # End of the array — back to top-level
                    state = 2
                    continue
                if not stripped:
                    continue
                # Each record is on its own line, optionally followed
                # by a comma. Strip the comma + write as NDJSON.
                rec = stripped.rstrip(",")
                rec_with_nl = rec + "\n"
                rec_bytes = len(rec_with_nl.encode("utf-8"))
                # Rotate before writing if this record would push the
                # current chunk over the limit (and at least one record
                # is already in it — don't rotate to an empty file).
                if chunk_bytes_cur > 0 and chunk_bytes_cur + rec_bytes > chunk_bytes:
                    open_next_chunk()
                fnd.write(rec_with_nl)
                chunk_bytes_cur += rec_bytes
                photo_count += 1

            else:  # state == 2: top-level after the array closed
                fmain.write(line)

            # Periodic progress for very large files
            if bytes_read - last_report > 50 * 1048576:
                print(
                    f"  ... {bytes_read / 1048576:.0f} MB read, "
                    f"{photo_count:,} geophotos, "
                    f"{chunk_idx} chunk file(s)"
                )
                last_report = bytes_read

    if fnd is not None:
        fnd.close()

    main_size = os.path.getsize(main_path)
    print()
    print(f"Wrote: {main_path}  ({main_size / 1048576:.2f} MB)")
    for p in chunk_paths:
        sz = os.path.getsize(p) / 1048576
        print(f"Wrote: {p}  ({sz:.1f} MB)")
    print(f"\n{photo_count:,} geophotos across {len(chunk_paths)} chunk file(s)")
    print()
    print("Import on the destination laptop:")
    print("  1. Open RoadWalk")
    print("  2. Click 'Import Project' (or wherever you usually load")
    print("     a snapshot)")
    print("  3. In the file picker, navigate to the folder with the")
    print("     split files. Select ALL of them (Ctrl+A) — the main.json")
    print(f"     plus every {os.path.basename(base)}.geophotos.NNN.ndjson")
    print("  4. The importer reads main.json normally, then streams each")
    print("     chunk in order, restoring photos in 200-record batches.")


if __name__ == "__main__":
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(__doc__)
        sys.exit(1)
    in_path = sys.argv[1]
    chunk_mb = int(sys.argv[2]) if len(sys.argv) == 3 else 250
    split(in_path, chunk_mb)
