"""
Smart layout engine — rule-based garden layouts, and the check & repair step for
AI-generated ones. Pure Python: no network, no AI quota, deterministic for a seed.

Coordinates are metres in the lot: x 0..width (left → right as seen in the 3D/AR view),
z 0..length where z = 0 is the BACK of the garden (far from the viewer) and z = length
is the FRONT. The same 20×20 cell grid as the AI pipeline is used for keep-clear zones.

Every layout this module returns satisfies, by construction and by repair:
  • spacing   — no two plants closer than the average of their spacings
  • bounds    — every plant inside the lot
  • zones     — no plant inside a keep-clear cell (paths, pools, patios…)
  • stock     — never more of a plant than is in stock
  • budget    — plant cost never above the plant budget
  • companions — extra soil between plants that must not share a bed (see companion_gap):
                 low- and high-water plants (separate watering zones), aggressive root
                 spreaders and their neighbours (room for a root barrier), and low
                 sun-loving plants kept out from under tall canopy
  • features  — garden features (seating, accent rocks) are placed first, as the bones of the
                design; a seating spot keeps a clear pad and a path to the front (house side)
                that nothing is planted on
  • lawn      — every style has a garden plan (GardenPlan): planting beds around the edges and an
                open lawn in the middle, reached from the house through a gap in the front border.
                Nothing but seating goes on the lawn: a garden people can use, not a filled lot
  • layering  — tall plants stay in the back half, where they don't block the view from the house
"""
import math
import random
from dataclasses import dataclass, replace

from . import lot_shape, sun_map

GRID = 20  # cells per side, shared with the AI prompt and the zone map

# Keep-clear zones as cell ranges. gz 0 is the back of the garden, gz 19 the front.
ZONES = {
    'background_perimeter': {'gx': (0, 19), 'gz': (0, 4)},
    'midground_left':       {'gx': (0, 6),  'gz': (5, 11)},
    'midground_center':     {'gx': (7, 12), 'gz': (5, 11)},
    'midground_right':      {'gx': (13, 19), 'gz': (5, 11)},
    'foreground_left':      {'gx': (0, 6),  'gz': (12, 19)},
    'foreground_center':    {'gx': (7, 12), 'gz': (12, 19)},
    'foreground_right':     {'gx': (13, 19), 'gz': (12, 19)},
    'blocked':              {'gx': (6, 13), 'gz': (7, 12)},
}

STYLES = ('Symmetrical', 'Minimalist', 'Lush / Organic')
BUDGET_FLOOR = 0.75          # aim to use at least 75% of the plant budget
MIN_SPACING, MAX_SPACING = 0.3, 4.0
MAX_PLANTS = 250             # a realistic upper bound for one residential design
LOW_CARE_DENSITY = 1.0       # plants per m² of planting bed for a low-maintenance garden (less to prune and water)
MAX_SHARE = 0.4              # variety: no tree or shrub species past 40% of a design's plants…
MAX_SHARE_LOW = 0.5          # …low border plants and groundcovers may be massed a little more (edging is)…
VARIETY_FROM = 10            # …once it has this many plants (small designs may repeat one plant)…
VARIETY_MIN_KINDS = 3        # …and only when there are at least this many species to choose from
MINIMALIST_DENSITY = 0.3     # plants per m² at most: minimalism means open space and few kinds — a bigger budget
                             # grows its blocks (mass planting), it never adds kinds or fills the lawn

# Companion planting gaps (metres of extra soil on top of normal spacing)
HYDROZONE_GAP = 1.0          # low-water vs high-water: separate beds so one watering schedule fits each
ROOT_BARRIER_GAP = 1.0       # aggressive spreader (bamboo) vs anything else: room for a root barrier
CANOPY_GAP = 0.5             # low sun-loving plant vs tall canopy plant: keep it out of the shade
CANOPY_HEIGHT, GROUNDCOVER_HEIGHT = 3.0, 0.6
MAX_GAP = max(HYDROZONE_GAP, ROOT_BARRIER_GAP, CANOPY_GAP)

# Garden features (furniture = 'seat', hardscape = 'rock')
SEAT_PAD = 2.4               # m: a ~1.7 m bench plus legroom and clear ends; nothing else within half of this + their own half
SEAT_PAIR_GAP = 2.6          # m centre to centre: ~0.9 m between two 1.7 m benches (walk between, side table)
PATH_WIDTH = 1.0             # m: walkway from the seating spot to the front of the garden
SEAT_DEPTH = 0.62            # seating spot depth (fraction of the lot, 0 = back): open ground, near the house
SEAT_FACING = 0.0            # degrees: models face +z, i.e. towards the house / viewer
ROCK_GROUP = 3               # accent rocks in odd-numbered groups
STONE_GROUP = 7              # small garden stones (< SMALL_STONE m) in looser clusters of about seven
SMALL_STONE = 0.35           # m (pricing.SMALL_STONE)

# Garden plan: beds around the edges, open lawn in the middle
MIN_OPEN = 2.0               # m: the smallest lawn worth keeping (room to stand, set a table, walk)
LAWN_EDGE = 0.5              # m: a plant's centre stays this far outside the lawn (less for small plants)
ENTRY_WIDTH = 1.2            # m: gap in the front border, from the house onto the lawn
BED_SLACK = 0.3              # m: beds sized for a plant get this much room to move, so placement can find a spot
NO_LAWN_AREA = 12.0          # m²: courtyards smaller than this are planted freely
NO_LAWN_SIDE = 2.5           # m: so are lots narrower than this
TALL_DEPTH = 0.5             # fraction of the lot: tall plants stay behind this line (0 = back)
HOUSE_SETBACK = 0.6          # m: plant centres stay this far from the house side (z = length): drip line,
                             # termites, and room to paint, clean gutters and walk past the wall
WALK_WIDTH = 1.0             # m: the path from a gate or door the customer marked to the lawn
WALK_EDGE = 0.4              # m: a plant's centre stays this far off that path (less for small plants)


def minimalist_cap(site):
    return max(6, int(site.area * MINIMALIST_DENSITY))


def _r(v):
    # Coordinates are stored rounded to the millimetre; checks use the same values
    return round(v, 3)


def zone_cells(zone_ids):
    """All grid cells covered by the given zone ids (unknown ids are ignored)."""
    cells = set()
    for zid in zone_ids or []:
        zone = ZONES.get(zid)
        if not zone:
            continue
        for gx in range(zone['gx'][0], zone['gx'][1] + 1):
            for gz in range(zone['gz'][0], zone['gz'][1] + 1):
                cells.add((gx, gz))
    return cells


@dataclass(frozen=True)
class PlantSpec:
    id: str
    name: str
    price: int
    stock: int
    height: float
    spacing: float   # metres between centres for healthy growth
    sunlight: str = 'full_sun'
    water_need: str = 'moderate'
    is_invasive: bool = False
    kind: str = 'plant'   # 'plant', or a garden feature: 'seat' / 'rock'
    mature: float = 0.0   # fully grown height (0 = unknown → the established height)

    @property
    def grown(self):
        """Height the design must leave room for: fully grown when known (professionals space and
        layer for maturity), else the established height. Pricing never uses this (pricing.layer_of)."""
        return max(self.height, self.mature or 0.0)

    @property
    def is_plant(self):
        return self.kind == 'plant'

    @property
    def layer(self):
        """tall = back of the garden, low = front border — by fully grown height, so a young tree
        (plumeria: 2 m now, 6 m grown) goes to the back where it won't shade the border later."""
        if self.grown >= 2.5:
            return 'tall'
        if self.grown >= 0.8:
            return 'mid'
        return 'low'


def companion_gap(a, b):
    """Extra distance two plants need because of their growing needs (0 when they get along)."""
    if not (a.is_plant and b.is_plant):
        # Features have no sun or water needs, but spreading roots still lift stones and seats
        return ROOT_BARRIER_GAP if a.is_invasive != b.is_invasive else 0.0
    gap = 0.0
    if {a.water_need, b.water_need} == {'low', 'high'}:
        gap = max(gap, HYDROZONE_GAP)
    if a.is_invasive != b.is_invasive:
        gap = max(gap, ROOT_BARRIER_GAP)
    tall, low = (a, b) if a.grown >= b.grown else (b, a)
    if tall.grown >= CANOPY_HEIGHT and low.grown <= GROUNDCOVER_HEIGHT and low.sunlight == 'full_sun':
        gap = max(gap, CANOPY_GAP)
    return gap


def min_distance(a, b):
    """Closest two plants may be, centre to centre."""
    return (a.spacing + b.spacing) / 2 + companion_gap(a, b)


def spacing_for(height, spacing_cm=None):
    """Explicit spacing wins; otherwise a rule of thumb from mature height."""
    if spacing_cm:
        return max(MIN_SPACING, min(MAX_SPACING, abs(spacing_cm) / 100))
    return round(max(MIN_SPACING, min(MAX_SPACING, 0.55 * (height or 1.0) + 0.2)), 2)


def specs_from_inventory(items, keep_all=False):
    """InventoryItem queryset/list → PlantSpecs (plants with no stock are left out, unless keep_all:
    checking a layout that already has them)."""
    specs = []
    for item in items:
        if getattr(item, 'is_groundcover', False):
            continue   # groundcover is a carpet over the beds (garden_ai), never single plants
        height = float(item.real_world_size or 1.0)
        mature = float(getattr(item, 'mature_height', None) or 0.0)
        specs.append(PlantSpec(
            id=str(item.id), name=item.name, price=int(item.unit_price),
            stock=max(0, int(item.stock_quantity or 0)), height=height,
            # Spaced for the fully grown plant, so it doesn't crowd its neighbours in a few years
            spacing=spacing_for(max(height, mature), item.spacing_cm),
            mature=mature,
            sunlight=getattr(item, 'sunlight', 'full_sun') or 'full_sun',
            water_need=getattr(item, 'water_need', 'moderate') or 'moderate',
            is_invasive=bool(getattr(item, 'is_invasive', False)),
        ))
    return [s for s in specs if keep_all or (s.stock > 0 and s.price > 0)]


def feature_specs_from_inventory(items, keep_all=False):
    """Furniture / hardscape InventoryItems → feature specs (none without stock or price, unless keep_all)."""
    specs = []
    for item in items:
        size = float(item.real_world_size or 1.0)
        kind = 'seat' if item.category == 'furniture' else 'rock'
        # Small stones sit closer together (a cluster), boulders need room round them
        spacing = SEAT_PAD if kind == 'seat' else (round(max(0.3, size + 0.1), 2) if size < SMALL_STONE else round(max(0.4, min(2.0, size + 0.2)), 2))
        specs.append(PlantSpec(id=str(item.id), name=item.name, price=int(item.unit_price),
                               stock=max(0, int(item.stock_quantity or 0)), height=size, spacing=spacing,
                               kind=kind))
    return [s for s in specs if keep_all or (s.stock > 0 and s.price > 0)]


def seat_path(seats, site):
    """The clear walkway (x0, x1, z0, z1) from a seating spot to the front edge; None without seats."""
    if not seats:
        return None
    x = sum(p['x'] for p in seats) / len(seats)
    z = sum(p['z'] for p in seats) / len(seats)
    return (x - PATH_WIDTH / 2, x + PATH_WIDTH / 2, z + SEAT_PAD / 4, site.length)


def on_path(path, spec, x, z):
    """True when an item's centre (plus half its spread) would sit on the walkway."""
    if path is None or spec.kind == 'seat':
        return False
    r = spec.spacing / 2
    return path[0] - r < x < path[1] + r and path[2] - r < z <= path[3]


@dataclass
class SiteRules:
    """What the customer marked on their yard (site_survey.py builds these), in lot metres:
    clear: [(x0, z0, x1, z1)] nothing planted · keeps: [(x, z, r)] existing trees / plants that stay
    entries: [(x, z, edge)] gates and doors · screens: [(edge, a, b)] stretches to hide with tall planting
    views: [(x0, z0, x1, z1)] low planting only."""
    clear: tuple = ()
    keeps: tuple = ()
    entries: tuple = ()
    screens: tuple = ()
    views: tuple = ()
    sun: tuple = None     # hours of direct sun per cell, rows[gz][gx] (sun_map.py); None = not known
    outline: tuple = None      # the yard's own shape ((x, z), …) inside the box (lot_shape.py); None = the box
    house_walls: tuple = None  # sides of that shape that are house walls (((x0, z0), (x1, z1)), …)


