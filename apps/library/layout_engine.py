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
from dataclasses import dataclass

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
MINIMALIST_DENSITY = 0.15    # plants per m²: minimalism means open space, not filling the budget

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

# Garden plan: beds around the edges, open lawn in the middle
MIN_OPEN = 2.0               # m: the smallest lawn worth keeping (room to stand, set a table, walk)
LAWN_EDGE = 0.5              # m: a plant's centre stays this far outside the lawn (less for small plants)
ENTRY_WIDTH = 1.2            # m: gap in the front border, from the house onto the lawn
BED_SLACK = 0.3              # m: beds sized for a plant get this much room to move, so placement can find a spot
NO_LAWN_AREA = 12.0          # m²: courtyards smaller than this are planted freely
NO_LAWN_SIDE = 2.5           # m: so are lots narrower than this
TALL_DEPTH = 0.5             # fraction of the lot: tall plants stay behind this line (0 = back)


def minimalist_cap(site):
    return max(6, int(site.width * site.length * MINIMALIST_DENSITY))


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
        spacing = SEAT_PAD if kind == 'seat' else round(max(0.4, min(2.0, size + 0.2)), 2)
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


class Site:
    def __init__(self, width, length, keep_clear_cells=()):
        self.width = float(width)
        self.length = float(length)
        self.keep_clear = set(keep_clear_cells)
        self.cell_w = self.width / GRID
        self.cell_l = self.length / GRID

    def cell_of(self, x, z):
        return (min(GRID - 1, max(0, int(x / self.cell_w))), min(GRID - 1, max(0, int(z / self.cell_l))))

    def margin(self, spec):
        # Keep the canopy mostly inside the lot without wasting small lots
        return min(spec.spacing / 2, self.width / 4, self.length / 4)

    def in_bounds(self, spec, x, z):
        m = self.margin(spec)
        return m <= x <= self.width - m and m <= z <= self.length - m

    def is_clear(self, spec, x, z):
        """Centre and a ring at a third of the spacing must avoid keep-clear cells."""
        r = spec.spacing / 3
        for dx, dz in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r)):
            px = min(self.width - 1e-6, max(0.0, x + dx))
            pz = min(self.length - 1e-6, max(0.0, z + dz))
            if self.cell_of(px, pz) in self.keep_clear:
                return False
        return True


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

    def blocks(self, spec, x, z):
        """True when an item would sit on the open lawn. Seats belong on it; plants and rocks don't."""
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

    def to_dict(self):
        if not self.lawn:
            return {'lawn': None, 'entry': None, 'beds': {}, 'lawn_area': 0.0}
        lawn = ({'shape': 'rect', 'x0': self.lawn[1], 'x1': self.lawn[2], 'z0': self.lawn[3], 'z1': self.lawn[4]}
                if self.lawn[0] == 'rect' else
                {'shape': 'ellipse', 'cx': self.lawn[1], 'cz': self.lawn[2], 'rx': self.lawn[3], 'rz': self.lawn[4]})
        entry = self.entry and dict(zip(('x0', 'x1', 'z0', 'z1'), self.entry))
        return {'lawn': lawn, 'entry': entry, 'beds': {'back': self.back, 'side': self.side, 'front': self.front},
                'lawn_area': self.lawn_area()}

    @classmethod
    def from_dict(cls, d):
        """Inverse of to_dict (a saved or returned design → its plan, e.g. to check it again)."""
        lawn = (d or {}).get('lawn')
        if not lawn:
            return cls()
        keys = ('x0', 'x1', 'z0', 'z1') if lawn['shape'] == 'rect' else ('cx', 'cz', 'rx', 'rz')
        entry = d.get('entry')
        beds = d.get('beds') or {}
        return cls(lawn=(lawn['shape'], *(lawn[k] for k in keys)),
                   entry=entry and tuple(entry[k] for k in ('x0', 'x1', 'z0', 'z1')),
                   back=beds.get('back', 0.0), side=beds.get('side', 0.0), front=beds.get('front', 0.0))


def _rect_depth(x, z, x0, x1, z0, z1):
    """Signed distance into a rectangle (positive inside)."""
    inside = min(x - x0, x1 - x, z - z0, z1 - z)
    if inside >= 0:
        return inside
    dx, dz = max(x0 - x, 0, x - x1), max(z0 - z, 0, z - z1)
    return -math.hypot(dx, dz)


# Bed depths per style, as (fraction of the lot, smallest, largest) in metres
_BEDS = {
    'Symmetrical':    {'back': (0.25, 1.0, 4.0), 'side': (0.18, 0.8, 2.5), 'front': (0.10, 0.6, 1.2)},
    'Minimalist':     {'back': (0.20, 1.0, 3.5), 'side': (0.10, 0.6, 1.2), 'front': None},
    'Lush / Organic': {'back': (0.30, 1.2, 4.5), 'side': (0.20, 0.9, 2.5), 'front': (0.12, 0.8, 1.5)},
}


