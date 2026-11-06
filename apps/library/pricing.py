"""
Rate-card pricing — one place that turns a garden into a price.

  GET   /api/pricing/            anyone: current rates (shop delivery fee, estimate basis)
  PATCH /api/pricing/            office admin: update rates (clears the "sample rates" flag)
  POST  /api/pricing/estimate/   anyone: {items: [{product_id, quantity}], area_sqm} → estimate
  GET   /api/pricing/quick/      anyone: ?area_sqm&fullness&mode → estimate for a typical planting of that yard

An estimate is built from transparent lines:
  plants (catalog prices) + planting/placing labour (per item, by size)
  + site preparation and materials (per m²) + mobilisation
  → subtotal + design & project management fee (%) → minimum project charge → VAT (if registered)
Shown as a range (± estimate_range_percent): the final price is confirmed after site inspection.
"""
from decimal import ROUND_HALF_UP, Decimal

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .layout_engine import PlantSpec, spacing_for
from .models import InventoryItem, PricingSettings

RATE_FIELDS = [
    'planting_tall', 'planting_mid', 'planting_low', 'install_other', 'site_prep_per_sqm', 'materials_per_sqm',
    'mobilization_fee', 'design_fee_percent', 'minimum_project', 'estimate_range_percent', 'vat_percent',
    'shipping_fee', 'cod_max_amount',
]
PERCENT_FIELDS = {'design_fee_percent', 'estimate_range_percent', 'vat_percent'}


