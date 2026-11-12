"""
Sun and shade across the yard: hours of direct sun per spot, from which way the yard faces, the house beside it,
the boundary walls and the trees the customer is keeping (Mark your yard → site_survey.py → layout_engine).

The sun's real path at Quezon Province's latitude (≈13.9° N) on four days (March and September equinoxes, June and
December solstices), every half hour from 6:00 to 18:00 solar time; a spot counts the half hours when the sun is
above 5° and nothing stands between it and the sun. The average of the four days is the spot's sun hours.

Lot coordinates as everywhere: x 0..W left → right (seen from the house), z 0..L with z = 0 the BACK of the yard and
z = L the house side; y up. facing = compass bearing (degrees, 0 = north, clockwise) the back of the yard faces,
seen from the house. The house is a box behind the front edge; boundary walls run along the other sides — the
yard's own outline when it has one (lot_shape.py), and spots outside that outline get no value (None).

Horticultural classes (hours of direct sun): full sun ≥ 6 · part shade 3–6 · shade < 3. Plant rules (engine):
full-sun plants need SUN_PLANT_MIN, shade plants at most SHADE_PLANT_MAX (or a tree's canopy), part-shade plants
PART_PLANT_MIN.
"""
import math
from functools import lru_cache

from . import lot_shape

LATITUDE = 13.9            # degrees north: Quezon Province (Lucena 13.94, Infanta 14.74, Catanauan 13.59)
DAYS = (80, 172, 266, 355)  # day of the year: equinox, June solstice, equinox, December solstice
STEP_H = 0.5
MIN_ELEVATION = 5.0        # degrees: lower sun is too weak to count (and hidden by neighbours anyway)
PLANT_HEIGHT = 0.3         # m: the sun is measured at the top of a young plant, not at the soil
HOUSE_DEPTH = 10.0         # m: how far the house extends behind the front edge
HOUSE_OVERHANG = 3.0       # m: how far it extends past the side boundaries
WALL_HEIGHT = 1.8          # m: a typical boundary wall (hollow blocks)
TRUNK = 1.5                # m: a kept tree's crown starts this high

FULL_SUN_H, SHADE_H = 6.0, 3.0           # classes for the map
SUN_PLANT_MIN = 5.0                      # a full-sun plant needs at least this many hours
SHADE_PLANT_MAX = 4.0                    # a shade plant gets at most this many (else: under a canopy)
PART_PLANT_MIN = 2.0                     # a part-shade plant needs at least this many
HOUSE_HEIGHTS = {'none': 0.0, 'one': 3.5, 'two': 6.5}


def sun_class(hours):
    return 'full_sun' if hours >= FULL_SUN_H else 'partial_shade' if hours >= SHADE_H else 'full_shade'


def _sun_directions(facing):
    """(dx, dy, dz) unit vectors towards the sun in lot coordinates, one per half hour above MIN_ELEVATION,
    per day: [[...], [...], [...], [...]]."""
    phi = math.radians(LATITUDE)
    a = math.radians(facing)
    back = (math.sin(a), math.cos(a))           # (east, north) of the yard's back (−z)
    right = (math.cos(a), -math.sin(a))         # (east, north) of +x
    days = []
    for n in DAYS:
        decl = math.radians(23.44 * math.sin(2 * math.pi * (284 + n) / 365))
        dirs = []
        t = 6.0
        while t <= 18.0 + 1e-9:
            h = math.radians(15 * (t - 12))
            sin_el = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(h)
            el = math.asin(max(-1.0, min(1.0, sin_el)))
            if math.degrees(el) > MIN_ELEVATION:
                az = math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(decl) * math.cos(phi)) + math.pi
                east, north, up = math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)
                dx = east * right[0] + north * right[1]
                dz = -(east * back[0] + north * back[1])
                dirs.append((dx, up, dz))
            t += STEP_H
        days.append(dirs)
    return days


def _hits_box(p, d, lo, hi):
    """Does the ray p + t·d (t > 0) pass through the axis-aligned box lo..hi?"""
    t0, t1 = 1e-6, math.inf
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if not (lo[i] <= p[i] <= hi[i]):
                return False
            continue
        a, b = (lo[i] - p[i]) / d[i], (hi[i] - p[i]) / d[i]
        if a > b:
            a, b = b, a
        t0, t1 = max(t0, a), min(t1, b)
        if t0 > t1:
            return False
    return True