class Site:
    def __init__(self, width, length, keep_clear_cells=(), rules=None):
        self.width = float(width)
        self.length = float(length)
        self.keep_clear = set(keep_clear_cells)
        self.cell_w = self.width / GRID
        self.cell_l = self.length / GRID
        self.rules = rules or SiteRules()
        shape = self.rules.outline
        self.area = round(lot_shape.area(list(shape)), 3) if shape else self.width * self.length

    def inside(self, x, z):
        """Is this spot in the yard (its own shape, or the box)?"""
        shape = self.rules.outline
        if shape:
            return lot_shape.contains(shape, x, z)
        return 0 <= x <= self.width and 0 <= z <= self.length

    def cell_of(self, x, z):
        return (min(GRID - 1, max(0, int(x / self.cell_w))), min(GRID - 1, max(0, int(z / self.cell_l))))

    def margin(self, spec):
        # Keep the canopy mostly inside the lot without wasting small lots
        return min(spec.spacing / 2, self.width / 4, self.length / 4)

    def in_bounds(self, spec, x, z):
        m = self.margin(spec)
        if not (m <= x <= self.width - m and m <= z <= self.length - m):
            return False
        shape = self.rules.outline
        return not shape or (lot_shape.contains(shape, x, z) and lot_shape.edge_dist(shape, x, z) >= m - 1e-9)

    def is_clear(self, spec, x, z):
        """Centre and a ring at a third of the spacing must avoid keep-clear cells, and the same circle the
        customer's marked areas (exact boxes) and the canopies of the trees they're keeping."""
        r = spec.spacing / 3
        for dx, dz in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            px = min(self.width - 1e-6, max(0.0, x + dx))
            pz = min(self.length - 1e-6, max(0.0, z + dz))
            if self.cell_of(px, pz) in self.keep_clear:
                return False
        for x0, z0, x1, z1 in self.rules.clear:
            if _rect_depth(x, z, x0, x1, z0, z1) > -r + 1e-9:
                return False
        for kx, kz, kr in self.rules.keeps:
            if math.hypot(x - kx, z - kz) < kr + r - 1e-9:
                return False
        return True

    def sun_hours(self, x, z):
        """Hours of direct sun at this spot, or None when we don't know how the yard faces."""
        if not self.rules.sun:
            return None
        gx, gz = self.cell_of(min(self.width - 1e-6, max(0.0, x)), min(self.length - 1e-6, max(0.0, z)))
        return self.rules.sun[gz][gx]

    def near_house(self, spec, x, z):
        """A plant this close to the house (the lot's house side z = length, or the house walls of the yard's
        own shape) would crowd the wall."""
        if not spec.is_plant:
            return False
        if self.rules.outline:
            return any(lot_shape.seg_dist(x, z, a, b) < HOUSE_SETBACK - 1e-9 for a, b in self.rules.house_walls or ())
        return z > self.length - HOUSE_SETBACK + 1e-9

    def blocks_view(self, spec, x, z):
        """Only low plants where the customer wants to keep a view."""
        if not spec.is_plant or spec.layer == 'low':
            return False
        r = spec.spacing / 3
        return any(_rect_depth(x, z, x0, x1, z0, z1) > -r + 1e-9 for x0, z0, x1, z1 in self.rules.views)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class GardenPlan:
    """
    The bones of a design: planting beds around the edges and an open lawn in the middle.
    lawn: ('rect', x0, x1, z0, z1) or ('ellipse', cx, cz, rx, rz); entry: (x0, x1, z0, z1), the gap
    in the front border from the house onto the lawn (None when there is no front border).
    back / side / front: bed depths in metres (for the style builders and the 3D view).
    lawn None = a small courtyard, planted freely.
    """
    lawn: tuple = None
    entry: tuple = None
    back: float = 0.0
    side: float = 0.0
    front: float = 0.0
    paths: tuple = ()     # ((x0, x1, z0, z1), …): walkways from the customer's gates / doors to the lawn
    outline: tuple = None      # the yard's own shape, for the views (Site.rules.outline)
    house_walls: tuple = None

    def depth_inside(self, x, z):
        """How far (m) a point is inside the open area (lawn or entry); negative = outside, in a bed."""
        if not self.lawn:
            return -math.inf
        if self.lawn[0] == 'rect':
            _, x0, x1, z0, z1 = self.lawn
            d = _rect_depth(x, z, x0, x1, z0, z1)
        else:
            _, cx, cz, rx, rz = self.lawn
            # Exact for a circle; for an ellipse it is never larger than the true distance outside
            d = (1 - math.hypot((x - cx) / rx, (z - cz) / rz)) * min(rx, rz)
        if self.entry:
            d = max(d, _rect_depth(x, z, *self.entry))
        return d

    def on_walk(self, spec, x, z):
        """True when an item would sit on a path from a gate or door (seats too: it's for walking)."""
        edge = min(WALK_EDGE, spec.spacing / 2)
        return any(_rect_depth(x, z, *p) > -edge + 1e-9 for p in self.paths)

    def blocks(self, spec, x, z):
        """True when an item would sit on the open lawn (seats belong on it; plants and rocks don't) or on a path."""
        if self.on_walk(spec, x, z):
            return True
        if not self.lawn or spec.kind == 'seat':
            return False
        return self.depth_inside(x, z) > -min(LAWN_EDGE, spec.spacing / 2) + 1e-9

    def lawn_area(self):
        """Open ground in m²: the lawn plus the part of the entry outside it (pricing: turf, not mulch)."""
        if not self.lawn:
            return 0.0
        if self.lawn[0] == 'rect':
            _, x0, x1, z0, z1 = self.lawn
            area = (x1 - x0) * (z1 - z0)
        else:
            area = math.pi * self.lawn[3] * self.lawn[4]
        if self.entry:
            x0, x1, z0, z1 = self.entry
            step = 0.05
            nx, nz = max(1, round((x1 - x0) / step)), max(1, round((z1 - z0) / step))
            lawn_only = GardenPlan(lawn=self.lawn)
            outside = sum(1 for i in range(nx) for j in range(nz)
                          if lawn_only.depth_inside(x0 + (i + 0.5) * step, z0 + (j + 0.5) * step) <= 0)
            area += outside * (x1 - x0) * (z1 - z0) / (nx * nz)
        return round(area, 1)

    def _shape_dict(self):
        if not self.outline:
            return {}
        house = set(self.house_walls or ())
        pts = list(self.outline)
        return {'outline': [{'x': a[0], 'z': a[1], 'house': (a, b) in house} for a, b in zip(pts, pts[1:] + pts[:1])]}

    def to_dict(self):
        if not self.lawn:
            return {'lawn': None, 'entry': None, 'beds': {}, 'lawn_area': 0.0, **self._shape_dict()}
        lawn = ({'shape': 'rect', 'x0': self.lawn[1], 'x1': self.lawn[2], 'z0': self.lawn[3], 'z1': self.lawn[4]}
                if self.lawn[0] == 'rect' else
                {'shape': 'ellipse', 'cx': self.lawn[1], 'cz': self.lawn[2], 'rx': self.lawn[3], 'rz': self.lawn[4]})
        entry = self.entry and dict(zip(('x0', 'x1', 'z0', 'z1'), self.entry))
        return {'lawn': lawn, 'entry': entry, 'beds': {'back': self.back, 'side': self.side, 'front': self.front},
                'paths': [dict(zip(('x0', 'x1', 'z0', 'z1'), p)) for p in self.paths],
                'lawn_area': self.lawn_area(), **self._shape_dict()}

    @classmethod
    def from_dict(cls, d):
        """Inverse of to_dict (a saved or returned design → its plan, e.g. to check it again)."""
        shape = (d or {}).get('outline') or None
        pts = tuple((float(p['x']), float(p['z'])) for p in shape) if shape else None
        walls = tuple((a, b) for a, b, p in zip(pts, pts[1:] + pts[:1], shape) if p.get('house')) if shape else None
        lawn = (d or {}).get('lawn')
        if not lawn:
            return cls(outline=pts, house_walls=walls)
        keys = ('x0', 'x1', 'z0', 'z1') if lawn['shape'] == 'rect' else ('cx', 'cz', 'rx', 'rz')
        entry = d.get('entry')
        beds = d.get('beds') or {}
        return cls(lawn=(lawn['shape'], *(lawn[k] for k in keys)),
                   entry=entry and tuple(entry[k] for k in ('x0', 'x1', 'z0', 'z1')),
                   back=beds.get('back', 0.0), side=beds.get('side', 0.0), front=beds.get('front', 0.0),
                   paths=tuple(tuple(float(p[k]) for k in ('x0', 'x1', 'z0', 'z1')) for p in (d.get('paths') or [])),
                   outline=pts, house_walls=walls)


def _rect_depth(x, z, x0, x1, z0, z1):
    """Signed distance into a rectangle (positive inside)."""
    inside = min(x - x0, x1 - x, z - z0, z1 - z)
    if inside >= 0:
        return inside
    dx, dz = max(x0 - x, 0, x - x1), max(z0 - z, 0, z - z1)
    return -math.hypot(dx, dz)


# Bed depths per style, as (fraction of the lot, smallest, largest) in metres
# Bed depths per style: (share of the lot's length or width, min m, max m), and the share of the yard kept as open
# lawn. Landscapers keep roughly two-thirds open (lawn, paths) and plant heavily round its edge: beds deep enough for
# layers (tall at the back, mid, low and groundcover in front), not most of the yard. Rebalanced 2026-11-12 (the
# beds took 60–75% of the yard: plain soil, a small lawn island).
_BEDS = {
    'Symmetrical':    {'back': (0.18, 1.2, 3.0), 'side': (0.12, 1.2, 2.0), 'front': (0.08, 1.2, 1.5), 'lawn': 0.58},
    'Minimalist':     {'back': (0.16, 1.0, 2.5), 'side': (0.08, 0.6, 1.2), 'front': None, 'lawn': 0.65},
    'Lush / Organic': {'back': (0.22, 1.5, 3.0), 'side': (0.15, 1.2, 2.2), 'front': (0.10, 1.1, 1.5), 'lawn': 0.48},
    # (front borders from 1.0 m: plants keep HOUSE_SETBACK 0.6 m off the house, a thinner border can't be planted)
}
OVAL = math.pi / 4   # an oval lawn's share of the rectangle it fills


def screen_row(specs, quantities):
    """The tall plant the customer wants three or more of (place_quantities plants it as a row along the back
    fence), or None. garden_plan makes the back bed deep enough for the other tall plants in front of it."""
    q = {str(k): int(v) for k, v in (quantities or {}).items()}
    rows = [s for s in specs if s.is_plant and s.layer == 'tall' and q.get(s.id, 0) >= 3]
    return max(rows, key=lambda s: (s.grown, s.id), default=None)


def tall_picks(specs, preferred, quantities, style):
    """The tall plants the customer picked, as many as the design must fit (their amount, else a mirrored pair
    for Symmetrical and one otherwise) — the screen row's plant aside. garden_plan sizes the back bed for them."""
    q = {str(k): int(v) for k, v in (quantities or {}).items()}
    pre = {str(p) for p in (preferred or ())}
    out = []
    for s in specs:
        if s.is_plant and s.layer == 'tall' and s.id in pre and q.get(s.id, 0) < 3:
            out += [s] * (q.get(s.id) or (2 if style == 'Symmetrical' else 1))
    return tuple(out)


def garden_plan(style, site, specs=(), seed=0, screen=None, must=()):
    """
    Where the beds and the lawn go for this style. Deterministic for (style, lot, plants, seed), so the
    generator, the AI prompt, repair and check all see the same plan.
    The back bed is made deep enough for the biggest tall plant (its centre fits, canopy over the lawn edge).
    """
    W, L = site.width, site.length
    shape = site.rules.outline
    bare = GardenPlan(outline=shape, house_walls=site.rules.house_walls)
    if site.area < NO_LAWN_AREA or min(W, L) < NO_LAWN_SIDE:
        return bare
    rule = _BEDS.get(style, _BEDS['Symmetrical'])
    # Deep enough for the biggest tall plant with a little room to move (BED_SLACK), and for an understorey
    # of shade plants in front of it when the design has any
    tall = [s for s in specs if s.is_plant and s.layer == 'tall']
    shade = [s for s in specs if s.is_plant and s.sunlight == 'full_shade' and s.layer != 'tall']
    need = max((t.spacing / 2 + LAWN_EDGE + BED_SLACK for t in tall), default=0.0)
    need = max([need] + [t.spacing / 2 + min_distance(s, t) + min(LAWN_EDGE, s.spacing / 2) + BED_SLACK
                         for t in tall for s in shade])
    deep = screen is not None
    if must:
        # The customer's tall picks must all fit: more than one row across the yard → a second, staggered row
        across = sum(t.spacing for t in must)
        biggest = max(t.spacing for t in must)
        rows = math.ceil(across / max(1.0, W - biggest))
        if rows >= 2:
            need = max(need, biggest / 2 + (rows - 1) * 0.87 * biggest + LAWN_EDGE + BED_SLACK)
            deep = True
    if screen is not None:
        # A screen row along the back fence (the customer's own amount): the other tall plants stand in front of it
        row_z = site.margin(screen) + 0.05
        need = max([need] + [row_z + min_distance(screen, t) + min(LAWN_EDGE, t.spacing / 2) + BED_SLACK
                             for t in tall if t.id != screen.id])
    frac, lo, hi = rule['back']
    # (a screen row's plants in front may take the back bed past the style's usual depth: the customer asked for both)
    back = _clamp(max(frac * L, need), lo, max(lo, hi, need if deep else 0.0))
    frac, lo, hi = rule['side']
    side = _clamp(frac * W, lo, hi)
    front = _clamp(rule['front'][0] * L, *rule['front'][1:]) if rule['front'] else 0.0
    # Keep at least MIN_OPEN of lawn each way: narrow lots lose side bed depth, shallow ones the front border first
    if W - 2 * side < MIN_OPEN:
        side = max(0.0, (W - MIN_OPEN) / 2)
    if L - back - front < MIN_OPEN:
        front = max(0.0, L - back - MIN_OPEN)
        if front < HOUSE_SETBACK + 0.3:   # too shallow for anything behind the setback
            front = 0.0
        back = min(back, L - MIN_OPEN - front)
    # Still less lawn than the style keeps? Shrink the beds evenly towards their minimums (the back bed never below
    # what its biggest tall plant needs)
    oval = OVAL if style == 'Lush / Organic' else 1.0
    # (never above where the beds already are: a narrow lot has already given up side depth for MIN_OPEN of lawn)
    floors = (min(back, max(rule['back'][1], min(back, need))), min(side, rule['side'][1]),
              min(front, rule['front'][1]) if rule['front'] and front else 0.0)
    for _ in range(40):
        if (W - 2 * side) * (L - back - front) * oval >= rule['lawn'] * W * L:
            break
        nb, ns, nf = (max(fl, v * 0.95) for v, fl in zip((back, side, front), floors))
        if (nb, ns, nf) == (back, side, front):
            break
        back, side, front = nb, ns, nf
    back, side, front = round(back, 3), round(side, 3), round(front, 3)
    x0, x1, z0, z1 = side, W - side, back, L - front
    if shape:
        # A shaped yard: the biggest open rectangle that keeps these bed depths from each of its sides (the
        # back bed along sides facing the back, the front border along the house, side beds elsewhere)
        def fit(b, sd, fr):
            box = lot_shape.largest_rect(list(shape), W, L, lambda nx, nz: (
                b if nz > 0.7 else (fr or HOUSE_SETBACK + LAWN_EDGE) if nz < -0.7 else sd))
            return box if box and box[1] - box[0] >= MIN_OPEN and box[3] - box[2] >= MIN_OPEN else None

        box = fit(back, side, front)
        if not box:
            # A tight shape: the style's slimmest beds (still deep enough for the biggest tall plant) before
            # giving up the lawn
            back = round(max(rule['back'][1], min(back, need)), 3)
            side = rule['side'][1]
            front = rule['front'][1] if rule['front'] and front else front
            box = fit(back, side, front)
        if not box:
            return bare
        x0, x1, z0, z1 = box
    if style == 'Lush / Organic':
        # An organic oval, a little off-centre (seeded), so the corners become deep, generous beds
        rng = random.Random(f'plan|{seed}')
        rx, rz = (x1 - x0) / 2, (z1 - z0) / 2
        shift = rng.uniform(-0.08, 0.08) * W
        cx = _clamp(W / 2 + shift, x0 + rx * 0.9, x1 - rx * 0.9) if rx > 0 else W / 2
        rx = min(cx - x0, x1 - cx)
        lawn = ('ellipse', round(cx, 3), round((z0 + z1) / 2, 3), round(rx, 3), round(rz, 3))
        entry_x, entry_z = cx, (z0 + z1) / 2
    else:
        lawn = ('rect', x0, x1, z0, z1)
        entry_x, entry_z = (x0 + x1) / 2, z1
    entry = None
    entries = site.rules.entries
    if front > 0 and not any(edge == 'front' for _, _, edge in entries):   # no door marked: the middle
        half = min(ENTRY_WIDTH, x1 - x0) / 2
        entry = (round(entry_x - half, 3), round(entry_x + half, 3), round(entry_z, 3), L)
        # On a shaped yard the gap must lead to the house through the yard itself
        if shape and not all(lot_shape.contains(shape, entry_x, z) for z in
                             [entry_z + (L - entry_z) * k / 8 for k in range(1, 8)]):
            entry = None
    plan = GardenPlan(lawn=lawn, entry=entry, back=back, side=side, front=front,
                      outline=shape, house_walls=site.rules.house_walls)
    return replace(plan, paths=tuple(leg for gx, gz, edge in entries for leg in _walk(plan, gx, gz, edge, W, L)))


