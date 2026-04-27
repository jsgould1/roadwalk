"""
Discover available Nearmap captures over each trial section.

Reads the API key from a file outside the repo (default C:\\Dev\\nearmapkey.txt),
hits Nearmap's Coverage API v2 for each section's bounding polygon, and lists
all available captures grouped by season.

No tiles are fetched here — discovery only. Run this first to confirm what
imagery is available before committing to the render pass.
"""
import json, os, sys, urllib.parse, urllib.request, ssl
from datetime import datetime
from collections import defaultdict

DATA = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.environ.get('NEARMAP_KEY_PATH', r'C:\Dev\nearmapkey.txt')

def read_key():
    if not os.path.exists(KEY_PATH):
        sys.exit(f'Key file not found at {KEY_PATH}. Set NEARMAP_KEY_PATH or place key there.')
    with open(KEY_PATH, encoding='utf-8') as f:
        return f.read().strip()

API_KEY = read_key()

# Schannel can balk at cert revocation checks; allow unverified context for the
# CLI run. Same posture as the curl --ssl-no-revoke we used earlier.
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def fetch_json(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
        return json.loads(resp.read())

# Section bounding polygons — buffer each alignment by ~150 ft to capture
# a corridor of imagery (~50ft each side of centerline + a margin).
def alignment_bbox(coords, buffer_deg=0.0006):
    """coords: list of [lng, lat] pairs. Returns [minLng, minLat, maxLng, maxLat] expanded by buffer."""
    lats = [c[1] for c in coords]
    lngs = [c[0] for c in coords]
    return [min(lngs) - buffer_deg, min(lats) - buffer_deg,
            max(lngs) + buffer_deg, max(lats) + buffer_deg]

def bbox_polygon_str(bbox):
    """Nearmap coverage API expects: lng1,lat1,lng2,lat2,...,lng1,lat1 (closed ring)."""
    minLng, minLat, maxLng, maxLat = bbox
    pts = [(minLng, minLat), (maxLng, minLat), (maxLng, maxLat), (minLng, maxLat), (minLng, minLat)]
    return ','.join(f'{lng:.6f},{lat:.6f}' for lng, lat in pts)

def discover_captures(section_id, alignment_file):
    with open(os.path.join(DATA, alignment_file), encoding='utf-8') as f:
        d = json.load(f)
    coords = d['features'][0]['geometry']['coordinates']  # [lng, lat] pairs
    bbox = alignment_bbox(coords)
    poly_str = bbox_polygon_str(bbox)
    url = (f'https://api.nearmap.com/coverage/v2/poly/{urllib.parse.quote(poly_str, safe=",")}'
           f'?apikey={API_KEY}&limit=200&fields=id,captureDate,firstPhotoTime,resolution,tags')
    print(f'\n=== Section {section_id} ===')
    print(f'  bbox: {bbox}')
    print(f'  query: {url[:120]}...')
    try:
        data = fetch_json(url)
    except Exception as e:
        print(f'  ERROR: {e}')
        return None
    surveys = data.get('surveys', [])
    print(f'  captures available: {len(surveys)}')
    if not surveys:
        print(f'  raw response keys: {list(data.keys())}')
        if 'error' in data:
            print(f'  ERROR: {data["error"]}')
    # Group by season
    by_season = defaultdict(list)
    for s in surveys:
        date_str = s.get('captureDate') or s.get('firstPhotoTime', '')[:10]
        if not date_str: continue
        try:
            dt = datetime.fromisoformat(date_str[:10])
        except ValueError:
            continue
        m = dt.month
        season = ('Winter' if m in (12,1,2) else
                  'Spring' if m in (3,4,5) else
                  'Summer' if m in (6,7,8) else
                  'Fall')
        by_season[season].append({
            'date': date_str[:10],
            'survey_id': s.get('id'),
            'resolution': s.get('resolution'),
            'tags': s.get('tags', []),
        })
    # Sort within each season by date desc
    for season in by_season:
        by_season[season].sort(key=lambda c: c['date'], reverse=True)
    # Print compact
    for season in ['Winter','Spring','Summer','Fall']:
        rows = by_season.get(season, [])
        if not rows: continue
        print(f'  {season}:')
        for r in rows[:5]:    # show top 5 per season
            tags = ','.join(r['tags']) if r['tags'] else ''
            print(f'    {r["date"]}  res={r["resolution"]}  id={r["survey_id"]}  {tags}')
        if len(rows) > 5:
            print(f'    ... +{len(rows)-5} older')
    return {'section': section_id, 'bbox': bbox, 'by_season': dict(by_season)}

SECTIONS = [
    ('A', 'section-A-gatlinburg-bypass.geojson'),
    ('B', 'section-B-newfound-gap-NC-1.geojson'),
    ('C', 'section-C-newfound-gap-NC-2.geojson'),
    ('D', 'section-D-newfound-gap-TN.geojson'),
]

results = {}
for sid, fn in SECTIONS:
    r = discover_captures(sid, fn)
    if r: results[sid] = r

# Write a discovery report
out_path = os.path.join(DATA, 'nearmap-coverage-discovery.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(results, f, indent=2, default=str)
print(f'\nWrote {out_path}')
