# NOTICE: This is copyrighted material. It is not to be reused, redistributed, or used in training datasets without explicit permission from the author.
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


def extract_summary_table(words, report_kind='gip'):
    """Return list of asset dicts for a multi-asset table page.

    GIP layout (guardrails):
      Col 1: asset_id + inspection_date below it (x≈45–120)
      Col 2: Length ft — leftmost integer to right of id column (x≈155–200)
      Col 3: Barrier Type — all-caps words (x≈230–340)
      Col 4+: Begin/End treatments, repair cost

    WIP layout (retaining walls):
      Col 1: asset_id + inspection_date below it (x≈45–120)
      Col 2: Wall Area sq ft  — integer at x≈167–195  (NOT length — ignored)
      Col 3: Wall Length ft   — integer at x≈215–260  (this is the length we want)
      Col 4: Wall Type        — Title-Case words  x≈280–390  ("Gravity - Mortared Stone")
      Col 5: Wall Function    — Title-Case words  x≈410–465  ("Head Wall")
      Col 6: Overall Rating   — integer 0–100     x≈470–515
      Col 7: Repair Cost      — $NNN token        x≈515–570
    """
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

    # Layout-specific column config
    if report_kind == 'wip':
        # WIP: Wall Length is in a bounded column, NOT the leftmost integer after the id
        LENGTH_X_MIN, LENGTH_X_MAX = 210, 265
        TYPE_X_MIN,   TYPE_X_MAX   = 280, 395
        TYPE_RE = re.compile(r'^([A-Za-z][A-Za-z\-/]*|-+)$')
        FUNC_X_MIN,   FUNC_X_MAX   = 410, 468
        RATING_X_MIN, RATING_X_MAX = 470, 518
        COST_X_MIN,   COST_X_MAX   = 515, 575
    else:
        # GIP: length is the leftmost integer right of the id column
        LENGTH_X_MIN, LENGTH_X_MAX = None, None   # sentinel → use leftmost logic
        TYPE_X_MIN,   TYPE_X_MAX   = 230, 340
        TYPE_RE = re.compile(r'^[A-Z][A-Z\-/]*$')

    # For each id row, extract available fields
    rows = []
    for b in id_boxes:
        row_y_top = b['cy'] - 3
        row_y_bot = b['cy'] + row_h - 3

        # --- Inspection date (same x-band as id, slightly below it) ---
        date = None
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            cy = (y0+y1)/2
            if not (row_y_top - 5 <= cy <= row_y_bot):
                continue
            if x0 <= b['x1'] and DATE_RE.match(t) and cy > b['cy'] + 5:
                date = t
                break

        # --- Length ---
        length = None
        if report_kind == 'wip':
            # Pick integer from the bounded Wall-Length column
            cands = []
            for w in words:
                x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
                cy = (y0+y1)/2
                if (row_y_top - 5 <= cy <= row_y_bot - 8
                        and LENGTH_X_MIN <= x0 <= LENGTH_X_MAX
                        and INT_RE.match(t)):
                    cands.append((x0, t))
            cands.sort()
            length = float(cands[0][1]) if cands else None
        else:
            # GIP: leftmost integer to the right of the id column
            cands = []
            for w in words:
                x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
                cy = (y0+y1)/2
                if (row_y_top - 5 <= cy <= row_y_bot - 8
                        and x0 > b['x1']
                        and INT_RE.match(t)):
                    cands.append((x0, t))
            cands.sort()
            length = float(cands[0][1]) if cands else None

        # --- Type ---
        # GIP: all-caps words (STEEL-BACKED / TIMBER WITHOUT / BLOCKOUT)
        # WIP: Title-Case words joined with spaces ("Gravity - Mortared Stone")
        type_words = []
        for w in words:
            x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
            cy = (y0+y1)/2
            if not (row_y_top - 5 <= cy <= row_y_bot):
                continue
            if not (TYPE_X_MIN <= x0 < TYPE_X_MAX):
                continue
            if TYPE_RE.match(t) and len(t) > 1:
                type_words.append((x0, cy, t))
        type_words.sort(key=lambda ww: (ww[1], ww[0]))
        type_text = ' '.join(t for _, _, t in type_words) if type_words else None
        # Strip any leading/trailing lone hyphens left by WIP separator tokens
        if type_text:
            type_text = type_text.strip('- ').strip()
            if not type_text:
                type_text = None

        # --- WIP-only extra columns ---
        wall_function  = None
        overall_rating = None
        repair_cost    = None
        if report_kind == 'wip':
            # Wall Function
            func_words = []
            for w in words:
                x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
                cy = (y0+y1)/2
                if (row_y_top - 5 <= cy <= row_y_bot
                        and FUNC_X_MIN <= x0 <= FUNC_X_MAX
                        and re.match(r'^[A-Za-z]', t)):
                    func_words.append((x0, cy, t))
            func_words.sort(key=lambda ww: (ww[1], ww[0]))
            wall_function = ' '.join(t for _, _, t in func_words) if func_words else None

            # Overall Rating (integer 0–100)
            for w in words:
                x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
                cy = (y0+y1)/2
                if (row_y_top - 5 <= cy <= row_y_bot - 8
                        and RATING_X_MIN <= x0 <= RATING_X_MAX
                        and INT_RE.match(t)):
                    overall_rating = int(t)
                    break

            # Repair Cost
            for w in words:
                x0, y0, x1, y1, t = w[0], w[1], w[2], w[3], w[4]
                cy = (y0+y1)/2
                if (row_y_top - 5 <= cy <= row_y_bot - 8
                        and COST_X_MIN <= x0 <= COST_X_MAX
                        and COST_RE.match(t)):
                    repair_cost = t
                    break

        row = {'asset_id': b['aid'], 'route': b['route'], 'mp': b['mp'], 'side': b['side']}
        if length is not None:          row['length_ft']    = length
        if date:                        row['inspection_date'] = date
        if type_text:                   row['type']         = type_text
        if wall_function:               row['wall_function']= wall_function
        if overall_rating is not None:  row['rating']       = overall_rating
        if repair_cost:                 row['repair_cost']  = repair_cost
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


def extract_pdf(pdf_path, source_label, report_kind='gip'):
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
        rows = extract_summary_table(words, report_kind=report_kind)
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
    wip = extract_pdf(os.path.join(NPS, 'GRSM_WIPReport.pdf'), 'WIP', report_kind='wip')
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