def _walk(plan, gx, gz, edge, W, L):
    """The path from a gate or door to the lawn: straight in from its edge, then along to the lawn if the
    gate isn't opposite it — at most two legs (rectangles) WALK_WIDTH wide."""
    h = WALK_WIDTH / 2
    if plan.lawn[0] == 'rect':
        _, x0, x1, z0, z1 = plan.lawn
        tx, tz = _clamp(gx, x0 + h, max(x0 + h, x1 - h)), _clamp(gz, z0 + h, max(z0 + h, z1 - h))
    else:
        _, cx, cz, rx, rz = plan.lawn
        k = math.hypot((gx - cx) / rx, (gz - cz) / rz)
        t = 0.75 / k if k > 0.75 else 1.0
        tx, tz = cx + (gx - cx) * t, cz + (gz - cz) * t
    legs = []

    def rect(ax, az, bx, bz):
        x_lo, x_hi, z_lo, z_hi = min(ax, bx), max(ax, bx), min(az, bz), max(az, bz)
        if x_hi - x_lo < WALK_WIDTH:
            m = (x_lo + x_hi) / 2
            x_lo, x_hi = m - h, m + h
        if z_hi - z_lo < WALK_WIDTH:
            m = (z_lo + z_hi) / 2
            z_lo, z_hi = m - h, m + h
        legs.append(tuple(round(v, 3) for v in (_clamp(x_lo, 0, W), _clamp(x_hi, 0, W), _clamp(z_lo, 0, L), _clamp(z_hi, 0, L))))

    if edge in ('front', 'back'):
        rect(gx, gz, gx, tz)                     # straight in
        if abs(tx - gx) > h:
            rect(gx, tz, tx, tz)                 # then along to the lawn
    else:
        rect(gx, gz, tx, gz)
        if abs(tz - gz) > h:
            rect(tx, gz, tx, tz)
    return legs


def tall_limit(site, plan=None):
    """Tall plants stay behind this depth (z), where they don't block the view from the house."""
    return max(site.length * TALL_DEPTH, plan.back if plan else 0.0)


def too_far_forward(spec, site, z, plan=None):
    return spec.is_plant and spec.layer == 'tall' and z > tall_limit(site, plan) + 1e-9


def under_canopy(spec, x, z, plants, specs_by_id):
    """Is (x, z) in the shade of a tall plant already in the design (its crown, about its spacing wide)?"""
    for p in plants:
        host = specs_by_id.get(str(p['plant_id']))
        if host and host.is_plant and host.grown >= CANOPY_HEIGHT * 0.8 \
                and math.hypot(p['x'] - x, p['z'] - z) <= host.spacing / 2 + spec.spacing / 2 + 0.3:
            return True
    return False


def sun_suits(spec, x, z, site, plants=(), specs_by_id=None):
    """Does this spot get the light the plant needs? (Unknown sun: yes.) Shade plants also do well under a
    tall plant's crown, whatever the open-sky hours say."""
    hours = site.sun_hours(x, z)
    if hours is None or not spec.is_plant or sun_map.plant_fits_hours(spec.sunlight, hours):
        return True
    return spec.sunlight == 'full_shade' and under_canopy(spec, x, z, plants, specs_by_id or {})


class Layout:
    """Plants placed so far, with the constraint checks (spatial hash for speed)."""

    BUCKET = MAX_SPACING + MAX_GAP  # any conflicting neighbour is at most one bucket away

    def __init__(self, site, specs, plan=None, max_plants=MAX_PLANTS):
        self.site = site
        self.plan = plan or GardenPlan()
        self.max_plants = min(MAX_PLANTS, max_plants)
        self.specs = {s.id: s for s in specs}
        self.plants = []           # [{plant_id, x, z, rotation}]
        self.count = {}
        self.total = 0
        self.buckets = {}
        self.path = None           # walkway to the seating spot (set once seats are placed)

    def _key(self, x, z):
        return int(x // self.BUCKET), int(z // self.BUCKET)

    def cost(self):
        return self.total

    def full(self):
        return not self.room_for(1)

    def room_for(self, n):
        """Can n more plants go in (the hard upper bound, and the low-maintenance cap on plants)?"""
        return len(self.plants) + n <= MAX_PLANTS and self.plant_count() + n <= self.max_plants

    def plant_count(self):
        return sum(1 for p in self.plants if self.specs[p['plant_id']].is_plant)

    def fits(self, spec, x, z, ignore=None):
        x, z = _r(x), _r(z)
        if not self.site.in_bounds(spec, x, z) or not self.site.is_clear(spec, x, z):
            return False
        if on_path(self.path, spec, x, z):
            return False
        if self.plan.blocks(spec, x, z) or too_far_forward(spec, self.site, z, self.plan):
            return False
        if self.site.near_house(spec, x, z) or self.site.blocks_view(spec, x, z):
            return False
        if not sun_suits(spec, x, z, self.site, self.plants, self.specs):
            return False
        kx, kz = self._key(x, z)
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for p in self.buckets.get((kx + dx, kz + dz), ()):
                    if p is ignore:
                        continue
                    other = self.specs[p['plant_id']]
                    if math.hypot(p['x'] - x, p['z'] - z) < min_distance(spec, other) - 1e-6:
                        return False
        return True

    def has_stock(self, spec):
        return self.count.get(spec.id, 0) < spec.stock

    def add(self, spec, x, z, rotation):
        plant = {'plant_id': spec.id, 'x': _r(x), 'z': _r(z), 'rotation': round(rotation % 360, 1)}
        self.plants.append(plant)
        self.buckets.setdefault(self._key(plant['x'], plant['z']), []).append(plant)
        self.count[spec.id] = self.count.get(spec.id, 0) + 1
        self.total += spec.price
        return plant

    def remove(self, plant):
        self.plants.remove(plant)
        self.buckets[self._key(plant['x'], plant['z'])].remove(plant)
        self.count[plant['plant_id']] -= 1
        self.total -= self.specs[plant['plant_id']].price

    def find_spot(self, spec, x, z, max_radius, rng=None):
        """Nearest valid spot to (x, z) within max_radius, searching outward in rings."""
        if self.full():
            return None
        if self.fits(spec, x, z):
            return _r(x), _r(z)
        step = max(0.2, spec.spacing / 3)
        r = step
        offset = rng.random() * 2 * math.pi if rng else 0.0
        while r <= max_radius + 1e-9:
            n = min(48, max(8, int(2 * math.pi * r / step)))
            for i in range(n):
                a = offset + 2 * math.pi * i / n
                cx, cz = _r(x + r * math.cos(a)), _r(z + r * math.sin(a))
                if self.fits(spec, cx, cz):
                    return cx, cz
            r += step
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Generation
# ─────────────────────────────────────────────────────────────────────────────

def _band(site, layer):
    """Depth band (z range) for each layer: tall at the back, low at the front."""
    L = site.length
    return {'tall': (0.0, 0.42 * L), 'mid': (0.3 * L, 0.75 * L), 'low': (0.6 * L, L)}[layer]


def _bed_point(layout, spec, z0, z1, rng, x0=None, x1=None):
    """A random point in a planting bed within this depth band (the search starts here, not on the lawn)."""
    site, plan = layout.site, layout.plan
    x0 = 0.0 if x0 is None else x0
    x1 = site.width if x1 is None else x1
    x, z = rng.uniform(x0, x1), rng.uniform(z0, z1)
    for _ in range(40):
        if site.inside(x, z) and not plan.blocks(spec, x, z) and not too_far_forward(spec, site, z, plan):
            break
        x, z = rng.uniform(x0, x1), rng.uniform(z0, z1)
    return x, z


def _shade_spot(layout, spec, x, z, rng):
    """Shade plants start their search beside a tall plant (its shade), not in the open front border.
    Returns (x, z) unchanged for other plants or when nothing tall has been planted yet."""
    if spec.sunlight != 'full_shade':
        return x, z
    tall = [p for p in layout.plants
            if layout.specs[p['plant_id']].is_plant and layout.specs[p['plant_id']].grown >= CANOPY_HEIGHT * 0.8]
    if not tall:
        return x, z
    host = rng.choice(tall)
    r = min_distance(spec, layout.specs[host['plant_id']]) + 0.05
    a = rng.uniform(0, 2 * math.pi)
    return host['x'] + r * math.cos(a), host['z'] + r * math.sin(a)


def _varied(pool, specs, layout):
    """Variety: once a design has VARIETY_FROM plants, no tree or shrub species past MAX_SHARE of them, and
    no low border plant past MAX_SHARE_LOW (edging is massed on purpose) — repeated groups, not one plant
    taking over (44 geraniums of 57). Only when the design can choose from at least VARIETY_MIN_KINDS
    species (a customer who picked one or two plants gets those), and never down to nothing."""
    kinds = {s.id for s in specs if s.is_plant and layout.has_stock(s)}
    total = layout.plant_count()
    if len(kinds) < VARIETY_MIN_KINDS or total < VARIETY_FROM:
        return pool
    varied = [s for s in pool
              if layout.count.get(s.id, 0) + 1 <= (MAX_SHARE_LOW if s.layer == 'low' else MAX_SHARE) * (total + 1)]
    return varied or pool


def _choose(specs, layer, preferred, rng, layout, prefer_price=None):
    """Pick a species for a layer: preferred plants first, then the rest; respects stock and variety."""
    pool = _varied([s for s in specs if s.is_plant and s.layer == layer and layout.has_stock(s)], specs, layout)
    if not pool:
        # Small catalogs: borrow from the neighbouring layer rather than leave a gap
        neighbours = {'tall': ['mid'], 'mid': ['tall', 'low'], 'low': ['mid']}[layer]
        pool = _varied([s for s in specs if s.is_plant and s.layer in neighbours and layout.has_stock(s)], specs, layout)
    if not pool:
        return None
    fav = [s for s in pool if s.id in preferred]
    if fav and rng.random() < 0.8:
        pool = fav
    if prefer_price == 'high':
        pool = sorted(pool, key=lambda s: -s.price)[:max(1, len(pool) // 2 + 1)]
    return rng.choice(pool)


def _fill(layout, specs, plant_budget, preferred, rng, layers, attempts, style, cap=None):
    """Keep adding plants (style-aware) until the budget, the space, the stock or the cap runs out."""
    misses = total_misses = 0
    while misses < attempts and total_misses < attempts * 3:
        if cap is not None and layout.plant_count() >= cap:
            return
        layer = rng.choice(layers)
        spec = _choose(specs, layer, preferred, rng, layout,
                       prefer_price='high' if style == 'Minimalist' else None)
        if spec is None or layout.cost() + spec.price > plant_budget:
            misses += 1
            total_misses += 1
            continue
        if layout.full():
            return
        z0, z1 = _band(layout.site, spec.layer)
        x, z = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
        spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 3, rng=rng)
        if spot is None:
            misses += 1
            total_misses += 1  # a nearly full yard stops the search instead of grinding on
            continue
        layout.add(spec, *spot, rng.uniform(0, 360))
        misses = 0 if style == 'Lush / Organic' else misses


def _symmetrical_courtyard(layout, specs, plant_budget, preferred, rng):
    """Mirrored rows about the centre line: formal back row, flanking pairs, low border."""
    site, plan = layout.site, layout.plan
    cx = site.width / 2

    def room_for_pair(spec):
        return (layout.count.get(spec.id, 0) + 2 <= spec.stock and layout.cost() + 2 * spec.price <= plant_budget
                and layout.room_for(2))

    def bed_middle(depth, spec):
        # The middle of a bed this deep, kept inside the lot and off the lawn edge
        edge = min(LAWN_EDGE, spec.spacing / 2)
        return max(site.margin(spec), min(depth / 2, depth - edge - 0.01))

    # Rows across the lot (depth from the back): a tall back row in the back bed, a low border in the front
    # bed. A courtyard without a lawn keeps the classic three rows.
    def back_row(spec):
        # Middle of the back bed; nearer the fence when shade plants need an understorey row in front of it
        z = bed_middle(plan.back, spec)
        for s in specs:
            if s.is_plant and s.sunlight == 'full_shade' and s.layer != 'tall':
                z = min(z, plan.back - min(LAWN_EDGE, s.spacing / 2) - min_distance(s, spec) - 0.07)
        return max(site.margin(spec), z)

    if plan.lawn:
        rows = [('tall', back_row)]
        if plan.front:
            rows.append(('low', lambda s: site.length - bed_middle(plan.front, s)))
    else:
        rows = [(layer, lambda s, d=depth: site.length * d) for layer, depth in (('tall', 0.18), ('mid', 0.5), ('low', 0.82))]
    back = None  # (spec, z) of the tall back row: shade plants make a formal under-row in its shade
    border = None   # the low border's plant: repeated as edging down the side beds
    for layer, depth_of in rows:
        spec = _choose(specs, layer, preferred, rng, layout)
        if spec is None:
            continue
        z = depth_of(spec)
        if layer == 'tall':
            back = (spec, z)
        elif spec.sunlight == 'full_shade' and back:
            z = back[1] + min_distance(spec, back[0]) + 0.05
        if layer == 'low':
            border = spec
        # Centre piece — on the back row, the finest tall plant as the focal point — then mirrored pairs outward
        centre = spec
        if layer == 'tall' and plan.lawn:
            centre = _specimen(specs, preferred, layout) or spec
        if layout.cost() + centre.price <= plant_budget and layout.room_for(1) and layout.fits(centre, cx, z):
            layout.add(centre, cx, z, 0)
        offset = spec.spacing
        while offset < site.width / 2 and room_for_pair(spec):
            left, right = (cx - offset, z), (cx + offset, z)
            if layout.fits(spec, *left) and layout.fits(spec, *right):
                layout.add(spec, *left, 0)
                layout.add(spec, *right, 0)
            offset += spec.spacing

    # Matching columns down the two side beds, from the back bed to the front border
    if plan.lawn and plan.side:
        # Open, sunny side beds: no shade plants here (they go under the trees)
        spec = _choose([s for s in specs if s.sunlight != 'full_shade'], 'mid', preferred, rng, layout)
        if spec is not None:
            x = bed_middle(plan.side, spec)
            z = plan.back + spec.spacing / 2
            while z <= site.length - plan.front and room_for_pair(spec):
                if layout.fits(spec, x, z) and layout.fits(spec, site.width - x, z):
                    layout.add(spec, x, z, 0)
                    layout.add(spec, site.width - x, z, 0)
                    z += spec.spacing
                else:
                    z += spec.spacing / 3
    # Low edging along both side beds, on the lawn's edge: the front border's plant repeated (one rhythm round the lawn)
    if plan.lawn and plan.side and plan.lawn[0] == 'rect':
        # (out in the open beside the lawn: never a shade plant — the border's own plant only if it takes sun)
        spec = border if border is not None and border.sunlight != 'full_shade' else             _choose([s for s in specs if s.sunlight != 'full_shade'], 'low', preferred, rng, layout)
        if spec is not None:
            x = max(site.margin(spec), plan.lawn[1] - min(LAWN_EDGE, spec.spacing / 2) - 0.02)
            z = plan.back + spec.spacing / 2
            while z <= site.length - plan.front - spec.spacing / 2 and room_for_pair(spec):
                if layout.fits(spec, x, z) and layout.fits(spec, site.width - x, z):
                    layout.add(spec, x, z, 0)
                    layout.add(spec, site.width - x, z, 0)
                    z += spec.spacing
                else:
                    z += spec.spacing / 3
    # Mirrored fill pairs for the rest of the budget
    misses = 0
    while misses < 60:
        spec = _choose(specs, rng.choice(['tall', 'mid', 'low']), preferred, rng, layout)
        if spec is None or layout.count.get(spec.id, 0) + 2 > spec.stock \
                or layout.cost() + 2 * spec.price > plant_budget:
            misses += 1
            continue
        z0, z1 = _band(site, 'tall' if spec.sunlight == 'full_shade' else spec.layer)  # shade plants: back, among tall ones
        if not layout.room_for(2):  # pairs need room for both
            break
        x, z = _bed_point(layout, spec, z0, z1, rng, site.margin(spec), cx - spec.spacing / 2)
        x, z = _shade_spot(layout, spec, x, z, rng)       # shade plants: beside a tree (then mirrored)
        x, z = _r(min(x, site.width - x)), _r(z)
        if layout.fits(spec, x, z) and layout.fits(spec, site.width - x, z) \
                and site.width - 2 * x >= spec.spacing:
            layout.add(spec, x, z, 0)
            layout.add(spec, site.width - x, z, 0)
            misses = 0
        else:
            misses += 1


def _minimalist_courtyard(layout, specs, plant_budget, preferred, rng):
    """A focal specimen on a third, odd-numbered groups, generous open space."""
    site, plan = layout.site, layout.plan
    focal = _choose(specs, 'tall', preferred, rng, layout, prefer_price='high')
    if focal and layout.cost() + focal.price <= plant_budget:  # features may already be placed
        # On a third of the width, in the back bed: framed by the open lawn in front of it
        fx = site.width * rng.choice([1 / 3, 2 / 3])
        fz = max(site.margin(focal), plan.back / 2) if plan.lawn else site.length / 3
        spot = layout.find_spot(focal, fx, fz, max_radius=site.width / 3, rng=rng)
        if spot:
            layout.add(focal, *spot, rng.uniform(0, 360))
    for layer, count in (('mid', 3), ('low', 5)):
        spec = _choose(specs, layer, preferred, rng, layout, prefer_price='high')
        if spec is None:
            continue
        z0, z1 = _band(site, layer)
        gx, gz = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng, site.width * 0.2, site.width * 0.8), rng)
        for _ in range(count):
            if not layout.has_stock(spec) or layout.cost() + spec.price > plant_budget:
                break
            spot = layout.find_spot(spec, gx, gz, max_radius=site.width / 3, rng=rng)
            if spot:
                layout.add(spec, *spot, rng.uniform(0, 360))
        if layer == 'low' and layout.plant_count() < minimalist_cap(site):
            # Echo: a smaller group of the same plant on the other side (repetition ties the garden together)
            ex = site.width - gx
            _drift(layout, spec, ex, gz, 3, plant_budget, rng)
    _fill(layout, specs, plant_budget, preferred, rng, ['mid', 'low'], attempts=25, style='Minimalist',
          cap=minimalist_cap(site))


