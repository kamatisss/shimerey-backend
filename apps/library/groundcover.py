"""
Groundcover for AI Designer designs: the beds between plants planted as a living carpet instead of bare mulch,
as landscapers do (plants spaced for their grown size, the gaps filled with groundcover that grows in within about
a year). Priced per m² covered (InventoryItem.is_groundcover, unit_price per m²; labour pricing.labour_rate).

 - Where: the planting beds = the yard less its open lawn, its paths and the hard ground the customer marked.
 - What: a sun groundcover over most of the beds, and a shade-tolerant one under the crowns of the trees and tall
   plants (where grass and sun lovers fail). The customer's own picks win when they picked groundcovers.
 - How much it costs: set aside from each design's plant budget before the plants are chosen, at most
   MAX_BUDGET_SHARE of it (the plants come first: a smaller budget covers part of the beds, and says so).
"""
import math

MAX_BUDGET_SHARE = 0.4      # of a design's plant budget, at most
MIN_AREA = 1.0              # m²: less than this isn't worth a line on the quote
TALL = 2.4                  # m: plants this tall (grown) shade the ground under their crown


def items_for(catalog, preferred=()):
    """In-stock groundcover products (InventoryItems), the customer's own picks first if they picked any.
    → (sun item or None, shade item or None)."""
    pool = [i for i in catalog if getattr(i, 'is_groundcover', False) and i.is_active
            and (i.stock_quantity or 0) > 0 and (i.unit_price or 0) > 0]
    picked = [i for i in pool if str(i.id) in set(map(str, preferred))]
    pool = picked or pool
    if not pool:
        return None, None
    by_price = sorted(pool, key=lambda i: (i.unit_price, i.id))
    sun = next((i for i in by_price if i.sunlight == 'full_sun'), None) \
        or next((i for i in by_price if i.sunlight == 'partial_shade'), None) or by_price[0]
    shade = next((i for i in by_price if i.sunlight == 'full_shade'), None) \
        or next((i for i in by_price if i.sunlight == 'partial_shade'), None) or sun
    return sun, shade


def bed_area(site_area, plan, survey):
    """m² of planting bed: the yard less the open lawn, the paths from the gates and the marked hard ground."""
    paths = sum(max(0.0, x1 - x0) * max(0.0, z1 - z0) for x0, x1, z0, z1 in (plan.paths or ()))
    clear = sum(max(0.0, i['x1'] - i['x0']) * max(0.0, i['z1'] - i['z0'])
                for i in (survey or {}).get('items', []) if i.get('kind') == 'clear')
    return max(0.0, float(site_area) - plan.lawn_area() - paths - clear)


def reserve(beds, unit_cost, plant_budget):
    """How many m² to cover and what that costs, within MAX_BUDGET_SHARE of the plant budget.
    → (area m², cost) — (0, 0) when it isn't worth it."""
    if beds < MIN_AREA or unit_cost <= 0 or plant_budget <= 0:
        return 0.0, 0
    area = min(beds, MAX_BUDGET_SHARE * plant_budget / unit_cost)
    if area < MIN_AREA:
        return 0.0, 0
    area = math.floor(area)
    return float(area), int(area * unit_cost)


def assign(plants, specs_by_id, area, sun, shade, spreads=None):
    """Split `area` m² between the sun and the shade groundcover: the shade one under the crowns of tall plants
    (their grown spread), at most 60%. → [{plant_id, name, area_sqm, sunlight}] (whole m², stock-capped)."""
    if area < MIN_AREA or sun is None:
        return []
    shaded = 0.0
    if shade is not None and shade.id != sun.id:
        for p in plants:
            spec = specs_by_id.get(str(p.get('plant_id')))
            if p.get('is_existing'):
                r = float(p.get('r') or 0)   # trees the customer keeps shade the ground too
            elif spec is not None and spec.is_plant and spec.grown >= TALL:
                r = float((spreads or {}).get(spec.id) or spec.spacing) / 2
            else:
                continue
            shaded += math.pi * r * r
    shaded = min(shaded * 0.8, area * 0.6)   # crowns overlap each other and the lawn
    shade_m2 = int(round(shaded)) if shade is not None and shade.id != sun.id else 0
    sun_m2 = int(area) - shade_m2
    out = []
    for item, m2 in ((sun, sun_m2), (shade, shade_m2)):
        m2 = min(m2, int(item.stock_quantity or 0)) if item is not None else 0
        if item is not None and m2 >= MIN_AREA:
            out.append({'plant_id': str(item.id), 'name': item.name, 'area_sqm': m2, 'sunlight': item.sunlight})
    return out


def note(entries, beds):
    """Customer-facing line for the design's notes."""
    if not entries:
        return None
    covered = sum(e['area_sqm'] for e in entries)
    parts = [f"{e['area_sqm']} m² of {e['name']}" + (' under the trees' if e['sunlight'] == 'full_shade' and len(entries) > 1 else '')
             for e in entries]
    text = ' and '.join(parts)
    if covered < beds * 0.95:   # (whole m² are quoted: 90 of 91 m² is the beds filled)
        return (f"Groundcover: {text} covers {covered:g} of the {beds:.0f} m² of beds (as much as the budget allows "
                'beside the plants); the rest is mulched.')
    return f"Groundcover: {text} fills the beds between the plants, so no bare soil once it grows in (about a year)."