def _peso(value):
    return int(Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def rate_snapshot(ps=None):
    ps = ps or PricingSettings.current()
    data = {f: float(getattr(ps, f)) for f in RATE_FIELDS}
    data.update(vat_registered=ps.vat_registered, service_area=ps.service_area, is_sample=ps.is_sample,
                updated_at=ps.updated_at)
    return data


def layer_of(item):
    """Size class of an inventory item: plants by mature height; anything else is 'other'."""
    if item.category != 'plant':
        return 'other'
    height = float(item.real_world_size or 1.0)
    return PlantSpec('', '', 0, 0, height, spacing_for(height)).layer


def labour_rate(ps, layer):
    return {'tall': ps.planting_tall, 'mid': ps.planting_mid, 'low': ps.planting_low}.get(layer, ps.install_other)


def fixed_costs(ps, area_sqm):
    """Costs that don't depend on what is planted."""
    area = Decimal(str(max(0.0, float(area_sqm or 0))))
    return (ps.site_prep_per_sqm + ps.materials_per_sqm) * area + ps.mobilization_fee


def plant_budget_from_all_in(budget, area_sqm, ps=None):
    """
    How much of an all-in budget is left for plants *including their planting labour*,
    after VAT, the design fee, site preparation, materials and mobilisation.
    """
    ps = ps or PricingSettings.current()
    budget = Decimal(str(budget))
    if ps.vat_registered:
        budget /= (1 + ps.vat_percent / 100)
    budget /= (1 + ps.design_fee_percent / 100)
    return max(0, int(budget - fixed_costs(ps, area_sqm)))


def installed_price(ps, item):
    """Catalog price + the labour to plant/place it — what one more plant really costs."""
    return int(item.unit_price + labour_rate(ps, layer_of(item)))


CATEGORY_ORDER = {'plant': 0, 'hardscape': 1, 'furniture': 2}


def _ordered(lines):
    """Plants first, then garden features (rocks, then seating) — how every list shows them."""
    return sorted(lines, key=lambda l: CATEGORY_ORDER.get(l.get('category'), 3))


def project_estimate(counts, items_by_id, area_sqm, ps=None):
    """
    counts: {item_id(str): quantity}; items_by_id: {str(id): InventoryItem}.
    Returns lines, totals and the rates used (a snapshot for saving with a design).
    """
    ps = ps or PricingSettings.current()
    area = max(0.0, float(area_sqm or 0))
    plants = labour = Decimal(0)
    plant_lines = []
    for pid, qty in counts.items():
        item = items_by_id.get(str(pid))
        if item is None or qty <= 0:
            continue
        plants += item.unit_price * qty
        labour += labour_rate(ps, layer_of(item)) * qty
        plant_lines.append({'plant_id': str(item.id), 'name': item.name, 'quantity': qty, 'category': item.category,
                            'unit_price': _peso(item.unit_price), 'subtotal': _peso(item.unit_price * qty)})
    area_d = Decimal(str(area))
    lines = [
        {'key': 'plants', 'label': 'Plants & materials from our nursery', 'amount': _peso(plants)},
        {'key': 'labour', 'label': 'Planting & installation labour', 'amount': _peso(labour)},
        {'key': 'site_prep', 'label': f'Site preparation ({area:g} m²)', 'amount': _peso(ps.site_prep_per_sqm * area_d)},
        {'key': 'materials', 'label': f'Soil, compost & mulch ({area:g} m²)', 'amount': _peso(ps.materials_per_sqm * area_d)},
        {'key': 'mobilization', 'label': 'Crew mobilisation & delivery', 'amount': _peso(ps.mobilization_fee)},
    ]
    subtotal = sum(Decimal(l['amount']) for l in lines)
    fee = subtotal * ps.design_fee_percent / 100
    lines.append({'key': 'design_fee', 'label': f'Design & project management ({float(ps.design_fee_percent):g}%)',
                  'amount': _peso(fee)})
    before_vat = subtotal + Decimal(_peso(fee))
    if before_vat < ps.minimum_project:
        lines.append({'key': 'minimum', 'label': 'Minimum project charge adjustment',
                      'amount': _peso(ps.minimum_project - before_vat)})
        before_vat = ps.minimum_project
    vat = before_vat * ps.vat_percent / 100 if ps.vat_registered else Decimal(0)
    if vat:
        lines.append({'key': 'vat', 'label': f'VAT ({float(ps.vat_percent):g}%)', 'amount': _peso(vat)})
    total = _peso(before_vat + vat)
    spread = float(ps.estimate_range_percent) / 100
    return {
        'lines': lines,
        'plant_breakdown': _ordered(plant_lines),
        'plant_cost': _peso(plants),
        'total': total,
        # Ranges rounded to ₱100: an estimate shouldn't look more precise than it is
        'low': int(round(total * (1 - spread), -2)),
        'high': int(round(total * (1 + spread), -2)),
        'is_sample_rates': ps.is_sample,
        'rates': rate_snapshot(ps),
    }


def plants_only_estimate(counts, items_by_id, ps=None):
    """'I'll plant them myself': catalog prices + shop delivery, no labour or site work."""
    ps = ps or PricingSettings.current()
    plants = Decimal(0)
    plant_lines = []
    for pid, qty in counts.items():
        item = items_by_id.get(str(pid))
        if item is None or qty <= 0:
            continue
        plants += item.unit_price * qty
        plant_lines.append({'plant_id': str(item.id), 'name': item.name, 'quantity': qty, 'category': item.category,
                            'unit_price': _peso(item.unit_price), 'subtotal': _peso(item.unit_price * qty)})
    has_features = any(l['category'] != 'plant' for l in plant_lines)
    lines = [{'key': 'plants', 'label': 'Plants & garden features from our nursery' if has_features
              else 'Plants from our nursery', 'amount': _peso(plants)},
             {'key': 'delivery', 'label': 'Delivery', 'amount': _peso(ps.shipping_fee)}]
    total = sum(l['amount'] for l in lines)
    # Catalog prices are exact, so there is no range to show
    return {'lines': lines, 'plant_breakdown': _ordered(plant_lines), 'plant_cost': _peso(plants), 'total': total,
            'low': total, 'high': total, 'is_sample_rates': ps.is_sample, 'rates': rate_snapshot(ps)}


# Budget bands — the same labels as the Project Hub's public price ranges (portfolio.PRICE_BANDS),
# so budgets and finished-project prices read the same everywhere.
BUDGET_BANDS = [
    {'label': 'Under ₱25K',     'min': 10_000,  'max': 25_000},
    {'label': '₱25K – ₱50K',    'min': 25_000,  'max': 50_000},
    {'label': '₱50K – ₱100K',   'min': 50_000,  'max': 100_000},
    {'label': '₱100K – ₱250K',  'min': 100_000, 'max': 250_000},
    {'label': '₱250K – ₱500K',  'min': 250_000, 'max': 500_000},
    {'label': '₱500K and up',   'min': 500_000, 'max': 1_000_000},
]
PLANTS_ONLY_BANDS = [
    {'label': 'Under ₱5K',      'min': 1_000,   'max': 5_000},
    {'label': '₱5K – ₱10K',     'min': 5_000,   'max': 10_000},
    {'label': '₱10K – ₱25K',    'min': 10_000,  'max': 25_000},
    {'label': '₱25K – ₱50K',    'min': 25_000,  'max': 50_000},
    {'label': '₱50K and up',    'min': 50_000,  'max': 100_000},
]


def _round_to(value, step, up):
    import math
    return int((math.ceil if up else math.floor)(value / step) * step)


def suggest_range(area_sqm, installed=True, ps=None):
    """
    A realistic budget range for a garden of this size, from the rate card and catalog:
    a light planting (~0.25 plants/m², cheaper plants) to a full one (~0.6 plants/m²).
    """
    ps = ps or PricingSettings.current()
    plants = [i for i in InventoryItem.objects.filter(category='plant', stock_quantity__gt=0, is_active=True) if i.unit_price > 0]
    if not plants:
        return None
    area = max(1.0, float(area_sqm))
    unit = sorted(float(installed_price(ps, i) if installed else i.unit_price) for i in plants)
    cheap = unit[: max(1, len(unit) // 2)]
    light = area * 0.25 * cheap[len(cheap) // 2]
    full = area * 0.6 * (sum(unit) / len(unit))
    if installed:
        base = float(fixed_costs(ps, area))
        low = float(_apply_fee_vat(base + light, ps))
        high = float(_apply_fee_vat(base + full, ps))
        floor = float(ps.minimum_project) * (1 + float(ps.vat_percent) / 100 if ps.vat_registered else 1)
        low, high = max(low, floor), max(high, floor * 1.5)
    else:
        low, high = light + float(ps.shipping_fee), full + float(ps.shipping_fee)
    step = 1_000 if high < 20_000 else 5_000
    return {'low': max(step, _round_to(low, step, up=False)), 'high': _round_to(high, step, up=True)}


# Plants per m² for each fullness; light and full match suggest_range's ends
FULLNESS = {'light': 0.25, 'balanced': 0.4, 'full': 0.6}
# A layered planting: a few tall plants, more mid-height, mostly low plants and groundcover
LAYER_MIX = {'tall': 0.15, 'mid': 0.35, 'low': 0.5}
QUICK_MAX_AREA = 2500


def typical_planting(area_sqm, fullness='balanced'):
    """
    {item_id: quantity} for a typical garden of this size, from in-stock catalog plants:
    plants per m² by fullness, split across tall/mid/low, spread over the plants in each layer.
    A light planting uses the cheaper half of each layer. Returns (counts, items_by_id).
    """
    plants = [i for i in InventoryItem.objects.filter(category='plant', stock_quantity__gt=0, is_active=True) if i.unit_price > 0]
    layers = {}
    for item in sorted(plants, key=lambda i: (i.unit_price, i.id)):
        layers.setdefault(layer_of(item), []).append(item)
    if fullness == 'light':
        layers = {k: v[: max(1, (len(v) + 1) // 2)] for k, v in layers.items()}
    present = {k: w for k, w in LAYER_MIX.items() if layers.get(k)}
    if not present:
        return {}, {}
    total = max(1, round(float(area_sqm) * FULLNESS[fullness]))
    weight = sum(present.values())
    counts = {}
    for layer, w in present.items():
        n = max(1, round(total * w / weight))
        group = layers[layer]
        for k in range(n):
            pid = str(group[k % len(group)].id)
            counts[pid] = counts.get(pid, 0) + 1
    return counts, {str(i.id): i for i in plants}


def _apply_fee_vat(amount, ps):
    total = Decimal(str(amount)) * (1 + ps.design_fee_percent / 100)
    return total * (1 + ps.vat_percent / 100) if ps.vat_registered else total


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([AllowAny])
def budget_suggestion(request):
    """GET /api/pricing/suggest/?area_sqm=120&mode=install|plants → bands + a suggested range for the yard."""
    installed = request.query_params.get('mode', 'install') != 'plants'
    try:
        area = float(request.query_params.get('area_sqm', 100))
        if not 0 < area <= 40000:
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': 'area_sqm must be a number between 0 and 40000.'}, status=status.HTTP_400_BAD_REQUEST)
    return Response({'bands': BUDGET_BANDS if installed else PLANTS_ONLY_BANDS,
                     'suggested': suggest_range(area, installed)})

class EstimateThrottle(AnonRateThrottle):
    scope = 'estimates'


@api_view(['GET', 'PATCH'])
@permission_classes([AllowAny])
def pricing_settings(request):
    ps = PricingSettings.current()
    if request.method == 'GET':
        return Response(rate_snapshot(ps))

    from .views_api import IsOfficeAdmin
    if not IsOfficeAdmin().has_permission(request, None):
        return Response({'detail': 'Office admins only.'}, status=status.HTTP_403_FORBIDDEN)
    errors = {}
    for field in RATE_FIELDS:
        if field not in request.data:
            continue
        try:
            value = Decimal(str(request.data[field]))
            if value < 0 or (field in PERCENT_FIELDS and value > 100) or value > Decimal('100000000'):
                raise ValueError
            setattr(ps, field, value.quantize(Decimal('0.01')))
        except (ValueError, ArithmeticError):
            errors[field] = 'Enter a number from 0' + (' to 100.' if field in PERCENT_FIELDS else ' up.')
    if 'vat_registered' in request.data:
        ps.vat_registered = str(request.data['vat_registered']).lower() in ('1', 'true', 'yes', 'on')
    if 'service_area' in request.data:
        ps.service_area = str(request.data['service_area'])[:200]
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    before = rate_snapshot(PricingSettings.current())
    ps.is_sample = False
    ps.updated_by = request.user
    ps.save()
    after = rate_snapshot(ps)
    changed = [k for k in (*RATE_FIELDS, 'vat_registered', 'service_area') if str(before.get(k)) != str(after.get(k))]
    if changed:
        from . import activity
        shown = ', '.join(f'{k.replace("_", " ")} {before.get(k)} → {after.get(k)}' for k in changed[:4])
        activity.record(request.user, 'settings', 'pricing_changed',
                        'Changed the rate card: ' + shown + (f' and {len(changed) - 4} more' if len(changed) > 4 else ''),
                        link='/pricing-settings', fields=changed)
    return Response(after)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([EstimateThrottle])
def pricing_estimate(request):
    """Installed-price estimate for a list of catalog items on a garden of area_sqm."""
    raw = request.data.get('items')
    if not isinstance(raw, list) or len(raw) > 500:
        return Response({'error': 'items must be a list of {product_id, quantity}.'}, status=status.HTTP_400_BAD_REQUEST)
    counts = {}
    for entry in raw:
        try:
            pid, qty = str(int(entry['product_id'])), int(entry.get('quantity', 1))
        except (KeyError, TypeError, ValueError):
            return Response({'error': 'Each item needs a numeric product_id and quantity.'}, status=status.HTTP_400_BAD_REQUEST)
        if not 1 <= qty <= 999:
            return Response({'error': 'Quantities must be between 1 and 999.'}, status=status.HTTP_400_BAD_REQUEST)
        counts[pid] = counts.get(pid, 0) + qty
    try:
        area = float(request.data.get('area_sqm', 0))
        if not 0 <= area <= 40000:
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': 'area_sqm must be a number between 0 and 40000.'}, status=status.HTTP_400_BAD_REQUEST)
    items = {str(i.id): i for i in InventoryItem.objects.filter(id__in=counts.keys())}
    return Response(project_estimate(counts, items, area))


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([EstimateThrottle])
def quick_estimate(request):
    """GET /api/pricing/quick/?area_sqm=80&fullness=light|balanced|full&mode=install|plants → landing-page estimate."""
    fullness = request.query_params.get('fullness', 'balanced')
    installed = request.query_params.get('mode', 'install') != 'plants'
    if fullness not in FULLNESS:
        return Response({'error': 'fullness must be light, balanced or full.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        area = float(request.query_params.get('area_sqm', 0))
        if not 1 <= area <= QUICK_MAX_AREA:
            raise ValueError
    except (TypeError, ValueError):
        return Response({'error': f'area_sqm must be a number between 1 and {QUICK_MAX_AREA}.'}, status=status.HTTP_400_BAD_REQUEST)
    counts, items = typical_planting(area, fullness)
    if not counts:
        return Response({'error': 'No plants in stock to estimate with.'}, status=status.HTTP_404_NOT_FOUND)
    est = project_estimate(counts, items, area) if installed else plants_only_estimate(counts, items)
    est.update(area_sqm=area, fullness=fullness, mode='install' if installed else 'plants',
               plant_count=sum(counts.values()))
    return Response(est)