def _lawn_ring(plan, site, n, out):
    """n points evenly round the lawn, `out` metres outside its edge (in the beds), skipping the house side's entry
    gap and anything outside the yard: where repeated drifts go, so they frame the lawn in a rhythm."""
    if not plan.lawn or n <= 0:
        return []
    pts = []
    if plan.lawn[0] == 'ellipse':
        _, cx, cz, rx, rz = plan.lawn
        for k in range(n):
            a = 2 * math.pi * (k + 0.5) / n
            pts.append((cx + (rx + out) * math.cos(a), cz + (rz + out) * math.sin(a)))
    else:
        _, x0, x1, z0, z1 = plan.lawn
        x0, x1, z0, z1 = x0 - out, x1 + out, z0 - out, z1 + out
        per = 2 * ((x1 - x0) + (z1 - z0))
        for k in range(n):
            d = per * (k + 0.5) / n
            for (ax, az, bx, bz) in ((x0, z0, x1, z0), (x1, z0, x1, z1), (x1, z1, x0, z1), (x0, z1, x0, z0)):
                side = math.hypot(bx - ax, bz - az)
                if d <= side:
                    pts.append((ax + (bx - ax) * d / side, az + (bz - az) * d / side))
                    break
                d -= side
    entry = plan.entry
    return [(x, z) for x, z in pts if site.inside(x, z)
            and not (entry and entry[0] - 0.5 <= x <= entry[1] + 0.5 and z >= entry[2] - 0.5)]


def _drift(layout, spec, cx, cz, n, plant_budget, rng):
    """n of one plant round (cx, cz), as tight as their spacing allows. Returns how many went in."""
    placed = 0
    for _ in range(n):
        if not layout.has_stock(spec) or layout.cost() + spec.price > plant_budget or not layout.room_for(1):
            break
        spot = layout.find_spot(spec, cx, cz, max_radius=spec.spacing * 2.5, rng=rng)
        if spot:
            layout.add(spec, *spot, rng.uniform(0, 360))
            placed += 1
    return placed


def _specimen(specs, preferred, layout):
    """The finest tall plant in stock (the customer's own picks first): the dearest, then the tallest grown."""
    tall = [s for s in specs if s.is_plant and s.layer == 'tall' and layout.has_stock(s)]
    fav = [s for s in tall if s.id in preferred] or tall
    return max(fav, key=lambda s: (s.price, s.grown, s.id), default=None)


def _focal(layout, specs, plant_budget, preferred, rng, x, z):
    """One specimen — the finest tall plant — where the eye lands. Returns its spec (or None)."""
    spec = _specimen(specs, preferred, layout)
    if spec is None or layout.cost() + spec.price > plant_budget:
        return None
    spot = layout.find_spot(spec, x, z, max_radius=max(1.5, spec.spacing * 1.5), rng=rng)
    if not spot:
        return None
    layout.add(spec, *spot, rng.uniform(0, 360))
    return spec


def _palette(specs, layer, preferred, rng, layout, k=2):
    """Up to k species for a layer, chosen once and repeated through the design (rhythm, not a collection)."""
    out = []
    for _ in range(k * 3):
        spec = _choose(specs, layer, preferred, rng, layout)
        if spec is not None and spec not in out:
            out.append(spec)
        if len(out) >= k:
            break
    return out


def _lush(layout, specs, plant_budget, preferred, rng):
    """Layered, abundant beds round an organic lawn, the way a planting designer works:
    a focal specimen where the eye lands (the back bed, on the lawn's line from the house), a small palette repeated
    in odd-numbered drifts (3, 5, 7) round the lawn — alternating, for rhythm — and low drifts hugging the lawn edge
    so the front of the beds spills softly onto it. The rest fills in with the same palette."""
    site, plan = layout.site, layout.plan
    # The focal specimen first, on the lawn's centre line (a big specimen needs its room before anything else; on a
    # narrow lot that can cost the screen one slot, the right trade), then a tall back screen in groups of one or three
    if plan.lawn:
        cx = plan.lawn[1] if plan.lawn[0] == 'ellipse' else (plan.lawn[1] + plan.lawn[2]) / 2
        _focal(layout, specs, plant_budget, preferred, rng, cx, max(0.5, plan.back * 0.45))
    z0, z1 = _band(site, 'tall')
    for _ in range(2):
        spec = _choose(specs, 'tall', preferred, rng, layout)
        if spec is None:
            break
        gx, gz = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
        _drift(layout, spec, gx, gz, rng.choice([1, 3]), plant_budget, rng)
    mids = _palette(specs, 'mid', preferred, rng, layout)
    lows = _palette(specs, 'low', preferred, rng, layout)
    if plan.lawn:
        span = 2 * math.pi * math.sqrt(((plan.lawn[3] ** 2 + plan.lawn[4] ** 2) / 2)) if plan.lawn[0] == 'ellipse' \
            else 2 * ((plan.lawn[2] - plan.lawn[1]) + (plan.lawn[4] - plan.lawn[3]))
        # Mid drifts a little back from the lawn, low drifts right on its edge (offset half a step: they interlock)
        for pal, out, every, sizes in ((mids, 1.1, 4.5, (3, 5)), (lows, 0.35, 2.6, (5, 7))):
            if not pal:
                continue
            ring = _lawn_ring(plan, site, max(3, round(span / every)), out)
            for k, (x, z) in enumerate(ring):
                spec = pal[k % len(pal)]
                x, z = _shade_spot(layout, spec, x, z, rng)
                _drift(layout, spec, x, z, rng.choice(sizes), plant_budget, rng)
    else:
        for pal, layer in ((mids, 'mid'), (lows, 'low')):
            z0, z1 = _band(site, layer)
            for k in range(3):
                if pal:
                    spec = pal[k % len(pal)]
                    _drift(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng.choice([3, 5]), plant_budget, rng)
    # Fill with the same palette (repetition), the tall layer included
    keep = set(preferred) | {s.id for s in mids + lows}
    _fill(layout, specs, plant_budget, keep, rng, ['tall', 'mid', 'low', 'low'], attempts=80, style='Lush / Organic')


# ── Formal and minimalist builders (2026-11-12) ─────────────────────────────────────────────────────────────
# What made both weak: Symmetrical put its plants at fixed depths that the bed rules then refused (a 1.2 m croton
# can't sit in a 1.0 m side bed and keep off the lawn; the front row landed inside the house setback), so it came
# out as one row of palms and a line of geraniums. Minimalist scattered small groups at random points. A designer
# draws the structure first and plants it: an axis, a frame, an edging, repeated accents — or, for minimalism,
# a focal plant and a few large, deliberate blocks of one plant each with the space left open on purpose.

def _around(x, z, radius, step):
    """(x, z) first, then rings of points round it out to `radius`, nearest first."""
    yield x, z
    r = step
    while r <= radius + 1e-9:
        n = max(8, int(2 * math.pi * r / step))
        for i in range(n):
            a = 2 * math.pi * i / n
            yield x + r * math.cos(a), z + r * math.sin(a)
        r += step


def _affords(layout, spec, n, plant_budget):
    """n more of this plant: in stock, within the budget and the plant cap."""
    return (layout.count.get(spec.id, 0) + n <= spec.stock and layout.cost() + n * spec.price <= plant_budget
            and layout.room_for(n))


def _mirrored(layout, spec, x, z, cx, plant_budget, radius=None):
    """A plant at (x, z) and its mirror image across the axis x = cx, nudged together (up to `radius`) until both
    fit. A point on the axis places one centred plant (nudged along the axis only). Returns how many went in."""
    radius = spec.spacing * 0.35 if radius is None else radius
    step = max(0.08, spec.spacing / 6)
    if abs(x - cx) < spec.spacing / 2:
        if not _affords(layout, spec, 1, plant_budget):
            return 0
        k = 0
        while k * step <= radius + 1e-9:
            for dz in ((0.0,) if k == 0 else (-k * step, k * step)):
                if layout.fits(spec, cx, z + dz):
                    layout.add(spec, cx, z + dz, 0)
                    return 1
            k += 1
        return 0
    if not _affords(layout, spec, 2, plant_budget):
        return 0
    left = x < cx
    for px, pz in _around(x, z, radius, step):
        px, pz = _r(px), _r(pz)
        if (px < cx) != left or abs(px - cx) * 2 < spec.spacing:
            continue
        mx = _r(2 * cx - px)
        if layout.fits(spec, px, pz) and layout.fits(spec, mx, pz):
            layout.add(spec, px, pz, 0)
            layout.add(spec, mx, pz, 0)
            return 2
    return 0


