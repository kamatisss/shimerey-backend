"""
The shape of the customer's yard when it isn't a plain rectangle (Mark your yard → Yard shape): an L around the
house, narrower at the back, a cut corner… An outline is a simple polygon of (x, z) points in lot metres inside
the W × L box (x 0..W left → right seen from the house, z 0 = back, z = L the house side). None = the whole box.
Each side i (point i → i + 1) may be a house wall (`house` flags): the 0.6 m planting setback and the house's
shadow follow those sides. Without flags, the sides along z = L are the house.

Used by the engine (layout_engine: what's inside the yard, how far from its edges, where the lawn fits), the sun
map (walls along its sides), pricing (its real area) and the Gemini prompt (cells outside it are off limits).
"""
import math

MIN_POINTS, MAX_POINTS = 3, 16
MIN_AREA = 2.0       # m²
MIN_SIDE = 0.3       # m: shorter sides are merged away (a slip of the finger)


class ShapeError(ValueError):
    """Bad outline; the message is shown to the customer."""


def area(poly):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def contains(poly, x, z):
    """Point in polygon (even-odd); points on the boundary may go either way."""
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, z1), (x2, z2) = poly[i], poly[(i + 1) % n]
        if (z1 > z) != (z2 > z) and x < x1 + (z - z1) * (x2 - x1) / (z2 - z1):
            inside = not inside
    return inside


def seg_dist(x, z, a, b):
    ax, az = a
    dx, dz = b[0] - ax, b[1] - az
    l2 = dx * dx + dz * dz
    t = 0.0 if l2 < 1e-12 else max(0.0, min(1.0, ((x - ax) * dx + (z - az) * dz) / l2))
    return math.hypot(x - ax - t * dx, z - az - t * dz)


def edges(poly):
    return list(zip(poly, poly[1:] + poly[:1]))


def edge_dist(poly, x, z):
    """Distance to the nearest side of the outline."""
    return min(seg_dist(x, z, a, b) for a, b in edges(poly))


def depth(poly, x, z):
    """Signed distance into the yard: positive inside, negative outside."""
    d = edge_dist(poly, x, z)
    return d if contains(poly, x, z) else -d


def inward(poly, a, b):
    """Unit normal of side a→b pointing into the yard."""
    dx, dz = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dz) or 1.0
    nx, nz = -dz / n, dx / n
    mx, mz = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    return (nx, nz) if contains(poly, mx + nx * 0.05, mz + nz * 0.05) else (-nx, -nz)


def facing(poly, a, b):
    """Which side of the lot this stretch of the outline is on: 'back' | 'front' | 'left' | 'right'
    (by the direction it faces into the yard)."""
    nx, nz = inward(poly, a, b)
    if abs(nz) >= abs(nx):
        return 'back' if nz > 0 else 'front'
    return 'left' if nx > 0 else 'right'


def _crosses(p1, p2, p3, p4):
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    return orient(p1, p2, p3) * orient(p1, p2, p4) < 0 and orient(p3, p4, p1) * orient(p3, p4, p2) < 0


def is_simple(poly):
    sides = edges(poly)
    n = len(sides)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if _crosses(*sides[i], *sides[j]):
                return False
    return True


def is_full_box(poly, W, L, tol=0.05):
    """The outline is just the lot's rectangle (then the engine treats it as no outline at all)."""
    if abs(area(poly) - W * L) > tol * max(1.0, W * L) * 0.02:
        return False
    corners = ((0, 0), (W, 0), (W, L), (0, L))
    return all(any(math.hypot(p[0] - c[0], p[1] - c[1]) < tol * max(W, L) for p in poly) for c in corners)


def default_house(pts, L):
    """Sides along the house edge of the lot (z = L) are the house wall."""
    return tuple(abs(a[1] - L) < 1e-6 and abs(b[1] - L) < 1e-6 for a, b in edges(list(pts)))


def clean(raw, W, L):
    """Outline from the customer ([{x, z, house}] or [[x, z]]) → (points, house flags per side) inside the
    box, or None for the whole box / nothing sent. Raises ShapeError for a shape we can't use."""
    if raw in (None, '', []):
        return None
    if not isinstance(raw, list) or not (MIN_POINTS <= len(raw) <= MAX_POINTS):
        raise ShapeError(f'Your yard\'s shape needs {MIN_POINTS} to {MAX_POINTS} corners.')
    pts, flags = [], []
    any_flag = any(isinstance(p, dict) and 'house' in p for p in raw)
    for p in raw:
        try:
            x, z = (p['x'], p['z']) if isinstance(p, dict) else (p[0], p[1])
            x, z = float(x), float(z)
        except (KeyError, IndexError, TypeError, ValueError):
            raise ShapeError("Your yard's shape could not be read. Please draw it again.")
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ShapeError("Your yard's shape could not be read. Please draw it again.")
        pt = (round(min(W, max(0.0, x)), 3), round(min(L, max(0.0, z)), 3))
        if not pts or math.hypot(pt[0] - pts[-1][0], pt[1] - pts[-1][1]) >= MIN_SIDE:
            pts.append(pt)
            flags.append(bool(isinstance(p, dict) and p.get('house')))
    if len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < MIN_SIDE:
        pts.pop()
        flags.pop()
    if len(pts) < MIN_POINTS:
        raise ShapeError("Your yard's shape needs at least 3 corners.")
    if not is_simple(pts):
        raise ShapeError("Your yard's shape crosses over itself. Move the corners so the sides don't cross.")
    if area(pts) < MIN_AREA:
        raise ShapeError("Your yard's shape is too small.")
    house = tuple(flags) if any_flag else default_house(pts, L)
    if is_full_box(pts, W, L) and house == default_house(pts, L):
        return None
    return tuple(pts), house


def largest_rect(poly, W, L, inset):
    """The biggest axis-aligned rectangle (x0, x1, z0, z1) whose every point is inside the yard, at least
    inset(nx, nz) metres from each side (a function of the side's inward normal: deeper beds at the back).
    None when nothing fits. A grid search (≤ 120 × 120) with the largest-rectangle-in-histogram method."""
    step = max(0.2, max(W, L) / 120)
    nx_, nz_ = max(1, int(W / step)), max(1, int(L / step))
    cw, cl = W / nx_, L / nz_
    sides = [(a, b, inset(*inward(poly, a, b))) for a, b in edges(poly)]

    def ok(x, z):
        return contains(poly, x, z) and all(seg_dist(x, z, a, b) >= need - 1e-9 for a, b, need in sides)

    heights = [0] * nx_
    best, best_area = None, 0.0
    for j in range(nz_):
        z = (j + 0.5) * cl
        for i in range(nx_):
            heights[i] = heights[i] + 1 if ok((i + 0.5) * cw, z) else 0
        stack = []
        for i in range(nx_ + 1):
            h = heights[i] if i < nx_ else 0
            start = i
            while stack and stack[-1][1] >= h:
                s, sh = stack.pop()
                a = sh * cl * (i - s) * cw
                if a > best_area:
                    best_area, best = a, (s * cw, i * cw, (j + 1 - sh) * cl, (j + 1) * cl)
                start = s
            stack.append((start, h))
    if not best:
        return None
    # The cells' centres were tested: shrink by half a cell so the corners are inside too
    x0, x1, z0, z1 = best
    return (round(x0 + cw / 2, 3), round(x1 - cw / 2, 3), round(z0 + cl / 2, 3), round(z1 - cl / 2, 3))
