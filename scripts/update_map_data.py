"""Refresh the small, local OSM boundary snapshot. No runtime GIS dependency.

Run explicitly: python scripts/update_map_data.py
Only vector relations are downloaded; this does not download map tiles.
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

DISTRICTS = {
    "esil": (3479876, "Есиль"),
    "almaty": (3482819, "Алматы"),
    "saryarka": (3486954, "Сарыарка"),
    "baikonur": (8593081, "Байконур"),
    "nura": (20593940, "Нура"),
    "saraishyk": (19733918, "Сарайшык"),
}


def rings_from_ways(parts):
    pending = [list(p) for p in parts]
    rings = []
    while pending:
        ring = pending.pop()
        while ring[0] != ring[-1]:
            for i, part in enumerate(pending):
                if ring[-1] == part[0]:
                    ring.extend(part[1:])
                elif ring[-1] == part[-1]:
                    ring.extend(part[-2::-1])
                elif ring[0] == part[-1]:
                    ring = part[:-1] + ring
                elif ring[0] == part[0]:
                    ring = part[:0:-1] + ring
                else:
                    continue
                pending.pop(i)
                break
            else:
                raise ValueError("Open OSM boundary: refusing to invent a closing edge")
        rings.append(ring)
    return rings


def inside(point, ring):
    x, y = point
    result = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
            result = not result
    return result


def signed_area(ring):
    return sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(ring, ring[1:])) / 2


def label_point(polygon):
    """Interior label point, found by a deterministic grid search (not a centroid)."""
    outer = polygon[0]
    minx, maxx = min(p[0] for p in outer), max(p[0] for p in outer)
    miny, maxy = min(p[1] for p in outer), max(p[1] for p in outer)
    segments = [(a, b) for ring in polygon for a, b in zip(ring, ring[1:])]
    best, best_distance = None, -1
    # Correct longitude distance at Astana's latitude for label placement.
    scale = math.cos(math.radians((miny + maxy)/2))
    for _ in range(3):
        dx, dy = (maxx-minx)/18, (maxy-miny)/18
        for i in range(19):
            for j in range(19):
                p = [minx + i*dx, miny + j*dy]
                if not inside(p, outer) or any(inside(p, h) for h in polygon[1:]):
                    continue
                distance = float('inf')
                for a, b in segments:
                    vx, vy = (b[0]-a[0])*scale, b[1]-a[1]
                    px, py = (p[0]-a[0])*scale, p[1]-a[1]
                    t = max(0, min(1, (px*vx+py*vy)/(vx*vx+vy*vy))) if vx or vy else 0
                    distance = min(distance, (px-t*vx)**2 + (py-t*vy)**2)
                if distance > best_distance:
                    best, best_distance = p, distance
        if best is None:
            raise ValueError('No interior label point')
        minx, maxx, miny, maxy = best[0]-dx, best[0]+dx, best[1]-dy, best[1]+dy
    return [round(v, 6) for v in best]


def feature(district_id, relation_id, name):
    url = f'https://www.openstreetmap.org/api/0.6/relation/{relation_id}/full.json'
    request = Request(url, headers={'User-Agent': 'AkimHackathonMap/1.0 (boundary snapshot)'})
    with urlopen(request, timeout=40) as response:
        elements = json.load(response)['elements']
    nodes = {e['id']: [e['lon'], e['lat']] for e in elements if e['type'] == 'node'}
    ways = {e['id']: e['nodes'] for e in elements if e['type'] == 'way'}
    relation = next(e for e in elements if e['type'] == 'relation' and e['id'] == relation_id)
    boundary = [m for m in relation['members'] if m['role'] in ('outer', 'inner')]
    if any(m['type'] != 'way' for m in boundary):
        raise ValueError('Nested boundary relation requires an explicit converter update')
    groups = {role: rings_from_ways([ways[m['ref']] for m in boundary if m['role'] == role])
              for role in ('outer', 'inner')}
    outers = [[nodes[n] for n in ring] for ring in groups['outer']]
    inners = [[nodes[n] for n in ring] for ring in groups['inner']]
    polygons = [[ring] for ring in outers]
    for hole in inners:
        containers = [p for p in polygons if inside(hole[0], p[0])]
        if len(containers) != 1:
            raise ValueError('Ambiguous boundary hole')
        containers[0].append(hole)
    # RFC 7946 exterior counterclockwise, holes clockwise.
    for polygon in polygons:
        for i, ring in enumerate(polygon):
            if (signed_area(ring) > 0) != (i == 0):
                ring.reverse()
    biggest = max(polygons, key=lambda p: abs(signed_area(p[0])))
    return {
        'type': 'Feature',
        'properties': {'id': district_id, 'name': name, 'osm_relation': relation_id,
                       'osm_version': relation['version'], 'osm_timestamp': relation['timestamp'],
                       'model_data': district_id != 'saraishyk', 'label': label_point(biggest)},
        'geometry': {'type': 'MultiPolygon', 'coordinates': polygons},
    }


def main():
    features = []
    for district_id, (relation_id, name) in DISTRICTS.items():
        features.append(feature(district_id, relation_id, name))
        print(f'Fetched {district_id}: OSM relation {relation_id}', flush=True)
    snapshot = {
        'type': 'FeatureCollection',
        'source': 'OpenStreetMap contributors',
        'license': 'ODbL-1.0',
        'attribution_url': 'https://www.openstreetmap.org/copyright',
        'retrieved_at': datetime.now(timezone.utc).isoformat(),
        'features': features,
    }
    target = Path(__file__).resolve().parents[1] / 'static/data/astana-districts.geojson'
    target.parent.mkdir(parents=True, exist_ok=True)
    # Only replace the snapshot after all six boundaries were validated.
    target.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))+'\n', encoding='utf-8')
    print(f'Saved {len(features)} districts, {target.stat().st_size} bytes')


if __name__ == '__main__':
    main()
