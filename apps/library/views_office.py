"""
Money & control endpoints for the office and the owner.

  GET  /api/cash/                 office   crew holding COD cash (with their orders) + recent handovers
  POST /api/cash/receive/         office   {crew, orders: [ids], counted, note} → record a handover
  GET  /api/business/             anyone   the public business profile (site footer, contact details)
  PATCH /api/business/            owner    edit it
  GET  /api/admin/activity/       owner    the activity log, newest first (?area=&page=)
"""
import re

from django.core.validators import URLValidator, validate_email
from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import activity, cash
from .models import ActivityLog, BusinessProfile, CashHandover
from .views_api import IsOfficeAdmin


def _name(user):
    return (user.get_full_name() or user.username) if user else ''


def _forbidden(text):
    return Response({'error': text}, status=status.HTTP_403_FORBIDDEN)


# ── Cash on delivery ─────────────────────────────────────────────────────────

def _handover_data(h):
    return {
        'id': h.id, 'crew': _name(h.crew), 'received_by': _name(h.received_by) or 'Before cash tracking started',
        'expected': str(h.expected), 'counted': str(h.counted), 'difference': str(h.difference),
        'note': h.note, 'created_at': h.created_at, 'orders': [o.id for o in h.orders.all()],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cash_overview(request):
    if not IsOfficeAdmin().has_permission(request, None):
        return _forbidden('Office admins only.')
    crews = cash.crew_totals()
    orders = cash.with_crew().order_by('delivered_at')
    by_crew = {}
    for o in orders:
        by_crew.setdefault(o.delivered_by_id, []).append({
            'id': o.id, 'customer_name': o.customer_name, 'total_price': str(o.total_price), 'delivered_at': o.delivered_at,
        })
    recent = CashHandover.objects.select_related('crew', 'received_by').prefetch_related('orders')[:20]
    return Response({
        'crew': [{**c, 'total': str(c['total']), 'orders': by_crew.get(c['crew_id'], [])} for c in crews],
        'recent': [_handover_data(h) for h in recent],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cash_receive(request):
    if not IsOfficeAdmin().has_permission(request, None):
        return _forbidden('Office admins only.')
    d = request.data
    try:
        handover = cash.receive(d.get('crew'), d.get('orders'), d.get('counted'), d.get('note'), request.user)
    except cash.CashError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_handover_data(handover), status=status.HTTP_201_CREATED)


# ── Business profile ─────────────────────────────────────────────────────────

TEXT_FIELDS = {'name': 80, 'tagline': 160, 'phone': 30, 'address': 200, 'service_area': 160}
LINK_FIELDS = {'facebook': 'facebook.com', 'instagram': 'instagram.com', 'tiktok': 'tiktok.com'}


def business_data(bp):
    return {
        'name': bp.name, 'tagline': bp.tagline, 'founded_year': bp.founded_year, 'phone': bp.phone,
        'email': bp.email, 'address': bp.address, 'service_area': bp.service_area,
        'social': {k: getattr(bp, k) for k in LINK_FIELDS},
        'updated_at': bp.updated_at, 'updated_by': _name(bp.updated_by),
    }


@api_view(['GET', 'PATCH'])
@permission_classes([AllowAny])
def business_profile(request):
    bp = BusinessProfile.current()
    if request.method == 'GET':
        data = business_data(bp)
        if not (request.user.is_authenticated and request.user.is_superuser):
            data.pop('updated_by')
        return Response(data)

    if not (request.user.is_authenticated and request.user.is_superuser):
        return _forbidden('Only the owner can change the business profile.')
    d, errors, before = request.data, {}, business_data(bp)
    for field, limit in TEXT_FIELDS.items():
        if field in d:
            value = str(d[field] or '').strip()
            if len(value) > limit:
                errors[field] = f'Keep it under {limit} characters.'
            elif field == 'name' and not value:
                errors[field] = 'The business needs a name.'
            elif field == 'phone' and value and not re.fullmatch(r'[+\d][\d\s()-]{6,28}', value):
                errors[field] = 'Use digits, spaces and + only, e.g. +63 912 345 6789.'
            else:
                setattr(bp, field, value)
    if 'email' in d:
        value = str(d['email'] or '').strip()
        try:
            if value:
                validate_email(value)
            bp.email = value
        except ValidationError:
            errors['email'] = 'Enter a valid email address.'
    if 'founded_year' in d:
        raw = str(d['founded_year'] or '').strip()
        if not raw:
            bp.founded_year = None
        elif raw.isdigit() and 1900 <= int(raw) <= timezone.localdate().year:
            bp.founded_year = int(raw)
        else:
            errors['founded_year'] = f'Enter a year from 1900 to {timezone.localdate().year}.'
    for field, host in LINK_FIELDS.items():
        if field in d:
            value = str(d[field] or '').strip()
            try:
                if value:
                    URLValidator(schemes=['https'])(value)
                    if host not in value.split('/')[2].lower():
                        raise ValidationError('wrong site')
                setattr(bp, field, value)
            except ValidationError:
                errors[field] = f'Paste the full https:// link to your {host} page.'
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    bp.updated_by = request.user
    bp.save()
    after = business_data(bp)
    changed = [k for k in after if k not in ('updated_at', 'updated_by') and after[k] != before[k]]
    if changed:
        labels = {'social': 'social links', 'founded_year': 'founded year', 'service_area': 'service area'}
        activity.record(request.user, 'settings', 'business_profile_changed',
                        'Updated the business profile: ' + ', '.join(labels.get(k, k) for k in changed),
                        link='/admin-settings', fields=changed)
    return Response(after)


# ── Activity log ─────────────────────────────────────────────────────────────

PAGE_SIZE = 30


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def activity_list(request):
    if not request.user.is_superuser:
        return _forbidden('Only the owner can see the activity log.')
    qs = ActivityLog.objects.select_related('actor')
    area = request.query_params.get('area')
    if area in dict(ActivityLog.AREAS):
        qs = qs.filter(area=area)
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    count = qs.count()
    rows = qs[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    return Response({
        'count': count, 'page': page, 'pages': max(1, -(-count // PAGE_SIZE)), 'page_size': PAGE_SIZE,
        'results': [{'id': r.id, 'area': r.area, 'action': r.action, 'summary': r.summary, 'link': r.link,
                     'actor': _name(r.actor) or 'System', 'created_at': r.created_at} for r in rows],
    })