def _evenly(a, b, every):
    """Points from a to b about `every` apart, ends included (one in the middle when the stretch is short)."""
    if b - a < every * 0.5:
        return [(a + b) / 2]
    n = max(1, round((b - a) / every))
    return [a + (b - a) * k / n for k in range(n + 1)]


def _pick(specs, layer, preferred, layout, rng, ok=lambda s: True, key=None):
    """One species for a role: the customer's picks first, in stock, passing `ok`; the best by `key` (else at
    random). Borrows the neighbouring layer when this one has nothing."""
    for layers in ((layer,), {'tall': ('mid',), 'mid': ('tall', 'low'), 'low': ('mid',)}[layer]):
        pool = [s for s in specs if s.is_plant and s.layer in layers and layout.has_stock(s) and ok(s)]
        if pool:
            pool = [s for s in pool if s.id in preferred] or pool
            return min(pool, key=key) if key else rng.choice(pool)
    return None


def _symmetrical(layout, specs, plant_budget, preferred, rng):
    """A formal garden, drawn the way a designer draws one: an axis from the house door across the lawn to a focal
    specimen, a tall frame of matching pairs either side of it, a mid row stepping down in front, matching accent
    plants ("sentinels") at a steady rhythm down both side beds, a pair framing the entry, and one low edging plant
    run round the whole lawn like a clipped hedge. A small palette, every plant mirrored."""
    site, plan = layout.site, layout.plan
    if not plan.lawn or plan.lawn[0] != 'rect':
        return _symmetrical_courtyard(layout, specs, plant_budget, preferred, rng)
    _, lx0, lx1, lz0, lz1 = plan.lawn
    cx = (lx0 + lx1) / 2
    sunny = lambda s: s.sunlight != 'full_shade'
    edge = lambda s: min(LAWN_EDGE, s.spacing / 2)

    # The palette, chosen once
    focal = _specimen(specs, preferred, layout)
    frame = _pick(specs, 'tall', preferred, layout, rng, lambda s: sunny(s) and s is not focal) or focal
    # Sentinels: a mid plant that fits the side bed between the fence and the lawn's edge
    side_room = lambda s: plan.side - edge(s) - site.margin(s) >= -1e-9
    accent = (_pick(specs, 'mid', preferred, layout, rng, lambda s: sunny(s) and side_room(s))
              or _pick(specs, 'mid', preferred, layout, rng, sunny))
    step_down = _pick(specs, 'mid', preferred, layout, rng, lambda s: sunny(s) and s is not accent) or accent
    # Edging: the finest-leaved low plant that takes sun (a tidy line), the customer's picks first
    edging = _pick(specs, 'low', preferred, layout, rng, sunny, key=lambda s: (s.spacing, s.price, s.id))
    under = _pick(specs, 'low', preferred, layout, rng, lambda s: s.sunlight == 'full_shade') \
        or _pick(specs, 'mid', preferred, layout, rng, lambda s: s.sunlight == 'full_shade')

    def bed_z(spec, depth):   # middle of the back bed for this plant, off the lawn
        return max(site.margin(spec), min(depth / 2, depth - edge(spec) - 0.01))

    # 1 The focal specimen on the axis, at the far end of the view from the house
    talls = []
    if focal:
        z = bed_z(focal, plan.back)
        if under:   # leave an understorey row in front of the back row
            z = max(site.margin(focal), min(z, plan.back - edge(under) - min_distance(under, focal) - 0.07))
        # (along the axis as far as the back bed allows: a screen row along the fence pushes it forward)
        if _mirrored(layout, focal, cx, z, cx, plant_budget, radius=max(0.6, plan.back)):
            talls.append(layout.plants[-1])
    # 2 The tall frame: matching pairs outward from the specimen along the back
    if frame:
        z = bed_z(frame, plan.back)
        if under:
            z = max(site.margin(frame), min(z, plan.back - edge(under) - min_distance(under, frame) - 0.07))
        off = ((focal.spacing if focal else 0) + frame.spacing) / 2
        while off < site.width / 2 and _affords(layout, frame, 2, plant_budget):
            if _mirrored(layout, frame, cx - off, z, cx, plant_budget, radius=max(frame.spacing * 0.3, plan.back / 2)):
                talls += layout.plants[-2:]
                off += frame.spacing
            else:
                off += frame.spacing / 3
    # 3 Shade plants make a formal under-row in the tall plants' shade
    if under:
        for t in [p for p in talls if p['x'] <= cx + 1e-6]:
            host = layout.specs[t['plant_id']]
            _mirrored(layout, under, t['x'], t['z'] + min_distance(under, host) + 0.05, cx, plant_budget,
                      radius=under.spacing)
    # 4 The step-down row: mid plants at the front of the back bed, between the tall ones
    if step_down:
        z = lz0 - edge(step_down) - 0.02
        for x in _evenly(cx, lx0 - edge(step_down), max(step_down.spacing * 1.5, 1.6)):
            _mirrored(layout, step_down, x, z, cx, plant_budget, radius=step_down.spacing * 0.5)
    # 5 Sentinels down the side beds at a steady rhythm, and a pair framing the way in from the house
    if accent and plan.side and side_room(accent):
        x = (site.margin(accent) + lx0 - edge(accent)) / 2
        for z in _evenly(lz0 + accent.spacing / 2, lz1 - accent.spacing / 2, max(2.4, accent.spacing * 2)):
            _mirrored(layout, accent, x, z, cx, plant_budget, radius=0.3)
    if plan.entry and plan.front:
        ex0 = plan.entry[0]
        z = (lz1 + site.length - HOUSE_SETBACK) / 2
        for spec in (accent, edging):
            if spec and _mirrored(layout, spec, ex0 - spec.spacing / 2 - 0.05, z, cx, plant_budget,
                                  radius=spec.spacing * 0.4):
                break
    # 6 The edging: one low plant all round the lawn (back, sides, front either side of the entry)
    if edging:
        off = edge(edging) + 0.02
        ex, every = lx0 - off, edging.spacing
        runs = [(x, lz0 - off) for x in _evenly(ex, cx, every)]                  # back edge, half of it
        runs += [(ex, z) for z in _evenly(lz0 - off + every, lz1 + off, every)]  # down the side
        if plan.front:
            front_z = min(lz1 + off, site.length - HOUSE_SETBACK)
            runs += [(x, front_z) for x in _evenly(ex + every, cx, every)]       # the front, to the entry
        for x, z in runs:
            _mirrored(layout, edging, x, z, cx, plant_budget, radius=every * 0.3)
        # A second, inner line in the front border when it's deep enough (a low hedge reads as a band, not a dotted line)
        if plan.front >= HOUSE_SETBACK + edging.spacing * 2:
            z = site.length - HOUSE_SETBACK
            for x in _evenly(ex, cx, every):
                _mirrored(layout, edging, x, z, cx, plant_budget, radius=every * 0.3)
    # 7 The rest of the budget: mirrored pairs of the back-bed palette, in the back bed only (the side beds and the
    # front keep their clean rhythm: a formal garden is restrained, not packed)
    palette = [s for s in (frame, step_down, under) if s]
    misses = 0
    while palette and misses < 60 and layout.room_for(2):
        spec = rng.choice(palette)
        if not _affords(layout, spec, 2, plant_budget):
            misses += 1
            continue
        x, z = _bed_point(layout, spec, 0.0, max(plan.back, 0.5), rng, site.margin(spec), cx - spec.spacing / 2)
        x, z = _shade_spot(layout, spec, x, z, rng)
        if x > cx:
            x = 2 * cx - x
        misses = 0 if _mirrored(layout, spec, x, z, cx, plant_budget, radius=spec.spacing) == 2 else misses + 1


def _block(layout, spec, cx, cz, n, plant_budget):
    """A mass planting: up to n of one plant packed on a triangular grid round (cx, cz), nearest first, so it fills
    the bed's own shape (a ribbon in a slim bed, a block in a deep one). Tries a few grid offsets and keeps the one
    that fits most. Returns how many went in."""
    s = spec.spacing + 1e-3
    best = []
    for ox, oz in ((0, 0), (0.5, 0), (0.25, 0.43), (0.5, 0.5)):
        pts = sorted(((cx + (i + (j % 2) * 0.5 + ox) * s, cz + (j + oz) * s * 0.866)
                      for i in range(-8, 9) for j in range(-8, 9)),
                     key=lambda p: math.hypot(p[0] - cx, p[1] - cz))
        ok = []
        for x, z in pts:
            if len(ok) >= n:
                break
            # (a grid's own points are a spacing apart; only the rest of the design can block them)
            if layout.fits(spec, x, z):
                ok.append((x, z))
        if len(ok) > len(best):
            best = ok
    placed = 0
    for x, z in best:
        if not _affords(layout, spec, 1, plant_budget) or not layout.fits(spec, x, z):
            break
        layout.add(spec, x, z, 0)
        placed += 1
    return placed


