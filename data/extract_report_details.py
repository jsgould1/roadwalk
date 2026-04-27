"""
Extract per-asset details (length, type, dates, etc.) from the GIP and WIP
PDF text layer.

This replaces the old RapidOCR pipeline (ocr_report_details.py). The PDFs
have a clean text layer — no OCR is needed. We use PyMuPDF (fitz) to pull
positioned word boxes and reconstruct the table-cell relationships.

The PDFs use two layouts that both carry length data:

  Layout A — Multi-asset summary table (most common in the reduced reports):
    Column 1: "GRSM-<route>-<mp>-<side>" + inspection-date underneath.
    Column 2: Length in ft (just the integer).
    Column 3: Type (multi-line stacked, e.g. "STEEL-BACKED / TIMBER WITHOUT / BLOCKOUT").
    Column 4-5: Begin / End treatments.
    Column 6: *Repair cost ($N.NN).
    Each row's words are positioned at predictable x bands and matching y.

  Layout B — Single-asset detail page:
    "Barrier ID:" / "Wall ID:" with the ID adjacent.
    "Length (ft.):" with the value adjacent.
    Plus rating, type, material, speed limit, hazard, etc.
    Detect by exactly-1 asset_id on the page.

Output JSON shape (matches what build_report_polylines.py already expects):
{
  "source": "<pdf filename>",
  "extraction_method": "fitz text-layer",
  "total_assets": N,
  "assets_with_length": N,
  "assets": {
    "<route>-<mp>-<side>": {
      "asset_id": "<route>-<mp>-<side>",
      "length_ft": 319.0,
      "inspection_date": "9/24/2010",
      "type": "STONE MASONRY WITHOUT CONCRETE CORE WALL",
      "rating": 58.20,            # detail-page only
      "_source_page": 42,
      ...
    }, ...
  }
}
"""
import fitz, json, os, re, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

DATA = os.path.dirname(os.path.abspath(__file__))
NPS  = r'C:\Users\gouldj\OneDrive - AECOM\Documents\!DATA\NPS'

TARGET_ROUTES = {'0010N', '0010S', '0012ZZ'}

# Asset-id pattern. Underscores or hyphens between components — the table
# uses hyphens; photo-page filenames use underscores.
ASSET_RE = re.compile(r'GRSM[_-](00\d{2}[A-Z]{0,2})[_-](\d+\.\d+)[_-]([LRBC])')
# Loose "any GRSM-<route>" match for filtering, returns (route, mp, side)
def parse_asset(text):
    m = ASSET_RE.search(text)
    if not m: return None
    route, mp, side = m.group(1), m.group(2), m.group(3)
    if route not in TARGET_ROUTES:
        return None
    return f'{route}-{mp}-{side}', route, float(mp), side

DATE_RE = re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$')
INT_RE  = re.compile(r'^\d{1,5}$')         # pure integer length (33 .. 99999)
COST_RE = re.compile(r'^\$\d')             # repair cost token (starts $)


def get_words(page):
    """Return [(x0,y0,x1,y1,text,bno,lno,wno), ...]."""
    return page.get_text('words')