def garden_plan(style, site, specs=(), seed=0):
    """
    Where the beds and the lawn go for this style. Deterministic for (style, lot, plants, seed), so the
    generator, the AI prompt, repair and check all see the same plan.
    The back bed is made deep enough for the biggest tall plant (its centre fits, canopy over the lawn edge).
    """
    W, L = site.width, site.length
    if W * L < NO_LAWN_AREA or min(W, L) < NO_LAWN_SIDE:
        return GardenPlan()
    rule = _BEDS.get(style, _BEDS['Symmetrical'])
    # Deep enough for the biggest tall plant with a little room to move (BED_SLACK), and for an understorey
    # of shade plants in front of it when the design has any
    tall = [s for s in specs if s.is_plant and s.layer == 'tall']
    shade = [s for s in specs if s.is_plant and s.sunlight == 'full_shade' and s.layer != 'tall']
    need = max((t.spacing / 2 + LAWN_EDGE + BED_SLACK for t in tall), default=0.0)
    need = max([need] + [t.spacing / 2 + min_distance(s, t) + min(LAWN_EDGE, s.spacing / 2) + BED_SLACK
                         for t in tall for s in shade])
    frac, lo, hi = rule['back']
    back = _clamp(max(frac * L, need), lo, max(lo, hi))
    frac, lo, hi = rule['side']
    side = _clamp(frac * W, lo, hi)
    front = _clamp(rule['front'][0] * L, *rule['front'][1:]) if rule['front'] else 0.0
    # Keep at least MIN_OPEN of lawn each way: narrow lots lose side bed depth, shallow ones the front border first
    if W - 2 * side < MIN_OPEN:
        side = max(0.0, (W - MIN_OPEN) / 2)
    if L - back - front < MIN_OPEN:
        front = max(0.0, L - back - MIN_OPEN)
        if front < 0.5:
            front = 0.0
        back = min(back, L - MIN_OPEN - front)
    back, side, front = round(back, 3), round(side, 3), round(front, 3)
    x0, x1, z0, z1 = side, W - side, back, L - front
    if style == 'Lush / Organic':
        # An organic oval, a little off-centre (seeded), so the corners become deep, generous beds
        rng = random.Random(f'plan|{seed}')
        rx, rz = (x1 - x0) / 2, (z1 - z0) / 2
        shift = rng.uniform(-0.08, 0.08) * W
        cx = _clamp(W / 2 + shift, x0 + rx * 0.9, x1 - rx * 0.9) if rx > 0 else W / 2
        rx = min(cx - side, W - side - cx)
        lawn = ('ellipse', round(cx, 3), round((z0 + z1) / 2, 3), round(rx, 3), round(rz, 3))
        entry_x, entry_z = cx, (z0 + z1) / 2
    else:
        lawn = ('rect', x0, x1, z0, z1)
        entry_x, entry_z = W / 2, z1
    entry = None
    if front > 0:
        half = min(ENTRY_WIDTH, x1 - x0) / 2
        entry = (round(entry_x - half, 3), round(entry_x + half, 3), round(entry_z, 3), L)
    return GardenPlan(lawn=lawn, entry=entry, back=back, side=side, front=front)


def tall_limit(site, plan=None):
    """Tall plants stay behind this depth (z), where they don't block the view from the house."""
    return max(site.length * TALL_DEPTH, plan.back if plan else 0.0)


def too_far_forward(spec, site, z, plan=None):
    return spec.is_plant and spec.layer == 'tall' and z > tall_limit(site, plan) + 1e-9


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
        if not plan.blocks(spec, x, z) and not too_far_forward(spec, site, z, plan):
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


def _symmetrical(layout, specs, plant_budget, preferred, rng):
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
    for layer, depth_of in rows:
        spec = _choose(specs, layer, preferred, rng, layout)
        if spec is None:
            continue
        z = depth_of(spec)
        if layer == 'tall':
            back = (spec, z)
        elif spec.sunlight == 'full_shade' and back:
            z = back[1] + min_distance(spec, back[0]) + 0.05
        # Centre piece, then mirrored pairs outward while budget, stock and space allow
        if layout.cost() + spec.price <= plant_budget and layout.room_for(1) and layout.fits(spec, cx, z):
            layout.add(spec, cx, z, 0)
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


def _minimalist(layout, specs, plant_budget, preferred, rng):
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
    _fill(layout, specs, plant_budget, preferred, rng, ['mid', 'low'], attempts=25, style='Minimalist',
          cap=minimalist_cap(site))


def _lush(layout, specs, plant_budget, preferred, rng):
    """Layered drifts: back screen, mid clusters of the same plant, dense front border."""
    site = layout.site
    for layer in ('tall', 'mid', 'low'):
        z0, z1 = _band(site, layer)
        for _ in range(3):  # a few drifts per layer
            spec = _choose(specs, layer, preferred, rng, layout)
            if spec is None:
                break
            cx, cz = _shade_spot(layout, spec, *_bed_point(layout, spec, z0, z1, rng), rng)
            for _ in range(rng.randint(3, 5)):
                if not layout.has_stock(spec) or layout.cost() + spec.price > plant_budget:
                    break
                spot = layout.find_spot(spec, cx, cz, max_radius=spec.spacing * 3, rng=rng)
                if spot:
                    layout.add(spec, *spot, rng.uniform(0, 360))
    _fill(layout, specs, plant_budget, preferred, rng, ['tall', 'mid', 'low', 'low'], attempts=80, style='Lush / Organic')


