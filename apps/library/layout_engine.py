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
MINIMALIST_DENSITY = 0.15    # plants per m²: minimalism means open space, not filling the budget


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

    @property
    def layer(self):
        """tall = back of the garden, low = front border."""
        if self.height >= 2.5:
            return 'tall'
        if self.height >= 0.8:
            return 'mid'
        return 'low'


def spacing_for(height, spacing_cm=None):
    """Explicit spacing wins; otherwise a rule of thumb from mature height."""
    if spacing_cm:
        return max(MIN_SPACING, min(MAX_SPACING, abs(spacing_cm) / 100))
    return round(max(MIN_SPACING, min(MAX_SPACING, 0.55 * (height or 1.0) + 0.2)), 2)


def specs_from_inventory(items):
    """InventoryItem queryset/list → PlantSpecs (plants with no stock are left out)."""
    specs = []
    for item in items:
        height = float(item.real_world_size or 1.0)
        specs.append(PlantSpec(
            id=str(item.id), name=item.name, price=int(item.unit_price),
            stock=max(0, int(item.stock_quantity or 0)), height=height,
            spacing=spacing_for(height, item.spacing_cm),
        ))
    return [s for s in specs if s.stock > 0 and s.price > 0]


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


class Layout:
    """Plants placed so far, with the constraint checks (spatial hash for speed)."""

    BUCKET = MAX_SPACING  # any conflicting neighbour is at most one bucket away

    def __init__(self, site, specs):
        self.site = site
        self.specs = {s.id: s for s in specs}
        self.plants = []           # [{plant_id, x, z, rotation}]
        self.count = {}
        self.total = 0
        self.buckets = {}

    def _key(self, x, z):
        return int(x // self.BUCKET), int(z // self.BUCKET)

    def cost(self):
        return self.total

    def full(self):
        return len(self.plants) >= MAX_PLANTS

    def fits(self, spec, x, z, ignore=None):
        x, z = _r(x), _r(z)
        if not self.site.in_bounds(spec, x, z) or not self.site.is_clear(spec, x, z):
            return False
        kx, kz = self._key(x, z)
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for p in self.buckets.get((kx + dx, kz + dz), ()):
                    if p is ignore:
                        continue
                    other = self.specs[p['plant_id']]
                    if math.hypot(p['x'] - x, p['z'] - z) < (spec.spacing + other.spacing) / 2 - 1e-6:
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


def _choose(specs, layer, preferred, rng, layout, prefer_price=None):
    """Pick a species for a layer: preferred plants first, then the rest; respects stock."""
    pool = [s for s in specs if s.layer == layer and layout.has_stock(s)]
    if not pool:
        # Small catalogs: borrow from the neighbouring layer rather than leave a gap
        neighbours = {'tall': ['mid'], 'mid': ['tall', 'low'], 'low': ['mid']}[layer]
        pool = [s for s in specs if s.layer in neighbours and layout.has_stock(s)]
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
        if cap is not None and len(layout.plants) >= cap:
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
        x = rng.uniform(0, layout.site.width)
        z = rng.uniform(z0, z1)
        spot = layout.find_spot(spec, x, z, max_radius=spec.spacing * 3, rng=rng)
        if spot is None:
            misses += 1
            total_misses += 1  # a nearly full yard stops the search instead of grinding on
            continue
        layout.add(spec, *spot, rng.uniform(0, 360))
        misses = 0 if style == 'Lush / Organic' else misses


def _symmetrical(layout, specs, plant_budget, preferred, rng):
    """Mirrored rows about the centre line: formal back row, flanking pairs, low border."""
    site = layout.site
    cx = site.width / 2
    rows = [('tall', 0.18), ('mid', 0.5), ('low', 0.82)]
    for layer, depth in rows:
        spec = _choose(specs, layer, preferred, rng, layout)
        if spec is None:
            continue
        z = site.length * depth
        # Centre piece, then mirrored pairs outward while budget, stock and space allow
        if layout.cost() + spec.price <= plant_budget and layout.fits(spec, cx, z):
            layout.add(spec, cx, z, 0)
        offset = spec.spacing
        while offset < site.width / 2 and len(layout.plants) + 2 <= MAX_PLANTS:
            if layout.count.get(spec.id, 0) + 2 > spec.stock or layout.cost() + 2 * spec.price > plant_budget:
                break
            left, right = (cx - offset, z), (cx + offset, z)
            if layout.fits(spec, *left) and layout.fits(spec, *right):
                layout.add(spec, *left, 0)
                layout.add(spec, *right, 0)
            offset += spec.spacing
    # Mirrored fill pairs for the rest of the budget
    misses = 0
    while misses < 60:
        spec = _choose(specs, rng.choice(['tall', 'mid', 'low']), preferred, rng, layout)
        if spec is None or layout.count.get(spec.id, 0) + 2 > spec.stock \
                or layout.cost() + 2 * spec.price > plant_budget:
            misses += 1
            continue
        z0, z1 = _band(site, spec.layer)
        if len(layout.plants) + 2 > MAX_PLANTS:  # pairs need room for both
            break
        x, z = _r(rng.uniform(site.margin(spec), cx - spec.spacing / 2)), _r(rng.uniform(z0, z1))
        if layout.fits(spec, x, z) and layout.fits(spec, site.width - x, z) \
                and site.width - 2 * x >= spec.spacing:
            layout.add(spec, x, z, 0)
            layout.add(spec, site.width - x, z, 0)
            misses = 0
        else:
            misses += 1


def _minimalist(layout, specs, plant_budget, preferred, rng):
    """A focal specimen on a third, odd-numbered groups, generous open space."""
    site = layout.site
    focal = _choose(specs, 'tall', preferred, rng, layout, prefer_price='high')
    if focal and focal.price <= plant_budget:
        fx = site.width * rng.choice([1 / 3, 2 / 3])
        spot = layout.find_spot(focal, fx, site.length / 3, max_radius=site.width / 3, rng=rng)
        if spot:
            layout.add(focal, *spot, rng.uniform(0, 360))
    for layer, count in (('mid', 3), ('low', 5)):
        spec = _choose(specs, layer, preferred, rng, layout, prefer_price='high')
        if spec is None:
            continue
        z0, z1 = _band(site, layer)
        gx, gz = rng.uniform(site.width * 0.2, site.width * 0.8), (z0 + z1) / 2
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
            cx, cz = rng.uniform(0, site.width), rng.uniform(z0, z1)
            for _ in range(rng.randint(3, 5)):
                if not layout.has_stock(spec) or layout.cost() + spec.price > plant_budget:
                    break
                spot = layout.find_spot(spec, cx, cz, max_radius=spec.spacing * 3, rng=rng)
                if spot:
                    layout.add(spec, *spot, rng.uniform(0, 360))
    _fill(layout, specs, plant_budget, preferred, rng, ['tall', 'mid', 'low', 'low'], attempts=80, style='Lush / Organic')


_STYLE_BUILDERS = {'Symmetrical': _symmetrical, 'Minimalist': _minimalist, 'Lush / Organic': _lush}

_REASONING = {
    'Symmetrical': "A formal, mirrored layout: tall plants frame the back, matching pairs line the middle, "
                   "and a low border finishes the front.",
    'Minimalist': "A calm, uncluttered garden: one focal plant on a third, small odd-numbered groups, "
                  "and generous open space between them.",
    'Lush / Organic': "Layered and abundant: a tall back screen, drifts of the same plant in the middle "
                      "and a dense, low front border.",
}


def generate(style, specs, site, plant_budget, preferred_ids=(), seed=0):
    """A complete layout for one style. Returns {design_name, reasoning, plants}."""
    rng = random.Random(f'{style}|{seed}')
    preferred = {str(p) for p in preferred_ids}
    layout = Layout(site, specs)
    if specs and plant_budget > 0:
        _STYLE_BUILDERS[style](layout, specs, plant_budget, preferred, rng)
        # Top up towards the budget floor if the style left budget on the table (Minimalist keeps its space)
        if layout.cost() < plant_budget * BUDGET_FLOOR and style != 'Minimalist':
            _fill(layout, specs, plant_budget, preferred, rng, ['mid', 'low'], attempts=40, style=style)
    return {
        'design_name': style,
        'reasoning': _REASONING[style],
        'plants': layout.plants,
        'budget_note': budget_note(layout, specs, plant_budget, style),
    }


def budget_note(layout, specs, plant_budget, style=None):
    """Why a design uses less than 75% of the plant budget (None when it doesn't)."""
    if not plant_budget or layout.cost() >= plant_budget * BUDGET_FLOOR:
        return None
    if style == 'Minimalist' and len(layout.plants) >= minimalist_cap(layout.site):
        return ('Minimalist designs keep generous open space, so this one uses less of your plant budget. '
                'The rest could go to larger feature plants, stone or lighting.')
    if not specs or all(not layout.has_stock(s) for s in specs):
        return 'Limited stock: we used everything currently available. Our team can source more for your project.'
    return ('Your yard is full with healthy spacing before the plant budget is used up. '
            'The rest could go to larger specimens, hardscape or lighting; our team can advise.')


# ─────────────────────────────────────────────────────────────────────────────
# Check & repair (for AI-generated layouts)
# ─────────────────────────────────────────────────────────────────────────────

def repair(plants, specs, site, plant_budget, preferred_ids=(), seed=0):
    """
    Make an AI layout satisfy every rule, changing as little as possible.
    Returns (plants, adjustments) where adjustments are short customer-facing notes.
    """
    rng = random.Random(f'repair|{seed}')
    by_id = {s.id: s for s in specs}
    layout = Layout(site, specs)
    moved = removed = swapped = 0
    swapped_names = set()

    # Tall plants first: they anchor the design, small ones are easier to move
    ordered = sorted((p for p in plants if str(p.get('plant_id')) in by_id),
                     key=lambda p: -by_id[str(p['plant_id'])].height)
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
        if spot is None:
            removed += 1
            continue
        if math.hypot(spot[0] - x, spot[1] - z) > 0.05:
            moved += 1
        layout.add(spec, *spot, float(p.get('rotation', 0) or 0))

    # Budget: drop the cheapest front plants until within budget
    while layout.plants and layout.cost() > plant_budget:
        cheapest = min(layout.plants, key=lambda q: (by_id[q['plant_id']].height, by_id[q['plant_id']].price))
        layout.remove(cheapest)
        removed += 1

    added = 0
    before = len(layout.plants)
    if layout.cost() < plant_budget * BUDGET_FLOOR:
        _fill(layout, specs, plant_budget, {str(p) for p in preferred_ids}, rng, ['mid', 'low'], attempts=40, style='repair')
        added = len(layout.plants) - before

    notes = []
    if moved:
        notes.append(f"Moved {moved} plant{'s' if moved > 1 else ''} apart for healthy spacing and to keep clear areas free.")
    if swapped:
        notes.append(f"Swapped {swapped} plant{'s' if swapped > 1 else ''} that were low on stock "
                     f"({', '.join(sorted(swapped_names))}) for similar ones.")
    if removed:
        notes.append(f"Removed {removed} plant{'s' if removed > 1 else ''} that didn't fit the space, stock or budget.")
    if added:
        notes.append(f"Added {added} plant{'s' if added > 1 else ''} to make better use of your budget.")
    if unknown:
        notes.append(f"Ignored {unknown} plant{'s' if unknown > 1 else ''} that aren't in our catalog.")
    return layout.plants, notes


def _similar_in_stock(spec, specs, layout):
    """Same layer, closest price, with stock left."""
    candidates = [s for s in specs if s.id != spec.id and s.layer == spec.layer and layout.has_stock(s)]
    return min(candidates, key=lambda s: abs(s.price - spec.price)) if candidates else None


def check(plants, specs, site, plant_budget):
    """Rule violations of a finished layout (empty list = valid). Used by tests and logging."""
    by_id = {s.id: s for s in specs}
    problems = []
    counts = {}
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
        for q in plants[i + 1:]:
            other = by_id.get(str(q['plant_id']))
            if other and math.hypot(p['x'] - q['x'], p['z'] - q['z']) < (spec.spacing + other.spacing) / 2 - 1e-6:
                problems.append(f'{spec.name} too close to {other.name}')
    for pid, n in counts.items():
        if n > by_id[pid].stock:
            problems.append(f'{by_id[pid].name} over stock')
    if sum(by_id[str(p['plant_id'])].price for p in plants if str(p['plant_id']) in by_id) > plant_budget:
        problems.append('over budget')
    return problems