def _minimalist(layout, specs, plant_budget, preferred, rng):
    """Few plants, placed with intent: a wide open lawn, one sculptural focal plant on a third of the back bed,
    and two or three kinds of plant each massed in one block or ribbon — never dotted about. Balanced, not mirrored:
    the focal plant on one side, the mass on the other, a low ribbon tying the side to the front."""
    site, plan = layout.site, layout.plan
    if not plan.lawn or plan.lawn[0] != 'rect':
        return _minimalist_courtyard(layout, specs, plant_budget, preferred, rng)
    _, lx0, lx1, lz0, lz1 = plan.lawn
    cap = minimalist_cap(site)
    flip = rng.random() < 0.5
    third = lambda k: site.width * (k if not flip else 1 - k)   # mirror the composition (seeded)
    edge = lambda s: min(LAWN_EDGE, s.spacing / 2)
    dear = lambda s: (-s.price, s.id)

    # A sculptural focal plant (not a spreading clump), the customer's picks first
    focal = _pick(specs, 'tall', preferred, layout, rng, lambda s: not s.is_invasive, key=dear) \
        or _pick(specs, 'tall', preferred, layout, rng, key=dear)
    mass = _pick(specs, 'mid', preferred, layout, rng, lambda s: s is not focal and s.sunlight != 'full_shade', key=dear)
    ribbon = _pick(specs, 'low', preferred, layout, rng, key=lambda s: (s.sunlight == 'full_shade', s.spacing, s.id))
    picked = [s for s in specs if s.is_plant and s.id in preferred and s not in (focal, mass, ribbon) and layout.has_stock(s)]

    left = lambda: cap - layout.plant_count()
    if focal and left() > 0 and _affords(layout, focal, 1, plant_budget):
        z = max(site.margin(focal), min(plan.back / 2, plan.back - edge(focal) - 0.01))
        spot = layout.find_spot(focal, third(1 / 3), z, max_radius=site.width / 4, rng=rng)
        if spot:
            layout.add(focal, *spot, 0)
    # The mass: one mid plant in a block on the other third of the back bed (3 or 5)
    if mass and left() > 0:
        z = plan.back - edge(mass) - mass.spacing * 0.5
        _block(layout, mass, third(2 / 3), z, min(left(), 5 if site.width >= 8 else 3), plant_budget)
    # The ribbon: one low plant down the side bed on the mass's side, from the back to the front, and round the
    # front border towards the entry — the line that leads the eye. Shade plants go under the focal plant instead.
    if ribbon and left() > 0:
        if ribbon.sunlight == 'full_shade':
            tall = [p for p in layout.plants if layout.specs[p['plant_id']].layer == 'tall']
            if tall:
                t = tall[0]
                _block(layout, ribbon, t['x'], t['z'] + min_distance(ribbon, layout.specs[t['plant_id']]) + 0.05,
                       min(left(), 5), plant_budget)
        else:
            sx = lx1 + edge(ribbon) + 0.02 if not flip else lx0 - edge(ribbon) - 0.02
            sx = _clamp(sx, site.margin(ribbon), site.width - site.margin(ribbon))
            n = min(left(), 9 if site.length >= 10 else 7)
            _block(layout, ribbon, sx, (lz0 + lz1) / 2 + (lz1 - lz0) * 0.15, n, plant_budget)
            # Echoed once, smaller, on the focal plant's side (repetition ties it together): in the front border,
            # or at the focal plant's foot when the lawn runs up to the house
            if left() > 0:
                fz = min(lz1 + edge(ribbon) + 0.02, site.length - HOUSE_SETBACK) if plan.front                     else lz0 - edge(ribbon) - 0.02
                _block(layout, ribbon, third(1 / 3), fz, min(left(), 3), plant_budget)
    # Any other plants the customer picked: one small block each, beside the mass (never left out)
    for spec in picked:
        if left() <= 0:
            break
        z0, z1 = _band(site, 'tall' if spec.sunlight == 'full_shade' else spec.layer)
        x, z = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
        _block(layout, spec, x, z, min(left(), 3), plant_budget)
    # Room under the cap and budget left: grow the blocks (more of the same, never a new kind) — a bigger budget
    # buys bolder masses, not more variety; round by round so both blocks grow together
    for _ in range(12):
        grew = 0
        for spec in (mass, ribbon):
            mine = [p for p in layout.plants if spec and p['plant_id'] == spec.id]
            if mine and left() > 0 and layout.cost() + spec.price <= plant_budget:
                p = mine[len(mine) // 2]
                grew += _block(layout, spec, p['x'], p['z'], min(left(), 3), plant_budget)
        if not grew:
            break
    # A tiny catalogue (no plant for the mass or the ribbon): more of what there is, still under the cap
    if not (mass or ribbon) and left() > 0:
        palette = [s for s in (focal, *picked) if s]
        _fill(layout, palette, plant_budget, {s.id for s in palette}, rng, ['mid', 'low'], attempts=25,
              style='Minimalist', cap=cap)


_STYLE_BUILDERS ={'Symmetrical': _symmetrical, 'Minimalist': _minimalist, 'Lush / Organic': _lush}

_REASONING = {
    'Symmetrical': "A formal, mirrored garden on one centre line: from the house across a rectangular lawn to a "
                   "specimen plant, framed by matching tall pairs. Matching accent plants mark a steady rhythm down "
                   "both side beds, a pair frames the way in, and one low edging plant runs round the lawn like a "
                   "clipped hedge.",
    'Minimalist': "A calm, uncluttered garden: a wide open lawn, one sculptural plant on a third of the back bed, "
                  "balanced by a single block of one plant on the other side and a low ribbon of one plant leading "
                  "the eye down the side. Few kinds, planted in masses, with the space left open on purpose.",
    'Lush / Organic': "Layered and abundant beds wrapped around an organic oval lawn: a focal specimen where the eye "
                      "lands, a tall back screen, and a few plants repeated in drifts of 3, 5 and 7 all round the lawn, "
                      "the low ones spilling softly onto its edge.",
}

_COURTYARD_REASONING = {
    'Symmetrical': "A formal, mirrored layout: tall plants frame the back, matching pairs line the middle, "
                   "and a low border finishes the front.",
    'Minimalist': "A calm, uncluttered garden: one focal plant on a third, small odd-numbered groups, "
                  "and generous open space between them.",
    'Lush / Organic': "Layered and abundant: a tall back screen, drifts of the same plant in the middle "
                      "and a dense, low front border.",
}


def _seat_spot(style, layout, rng):
    """On the lawn, nearer the house; Lush seats line up with the oval lawn and its entry."""
    site, lawn = layout.site, layout.plan.lawn
    if style == 'Symmetrical':
        x = site.width / 2
    elif style == 'Minimalist':
        x = site.width * rng.choice([0.3, 0.7])   # a third, like the focal plant
    elif lawn and lawn[0] == 'ellipse':
        x = lawn[1]
    else:
        x = site.width * rng.uniform(0.3, 0.7)
    return x, site.length * SEAT_DEPTH


def _rock_centres(style, layout, n, rng):
    """Where each rock group goes: in the planting beds, at the front of the back bed (seen across the lawn)."""
    site, plan = layout.site, layout.plan
    if plan.lawn:
        z = max(0.4, plan.back - 0.6)
        if style == 'Symmetrical':
            # Groups in balanced places along the back bed; many groups (a big yard) spread evenly across it
            xs = [0.5] if n == 1 else [0.25, 0.75, 0.1, 0.9] if n <= 4 else [(i + 0.5) / n for i in range(n)]
            return [(site.width * xs[i % len(xs)], z) for i in range(n)]
        rock = PlantSpec('rock', 'rock', 1, 1, 0.6, 0.8, kind='rock')
        return [_bed_point(layout, rock, 0, site.length * 0.5, rng) for _ in range(n)]
    if style == 'Symmetrical':
        if n == 1:
            return [(site.width / 2, site.length * 0.42)]
        xs = [0.25, 0.75, 0.1, 0.9] if n <= 4 else [(i + 0.5) / n for i in range(n)]
        return [(site.width * xs[i % len(xs)], site.length * 0.45) for i in range(n)]
    return [(rng.uniform(0.15, 0.85) * site.width, rng.uniform(0.25, 0.5) * site.length) for _ in range(n)]


def _group_spot(layout, specs, offsets, x, z, max_radius, step=0.25):
    """Nearest (x, z) to the target where every spec fits at its x-offset (rings outward)."""
    def ok(ax, az):
        # Offsets are SEAT_PAIR_GAP apart (≥ SEAT_PAD, the seats' own minimum distance), so they never clash
        return all(layout.fits(s, ax + off, az) for s, off in zip(specs, offsets))
    if ok(x, z):
        return _r(x), _r(z)
    r = step
    while r <= max_radius + 1e-9:
        n = max(8, int(2 * math.pi * r / step))
        for i in range(n):
            a = 2 * math.pi * i / n
            ax, az = _r(x + r * math.cos(a)), _r(z + r * math.sin(a))
            if ok(ax, az):
                return ax, az
        r += step
    return None


def place_features(layout, features, budget, style, seed=0):
    """
    Put the customer's garden features in first. features: [(spec, quantity)].
    Seats make one seating spot (side by side, facing the house) with a walkway to the front;
    rocks go in tight groups of about three in the beds. Returns customer-facing notes for
    anything that didn't fit the space, stock or budget. Deterministic for (style, seed).
    """
    rng = random.Random(f'features|{style}|{seed}')
    site, notes, short = layout.site, [], []
    seats = [(s, q) for s, q in features if s.kind == 'seat']
    rocks = [(s, q) for s, q in features if s.kind == 'rock']

    placed_seats = []
    wanted = [s for s, q in seats for _ in range(q)]
    if wanted:
        cx, cz = _seat_spot(style, layout, rng)
        affordable, cost = [], layout.cost()
        for spec in wanted:
            if layout.count.get(spec.id, 0) + sum(1 for s in affordable if s.id == spec.id) >= spec.stock \
                    or cost + spec.price > budget:
                short.append(spec.name)
                continue
            affordable.append(spec)
            cost += spec.price
        # The whole group side by side, at the nearest spot where all of it fits; else fewer seats
        while affordable:
            offsets = [(i - (len(affordable) - 1) / 2) * SEAT_PAIR_GAP for i in range(len(affordable))]
            # Start inside the range where the whole group stays in bounds (tiny lots have one exact spot)
            m = max(site.margin(s) for s in affordable)
            lo, hi = m - offsets[0], site.width - m - offsets[-1]
            if lo <= hi:
                cx = min(hi, max(lo, cx))
            anchor = _group_spot(layout, affordable, offsets, cx, cz, max(site.width, site.length) / 2)
            if anchor:
                for spec, off in zip(affordable, offsets):
                    placed_seats.append(layout.add(spec, anchor[0] + off, anchor[1], SEAT_FACING))
                break
            short.append(affordable.pop().name)
        layout.path = seat_path(placed_seats, site)

    groups = []
    for spec, q in rocks:
        n = max(1, q // (STONE_GROUP if spec.height < SMALL_STONE else ROCK_GROUP))
        if style == 'Symmetrical' and q >= 2 and n % 2:
            n += 1   # mirrored groups either side of the axis: the axis itself is the focal plant's
        sizes = [q // n + (1 if i < q % n else 0) for i in range(n)]
        groups += [(spec, k) for k in sizes]
    for (spec, k), (gx, gz) in zip(groups, _rock_centres(style, layout, len(groups), rng)):
        for _ in range(k):
            if not layout.has_stock(spec) or layout.cost() + spec.price > budget:
                short.append(spec.name)
                continue
            spot = layout.find_spot(spec, gx, gz, max_radius=max(2.5, spec.spacing * 3), rng=rng)
            for _ in range(0 if spot else 16):
                # Its spot is taken (a screen, a big plant): the group moves somewhere else in the beds
                ax, az = _bed_point(layout, spec, 0.0, site.length, rng)
                spot = layout.find_spot(spec, ax, az, max_radius=1.5, rng=rng)
                if spot:
                    gx, gz = spot   # the rest of the group gathers here
                    break
            if spot is None:
                short.append(spec.name)
                continue
            layout.add(spec, *spot, rng.uniform(0, 360))

    if short:
        names = sorted(set(short))
        notes.append(f"Left out {len(short)} garden feature{'s' if len(short) > 1 else ''} ({', '.join(names)}) "
                     "that didn't fit the space, stock or budget.")
    return notes


def low_care_cap(site, plan):
    """Most plants for a low-maintenance garden: LOW_CARE_DENSITY per m² of planting bed."""
    beds = max(0.0, site.area - plan.lawn_area())
    return max(6, int(beds * LOW_CARE_DENSITY))


_EDGE_NAMES = {'back': 'back fence', 'front': 'house side', 'left': 'left side', 'right': 'right side'}


def _screen_species(specs, layer, preferred, layout):
    """The best screening plant for a layer: tall for its width (a row of narrow, tall plants hides a fence;
    one wide tree every 3 m doesn't), in stock, not a deep-shade plant. The customer's own picks first."""
    pool = [s for s in specs if s.is_plant and s.layer == layer and s.sunlight != 'full_shade' and layout.has_stock(s)]
    if not pool:
        return None
    mine = [s for s in pool if s.id in preferred]
    return max(mine or pool, key=lambda s: (s.grown / max(s.spacing, 0.1), s.grown))


def plant_screens(layout, specs, plant_budget, preferred, rng):
    """Each stretch the customer wants hidden ("Hide this"): a row of one species (a hedge reads as one
    screen), the tallest the rules allow there — tall plants where tall ones may stand (the back of the
    yard), shrubs nearer the house. Planted before anything else. Returns notes for the design."""
    site, notes = layout.site, []
    W, L = site.width, site.length
    shape = list(site.rules.outline) if site.rules.outline else [(0.0, 0.0), (W, 0.0), (W, L), (0.0, L)]
    for edge, a, b in site.rules.screens:
        by_layer, placed = {}, 0
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uz = ((b[0] - a[0]) / length, (b[1] - a[1]) / length) if length else (1.0, 0.0)
        nx, nz = lot_shape.inward(shape, a, b)
        house_side = edge == 'front' or any(
            lot_shape.seg_dist((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, *w) < 0.05 for w in site.rules.house_walls or ())
        t = 0.0
        while t <= length + 1e-9:
            if layout.full():
                break
            for layer in ('tall', 'mid'):
                spec = by_layer.get(layer) or _screen_species(specs, layer, preferred, layout)
                if spec is None:
                    continue
                by_layer[layer] = spec
                if not layout.has_stock(spec) or layout.cost() + spec.price > plant_budget:
                    continue
                inset = max(site.margin(spec), spec.spacing / 2, HOUSE_SETBACK if house_side else 0.0)
                x, z = a[0] + ux * t + nx * inset, a[1] + uz * t + nz * inset
                spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 0.6, rng=rng)
                if spot:
                    layout.add(spec, *spot, rng.uniform(0, 360))
                    placed += 1
                    t += spec.spacing
                    break
            else:
                t += 0.5
        if placed:
            notes.append(f'Screened the {_EDGE_NAMES[edge]} with a row of {placed} plant{"s" if placed > 1 else ""}, as you asked.')
        else:
            notes.append(f"Couldn't plant a screen along the {_EDGE_NAMES[edge]}: no room, stock or budget for it there.")
    return notes


def generate(style, specs, site, plant_budget, preferred_ids=(), seed=0, features=(), max_plants=MAX_PLANTS,
             quantities=None):
    """A complete layout for one style. Returns {design_name, reasoning, plants, feature_notes}.
    features: [(feature spec, quantity)], placed before any plant. max_plants: e.g. low_care_cap().
    quantities: {plant id: how many} the customer asked for — placed right after the features, exactly that many
    where space, stock and budget allow (place_quantities); the style then fills the beds with the other plants."""
    rng = random.Random(f'{style}|{seed}')
    preferred = {str(p) for p in preferred_ids}
    plan = garden_plan(style, site, specs, seed, screen=screen_row(specs, quantities),
                       must=tall_picks(specs, preferred, quantities, style))
    layout = Layout(site, list(specs) + [f for f, _ in features], plan, max_plants)
    # What the customer asked for comes first, in every style: their amounts of a plant (they need bed room, so
    # before the features' stones take it), then their seats and rocks
    quantities = {str(k): int(v) for k, v in (quantities or {}).items() if int(v) > 0}
    feature_notes = []
    if quantities and plant_budget > 0:
        feature_notes += place_quantities(layout, specs, quantities, plant_budget, style, rng)
    if features:
        feature_notes = place_features(layout, features, plant_budget, style, seed) + feature_notes
        # The seats' walkway is laid after the counted plants: any on it step aside (or make way, said)
        for p in [p for p in list(layout.plants) if layout.specs[p['plant_id']].is_plant
                  and on_path(layout.path, layout.specs[p['plant_id']], p['x'], p['z'])]:
            spec = layout.specs[p['plant_id']]
            layout.remove(p)
            spot = layout.find_spot(spec, p['x'], p['z'], max_radius=spec.spacing * 4, rng=rng)
            if spot:
                layout.add(spec, *spot, p['rotation'])
            else:
                feature_notes.append(f"One {spec.name} made way for the path to your seating.")
    rest = [s for s in specs if s.id not in quantities]   # the amounts asked for are final: no more of those
    if rest and plant_budget > 0 and site.rules.screens:
        feature_notes += plant_screens(layout, rest, plant_budget, preferred, rng)
    if any(s.is_plant for s in rest) and plant_budget > 0:
        # Their tall picks first (the back of the garden has room for only so many trees: theirs come first)
        tall_notes = ensure_picks(layout, [s for s in rest if s.is_plant and s.layer == 'tall'], preferred,
                                  plant_budget, style, rng, protected=quantities)
        _STYLE_BUILDERS[style](layout, rest, plant_budget, preferred, rng)
        # Every plant the customer picked shows up in this design (in the style's own way), or we say why not
        # (a tall pick that didn't fit before the style's planting has had its say: one note, not two)
        noted = {s.id for s in rest if any(f'Your {s.name} ' in n for n in tall_notes)}
        feature_notes += tall_notes + ensure_picks(layout, [s for s in rest if s.id not in noted], preferred,
                                                   plant_budget, style, rng, protected=quantities)
        # Top up towards the budget floor (Lush only: Minimalist keeps its space, Symmetrical stays mirrored)
        if layout.cost() < plant_budget * BUDGET_FLOOR and style == 'Lush / Organic':
            _fill(layout, rest, plant_budget, preferred, rng, ['mid', 'low'], attempts=40, style=style)
    return {
        'design_name': style,
        'reasoning': (_REASONING if plan.lawn else _COURTYARD_REASONING)[style],
        'plants': layout.plants,
        'budget_note': budget_note(layout, specs, plant_budget, style),
        'feature_notes': feature_notes,
        'garden_plan': plan.to_dict(),
    }


def _axis(layout):
    """The design's centre line (Symmetrical mirrors about it)."""
    lawn = layout.plan.lawn
    if lawn and lawn[0] == 'rect':
        return (lawn[1] + lawn[2]) / 2
    return lawn[1] if lawn else layout.site.width / 2


def _pack_tall(layout, spec, n, plant_budget, style):
    """Up to n of a tall plant packed tightly into the back of the garden, from one side (the leftmost spot that
    fits, nearest the back fence first), so the row stays in one piece and the next tall plant still has room —
    random spots split the back bed into gaps nothing else tall fits in. Symmetrical: mirrored pairs from the
    sides inwards (an odd one on the axis). Returns how many went in."""
    site, cx = layout.site, _axis(layout)
    zmax = min(tall_limit(site, layout.plan), site.length)
    step = 0.25
    xs = [site.margin(spec) + i * step for i in range(int((site.width - 2 * site.margin(spec)) / step) + 1)]
    zs = [site.margin(spec) + j * step for j in range(int(max(0.0, zmax - site.margin(spec)) / step) + 1)]
    placed = 0
    while placed < n:
        if style == 'Symmetrical' and n - placed >= 2:
            spot = next(((x, z) for x in xs if x <= cx - spec.spacing / 2 for z in zs
                         if layout.fits(spec, x, z) and layout.fits(spec, _r(2 * cx - x), z)), None)
            if spot and _affords(layout, spec, 2, plant_budget):
                layout.add(spec, *spot, 0)
                layout.add(spec, _r(2 * cx - spot[0]), spot[1], 0)
                placed += 2
                continue
            if spot is None and n - placed >= 2 and placed:
                break
        if not _affords(layout, spec, 1, plant_budget):
            break
        if style == 'Symmetrical':
            spot = next(((cx, z) for z in zs if layout.fits(spec, cx, z)), None)
        else:
            spot = next(((x, z) for x in xs for z in zs if layout.fits(spec, x, z)), None)
        if spot is None:
            break
        layout.add(spec, *spot, 0)
        placed += 1
    return placed


def _place_pick(layout, spec, plant_budget, style, rng, n):
    """A group of a picked plant somewhere it belongs (its layer's band; shade plants beside a tall one):
    a mirrored pair for Symmetrical, a block for Minimalist, a drift for Lush. Returns how many went in."""
    site, cx = layout.site, _axis(layout)
    if spec.layer == 'tall':
        got = _pack_tall(layout, spec, n, plant_budget, style)
        if got:
            return got
    z0, z1 = _band(site, 'tall' if spec.sunlight == 'full_shade' else spec.layer)
    for attempt in range(30):
        if attempt == 15:
            z0, z1 = 0.0, site.length   # its own band is full: anywhere in the beds
        if style == 'Symmetrical':
            x, z = _bed_point(layout, spec, z0, z1, rng, site.margin(spec), max(site.margin(spec), cx - spec.spacing / 2))
            x, z = _shade_spot(layout, spec, x, z, rng)
            x = min(x, 2 * cx - x)
            got = _mirrored(layout, spec, x, z, cx, plant_budget, radius=spec.spacing)
            if not got and _affords(layout, spec, 1, plant_budget):
                got = _mirrored(layout, spec, cx, z, cx, plant_budget, radius=spec.spacing)   # one, on the axis
        else:
            x, z = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
            got = _block(layout, spec, x, z, n, plant_budget) if style == 'Minimalist' \
                else _drift(layout, spec, x, z, n, plant_budget, rng)
        if got:
            return got
    return 0


def _swap_in(layout, spec, plant_budget, style, protected):
    """No free room: replace a plant the design has plenty of (3 or more, not one the customer counted) with the
    picked one, in the same spot — and its mirror twin in a symmetrical design. Returns how many went in."""
    cx = _axis(layout)
    near = {'tall': ('tall', 'mid'), 'mid': ('mid', 'tall', 'low'), 'low': ('low', 'mid')}[spec.layer]
    cands = [p for p in layout.plants
             if (o := layout.specs[p['plant_id']]).is_plant and o.id != spec.id and o.id not in protected
             and layout.count.get(o.id, 0) >= 3 and o.layer in near]
    cands.sort(key=lambda p: (near.index(layout.specs[p['plant_id']].layer),
                              abs(layout.specs[p['plant_id']].spacing - spec.spacing)))
    for p in cands[:60]:
        if p not in layout.plants:
            continue
        group = [p]
        if style == 'Symmetrical' and abs(p['x'] - cx) > 0.05:
            twin = next((q for q in layout.plants if q is not p and q['plant_id'] == p['plant_id']
                         and abs(q['x'] - (2 * cx - p['x'])) < 0.03 and abs(q['z'] - p['z']) < 0.03), None)
            if twin:
                group.append(twin)
        if layout.count[p['plant_id']] - len(group) < 1:
            continue
        old = layout.specs[p['plant_id']]
        for q in group:
            layout.remove(q)
        if _affords(layout, spec, len(group), plant_budget) and all(layout.fits(spec, q['x'], q['z']) for q in group):
            for q in group:
                layout.add(spec, q['x'], q['z'], q['rotation'])
            return len(group)
        for q in group:   # didn't fit: put them back
            layout.add(old, q['x'], q['z'], q['rotation'])
    return 0


def ensure_picks(layout, specs, preferred, plant_budget, style, rng, protected=()):
    """
    Every plant the customer picked appears in every design: picks are their brief, not just a list of what's
    allowed (a style that needs only five roles used to leave the rest out without a word). Each missing pick
    goes in as the style would plant it — a mirrored pair, a block of three, a drift of three — where there is
    room, else in place of a plant the design has plenty of. What still can't go in is named, with the reason.
    """
    notes = []
    for spec in [s for s in specs if s.is_plant and s.id in preferred]:
        if layout.count.get(spec.id, 0):
            continue
        if not layout.has_stock(spec):
            notes.append(f"Your {spec.name} isn't in this design: none left in stock.")
            continue
        n = 2 if style == 'Symmetrical' else 3
        if _place_pick(layout, spec, plant_budget, style, rng, n):
            continue
        if _swap_in(layout, spec, plant_budget, style, protected=set(protected)):
            continue
        why = ('the budget ran out' if layout.cost() + spec.price > plant_budget else
               f"there's no room left for it ({spec.spacing:g} m apart from its neighbours"
               + (', with a 1 m root-barrier gap' if any(layout.specs[p['plant_id']].is_invasive != spec.is_invasive
                                                       for p in layout.plants) else '') + ')')
        notes.append(f"Your {spec.name} isn't in this design: {why}.")
    return notes


MAX_PLANT_QTY = 300   # most of one plant a customer can ask for


def place_quantities(layout, specs, quantities, plant_budget, style, rng):
    """
    The customer's own amounts ({plant id: how many}), placed in that plant's bed band (tall at the back, low at
    the front), tallest first: in mirrored pairs about the centre line for Symmetrical (an odd one on the line),
    in drifts of the same plant for the other styles. Stops at the yard's room, the stock and the plant budget,
    and says which stopped it: "Fitted 18 of your 25 Bamboo: not enough room at 1 m apart."
    """
    site, notes = layout.site, []
    by_id = {s.id: s for s in specs if s.is_plant}
    wanted = sorted(((by_id[k], q) for k, q in quantities.items() if k in by_id),
                    key=lambda sq: (-sq[0].grown, sq[0].id))
    for spec, want in wanted:
        want = min(want, MAX_PLANT_QTY)
        z0, z1 = _band(site, spec.layer)
        placed, why = 0, None

        def stop():
            if not layout.has_stock(spec):
                return 'stock'
            if layout.cost() + spec.price > plant_budget:
                return 'budget'
            if not layout.room_for(1):
                return 'cap'
            return None

        def try_add(x, z, radius):
            spot = layout.find_spot(spec, x, z, max_radius=radius, rng=rng)
            if spot is None:
                return False
            layout.add(spec, *spot, rng.uniform(0, 360))
            return True

        misses, centre = 0, None
        # Three or more of a tall plant: a row along the back fence, centred (how a screen or a bamboo hedge is
        # planted, and symmetrical for every style), at its planting distance; what doesn't fit carries on below
        if spec.layer == 'tall' and want >= 3:
            cx = site.width / 2
            z = site.margin(spec) + 0.05
            for k in range(want):
                if stop():
                    break
                x = cx + (k - (want - 1) / 2) * spec.spacing
                if try_add(x, z, spec.spacing * 0.4):
                    placed += 1
        if spec.layer == 'tall' and placed < want:
            placed += _pack_tall(layout, spec, want - placed, plant_budget, style)
        while placed < want and misses < 60:
            why = stop()
            if why:
                break
            if misses == 20:   # its own band is full: carry on anywhere in the beds (still no tall plants up front)
                z0, z1, centre = 0.0, site.length, None
            if style == 'Symmetrical' and want - placed >= 2 and misses < 20:
                x, z = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng, 0, site.width / 2 - spec.spacing / 2), rng)
                left = layout.find_spot(spec, x, z, max_radius=spec.spacing * 2, rng=rng)
                twin = left and (_r(site.width - left[0]), left[1])
                if left and twin and abs(twin[0] - left[0]) >= spec.spacing and layout.fits(spec, *left):
                    layout.add(spec, *left, 0)
                    if stop() is None and layout.fits(spec, *twin):
                        layout.add(spec, *twin, 0)
                        placed += 2
                    else:
                        placed += 1
                    misses = 0
                    continue
                misses += 1
                continue
            if style == 'Symmetrical' and want - placed == 1 and misses < 20:   # the odd one: on the centre line
                if try_add(site.width / 2, rng.uniform(z0, z1), spec.spacing * 3):
                    placed, misses = placed + 1, 0
                else:
                    misses += 1
                continue
            # Drifts: start a group, keep adding beside it, start a new one when it is full
            if centre is None:
                centre = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
            if try_add(*centre, spec.spacing * 3):
                placed, misses = placed + 1, 0
                if placed % 7 == 0:
                    centre = None
            else:
                centre, misses = None, misses + 1
        if placed < want:
            why = why or stop() or 'space'
            reason = {'stock': f'we have {layout.count.get(spec.id, 0)} in stock',
                      'budget': 'the budget ran out',
                      'cap': 'you asked for a low-maintenance garden' if layout.max_plants < MAX_PLANTS else 'the design is full',
                      'space': f'not enough room at {spec.spacing:g} m apart'}[why]
            notes.append(f'Fitted {placed} of your {want} {spec.name}: {reason}.')
    return notes


def budget_note(layout, specs, plant_budget, style=None):
    """Why a design uses less than 75% of the plant budget (None when it doesn't)."""
    if not plant_budget or layout.cost() >= plant_budget * BUDGET_FLOOR:
        return None
    if style == 'Minimalist' and layout.plant_count() >= minimalist_cap(layout.site):
        return ('Minimalist designs keep generous open space, so this one uses less of your plant budget. '
                'The rest could go to larger feature plants, stone or lighting.')
    if not specs or all(not layout.has_stock(s) for s in specs):
        return 'Limited stock: we used everything currently available. Our team can source more for your project.'
    # (one short counts: a mirrored design adds plants in pairs)
    if layout.max_plants < MAX_PLANTS and layout.plant_count() >= layout.max_plants - 1:
        return ('You asked for a low-maintenance garden, so the beds are planted more sparingly (less to prune and '
                'water). The rest of the budget could go to larger specimens, mulch or edging.')
    if layout.plan.lawn:
        return ('The planting beds are full with healthy spacing before the plant budget is used up; we keep the '
                'middle open as lawn. The rest could go to turf, larger specimens, edging or lighting; our team can advise.')
    return ('Your yard is full with healthy spacing before the plant budget is used up. '
            'The rest could go to larger specimens, hardscape or lighting; our team can advise.')


# ─────────────────────────────────────────────────────────────────────────────
# Check & repair (for AI-generated layouts)
# ─────────────────────────────────────────────────────────────────────────────

def repair(plants, specs, site, plant_budget, preferred_ids=(), seed=0, features=(), style='Lush / Organic',
           max_plants=MAX_PLANTS):
    """
    Make an AI layout satisfy every rule, changing as little as possible.
    Garden features are placed first exactly as generate() would for this style and seed (the AI
    was told where), so the AI's plants are moved off them. AI entries that aren't plants are ignored.
    Returns (plants, adjustments) where adjustments are short customer-facing notes.
    """
    rng = random.Random(f'repair|{seed}')
    by_id = {s.id: s for s in specs if s.is_plant}
    plan = garden_plan(style, site, specs, seed)
    layout = Layout(site, list(specs) + [f for f, _ in features], plan, max_plants)
    feature_notes = place_features(layout, features, plant_budget, style, seed) if features else []
    if site.rules.screens:
        feature_notes += plant_screens(layout, specs, plant_budget, {str(p) for p in preferred_ids}, rng)
    moved = removed = swapped = to_beds = 0
    far = max(site.width, site.length)
    swapped_names = set()

    # Tall plants first: they anchor the design, small ones are easier to move
    ordered = sorted((p for p in plants if str(p.get('plant_id')) in by_id),
                     key=lambda p: -by_id[str(p['plant_id'])].grown)
    unknown = len(plants) - len(ordered)

    for p in ordered:
        spec = by_id[str(p['plant_id'])]
        if not layout.has_stock(spec):
            alt = _similar_in_stock(spec, specs, layout)
            if alt is None:
                removed += 1
                continue
            swapped += 1
            swapped_names.add(spec.name)
            spec = alt
        x = min(site.width, max(0.0, float(p.get('x', 0))))
        z = min(site.length, max(0.0, float(p.get('z', 0))))
        spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 2, rng=rng)
        misplaced = plan.blocks(spec, x, z) or too_far_forward(spec, site, z, plan)
        if spot is None and misplaced:
            # Put on the lawn or (a tall plant) at the front: move it to the nearest bed rather than drop it
            spot = layout.find_spot(spec, x, min(z, tall_limit(site, plan)) if spec.layer == 'tall' else z,
                                    max_radius=far, rng=rng)
        if spot is None:
            removed += 1
            continue
        if misplaced:
            to_beds += 1
        elif math.hypot(spot[0] - x, spot[1] - z) > 0.05:
            moved += 1
        layout.add(spec, *spot, float(p.get('rotation', 0) or 0))

    # Budget: drop the cheapest front plants until within budget (features stay: the customer asked for them)
    while layout.plant_count() and layout.cost() > plant_budget:
        cheapest = min((q for q in layout.plants if q['plant_id'] in by_id),
                       key=lambda q: (by_id[q['plant_id']].grown, by_id[q['plant_id']].price))
        layout.remove(cheapest)
        removed += 1

    added = 0
    before = len(layout.plants)
    if layout.cost() < plant_budget * BUDGET_FLOOR:
        _fill(layout, specs, plant_budget, {str(p) for p in preferred_ids}, rng, ['mid', 'low'], attempts=40, style='repair')
        added = len(layout.plants) - before

    notes = list(feature_notes)
    if to_beds:
        notes.append(f"Moved {to_beds} plant{'s' if to_beds > 1 else ''} into the beds to keep the lawn open "
                     "and tall plants at the back.")
    if moved:
        notes.append(f"Moved {moved} plant{'s' if moved > 1 else ''} apart for healthy spacing, separate watering "
                     "zones and to keep clear areas free.")
    if swapped:
        notes.append(f"Swapped {swapped} plant{'s' if swapped > 1 else ''} that were low on stock "
                     f"({', '.join(sorted(swapped_names))}) for similar ones.")
    if removed:
        notes.append(f"Removed {removed} plant{'s' if removed > 1 else ''} that didn't fit the space, stock or budget.")
    if added:
        notes.append(f"Added {added} plant{'s' if added > 1 else ''} to make better use of your budget.")
    if unknown:
        notes.append(f"Ignored {unknown} plant{'s' if unknown > 1 else ''} that aren't available for this design.")
    return layout.plants, notes


