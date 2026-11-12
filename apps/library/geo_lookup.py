"""
Which town and barangay is this map pin in? (Quezon Province, official boundaries.)

Data: data/quezon_barangay_bounds.json, built by `manage.py build_barangay_bounds` from NAMRIA/PSA boundaries
(OCHA COD-AB, CC BY-IGO; credited on /credits). Codes are the same 9-digit PSGC codes as quezon_areas.json,
so a result drops straight into the address form's town and barangay.

locate(lat, lng) →
  {'match': 'inside', city_code, barangay_code, …}            the pin is inside that barangay's boundary
  {'match': 'near',   city_code, barangay_code, distance_m, …} just outside every boundary (a pin on the shore,
                                                                or in a sliver between two shapes): the nearest
                                                                barangay within NEAR_M
  None                                                          not in Quezon Province (or far out at sea)
Point-in-polygon by ray casting (even-odd over all rings, so holes work), after a bounding-box check;
the whole province takes a few milliseconds. Everything runs here: the pin never leaves our server.
"""
import json
import math
import os
from functools import lru_cache

from django.conf import settings

NEAR_M = 150   # a pin this close to a barangay still counts (GPS drift at the shore, gaps between shapes)
_DEG_M = 111_320


@lru_cache(maxsize=1)
def _bounds():
    path = os.path.join(settings.BASE_DIR, 'apps', 'library', 'data', 'quezon_barangay_bounds.json')
    with open(path, encoding='utf-8') as f:
        rows = json.load(f)['barangays']
    for row in rows:   # flat [lng, lat, lng, lat, …] → [(lng, lat), …]
        row['rings'] = [list(zip(flat[0::2], flat[1::2])) for flat in row['rings']]
    return rows


@lru_cache(maxsize=1)
def _names():
    path = os.path.join(settings.BASE_DIR, 'apps', 'library', 'data', 'quezon_areas.json')
    with open(path, encoding='utf-8') as f:
        towns = json.load(f)['cities']
    return ({t['code']: t['name'] for t in towns},
            {b['code']: b['name'] for t in towns for b in t['barangays']})


def _inside(lng, lat, rings):
    hit = False
    for ring in rings:
        x1, y1 = ring[-1]
        for x2, y2 in ring:
            if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                hit = not hit
            x1, y1 = x2, y2
    return hit


def _edge_m(lng, lat, rings):
    """Metres from the point to the nearest boundary edge (flat-earth: fine at this scale)."""
    kx, ky = _DEG_M * math.cos(math.radians(lat)), 110_540
    best = float('inf')
    for ring in rings:
        ax, ay = (ring[-1][0] - lng) * kx, (ring[-1][1] - lat) * ky
        for x, y in ring:
            bx, by = (x - lng) * kx, (y - lat) * ky
            dx, dy = bx - ax, by - ay
            length = dx * dx + dy * dy
            t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
            px, py = ax + t * dx, ay + t * dy
            best = min(best, px * px + py * py)
            ax, ay = bx, by
    return math.sqrt(best)


def _result(row, match, distance=0.0):
    towns, barangays = _names()
    return {'match': match, 'city_code': row['city'], 'barangay_code': row['code'],
            'city_name': towns.get(row['city'], ''), 'barangay_name': barangays.get(row['code'], ''),
            'distance_m': round(distance)}


def locate(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    rows = _bounds()
    for row in rows:
        w, s, e, n = row['bbox']
        if w <= lng <= e and s <= lat <= n and _inside(lng, lat, row['rings']):
            return _result(row, 'inside')
    # Not inside any: the nearest boundary, if it's close (only barangays whose box is within reach)
    pad_lat = NEAR_M / 110_540
    pad_lng = NEAR_M / (_DEG_M * max(0.1, math.cos(math.radians(lat))))
    best, best_d = None, NEAR_M
    for row in rows:
        w, s, e, n = row['bbox']
        if w - pad_lng <= lng <= e + pad_lng and s - pad_lat <= lat <= n + pad_lat:
            d = _edge_m(lng, lat, row['rings'])
            if d <= best_d:
                best, best_d = row, d
    return _result(best, 'near', best_d) if best else None
