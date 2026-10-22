"""
AI Garden Designer — hybrid pipeline: Gemini for creativity, the smart layout engine
for correctness and availability.

  POST /api/scan-zones/        photo → suggested keep-clear zones (customer confirms them)
  POST /api/generate-layouts/  budget, lot size, plants, keep_clear_zones → 3 designs
  GET  /api/ai-designer/health/   office admin: how often AI vs smart layouts were used

Per request:
  • Symmetrical and Minimalist always come from the layout engine (instant, free).
  • Lush / Organic is the AI "signature" design (1 Gemini call) when available; the
    engine checks and repairs it (spacing, keep-clear zones, stock, budget).
  • If Gemini is off, out of quota, failing, or the customer used their daily AI
    designs, the engine designs Lush / Organic too. The customer always gets 3 designs.

Settings: AI_MODE = 'auto' | 'engine_only' (env; 'engine_only' skips Gemini, e.g. on demo day).
"""
import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import List
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, Count
from django.utils import timezone
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import layout_engine as engine
from .models import InventoryItem, LayoutGenerationLog, PricingSettings
from .pricing import fixed_costs, installed_price, plant_budget_from_all_in, plants_only_estimate, project_estimate
from .utils import scan_image_zones

logger = logging.getLogger(__name__)

GRID_COLS = GRID_ROWS = engine.GRID
GEMINI_TIMEOUT_MS = 20_000
AI_STYLE = 'Lush / Organic'          # the one design Gemini makes
_CACHE_TTL = 300                     # identical requests within 5 min reuse the result
_BREAKER_KEY = 'gemini_breaker'


# ─────────────────────────────────────────────────────────────────────────────
# Gemini response schema
# ─────────────────────────────────────────────────────────────────────────────

class PlantArrangement(BaseModel):
    plant_id: str
    gx: int = Field(..., ge=0)   # grid column 0..GRID_COLS-1
    gz: int = Field(..., ge=0)   # grid row    0..GRID_ROWS-1 (0 = back, 19 = front)
    rotation: float


class PlantCostBreakdown(BaseModel):
    plant_id: str
    name: str
    quantity: int
    unit_price: int
    subtotal: int


class GardenDesignSchema(BaseModel):
    design_name: str
    reasoning: str
    total_cost: int
    plants: List[PlantArrangement]
    plant_breakdown: List[PlantCostBreakdown]


def expand_grid_coords(plants, lot_width, lot_length):
    """AI grid indices → metres (cell centres)."""
    cell_w, cell_l = lot_width / GRID_COLS, lot_length / GRID_ROWS
    out = []
    for p in plants:
        item = dict(p)
        item['x'] = round((item.pop('gx', 0) + 0.5) * cell_w, 3)
        item['z'] = round((item.pop('gz', 0) + 0.5) * cell_l, 3)
        out.append(item)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Availability: AI mode, circuit breaker, quotas
# ─────────────────────────────────────────────────────────────────────────────

def ai_mode():
    return os.environ.get('AI_MODE', 'auto').strip().lower()


def gemini_api_key():
    key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
    return None if not key or key == 'your_gemini_api_key_here' else key


def breaker_open():
    """True while Gemini is paused after a failure (no point waiting on it)."""
    return cache.get(_BREAKER_KEY) is not None


def trip_breaker(error_text):
    """
    Daily quota exhausted → pause until the quota resets (midnight Pacific time).
    Anything else (outage, timeout) → pause for 5 minutes.
    """
    text = str(error_text)
    if 'RESOURCE_EXHAUSTED' in text and ('PerDay' in text or 'per day' in text.lower()):
        now = datetime.now(ZoneInfo('America/Los_Angeles'))
        reset = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        seconds, reason = int((reset - now).total_seconds()), 'quota'
    else:
        seconds, reason = 300, 'unavailable'
    cache.set(_BREAKER_KEY, reason, timeout=seconds)
    logger.warning("Gemini paused for %s s (%s): %s", seconds, reason, text[:160])
    return reason


