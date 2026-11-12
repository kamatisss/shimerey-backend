"""
Setting out: where the crew puts each item, in measurements they can take with a tape on site.

Orientation (same as the 3D views and the proposal plan): the lot is `width` × `length` metres, x runs
left → right and z runs from the back of the garden (z = 0) to the house side (z = length). The crew
stands at the house facing the garden, so "left" is their left.

Every placed item gets a label "<type>.<n>": the type number follows the design's plant list (the same
numbers as the work order's bill of materials), n counts that type's items from the house outwards,
then left to right. Each item has two measurements:
  from_house  straight out from the house side of the lot
  from_side   across from the nearer side boundary (side = 'left' | 'right')
Items already on site (AI "keep" markers) are listed as E1, E2… so nobody digs them out.

Used by the work order PDF (plan + table) and the crew's Plan view (booking design_details.setting_out).
"""
from .design_items import lot_items
from .models import InventoryItem

ROUND_TO = 0.05   # metres: what a crew can set out with a tape


def _m(value):
    return round(round(float(value) / ROUND_TO) * ROUND_TO, 2)


def _num(value, default=0.0):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n == n and abs(n) < 1e6 else default


def setting_out(design):
    """{width, length, types: [{no, plant_id, name, category, quantity}], items: [...]} or None."""
    if design is None:
        return None
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    width = _num(dims.get('width'), 10.0) or 10.0
    length = _num(dims.get('length'), 15.0) or 15.0
    items = lot_items(design)
    if not items:
        return None

    breakdown = [r for r in (design.plant_breakdown or []) if isinstance(r, dict)]
    ids = {str(i.get('plant_id')) for i in items if i.get('plant_id') not in (None, '')}
    catalog = {str(c.id): c for c in InventoryItem.objects.filter(id__in=[int(p) for p in ids if p.isdigit()])}
    names = {str(r.get('plant_id')): r.get('name') for r in breakdown}

    # Type numbers: the plant list's order first (matches the bill of materials), then anything else placed
    order = [str(r.get('plant_id')) for r in breakdown if str(r.get('plant_id')) in ids]
    order += sorted(p for p in ids if p not in order)
    number = {pid: n for n, pid in enumerate(order, 1)}

    def where(x, z):
        side = 'left' if x <= width / 2 else 'right'
        return {
            'x': _m(x), 'z': _m(z),
            'from_house': _m(length - z),
            'side': side,
            'from_side': _m(x if side == 'left' else width - x),
            'outside': not (0 <= x <= width and 0 <= z <= length),
        }

    by_type, existing = {}, []
    for item in items:
        x, z = _num(item.get('x')), _num(item.get('z'))
        if item.get('is_existing'):
            existing.append(where(x, z))
            continue
        pid = str(item.get('plant_id') or '')
        if pid not in number:
            continue
        by_type.setdefault(pid, []).append(where(x, z))

    types, rows = [], []
    for pid in order:
        spots = by_type.get(pid)
        if not spots:
            continue
        product = catalog.get(pid)
        name = (product.name if product else None) or names.get(pid) or 'Item'
        category = (product.category if product else None) or 'plant'
        no = number[pid]
        types.append({'no': no, 'plant_id': pid, 'name': name, 'category': category, 'quantity': len(spots)})
        spots.sort(key=lambda s: (s['from_house'], s['x']))
        for n, spot in enumerate(spots, 1):
            rows.append({'label': f'{no}.{n}', 'no': no, 'plant_id': pid, 'name': name, 'category': category,
                         'existing': False, **spot})
    existing.sort(key=lambda s: (s['from_house'], s['x']))
    for n, spot in enumerate(existing, 1):
        rows.append({'label': f'E{n}', 'no': None, 'plant_id': '', 'name': 'Already on site: keep',
                     'category': 'existing', 'existing': True, **spot})
    return {'width': _m(width), 'length': _m(length), 'types': types, 'items': rows, 'outline': yard_outline(dims)}


