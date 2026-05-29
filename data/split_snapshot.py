"""Split a RoadWalk snapshot JSON into main + photos files.

Browsers can't hold a single JS string larger than ~500 MB (V8's
String::kMaxLength). A RoadWalk project with thousands of base64
field-photos can produce a snapshot well over that. The exporter
writes the file fine by streaming, but the importer's
`file.text()` call silently truncates oversized strings, after
which `JSON.parse` reports "Unexpected end of JSON input".

This tool splits one large snapshot into two files the streaming
importer can handle without ever building a single huge string:

    <name>.main.json          # bundle + field_data + photos
                              # (geophotos replaced with null)
    <name>.geophotos.ndjson   # one JSON object per line, one per
                              # field photo

Both files are produced by reading the input line-by-line, so even
multi-GB snapshots fit in modest RAM.

Usage:
    python split_snapshot.py <snapshot.json>

Writes both output files alongside the input. The streaming
importer (file picker in RoadWalk → pick BOTH files) restores them
in one pass.

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


def split(in_path):
    if not os.path.exists(in_path):
        raise SystemExit(f"Input file not found: {in_path}")

    in_size = os.path.getsize(in_path)
    base = re.sub(r"\.json$", "", in_path, flags=re.I)
    main_path = base + ".main.json"
    ndjson_path = base + ".geophotos.ndjson"

    print(f"Reading: {in_path}  ({in_size / 1048576:.1f} MB)")

    # State machine driven by the line structure of the streamed export:
    #   0 = top-level (lines copied to main.json verbatim)
    #   1 = inside the geophotos array (each line is one record,
    #       trailing comma stripped, written to NDJSON)
    #   2 = closed the array (top-level again until file end)
    state = 0
    photo_count = 0
    bytes_read = 0
    last_report = 0

    with open(in_path, "r", encoding="utf-8") as fin, \
            open(main_path, "w", encoding="utf-8") as fmain, \
            open(ndjson_path, "w", encoding="utf-8") as fnd:

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
                        # and skip the NDJSON file.
                        fmain.write(line)
                    else:
                        # Replace the array with `null` in main.json so
                        # the structure stays valid; photos live in the
                        # companion NDJSON.
                        fmain.write('"geophotos":null\n')
                        state = 1
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
                fnd.write(rec + "\n")
                photo_count += 1

            else:  # state == 2: top-level after the array closed
                fmain.write(line)

            # Periodic progress for very large files
            if bytes_read - last_report > 50 * 1048576:
                print(
                    f"  ... {bytes_read / 1048576:.0f} MB read, "
                    f"{photo_count:,} geophotos collected"
                )
                last_report = bytes_read

    main_size = os.path.getsize(main_path)
    nd_size = os.path.getsize(ndjson_path)
    print()
    print(f"Wrote: {main_path}  ({main_size / 1048576:.2f} MB)")
    print(f"Wrote: {ndjson_path}  ({nd_size / 1048576:.1f} MB, "
          f"{photo_count:,} geophotos)")
    print()
    print("Import on the destination laptop:")
    print("  1. Open RoadWalk")
    print("  2. Click 'Import Project' (or wherever you usually load")
    print("     a snapshot)")
    print("  3. In the file picker, select BOTH files (Ctrl+click)")
    print("  4. The importer will read main.json normally and stream")
    print("     the NDJSON photos in batches.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    split(sys.argv[1])