class StaffExemptThrottle(UserRateThrottle):
    """Per-customer daily limit; staff are exempt (so demos can't get locked out)."""

    def allow_request(self, request, view):
        if request.user.is_staff or request.user.is_superuser:
            return True
        return super().allow_request(request, view)


class AILayoutThrottle(StaffExemptThrottle):
    """Daily AI (Gemini) designs per customer — settings 'ai_layouts'."""
    scope = 'ai_layouts'


class SmartLayoutThrottle(StaffExemptThrottle):
    """Generous anti-abuse limit for engine generations — settings 'smart_layouts'."""
    scope = 'smart_layouts'


class ZoneScanThrottle(StaffExemptThrottle):
    """Photo zone scans (Gemini Vision) — settings 'zone_scans'."""
    scope = 'zone_scans'


# ─────────────────────────────────────────────────────────────────────────────
# Gemini call
# ─────────────────────────────────────────────────────────────────────────────

def _system_instruction(specs, keep_clear_cells):
    instruction = (
        "You are an expert tropical landscape architect. Your primary goal is to maximise plant density "
        "within the user's budget — treat the budget as a TARGET, not a ceiling. Aim for at least 75% of it "
        "without going over.\n\n"
        "Generate ONE lush, organic garden layout: layered planting, drifts of the same plant, a tall back "
        "screen and a dense low border.\n"
        f"Use integer grid indices: gx 0..{GRID_COLS - 1} (left → right), gz 0..{GRID_ROWS - 1} where "
        "gz 0 is the BACK of the garden (furthest from the viewer) and gz 19 the FRONT. Put tall plants "
        "towards the back and low plants towards the front.\n"
        "SPACING: keep plants at least their listed spacing apart (centre to centre) and never stack two "
        "plants in one cell. STOCK: never use more of a plant than its stock.\n"
        "Rotation must be between 0 and 360 degrees.\n\n"
    )
    if keep_clear_cells:
        cells = ', '.join(f'({gx},{gz})' for gx, gz in sorted(keep_clear_cells))
        instruction += (f"KEEP-CLEAR CELLS — existing paths, pools or structures, DO NOT PLANT HERE: {cells}\n\n")
    instruction += (
        "REASONING (MANDATORY): 1-2 sentences on plant choice, layout, spacing and budget.\n"
        "COST BREAKDOWN (MANDATORY): plant_id, name, quantity, unit_price, subtotal; subtotals sum to total_cost.\n"
        "PLANT ID (MANDATORY): only exact IDs from the catalog. Invented IDs are discarded."
    )
    return instruction


def _user_prompt(specs, catalog_items, plant_budget, lot_w, lot_l, preferred_ids, site_features, installed=True):
    desc = {str(i.id): i.description for i in catalog_items}
    catalog = '\n'.join(
        f"- ID '{s.id}': {s.name} — ₱{s.price}{' installed' if installed else ''}, height {s.height:g} m ({s.layer}), "
        f"spacing {s.spacing:g} m, stock {s.stock}" + (f" ({desc[s.id]})" if desc.get(s.id) else '')
        for s in specs
    )
    floor = int(plant_budget * engine.BUDGET_FLOOR)
    cell_w, cell_l = lot_w / GRID_COLS, lot_l / GRID_ROWS
    return (
        f"Plant catalog:\n{catalog}\n\n"
        + (f"Planting budget (installed prices, i.e. plant + planting labour): " if installed else "Plant budget: ")
        + f"spend between ₱{floor} and ₱{plant_budget}.\n"
        f"Lot: {lot_w:g} m wide × {lot_l:g} m deep; one grid cell is {cell_w:.2f} m × {cell_l:.2f} m.\n"
        f"Preferred plant IDs (prioritise): {json.dumps([str(p) for p in preferred_ids])}\n"
        + (f"Site features from the customer's photo: {site_features}.\n" if site_features else '')
        + f"Generate exactly ONE '{AI_STYLE}' design. Set design_name to '{AI_STYLE}'."
    )