def _similar_in_stock(spec, specs, layout):
    """Same layer, closest price, with stock left."""
    candidates = [s for s in specs if s.is_plant and s.id != spec.id and s.layer == spec.layer and layout.has_stock(s)]
    return min(candidates, key=lambda s: abs(s.price - spec.price)) if candidates else None


def check(plants, specs, site, plant_budget, plan=None):
    """Rule violations of a finished layout (empty list = valid). Used by tests and logging.
    plan: the design's GardenPlan (garden_plan(style, …)); without it the lawn isn't checked."""
    plan = plan or GardenPlan()
    by_id = {s.id: s for s in specs}
    problems = []
    counts = {}
    path = seat_path([p for p in plants if getattr(by_id.get(str(p['plant_id'])), 'kind', '') == 'seat'], site)
    for i, p in enumerate(plants):
        spec = by_id.get(str(p['plant_id']))
        if spec is None:
            problems.append(f'unknown plant {p["plant_id"]}')
            continue
        counts[spec.id] = counts.get(spec.id, 0) + 1
        if not site.in_bounds(spec, p['x'], p['z']):
            problems.append(f'{spec.name} out of bounds')
        if not site.is_clear(spec, p['x'], p['z']):
            problems.append(f'{spec.name} in keep-clear zone')
        if on_path(path, spec, p['x'], p['z']):
            problems.append(f'{spec.name} on the walkway to the seating')
        if plan.blocks(spec, p['x'], p['z']):
            problems.append(f'{spec.name} on the open lawn or a path')
        if site.near_house(spec, p['x'], p['z']):
            problems.append(f'{spec.name} too close to the house')
        if site.blocks_view(spec, p['x'], p['z']):
            problems.append(f'{spec.name} blocks a view to keep')
        if not sun_suits(spec, p['x'], p['z'], site, plants, by_id):
            problems.append(f'{spec.name} in the wrong light')
        if too_far_forward(spec, site, p['z'], plan):
            problems.append(f'{spec.name} is tall and too far forward')
        for q in plants[i + 1:]:
            other = by_id.get(str(q['plant_id']))
            if other and math.hypot(p['x'] - q['x'], p['z'] - q['z']) < min_distance(spec, other) - 1e-6:
                problems.append(f'{spec.name} too close to {other.name}')
    for pid, n in counts.items():
        if n > by_id[pid].stock:
            problems.append(f'{by_id[pid].name} over stock')
    if sum(by_id[str(p['plant_id'])].price for p in plants if str(p['plant_id']) in by_id) > plant_budget:
        problems.append('over budget')
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# Customer edits (AI Designer 3D view): designer's notes and "Tidy up"
# ─────────────────────────────────────────────────────────────────────────────

