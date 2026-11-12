"""
Share links for saved designs: the owner sends /d/<token> to family on Messenger etc. and anyone with
the link sees the garden in 3D, view-only.

  POST   /api/designs/<id>/share/     owner: make the link (or get the one already live)
  DELETE /api/designs/<id>/share/     owner: stop sharing (the link stops working)
  GET    /api/shared-designs/<token>/ anyone: the view-only copy

Only the design's owner can share it: never the office or crew, since it's the customer's garden.
The public copy leaves out the yard photo, the photo pins, the customer's notes and who made it.
Of the customer's survey of their yard it keeps only the shapes the 3D view rebuilds (house storeys, the
yard's outline, gates, paved areas by type): no names they typed, no compass direction.
"""
import secrets

from django.db.models import F
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .design_items import lot_items
from .models import DesignShare, GardenDesign

# What the 3D view needs to draw a plant; anything else in placed_items stays private
PLANT_FIELDS = ('uid', 'plant_id', 'x', 'y', 'z', 'rotation', 'is_existing', 'r', 'name', 'category')   # r: a kept tree's size
CLEAR_LABELS = ('Path', 'Driveway', 'Patio', 'Septic / tank', 'Water meter')   # data/yardSurvey.js; anything typed → 'Other'
EDGES = ('back', 'front', 'left', 'right')
BREAKDOWN_FIELDS = ('plant_id', 'name', 'category', 'quantity', 'unit_price', 'subtotal')
MAX_PLANTS = 400


class DesignShareThrottle(ScopedRateThrottle):
    scope = 'design_shares'


class SharedDesignThrottle(ScopedRateThrottle):
    scope = 'shared_design_views'


def _new_token():
    while True:
        token = secrets.token_urlsafe(9)   # 12 characters, ~72 bits: not guessable
        if not DesignShare.objects.filter(token=token).exists():
            return token


def _share_json(share):
    return {'token': share.token, 'path': f'/d/{share.token}', 'created_at': share.created_at,
            'view_count': share.view_count}


@api_view(['POST', 'DELETE'])
@permission_classes([IsAuthenticated])
@throttle_classes([DesignShareThrottle])
def design_share(request, pk):
    design = get_object_or_404(GardenDesign, pk=pk)
    if design.user_id != request.user.id:
        # Same answer as a design that doesn't exist: don't confirm other people's design ids
        return Response({'error': 'Design not found.'}, status=status.HTTP_404_NOT_FOUND)
    live = design.shares.filter(revoked_at__isnull=True).first()
    if request.method == 'DELETE':
        if live:
            design.shares.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)
    if live:
        return Response(_share_json(live))
    share = DesignShare.objects.create(design=design, created_by=request.user, token=_new_token())
    return Response(_share_json(share), status=status.HTTP_201_CREATED)


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) < 1e6 else None   # no NaN / silly values


def _public_plants(items):
    plants = []
    for item in (items if isinstance(items, list) else [])[:MAX_PLANTS]:
        if not isinstance(item, dict) or item.get('plant_id') in (None, ''):
            continue
        plant = {k: item[k] for k in PLANT_FIELDS if k in item}
        for k in ('x', 'y', 'z', 'rotation'):
            if k in plant:
                plant[k] = _num(plant[k]) or 0
        plants.append(plant)
    return plants


def _public_site(survey):
    """The yard as the 3D view rebuilds it (data/yardScene.js), from dimensions.siteSurvey; None if none."""
    if not isinstance(survey, dict):
        return None
    items = []
    for item in (survey.get('items') or [])[:40]:
        if not isinstance(item, dict):
            continue
        kind = item.get('kind')
        if kind == 'entry' and item.get('edge') in EDGES:
            items.append({'kind': 'entry', 'x': _num(item.get('x')) or 0, 'z': _num(item.get('z')) or 0, 'edge': item['edge']})
        elif kind == 'clear':
            box = {k: _num(item.get(k)) or 0 for k in ('x0', 'z0', 'x1', 'z1')}
            label = item.get('label') if item.get('label') in CLEAR_LABELS else 'Other'
            items.append({'kind': 'clear', **box, 'label': label})
    outline = [{'x': _num(p.get('x')) or 0, 'z': _num(p.get('z')) or 0, 'house': bool(p.get('house'))}
               for p in (survey.get('outline') or [])[:64] if isinstance(p, dict)]
    sun = survey.get('sun') if isinstance(survey.get('sun'), dict) else None
    house = sun.get('house') if sun and sun.get('house') in ('none', 'one', 'two') else None
    if not items and len(outline) < 3 and not house:
        return None
    return {'items': items, 'outline': outline if len(outline) >= 3 else None, 'sun': {'house': house} if house else None}


def public_design(design):
    """The view-only copy of a design for its share page."""
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    estimate = design.cost_estimate if isinstance(design.cost_estimate, dict) else {}
    lines = [{'label': str(line.get('label', ''))[:120], 'amount': _num(line.get('amount')) or 0}
             for line in (estimate.get('lines') or []) if isinstance(line, dict)]
    breakdown = [{k: row[k] for k in BREAKDOWN_FIELDS if k in row}
                 for row in (design.plant_breakdown or []) if isinstance(row, dict)]
    garden_plan = dims.get('gardenPlan')
    return {
        'name': design.name,
        'lot': {'width': _num(dims.get('width')) or 10, 'length': _num(dims.get('length')) or 15},
        'plants': _public_plants(lot_items(design)),   # Studio items too (design_items.py)
        'garden_plan': garden_plan if isinstance(garden_plan, dict) else None,
        'site': _public_site(dims.get('siteSurvey')),
        'groundcover': [{'plant_id': str(g.get('plant_id')), 'name': str(g.get('name', ''))[:100],
                         'area_sqm': _num(g.get('area_sqm')) or 0, 'sunlight': g.get('sunlight') if g.get('sunlight') in
                         ('full_sun', 'partial_shade', 'full_shade') else 'full_sun'}
                        for g in (dims.get('groundcover') or [])[:6] if isinstance(g, dict)],
        'plant_breakdown': breakdown,
        'estimate_lines': lines,
        'total_cost': design.total_cost,
        'priced_at': design.created_at,   # the prices are the ones the customer saw that day
    }


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([SharedDesignThrottle])
def shared_design(request, token):
    share = DesignShare.objects.select_related('design').filter(token=token, revoked_at__isnull=True).first()
    if not share:
        return Response({'error': 'This link was stopped or never existed.'}, status=status.HTTP_404_NOT_FOUND)
    is_owner = request.user.is_authenticated and request.user.id == share.created_by_id
    if not is_owner:
        DesignShare.objects.filter(pk=share.pk).update(view_count=F('view_count') + 1)
    data = public_design(share.design)
    data.update(shared_at=share.created_at, is_owner=is_owner)
    response = Response(data)
    response['X-Robots-Tag'] = 'noindex'   # private links: keep them out of search engines
    return response
