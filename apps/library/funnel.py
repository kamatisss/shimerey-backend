"""
Guest 3D Studio funnel — how many visitors try the free Studio, and how many of
them go on to create an account.

  POST /api/funnel-events/            anyone: {name, session_id} (deduped per session)
  GET  /api/funnel-events/summary/    office admin: ?days=30 → step counts + conversion
"""
import re
from datetime import timedelta

from django.db import IntegrityError
from django.db.models import Count
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .models import FunnelEvent
from .views_api import IsOfficeAdmin

EVENT_NAMES = [name for name, _ in FunnelEvent.EVENT_CHOICES]
SESSION_RE = re.compile(r'^[A-Za-z0-9-]{8,40}$')


class FunnelEventThrottle(AnonRateThrottle):
    scope = 'funnel_events'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([FunnelEventThrottle])
def record_funnel_event(request):
    name = request.data.get('name')
    session_id = str(request.data.get('session_id') or '')
    if name not in EVENT_NAMES or not SESSION_RE.match(session_id):
        return Response({'error': 'Unknown event or invalid session id.'}, status=status.HTTP_400_BAD_REQUEST)
    user = request.user if request.user.is_authenticated else None
    try:
        FunnelEvent.objects.get_or_create(session_id=session_id, name=name, defaults={'user': user})
    except IntegrityError:  # two tabs racing on the same event — already counted
        pass
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([IsOfficeAdmin])
def funnel_summary(request):
    try:
        days = max(1, min(int(request.query_params.get('days', 30)), 365))
    except ValueError:
        days = 30
    since = timezone.now() - timedelta(days=days)
    # A cohort: the visits that opened the Studio in the period, and what *those* visits did next.
    # (Counting every event in the period on its own let a sign-up from a visit opened before the
    # period count against nobody, so a step could pass 100% of "opened".)
    cohort = FunnelEvent.objects.filter(name='studio_opened', created_at__gte=since).values('session_id')
    in_cohort = FunnelEvent.objects.filter(session_id__in=cohort)
    counts = dict(in_cohort.values_list('name').annotate(n=Count('session_id', distinct=True)))
    labels = dict(FunnelEvent.EVENT_CHOICES)
    opened = counts.get('studio_opened', 0)
    steps = [
        {'name': name, 'label': labels[name], 'count': counts.get(name, 0),
         'rate': round(counts.get(name, 0) / opened * 100, 1) if opened else None}
        for name in EVENT_NAMES
    ]
    # Visits that created an account OR signed in, each once (one visit doing both isn't two)
    converted = in_cohort.filter(name__in=('account_created', 'signed_in')).values('session_id').distinct().count()
    return Response({
        'days': days,
        'steps': steps,
        'converted': converted,
        'conversion_rate': round(converted / opened * 100, 1) if opened else None,
    })