_STYLE_BUILDERS = {'Symmetrical': _symmetrical, 'Minimalist': _minimalist, 'Lush / Organic': _lush}

_REASONING = {
    'Symmetrical': "A formal, mirrored layout around a rectangular lawn: tall plants frame the back, matching "
                   "columns line the side beds, and a low border with a central entry finishes the front.",
    'Minimalist': "A calm, uncluttered garden: a wide open lawn, one focal plant on a third of the back bed "
                  "and small odd-numbered groups in slim beds around it.",
    'Lush / Organic': "Layered and abundant beds wrapped around an organic oval lawn: a tall back screen, drifts "
                      "of the same plant in deep corner beds and a dense, low front border.",
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
            xs = [0.5] if n == 1 else [0.25, 0.75, 0.1, 0.9]
            return [(site.width * xs[i % len(xs)], z) for i in range(n)]
        rock = PlantSpec('rock', 'rock', 1, 1, 0.6, 0.8, kind='rock')
        return [_bed_point(layout, rock, 0, site.length * 0.5, rng) for _ in range(n)]
    if style == 'Symmetrical':
        if n == 1:
            return [(site.width / 2, site.length * 0.42)]
        xs = [0.25, 0.75, 0.1, 0.9]
        return [(site.width * xs[i % 4], site.length * 0.45) for i in range(n)]
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
        n = max(1, q // ROCK_GROUP)
        sizes = [q // n + (1 if i < q % n else 0) for i in range(n)]
        groups += [(spec, k) for k in sizes]
    for (spec, k), (gx, gz) in zip(groups, _rock_centres(style, layout, len(groups), rng)):
        for _ in range(k):
            if not layout.has_stock(spec) or layout.cost() + spec.price > budget:
                short.append(spec.name)
                continue
            spot = layout.find_spot(spec, gx, gz, max_radius=max(2.5, spec.spacing * 3), rng=rng)
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
    beds = max(0.0, site.width * site.length - plan.lawn_area())
    return max(6, int(beds * LOW_CARE_DENSITY))


def generate(style, specs, site, plant_budget, preferred_ids=(), seed=0, features=(), max_plants=MAX_PLANTS):
    """A complete layout for one style. Returns {design_name, reasoning, plants, feature_notes}.
    features: [(feature spec, quantity)], placed before any plant. max_plants: e.g. low_care_cap()."""
    rng = random.Random(f'{style}|{seed}')
    preferred = {str(p) for p in preferred_ids}
    plan = garden_plan(style, site, specs, seed)
    layout = Layout(site, list(specs) + [f for f, _ in features], plan, max_plants)
    feature_notes = place_features(layout, features, plant_budget, style, seed) if features else []
    if specs and plant_budget > 0:
        _STYLE_BUILDERS[style](layout, specs, plant_budget, preferred, rng)
        # Top up towards the budget floor if the style left budget on the table (Minimalist keeps its space)
        if layout.cost() < plant_budget * BUDGET_FLOOR and style != 'Minimalist':
            _fill(layout, specs, plant_budget, preferred, rng, ['mid', 'low'], attempts=40, style=style)
    return {
        'design_name': style,
        'reasoning': (_REASONING if plan.lawn else _COURTYARD_REASONING)[style],
        'plants': layout.plants,
        'budget_note': budget_note(layout, specs, plant_budget, style),
        'feature_notes': feature_notes,
        'garden_plan': plan.to_dict(),
    }


def budget_note(layout, specs, plant_budget, style=None):
    """Why a design uses less than 75% of the plant budget (None when it doesn't)."""
    if not plant_budget or layout.cost() >= plant_budget * BUDGET_FLOOR:
        return None
    if style == 'Minimalist' and layout.plant_count() >= minimalist_cap(layout.site):
        return ('Minimalist designs keep generous open space, so this one uses less of your plant budget. '
                'The rest could go to larger feature plants, stone or lighting.')
    if not specs or all(not layout.has_stock(s) for s in specs):
        return 'Limited stock: we used everything currently available. Our team can source more for your project.'
    if layout.max_plants < MAX_PLANTS and layout.plant_count() >= layout.max_plants:
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
            problems.append(f'{spec.name} on the open lawn')
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
    [{'kind', 'items': [index, ...], 'message'}]; kinds: spacing, lawn, forward, walkway, keep_clear, edge.
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
        if plan.blocks(spec, x, z):
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
        if not site.in_bounds(spec, x, z):
            notes.append({'kind': 'edge', 'items': [i], 'message':
                          f'{spec.name} is close to the edge of the lot: when grown it will spread over the boundary.'})
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