def extract_summary_table(words):
    """Return list of {asset_id, length_ft, inspection_date} for a multi-asset table page."""
    # Find asset-id words
    id_boxes = []
    for w in words:
        x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
        info = parse_asset(t)
        if info:
            asset_id, route, mp, side = info
            id_boxes.append({'aid': asset_id, 'route': route, 'mp': mp,
                             'side': side, 'x0': x0, 'y0': y0,
                             'x1': x1, 'y1': y1, 'cy': (y0+y1)/2})
    if len(id_boxes) < 2:
        return []   # not a summary-table layout

    # Column-1 (asset_id) x-band: take the median x0 of id boxes
    id_x0s = sorted(b['x0'] for b in id_boxes)
    id_col_x0 = id_x0s[len(id_x0s)//2]
    # Filter to ids in the table column (drops map-label asset ids)
    id_boxes = [b for b in id_boxes if abs(b['x0'] - id_col_x0) < 30]
    if len(id_boxes) < 2:
        return []

    # Sort rows top→bottom and infer row height from spacing
    id_boxes.sort(key=lambda b: b['cy'])
    if len(id_boxes) >= 2:
        spacings = [id_boxes[i+1]['cy'] - id_boxes[i]['cy'] for i in range(len(id_boxes)-1)]
        spacings = [s for s in spacings if s > 5]
        row_h = sorted(spacings)[len(spacings)//2] if spacings else 47
    else:
        row_h = 47

    # For each id row, find the length and date
    rows = []
    for b in id_boxes:
        row_y_top = b['cy'] - 3
        row_y_bot = b['cy'] + row_h - 3
        # Length: pure-integer word, x > id's right edge, y close to id's cy
        length = None
        date   = None
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            cy = (y0+y1)/2
            if not (row_y_top - 5 <= cy <= row_y_bot):
                continue
            if x0 <= b['x1']:
                # Within the same column or to the left
                if DATE_RE.match(t) and abs(cy - b['cy']) < row_h:
                    # Inspection date sits in same x-band as asset_id, slightly below
                    if cy > b['cy'] + 5:
                        date = t
                continue
            if INT_RE.match(t) and length is None:
                length = float(t)
                # Length is the leftmost integer to the right of the id
                # We accept the first one we see (since words come in y,x order roughly,
                # but to be safe, sort all candidates by x and pick leftmost)
        # Re-scan more carefully for length: leftmost integer in row band, x > id.x1
        cands = []
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            cy = (y0+y1)/2
            if row_y_top - 5 <= cy <= row_y_bot - 8 and x0 > b['x1'] and INT_RE.match(t):
                cands.append((x0, t))
        cands.sort()
        length = float(cands[0][1]) if cands else None

        # Type: stacked words in the "Barrier/Wall Type" column (header is at
        # x≈270 in the page samples we measured). Tightly clamp the x-band so
        # we don't pick up the End-Treatment columns (which start around 371
        # with text like "SBT/LOG FLARED" / "NONE"). Type entries are all
        # uppercase, often hyphenated (STEEL-BACKED, MASONRY, etc.).
        # Type words measured at x≈241-287; the "Begin Treatment" column
        # starts at x≈346 (SBT/LOG, NONE). 340 is a safe boundary.
        TYPE_X_MIN, TYPE_X_MAX = 230, 340
        type_words = []
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            cy = (y0+y1)/2
            if not (row_y_top - 5 <= cy <= row_y_bot):
                continue
            if not (TYPE_X_MIN <= x0 < TYPE_X_MAX):
                continue
            if re.match(r'^[A-Z][A-Z\-/]*$', t) and len(t) > 1:
                type_words.append((x0, cy, t))
        type_words.sort(key=lambda w: (w[1], w[0]))
        type_text = ' '.join(t for _, _, t in type_words) if type_words else None

        row = {'asset_id': b['aid'], 'route': b['route'], 'mp': b['mp'], 'side': b['side']}
        if length is not None:
            row['length_ft'] = length
        if date:
            row['inspection_date'] = date
        if type_text:
            row['type'] = type_text
        rows.append(row)
    return rows


def extract_detail_page(words, page_text):
    """Return single-asset dict from a Layout-B detail page, or None."""
    # Find the lone asset id
    id_boxes = []
    for w in words:
        info = parse_asset(w[4])
        if info:
            id_boxes.append((info, w))
    # Detail-page heuristic: page text contains "Barrier ID:" or "Wall ID:" label
    if 'Barrier ID:' not in page_text and 'Wall ID:' not in page_text:
        return None
    if not id_boxes:
        return None
    # Take the first asset id (page may also reference adjacent ids in nav strips)
    info, w_id = id_boxes[0]
    aid, route, mp, side = info
    fields = {'asset_id': aid, 'route': route, 'mp': mp, 'side': side}

    # Build a label→value lookup using bbox proximity. For each known label,
    # find the label word, then the closest non-label word to its right within
    # ±10 px y.
    def find_label_value(label_re, want_re=None, max_x_dist=400):
        """Return the word value adjacent to a label matching label_re."""
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            if not label_re.match(t):
                continue
            # Look at words to the right within ±8 px y
            cy = (y0+y1)/2
            cands = []
            for w2 in words:
                if w2 is w: continue
                xa, ya, xb, yb, tt = w2[0], w2[1], w2[2], w2[3], w2[4]
                cy2 = (ya+yb)/2
                if abs(cy2 - cy) > 10: continue
                if xa < x1 - 1: continue
                if xa - x1 > max_x_dist: continue
                if want_re and not want_re.match(tt): continue
                # Skip pure label tokens like ":"
                if tt in (':', '(ft.):', '(In.):'): continue
                cands.append((xa - x1, tt, w2))
            if cands:
                cands.sort()
                # Concatenate continuous words on same line (e.g., "STONE MASONRY WITHOUT...")
                vx_thresh = 40
                ys = (cands[0][2][1] + cands[0][2][3]) / 2
                joined = []
                last_x1 = None
                for w2 in sorted(words, key=lambda w_: w_[0]):
                    xa, ya, xb, yb, tt = w2[0], w2[1], w2[2], w2[3], w2[4]
                    cy2 = (ya+yb)/2
                    if abs(cy2 - ys) > 6: continue
                    if xa < x1: continue
                    if want_re and not want_re.match(tt) and joined:
                        # if we want a strict pattern, stop after first match
                        break
                    if last_x1 is not None and xa - last_x1 > vx_thresh:
                        break
                    if tt in (':', '(ft.):', '(In.):'):
                        last_x1 = xb; continue
                    joined.append(tt)
                    last_x1 = xb
                return ' '.join(joined).strip() or cands[0][1]
        return None

    # Length
    v = find_label_value(re.compile(r'^Length$'),
                         want_re=re.compile(r'^\d+(?:\.\d+)?$'))
    if v:
        try: fields['length_ft'] = float(re.search(r'\d+(?:\.\d+)?', v).group(0))
        except: pass
    # Inspection Date
    v = find_label_value(re.compile(r'^Inspection$'),
                         want_re=re.compile(r'^\d{1,2}/\d{1,2}/\d{2,4}$'))
    if v: fields['inspection_date'] = v.split()[0]
    # Rating
    v = find_label_value(re.compile(r'^Rating:?$'),
                         want_re=re.compile(r'^\d+(?:\.\d+)?$'))
    if v:
        try: fields['rating'] = float(v.split()[0])
        except: pass
    # Type (Barrier Type or Wall Type) — uppercase phrase to the right
    for lbl in ('Barrier', 'Wall'):
        v = find_label_value(re.compile(rf'^{lbl}$'))
        if v and re.match(r'^[A-Z]', v):
            fields['type'] = v
            break
    # Speed Limit
    v = find_label_value(re.compile(r'^Speed$'),
                         want_re=re.compile(r'^\d+$'))
    if v:
        try: fields['speed_limit'] = int(re.search(r'\d+', v).group(0))
        except: pass
    # Hazard Behind
    v = find_label_value(re.compile(r'^Hazard$'))
    if v:
        m = re.search(r'\b(HIGH|MEDIUM|LOW|NONE)\b', v.upper())
        if m: fields['hazard_behind'] = m.group(1)
    # Crashworthy
    v = find_label_value(re.compile(r'^Crashworthy'))
    if v:
        m = re.search(r'\b(YES|NO)\b', v.upper())
        if m: fields['crashworthy'] = m.group(1)
    # Test Level
    v = find_label_value(re.compile(r'^Test$'))
    if v:
        m = re.search(r'TL-?(\d)', v)
        if m: fields['test_level'] = f'TL-{m.group(1)}'
    return fields


def extract_pdf(pdf_path, source_label):
    """Run extraction across a PDF, returning the merged asset dict."""
    doc = fitz.open(pdf_path)
    per_asset = {}
    pages_summary = pages_detail = pages_skipped = 0
    for i in range(len(doc)):
        page = doc[i]
        words = get_words(page)
        if not words: continue
        text = page.get_text()
        # Summary-table extraction first
        rows = extract_summary_table(words)
        if rows:
            pages_summary += 1
            for row in rows:
                aid = row['asset_id']
                # Don't overwrite richer entries with sparser ones
                if aid in per_asset and 'length_ft' in per_asset[aid] and 'length_ft' not in row:
                    continue
                row['_source_page'] = i + 1
                # merge — prefer existing fields if richer
                merged = dict(per_asset.get(aid, {}))
                for k, v in row.items():
                    if k not in merged or (k == 'length_ft' and 'length_ft' not in merged):
                        merged[k] = v
                per_asset[aid] = merged
            continue
        # Detail-page extraction
        det = extract_detail_page(words, text)
        if det:
            pages_detail += 1
            aid = det['asset_id']
            det['_source_page'] = i + 1
            merged = dict(per_asset.get(aid, {}))
            for k, v in det.items():
                merged[k] = v
            per_asset[aid] = merged
            continue
        pages_skipped += 1
    doc.close()
    with_length = sum(1 for a in per_asset.values() if 'length_ft' in a)
    out = {
        'source': os.path.basename(pdf_path),
        'extraction_method': 'fitz text-layer (no OCR)',
        'target_routes': sorted(TARGET_ROUTES),
        'total_assets': len(per_asset),
        'assets_with_length': with_length,
        'pages_summary_table': pages_summary,
        'pages_detail': pages_detail,
        'pages_skipped': pages_skipped,
        'assets': per_asset,
    }
    return out


def main():
    print('=== Extracting GIP guardrail details ===')
    gip = extract_pdf(os.path.join(NPS, 'GRSM_GIPReport_reduced.pdf'), 'GIP')
    print(f"  total assets: {gip['total_assets']}")
    print(f"  with length:  {gip['assets_with_length']}")
    print(f"  summary pages: {gip['pages_summary_table']}, detail pages: {gip['pages_detail']}, skipped: {gip['pages_skipped']}")
    out_path = os.path.join(DATA, 'gip-tier3-details.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(gip, f, indent=2)
    print(f'  wrote {out_path}')

    print()
    print('=== Extracting WIP wall details ===')
    wip = extract_pdf(os.path.join(NPS, 'GRSM_WIPReport.pdf'), 'WIP')
    print(f"  total assets: {wip['total_assets']}")
    print(f"  with length:  {wip['assets_with_length']}")
    print(f"  summary pages: {wip['pages_summary_table']}, detail pages: {wip['pages_detail']}, skipped: {wip['pages_skipped']}")
    out_path = os.path.join(DATA, 'wip-tier3-details.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(wip, f, indent=2)
    print(f'  wrote {out_path}')

    # Print a sample row from each
    for label, d in [('GIP', gip), ('WIP', wip)]:
        if d['assets']:
            sample_aid = next(iter(d['assets']))
            print()
            print(f'  {label} sample ({sample_aid}):')
            for k, v in d['assets'][sample_aid].items():
                print(f'    {k}: {v}')

if __name__ == '__main__':
    main()