def call_gemini(api_key, system_instruction, prompt):
    """One Gemini design. Raises on failure (callers trip the breaker). One retry on a 503."""
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
    last = None
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash', contents=prompt,
                config={'system_instruction': system_instruction, 'response_mime_type': 'application/json',
                        'response_schema': GardenDesignSchema, 'temperature': 0.5},
            )
            if getattr(response, 'parsed', None) is not None:
                return response.parsed.model_dump()
            return json.loads(response.text)
        except Exception as e:  # noqa: BLE001 — any failure means "use the engine"
            last = e
            if attempt == 0 and ('503' in str(e) or 'UNAVAILABLE' in str(e)):
                time.sleep(1.5)
                continue
            break
    raise last


# ─────────────────────────────────────────────────────────────────────────────
# Pricing & response shape
# ─────────────────────────────────────────────────────────────────────────────

def _price(design, items_by_id, area_sqm, ps, installed=True):
    """Price a design from the rate card (the AI's own totals are never trusted)."""
    counts = {}
    for p in design['plants']:
        if not p.get('is_existing'):
            counts[p['plant_id']] = counts.get(p['plant_id'], 0) + 1
    est = (project_estimate(counts, items_by_id, area_sqm, ps) if installed
           else plants_only_estimate(counts, items_by_id, ps))
    design['plant_cost'] = est['plant_cost']
    design['total_cost'] = est['total']
    design['cost_breakdown'] = {'lines': est['lines'], 'total': est['total'], 'low': est['low'],
                                'high': est['high'], 'is_sample_rates': est['is_sample_rates']}
    design['plant_breakdown'] = est['plant_breakdown']
    design['cost_estimate'] = est  # saved with the design when booked (includes the rates used)
    return design


def _all_in(amount, ps):
    """Pre-fee, pre-VAT amount → what the customer pays."""
    total = amount * (1 + float(ps.design_fee_percent) / 100)
    return total * (1 + float(ps.vat_percent) / 100) if ps.vat_registered else total


def _existing_markers(zones, lot_w, lot_l):
    """Hidden obstacle markers for detected features (the 3D canvas keeps plants off them)."""
    markers = []
    for z in zones:
        zone = engine.ZONES.get(z.get('zone'))
        if not zone:
            continue
        gx = (zone['gx'][0] + zone['gx'][1] + 1) / 2
        gz = (zone['gz'][0] + zone['gz'][1] + 1) / 2
        markers.append({'plant_id': z.get('label') or z['zone'], 'zone': z['zone'], 'label': z.get('label', ''),
                        'x': round(gx * lot_w / GRID_COLS, 3), 'z': round(gz * lot_l / GRID_ROWS, 3),
                        'rotation': 0.0, 'is_existing': True})
    return markers


_NOTICES = {
    'quota':       "Our AI designer has reached today's limit, so all three are Smart layouts from our design engine.",
    'unavailable': "Our AI designer is temporarily unavailable, so all three are Smart layouts from our design engine.",
    'error':       "Our AI designer didn't respond in time, so all three are Smart layouts from our design engine.",
    'user_quota':  "You've used today's AI designs, so these are Smart layouts. AI designs refresh tomorrow.",
    'disabled':    None,
}


