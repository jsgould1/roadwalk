"""
Extract per-asset condition photos from the GIP and WIP PDFs.

Each PDF has dedicated "<X> Condition Photos" pages with exactly one asset
referenced and one (or sometimes more) embedded JPEGs. The asset_id is in
the on-page text as a filename like 'GRSM_0010N_0.468_R_1.JPG'.

Strategy: for every page that contains a GRSM_<route>_<mp>_<side>_<n>.jpg
filename, extract every embedded image and write each to
data/photos/<asset_id>[-N].jpg. The first image becomes the canonical photo
(no suffix), subsequent ones get -2, -3, ...

Outputs:
  data/photos/<asset_id>.jpg            — canonical photo (first one found)
  data/photos/<asset_id>-2.jpg, etc.    — extras
  data/asset-photos-index.json          — { asset_id: [filename, ...], ... }
"""
import fitz, json, os, re, sys, io
from collections import defaultdict
from PIL import Image

sys.stdout.reconfigure(encoding='utf-8')

DATA = os.path.dirname(os.path.abspath(__file__))
NPS  = r'C:\Users\gouldj\OneDrive - AECOM\Documents\!DATA\NPS'
PHOTOS_DIR = os.path.join(DATA, 'photos')
os.makedirs(PHOTOS_DIR, exist_ok=True)

TARGET_ROUTES = {'0010N', '0010S', '0012ZZ'}

# Filename in page text — these always use underscores between components.
PHOTO_FN_RE = re.compile(
    r'GRSM_(00\d{2}[A-Z]{0,2})_(\d+\.\d+)_([LRBC])_(\d+)\.(?:jpe?g|JPE?G)',
    re.IGNORECASE,
)

PDFS = [
    ('GRSM_GIPReport_reduced.pdf', 'GIP'),
    ('GRSM_WIPReport.pdf', 'WIP'),
]

def extract_pdf_photos(pdf_path, source_label, index):
    """Append discovered photos to `index` (asset_id → [filenames])."""
    doc = fitz.open(pdf_path)
    pages_with_photos = 0
    photos_written = 0
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        # Find all asset_id references via filename matches on the page
        fn_matches = PHOTO_FN_RE.findall(text)
        if not fn_matches:
            continue
        # Filter to target routes
        relevant = [m for m in fn_matches if m[0] in TARGET_ROUTES]
        if not relevant:
            continue
        # Collect unique (route, mp, side) on this page
        unique_assets = sorted(set((m[0], m[1], m[2]) for m in relevant))
        if len(unique_assets) != 1:
            # Photo pages should reference a single asset. If multiple appear,
            # we can't safely pair embedded images, so log and skip.
            print(f'  page {page_num+1}: {len(unique_assets)} assets referenced — skipping pairing')
            continue
        route, mp, side = unique_assets[0]
        asset_id = f'{route}-{mp}-{side}'
        # Pull every embedded image on this page
        imgs = page.get_images(full=True)
        if not imgs:
            continue
        pages_with_photos += 1
        # Sort the filename refs by the trailing photo index (1, 2, …) so we
        # extract photos in stable order
        ordered_ns = sorted(set(int(m[3]) for m in relevant))
        for slot_i, img in enumerate(imgs):
            xref = img[0]
            try:
                info = doc.extract_image(xref)
            except Exception as e:
                print(f'  extract_image failed for page {page_num+1} xref {xref}: {e}')
                continue
            ext = info.get('ext', 'jpg')
            if ext.lower() in ('jpeg',): ext = 'jpg'
            # Skip tiny icons/banners (the actual photos are 824x618 px ≈ 60-70KB)
            if info.get('width', 0) < 400 or info.get('height', 0) < 300:
                continue
            n = ordered_ns[slot_i] if slot_i < len(ordered_ns) else slot_i + 1
            suffix = '' if n == 1 else f'-{n}'
            out_name = f'{asset_id}{suffix}.jpg'   # always .jpg on disk
            out_path = os.path.join(PHOTOS_DIR, out_name)
            # The PDF embeds the raw JPEG bytes in whatever orientation was
            # scanned; the page's content stream uses a transformation matrix
            # to flip/rotate during display. PyMuPDF's extract_image() returns
            # the raw bytes only, so we must reproduce the display transform.
            #
            # For each embedded image we look at the PDF transform matrix
            # (a, b, c, d, e, f). The signs of a,b,c,d tell us the orientation.
            # Most photos in these reports have a > 0, d < 0 — meaning a
            # straight vertical flip is required (since PDF y-axis is up,
            # negative d means the image's row 0 lands at the top of the
            # rendered area, but PIL stores row 0 at the *top* of the raw
            # data too — so when we fail to flip, the image looks inverted).
            #
            # We grab the matrix from page.get_image_info() and use the sign
            # of d to decide. b,c handle 90°/270° rotations if present.
            try:
                img = Image.open(io.BytesIO(info['image']))
                # Find the matching transform for this xref
                tform = None
                for ii in page.get_image_info(xrefs=True):
                    if ii.get('xref') == xref:
                        tform = ii.get('transform')
                        break
                if tform:
                    a, b, c, d, e, f = tform
                    # Vertical flip when scanned-photo bytes are bottom-up
                    # relative to display. The reports consistently show this.
                    if d > 0:
                        # Rare case: image already top-down, no flip
                        pass
                    else:
                        img = img.transpose(Image.FLIP_TOP_BOTTOM)
                    # Horizontal flip if a < 0 (mirror image — uncommon)
                    if a < 0:
                        img = img.transpose(Image.FLIP_LEFT_RIGHT)
                else:
                    # No transform info — assume upside-down (matches the
                    # 100%-of-samples observation from these PDFs).
                    img = img.transpose(Image.FLIP_TOP_BOTTOM)
                img.convert('RGB').save(out_path, 'JPEG', quality=88)
            except Exception as e:
                # Fallback: write raw bytes if PIL processing fails
                print(f'  PIL transform failed (page {page_num+1}): {e}; writing raw')
                with open(out_path, 'wb') as f:
                    f.write(info['image'])
            index[asset_id].append(out_name)
            photos_written += 1
    doc.close()
    print(f'  {source_label}: {pages_with_photos} photo pages → {photos_written} photos written')


def main():
    index = defaultdict(list)
    for pdf_name, label in PDFS:
        pdf_path = os.path.join(NPS, pdf_name)
        if not os.path.exists(pdf_path):
            print(f'WARN: {pdf_path} not found, skipping')
            continue
        print(f'=== {label}: {pdf_name} ===')
        extract_pdf_photos(pdf_path, label, index)
    # Sort each asset's photo list (canonical first)
    out = {aid: sorted(set(files)) for aid, files in index.items()}
    out_path = os.path.join(DATA, 'asset-photos-index.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'photos_dir': 'photos',
            'total_assets_with_photos': len(out),
            'total_photos': sum(len(v) for v in out.values()),
            'assets': out,
        }, f, indent=2)
    print()
    print(f'Total assets with photos: {len(out)}')
    print(f'Total photo files written: {sum(len(v) for v in out.values())}')
    print(f'Index: {out_path}')

if __name__ == '__main__':
    main()