def _hits_wall(p, d, a, b, height):
    """Does the ray p + t·d (t > 0) pass through the vertical wall from a to b (ground points (x, z)), up to
    height?"""
    ex, ez = b[0] - a[0], b[1] - a[1]
    # Solve p + t·d = a + s·e on the ground plane (x, z)
    det = d[0] * (-ez) - d[2] * (-ex)
    if abs(det) < 1e-12:
        return False
    rx, rz = a[0] - p[0], a[1] - p[2]
    t = (rx * (-ez) - rz * (-ex)) / det
    s = (d[0] * rz - d[2] * rx) / det
    return t > 1e-6 and -1e-9 <= s <= 1 + 1e-9 and p[1] + t * d[1] <= height


def _hits_sphere(p, d, c, r):
    ox, oy, oz = p[0] - c[0], p[1] - c[1], p[2] - c[2]
    b = ox * d[0] + oy * d[1] + oz * d[2]
    cc = ox * ox + oy * oy + oz * oz - r * r
    disc = b * b - cc
    return disc >= 0 and (-b + math.sqrt(disc)) > 1e-6


@lru_cache(maxsize=64)
def hours_grid(width, length, facing, house_height, keeps=(), grid=20, outline=None, house_walls=None):
    """Average hours of direct sun at the centre of each cell: rows[gz][gx] (gz 0 = back); None outside the
    yard's outline."""
    W, L = float(width), float(length)
    days = _sun_directions(float(facing))
    boxes, walls = [], []
    shape = list(outline) if outline else [(0.0, 0.0), (W, 0.0), (W, L), (0.0, L)]
    if outline:
        # A shaped yard: its house walls stand as tall as the house (any ray from the yard into the house
        # crosses the wall facing the yard), the other sides are boundary walls
        house = set(house_walls or ())
        walls = [(a, b, house_height if (a, b) in house else WALL_HEIGHT) for a, b in lot_shape.edges(shape)]
    else:
        if house_height > 0:
            boxes.append(((-HOUSE_OVERHANG, 0.0, L), (W + HOUSE_OVERHANG, house_height, L + HOUSE_DEPTH)))
        walls = [(a, b, WALL_HEIGHT) for a, b in lot_shape.edges(shape)
                 if not (abs(a[1] - L) < 1e-6 and abs(b[1] - L) < 1e-6)]   # the house box stands for z = L
    crowns = [((x, TRUNK + r, z), r) for x, z, r in keeps]
    cw, cl = W / grid, L / grid
    rows = []
    for gz in range(grid):
        row = []
        for gx in range(grid):
            p = ((gx + 0.5) * cw, PLANT_HEIGHT, (gz + 0.5) * cl)
            if outline and not lot_shape.contains(shape, p[0], p[2]):
                row.append(None)
                continue
            total = 0.0
            for dirs in days:
                lit = sum(1 for d in dirs
                          if not any(_hits_box(p, d, lo, hi) for lo, hi in boxes)
                          and not any(_hits_wall(p, d, a, b, h) for a, b, h in walls if h > 0)
                          and not any(_hits_sphere(p, d, c, r) for c, r in crowns))
                total += lit * STEP_H
            row.append(round(total / len(days), 2))
        rows.append(tuple(row))
    return tuple(rows)


def summary(rows):
    """Share of the yard in each class, e.g. {'full_sun': 0.62, 'partial_shade': 0.3, 'full_shade': 0.08}."""
    cells = [h for row in rows for h in row if h is not None]
    out = {'full_sun': 0, 'partial_shade': 0, 'full_shade': 0}
    for h in cells:
        out[sun_class(h)] += 1
    return {k: round(v / len(cells), 2) for k, v in out.items()} if cells else out


def plant_fits_hours(sunlight, hours):
    """Can a plant with this need grow in a spot with these sun hours? (Canopy shade is the engine's job.)"""
    if sunlight == 'full_sun':
        return hours >= SUN_PLANT_MIN
    if sunlight == 'full_shade':
        return hours <= SHADE_PLANT_MAX
    return hours >= PART_PLANT_MIN