def yard_outline(dims):
    """The yard's own shape ([{x, z, house}] lot metres) from wherever the design keeps it (the Studio's lot,
    the AI Designer's survey or garden plan), or None for a rectangle."""
    for raw in (dims.get('outline'), (dims.get('siteSurvey') or {}).get('outline'), (dims.get('gardenPlan') or {}).get('outline')):
        if isinstance(raw, list) and len(raw) >= 3 and all(isinstance(p, dict) for p in raw):
            return [{'x': _num(p.get('x')), 'z': _num(p.get('z')), 'house': bool(p.get('house'))} for p in raw]
        if 'outline' in dims and raw is dims.get('outline'):
            return None   # the Studio said: a rectangle
    return None


def _fmt(m):
    return f'{m:.2f} m'


def plan_drawing(design, data, width, max_height, font, bold):
    """
    The lot from above to scale for the work order: back at the top, house side at the bottom, beds and
    lawn, every item at its spot with its label, a 1 m grid and a scale bar. Mirrors proposal._site_plan.
    """
    from reportlab.graphics.shapes import Drawing, Rect, Circle, Ellipse, Line, String
    from reportlab.lib import colors

    lot_w, lot_l = max(data['width'], 1), max(data['length'], 1)
    top_pad, bottom_pad, side_pad = 26, 46, 30
    scale = min((width - 2 * side_pad) / lot_w, (max_height - top_pad - bottom_pad) / lot_l)
    pw, ph = lot_w * scale, lot_l * scale
    height = ph + top_pad + bottom_pad
    ox, oy = (width - pw) / 2, bottom_pad
    X = lambda x: ox + x * scale                 # noqa: E731  lot metres → points
    Y = lambda z: oy + (lot_l - z) * scale       # noqa: E731  back (z = 0) at the top

    d = Drawing(width, height)
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    plan = dims.get('gardenPlan') if isinstance(dims.get('gardenPlan'), dict) else None
    lawn, bed = colors.HexColor('#bbf7d0'), colors.HexColor('#e7d3b8')
    shape = data.get('outline') or [p for p in (plan or {}).get('outline') or [] if isinstance(p, dict)]
    if len(shape) >= 3:
        # A shaped yard: the rest of the lot in grey, the yard's outline in bed colour, house walls heavy
        from reportlab.graphics.shapes import Polygon
        d.add(Rect(ox, oy, pw, ph, fillColor=colors.HexColor('#e2e8f0'), strokeColor=None))
        pts = [v for p in shape for v in (X(_num(p.get('x'))), Y(_num(p.get('z'))))]
        d.add(Polygon(pts, fillColor=bed, strokeColor=colors.HexColor('#047857'), strokeWidth=1))
        for a, b in zip(shape, shape[1:] + shape[:1]):
            if a.get('house'):
                d.add(Line(X(_num(a.get('x'))), Y(_num(a.get('z'))), X(_num(b.get('x'))), Y(_num(b.get('z'))),
                           strokeColor=colors.HexColor('#0f172a'), strokeWidth=3))
    else:
        d.add(Rect(ox, oy, pw, ph, fillColor=bed if plan else colors.HexColor('#f0fdf4'), strokeColor=None))
    if plan:
        for key in ('lawn', 'entry'):
            g = plan.get(key) if isinstance(plan.get(key), dict) else None
            if not g:
                continue
            if g.get('shape') == 'ellipse':
                d.add(Ellipse(X(_num(g.get('cx'))), Y(_num(g.get('cz'))), _num(g.get('rx')) * scale,
                              _num(g.get('rz')) * scale, fillColor=lawn, strokeColor=None))
            else:
                x0, x1, z0, z1 = (_num(g.get(k)) for k in ('x0', 'x1', 'z0', 'z1'))
                d.add(Rect(X(x0), Y(z1), (x1 - x0) * scale, (z1 - z0) * scale, fillColor=lawn, strokeColor=None))
        # Paths from the customer's gates and doors (layout_engine._walk): stepping stones / gravel
        for g in plan.get('paths') or []:
            if isinstance(g, dict):
                x0, x1, z0, z1 = (_num(g.get(k)) for k in ('x0', 'x1', 'z0', 'z1'))
                d.add(Rect(X(x0), Y(z1), (x1 - x0) * scale, (z1 - z0) * scale, fillColor=colors.HexColor('#d6cfc2'),
                           strokeColor=None))

    grid = colors.HexColor('#94a3b8')
    for i in range(1, int(lot_w) + (0 if lot_w == int(lot_w) else 1)):
        d.add(Line(X(i), oy, X(i), oy + ph, strokeColor=grid, strokeWidth=0.15, strokeOpacity=0.6))
    for i in range(1, int(lot_l) + (0 if lot_l == int(lot_l) else 1)):
        d.add(Line(ox, Y(i), ox + pw, Y(i), strokeColor=grid, strokeWidth=0.15, strokeOpacity=0.6))
    d.add(Rect(ox, oy, pw, ph, fillColor=None, strokeColor=colors.HexColor('#047857'), strokeWidth=1.2))
    # The house side is the reference edge: draw it heavier
    d.add(Line(ox, oy, ox + pw, oy, strokeColor=colors.HexColor('#0f172a'), strokeWidth=2.4))

    dark, mid = colors.HexColor('#0f172a'), colors.HexColor('#475569')
    d.add(String(ox + pw / 2, oy - 13, 'HOUSE SIDE  (measure from here)', fontName=bold, fontSize=8, fillColor=dark, textAnchor='middle'))
    d.add(String(ox + pw / 2, oy + ph + 8, f'BACK OF GARDEN  ·  {_fmt(lot_w)} wide', fontName=bold, fontSize=7.5, fillColor=mid, textAnchor='middle'))
    d.add(String(ox - 6, oy + ph / 2, 'LEFT', fontName=bold, fontSize=7.5, fillColor=mid, textAnchor='end'))
    d.add(String(ox + pw + 6, oy + ph / 2, 'RIGHT', fontName=bold, fontSize=7.5, fillColor=mid, textAnchor='start'))
    d.add(String(ox + pw + 6, oy + ph / 2 - 10, f'{_fmt(lot_l)} deep', fontName=font, fontSize=6.5, fillColor=mid, textAnchor='start'))

    # Scale bar: 1 m (or 5 m on big lots)
    bar = 5 if lot_w > 20 else 1
    bx, by = ox, oy - 32
    d.add(Line(bx, by, bx + bar * scale, by, strokeColor=dark, strokeWidth=1.2))
    for t in (bx, bx + bar * scale):
        d.add(Line(t, by - 2.5, t, by + 2.5, strokeColor=dark, strokeWidth=1))
    d.add(String(bx + bar * scale + 4, by - 2.5, f'{bar} m  ·  grid squares are 1 m', fontName=font, fontSize=6.5, fillColor=mid))

    fills = {'plant': '#16a34a', 'hardscape': '#78716c', 'furniture': '#b45309', 'existing': '#334155'}
    r = max(4.5, min(9, scale * 0.35))
    for item in data['items']:
        if item['outside']:
            continue
        cx, cy = X(item['x']), Y(item['z'])
        fill = colors.HexColor(fills.get(item['category'], '#16a34a'))
        if item['existing']:
            d.add(Circle(cx, cy, r, fillColor=colors.white, strokeColor=fill, strokeWidth=1.2, strokeDashArray=[2, 1.5]))
        elif item['category'] == 'plant':
            d.add(Circle(cx, cy, r, fillColor=fill, fillOpacity=0.75, strokeColor=colors.white, strokeWidth=0.6))
        else:
            d.add(Rect(cx - r, cy - r, 2 * r, 2 * r, rx=1.5, ry=1.5, fillColor=fill, fillOpacity=0.75,
                       strokeColor=colors.white, strokeWidth=0.6))
        d.add(String(cx, cy - 2.2, item['label'], fontName=bold, fontSize=5.8 if len(item['label']) > 3 else 6.5,
                     fillColor=fill if item['existing'] else colors.white, textAnchor='middle'))
    return d