def _m(v):
    return f'{round(v, 1):g} m'


def _why_apart(a, b):
    """The reason two items need extra room, in plain words ('' when it is only their size)."""
    gap = companion_gap(a, b)
    if not gap:
        return ''
    if a.is_invasive != b.is_invasive:
        spreader = a if a.is_invasive else b
        return f' {spreader.name} spreads by its roots, so it needs room for a root barrier.'
    if a.is_plant and b.is_plant and {a.water_need, b.water_need} == {'low', 'high'}:
        return ' One needs much more water than the other, so they belong in separate watering zones.'
    tall, low = (a, b) if a.grown >= b.grown else (b, a)
    return f' {low.name} needs full sun and would be in the shade of {tall.name}.'


def review(plants, specs, site, plan=None):
    """
    Soft notes on a customer-edited layout: the same rules check() enforces on the engine's own designs,
    worded for the customer. Never blocks anything: the design is theirs.
    plants: [{plant_id, x, z, ...}] (is_existing markers are skipped). Returns
    [{'kind', 'items': [index, ...], 'message'}]; kinds: spacing, lawn, path, forward, walkway, keep_clear, edge,
    house, view, sun, outside.
    """
    plan = plan or GardenPlan()
    by_id = {s.id: s for s in specs}
    placed = [(i, p, by_id.get(str(p.get('plant_id')))) for i, p in enumerate(plants) if not p.get('is_existing')]
    placed = [(i, p, s) for i, p, s in placed if s is not None]
    path = seat_path([p for _, p, s in placed if s.kind == 'seat'], site)
    notes = []
    for n, (i, p, spec) in enumerate(placed):
        x, z = float(p['x']), float(p['z'])
        # Seats belong on the lawn and set the walkway, so only their room and the edges apply to them
        if plan.on_walk(spec, x, z):
            notes.append({'kind': 'path', 'items': [i], 'message':
                          f'{spec.name} is on the path from your gate or door. It is kept clear for walking.'})
        elif plan.blocks(spec, x, z):
            notes.append({'kind': 'lawn', 'items': [i], 'message':
                          f'{spec.name} is on the open lawn. The lawn is kept clear for walking and sitting, '
                          'with the planting in the beds around it.'})
        elif too_far_forward(spec, site, z, plan):
            notes.append({'kind': 'forward', 'items': [i], 'message':
                          f'{spec.name} grows to about {_m(spec.grown)}. This far forward it will block the view '
                          'from the house: tall plants go at the back.'})
        if on_path(path, spec, x, z):
            notes.append({'kind': 'walkway', 'items': [i], 'message': f'{spec.name} is on the walkway to the seating.'})
        if not site.is_clear(spec, x, z):
            notes.append({'kind': 'keep_clear', 'items': [i], 'message':
                          f'{spec.name} is in an area you asked us to keep clear.'})
        if not site.inside(x, z):
            notes.append({'kind': 'outside', 'items': [i], 'message':
                          f'{spec.name} is outside your yard. Move it inside the outline.'})
        elif not site.in_bounds(spec, x, z):
            notes.append({'kind': 'edge', 'items': [i], 'message':
                          f'{spec.name} is close to the edge of the lot: when grown it will spread over the boundary.'})
        if site.near_house(spec, x, z):
            notes.append({'kind': 'house', 'items': [i], 'message':
                          f'{spec.name} is very close to the house. Keep plants about {_m(HOUSE_SETBACK)} from the wall '
                          'for drainage, termites and room to work.'})
        if not sun_suits(spec, x, z, site, [p for _, p, _ in placed], by_id):
            hours = site.sun_hours(x, z)
            need = {'full_sun': f'at least {sun_map.SUN_PLANT_MIN:g} hours of sun',
                    'full_shade': 'shade (or the shelter of a tree)',
                    'partial_shade': f'at least {sun_map.PART_PLANT_MIN:g} hours of sun'}[spec.sunlight]
            notes.append({'kind': 'sun', 'items': [i], 'message':
                          f'{spec.name} needs {need}; this spot gets about {hours:.1f} hours of direct sun a day.'})
        if site.blocks_view(spec, x, z):
            notes.append({'kind': 'view', 'items': [i], 'message':
                          f'{spec.name} grows to about {_m(spec.grown)}: it will block the view you wanted to keep. '
                          'Only low plants go there.'})
        for j, q, other in placed[n + 1:]:
            need = min_distance(spec, other)
            d = math.hypot(x - float(q['x']), z - float(q['z']))
            if d < need - 0.05:   # a few cm of slack: the customer is dragging by hand
                both = spec.is_plant and other.is_plant
                pair = f'Two {spec.name} plants are' if spec.id == other.id else f'{spec.name} and {other.name} are'
                notes.append({'kind': 'spacing', 'items': [i, j], 'message':
                              f'{pair} {_m(d)} apart; they need about {_m(need)}'
                              + (' so they don\'t crowd each other when fully grown.' if both else '.')
                              + _why_apart(spec, other)})
    return notes


def tidy(plants, specs, site, plan=None):
    """
    "Tidy up" for a customer-edited layout: move as few items as possible, each the shortest distance
    that satisfies the rules, keeping everything they chose (nothing added, swapped or removed).
    1. Pick the culprits: repeatedly take the item in the most open notes (a plant dragged into a
       crowd is in all of them; between two equals, the smaller one, then the later one) until no
       note is left among the rest. Seats never move: they set the walkway.
    2. Everything else stays exactly where it is; the culprits, tallest first, go to the nearest
       spot that fits (anywhere when on the lawn or tall at the front); with no room, they stay put.
    Returns (positions, notes): positions = {index: (x, z)} for the items that moved.
    """
    plan = plan or GardenPlan()
    by_id = {s.id: s for s in specs}
    items = {i: (p, by_id.get(str(p.get('plant_id')))) for i, p in enumerate(plants) if not p.get('is_existing')}
    items = {i: t for i, t in items.items() if t[1] is not None}
    movable = {i for i, (_, s) in items.items() if s.kind != 'seat'}

    open_notes = [set(n['items']) & movable for n in review(plants, specs, site, plan)]
    open_notes = [n for n in open_notes if n]   # a seat's own problem can't be fixed by moving plants
    culprits = []
    while open_notes:
        count = {}
        for n in open_notes:
            for i in n:
                count[i] = count.get(i, 0) + 1
        pick = max(count, key=lambda i: (count[i], -items[i][1].grown, i))
        culprits.append(pick)
        open_notes = [n for n in open_notes if pick not in n]

    layout, _ = _layout_of([p if i not in culprits else {'is_existing': True} for i, p in enumerate(plants)],
                           specs, site, plan)
    rng = random.Random('tidy')
    far = max(site.width, site.length)
    moved, stuck = {}, 0
    for i in sorted(culprits, key=lambda i: -items[i][1].grown):
        p, spec = items[i]
        x = min(site.width, max(0.0, float(p['x'])))
        z = min(site.length, max(0.0, float(p['z'])))
        spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 2, rng=rng)
        if spot is None:
            # On the lawn or (tall) at the front: the nearest bed, wherever it is
            spot = layout.find_spot(spec, x, min(z, tall_limit(site, plan)) if spec.layer == 'tall' else z,
                                    max_radius=far, rng=rng)
        if spot is None:
            stuck += 1
            layout.add(spec, x, z, 0)   # no room anywhere: left where they put it (the note stays)
            continue
        moved[i] = spot
        layout.add(spec, *spot, 0)

    notes = []
    if moved:
        n = len(moved)
        notes.append(f"Moved {n} item{'s' if n > 1 else ''} the shortest distance that gives "
                     f"{'them' if n > 1 else 'it'} room to grow and keeps the lawn and walkway open.")
    if stuck:
        notes.append(f"{stuck} item{'s' if stuck > 1 else ''} had no room anywhere: left where you put "
                     f"{'them' if stuck > 1 else 'it'}. Removing a plant or two nearby would help.")
    if not moved and not stuck:
        notes.append('Everything already has room: nothing to move.')
    return moved, notes


def _layout_of(plants, specs, site, plan):
    """A Layout holding a customer's layout as it is (rule breakers included), seats first so the
    walkway is known. Returns (layout, items) with items = [(index, plant, spec)]."""
    by_id = {s.id: s for s in specs}
    layout = Layout(site, list(by_id.values()), plan)
    items = [(i, p, by_id.get(str(p.get('plant_id')))) for i, p in enumerate(plants) if not p.get('is_existing')]
    items = [(i, p, s) for i, p, s in items if s is not None]
    seats = [t for t in items if t[2].kind == 'seat']
    for _, p, s in seats:
        layout.add(s, float(p['x']), float(p['z']), float(p.get('rotation') or 0))
    layout.path = seat_path([p for _, p, _ in seats], site)
    for _, p, s in items:
        if s.kind != 'seat':
            layout.add(s, float(p['x']), float(p['z']), float(p.get('rotation') or 0))
    return layout, items


def place_one(plants, specs, site, spec, plan=None, seed=0):
    """
    Where to put one more item the customer adds: the spot a designer would pick for it (tall at the
    back, low border at the front, shade plants beside a tall one, seats on the open ground near the
    house), clear of everything already there. Returns (x, z) or None when there is no room.
    """
    plan = plan or GardenPlan()
    layout, _ = _layout_of(plants, specs, site, plan)
    rng = random.Random(f'place|{seed}|{spec.id}|{len(plants)}')
    far = max(site.width, site.length)
    if spec.kind == 'seat':
        return layout.find_spot(spec, site.width / 2, site.length * SEAT_DEPTH, max_radius=far, rng=rng)
    z0, z1 = _band(site, spec.layer)
    for _ in range(40):
        x, z = _bed_point(layout, spec, z0, z1, rng)
        x, z = _shade_spot(layout, spec, x, z, rng)
        spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 2, rng=rng)
        if spot:
            return spot
    return layout.find_spot(spec, site.width / 2, (z0 + z1) / 2, max_radius=far, rng=rng)