def _cache_key(*parts):
    return 'layouts:' + hashlib.md5(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _parse_list(value, name):
    if value in (None, ''):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a list or a JSON array string.')
    if not isinstance(parsed, list):
        raise ValueError(f'{name} must be a list or a JSON array string.')
    return parsed


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

class GenerateLayoutsView(APIView):
    """
    POST /api/generate-layouts/
      budget (int, required), lot_width / lot_length (m, required),
      preferred_plant_ids (list), keep_clear_zones (list of zone ids, confirmed by the customer),
      seed (int, optional — "show me other variations"), image (optional legacy: scanned for zones
      only when keep_clear_zones isn't sent).
    Always returns 3 designs unless the request is invalid or the anti-abuse limit is hit.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        started = time.monotonic()
        data = request.data

        # 1. Validate — a budget range (budget_min / budget_max); a single `budget` is still accepted
        try:
            if data.get('budget_min') is not None or data.get('budget_max') is not None:
                budget_min, budget_max = int(data.get('budget_min')), int(data.get('budget_max'))
            else:
                budget_min = budget_max = int(data.get('budget'))
            if budget_min <= 0 or budget_max < budget_min or budget_max > 50_000_000:
                raise ValueError
        except (TypeError, ValueError):
            missing = data.get('budget') is None and data.get('budget_min') is None
            msg = 'Budget is a required field.' if missing else 'Budget must be a positive range (budget_min ≤ budget_max).'
            return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)
        # "installed by our crew" (default) or "plants only — I'll plant them myself"
        installed = str(data.get('install', 'true')).strip().lower() not in ('false', '0', 'no')
        if data.get('lot_width') is None or data.get('lot_length') is None:
            return Response({'error': 'Lot width (lot_width) and lot length (lot_length) are required fields.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            lot_w, lot_l = float(data.get('lot_width')), float(data.get('lot_length'))
            if lot_w <= 0 or lot_l <= 0 or lot_w > 200 or lot_l > 200:
                raise ValueError
        except (TypeError, ValueError):
            return Response({'error': 'Lot width and lot length must be positive numbers (up to 200 m).'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            preferred = [str(p) for p in _parse_list(data.get('preferred_plant_ids'), 'preferred_plant_ids')]
            confirmed_zones = [str(z) for z in _parse_list(data.get('keep_clear_zones'), 'keep_clear_zones')]
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            seed = int(data.get('seed') or 0)
        except (TypeError, ValueError):
            seed = 0
        ps = PricingSettings.current()
        area = lot_w * lot_l
        catalog_items = list(InventoryItem.objects.filter(category='plant', is_active=True))
        items_by_id = {str(i.id): i for i in catalog_items}
        if installed:
            # All-in budget: VAT, design fee, site prep, materials and mobilisation come off first;
            # the rest pays for plants *installed* (plant + planting labour).
            specs = [replace(s, price=installed_price(ps, items_by_id[s.id]))
                     for s in engine.specs_from_inventory(catalog_items)]

            def to_plants(b):
                return plant_budget_from_all_in(b, area, ps)
        else:
            specs = engine.specs_from_inventory(catalog_items)

            def to_plants(b):
                return max(0, int(b - ps.shipping_fee))

        # Good / better / best across the range: Minimalist at the low end, Lush at the top
        targets = {'Minimalist': budget_min, 'Symmetrical': (budget_min + budget_max) // 2, AI_STYLE: budget_max}
        range_text = f"₱{budget_min:,}" if budget_min == budget_max else f"₱{budget_min:,}–₱{budget_max:,}"
        budget_warning = None
        if specs:
            cheapest = min(s.price for s in specs)
            if installed:
                basics = float(fixed_costs(ps, area))
                smallest = int(_all_in(basics + cheapest * 5, ps)) + 1
            else:
                smallest = int(cheapest * 5 + float(ps.shipping_fee))
            if to_plants(budget_max) < cheapest * 5:
                # Not enough for even a small planting: say what it takes instead of returning empty designs
                needed = int(round(smallest * 1.6, -3)) + 1000
                why = (f"site preparation, soil and crew mobilisation alone come to about ₱{int(_all_in(basics, ps)):,}"
                       if installed else f"a small planting starts at about ₱{smallest:,} including delivery")
                return Response({'error': (
                    f"A {range_text} budget doesn't cover the basics for a {lot_w:g} × {lot_l:g} m garden: {why}. "
                    f"Try a budget of at least ₱{needed:,}"
                    + (" or a smaller area." if installed else ".")), 'min_budget': needed},
                    status=status.HTTP_400_BAD_REQUEST)
            # The low end may be too small on its own: lift it to the smallest workable budget
            targets = {style: max(t, smallest) for style, t in targets.items()}
            if to_plants(budget_max) < max(5, int(area / 15)) * cheapest:
                budget_warning = ('Budget may be too tight for an area of this size. '
                                  'Consider increasing your budget or choosing low-cost flora.')
        plant_budgets = {style: to_plants(t) for style, t in targets.items()}

        # 2. Keep-clear zones: the customer's confirmed choice wins; an image is scanned only as a fallback
        image = request.FILES.get('image')
        zone_scan, detected = ('customer', [{'zone': z, 'label': z} for z in confirmed_zones if z in engine.ZONES])
        if not confirmed_zones:
            zone_scan, detected = 'none', []
            if image is not None and ai_mode() != 'engine_only' and gemini_api_key() and not breaker_open():
                found, error = scan_image_zones(image)
                if error in ('quota', 'unavailable'):
                    trip_breaker('RESOURCE_EXHAUSTED PerDay' if error == 'quota' else error)
                zone_scan, detected = ('failed' if error else 'ok'), [z for z in found if z['blocks_planting']]
            elif image is not None:
                zone_scan = 'failed'
        keep_clear = engine.zone_cells([z['zone'] for z in detected])
        site = engine.Site(lot_w, lot_l, keep_clear)

        # 3. Anti-abuse limit, then the cache (a cached answer costs nothing)
        key = _cache_key(budget_min, budget_max, installed, round(lot_w, 1), round(lot_l, 1), sorted(preferred),
                         sorted(keep_clear), seed, ps.updated_at)  # new rates → new estimates
        cached = cache.get(key)
        if cached is not None:
            self._log(request, 'cached', zone_scan, 0, started)
            return Response({**cached, 'budget_warning': budget_warning, 'zone_scan': zone_scan})
        if not SmartLayoutThrottle().allow_request(request, self):
            return Response({'error': "You've generated a lot of designs today. Please try again tomorrow, "
                                      "or keep refining your favourite in the 3D Studio."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)

        # 4. Engine designs (always) …
        designs = {}
        for style in ('Symmetrical', 'Minimalist'):
            d = engine.generate(style, specs, site, plant_budgets[style], preferred, seed)
            designs[style] = {**d, 'source': 'engine', 'adjustments': []}

        # … and the AI signature design when everything allows it
        ai_status, adjustments = self._ai_status(request), 0
        if ai_status == 'ok':
            try:
                raw = call_gemini(gemini_api_key(), _system_instruction(specs, keep_clear),
                                  _user_prompt(specs, catalog_items, plant_budgets[AI_STYLE], lot_w, lot_l, preferred,
                                               ', '.join(f"{z.get('label')} ({z['zone']})" for z in detected),
                                               installed))
                plants = expand_grid_coords(raw.get('plants') or [], lot_w, lot_l)
                fixed, notes = engine.repair(plants, specs, site, plant_budgets[AI_STYLE], preferred, seed)
                adjustments = len(notes)
                designs[AI_STYLE] = {
                    'design_name': AI_STYLE, 'reasoning': raw.get('reasoning', ''), 'plants': fixed,
                    'source': 'ai', 'adjustments': notes, 'budget_note': None,
                }
            except Exception as e:  # noqa: BLE001
                ai_status = trip_breaker(e)
                ai_status = 'quota' if ai_status == 'quota' else 'error'
        if AI_STYLE not in designs:
            d = engine.generate(AI_STYLE, specs, site, plant_budgets[AI_STYLE], preferred, seed)
            designs[AI_STYLE] = {**d, 'source': 'engine', 'adjustments': []}

        # 5. Price everything from the real price list, add the hidden obstacle markers
        markers = _existing_markers(detected, lot_w, lot_l)
        ordered = []
        for style in engine.STYLES:
            design = _price(designs[style], items_by_id, area, ps, installed)
            design['target_budget'] = targets[style]
            design['plants'] = design['plants'] + markers
            ordered.append(design)

        payload = {
            'designs': ordered,
            'ai_status': ai_status,
            'notice': _NOTICES.get(ai_status) if ai_status != 'ok' else None,
            'detected_zones': [{'zone': z['zone'], 'label': z.get('label', '')} for z in detected],
            'budget_range': {'min': budget_min, 'max': budget_max},
            'install': installed,
        }
        cache.set(key, payload, _CACHE_TTL)
        self._log(request, ai_status, zone_scan, adjustments, started)
        return Response({**payload, 'budget_warning': budget_warning, 'zone_scan': zone_scan})

    def _ai_status(self, request):
        """Whether to call Gemini for this request — checked last, so only real calls use the quota."""
        if ai_mode() == 'engine_only' or not gemini_api_key():
            return 'disabled'
        if breaker_open():
            return 'quota' if cache.get(_BREAKER_KEY) == 'quota' else 'unavailable'
        if not AILayoutThrottle().allow_request(request, self):
            return 'user_quota'
        return 'ok'

    def _log(self, request, ai_status, zone_scan, adjustments, started):
        try:
            LayoutGenerationLog.objects.create(
                user=request.user if request.user.is_authenticated else None, ai_status=ai_status,
                zone_scan=zone_scan, adjustments=adjustments, duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # monitoring must never break a design request
            logger.exception('Could not log layout generation')


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ZoneScanThrottle])
def scan_zones(request):
    """
    POST /api/scan-zones/  (multipart: image) → suggested keep-clear zones for the customer to confirm.
    Never fails the flow: if the AI can't scan, the customer marks the zones themselves.
    """
    image = request.FILES.get('image')
    if image is None:
        return Response({'error': 'An image is required (field name: image).'}, status=status.HTTP_400_BAD_REQUEST)
    if image.size > 15 * 1024 * 1024:
        return Response({'error': 'Image is larger than 15 MB.'}, status=status.HTTP_400_BAD_REQUEST)
    if ai_mode() == 'engine_only' or not gemini_api_key() or breaker_open():
        return Response({'status': 'unavailable', 'zones': []})
    found, error = scan_image_zones(image)
    if error in ('quota', 'unavailable'):
        trip_breaker('RESOURCE_EXHAUSTED PerDay' if error == 'quota' else error)
    if error:
        return Response({'status': 'failed', 'zones': []})
    return Response({'status': 'ok', 'zones': [z for z in found if z['blocks_planting']]})


@api_view(['GET'])
def ai_designer_health(request):
    """GET /api/ai-designer/health/?days=30 — office admin: AI vs smart-layout usage."""
    from .views_api import IsOfficeAdmin
    if not IsOfficeAdmin().has_permission(request, None):
        return Response({'detail': 'Office admins only.'}, status=status.HTTP_403_FORBIDDEN)
    try:
        days = max(1, min(int(request.query_params.get('days', 30)), 365))
    except ValueError:
        days = 30
    runs = LayoutGenerationLog.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
    by_status = dict(runs.values_list('ai_status').annotate(n=Count('id')))
    total = sum(by_status.values())
    labels = dict(LayoutGenerationLog.AI_STATUS_CHOICES)
    fresh = runs.exclude(ai_status='cached')
    return Response({
        'days': days,
        'total': total,
        'statuses': [{'status': k, 'label': labels.get(k, k), 'count': v} for k, v in
                     sorted(by_status.items(), key=lambda kv: -kv[1])],
        'ai_share': round(by_status.get('ok', 0) / total * 100, 1) if total else None,
        'avg_ms': int(fresh.aggregate(a=Avg('duration_ms'))['a'] or 0),
        'mode': ai_mode(),
        'paused': cache.get(_BREAKER_KEY),
    })
