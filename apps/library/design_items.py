"""
A saved design's items in one shape: lot metres from the lot's corner, rotation in degrees.

Designs store items two ways:
  AI Designer  {plant_id, x, z, rotation°, uid?, is_existing?}            (already lot metres)
  Studio       {product_id | productId, position{x,y,z}, rotation{y rad}}  (centred on the lot)
The office adjusts booked designs in the Studio (design_review.py), so a design can hold both. Anything
that draws a plan (PDF proposal, share page) reads lot_items() instead of placed_items. Same mapping as
the frontend's toCanvasPlants (data/projectHub.js); the inverse of lotPlantsToStudioItems.
"""
import math


def _num(value, default=0.0):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n == n and abs(n) < 1e6 else default


def lot_items(design):
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    width, length = _num(dims.get('width'), 10.0) or 10.0, _num(dims.get('length'), 15.0) or 15.0
    out = []
    for item in design.placed_items if isinstance(design.placed_items, list) else []:
        if not isinstance(item, dict):
            continue
        position = item.get('position')
        if not isinstance(position, dict):
            out.append(item)   # AI layout item: already in lot metres
            continue
        pid = item.get('productId', item.get('product_id'))
        rotation = item.get('rotation') if isinstance(item.get('rotation'), dict) else {}
        out.append({
            'plant_id': '' if pid in (None, '') else str(pid),
            'x': round(_num(position.get('x')) + width / 2, 3),
            'z': round(_num(position.get('z')) + length / 2, 3),
            'rotation': round(math.degrees(_num(rotation.get('y'))), 1),
        })
    return out
