"""
Crew phones' sync status: does the office see everything the crew did?

  POST /api/crew-sync/   field crew: what the phone's outbox holds (frontend src/crew/syncReport.js), sent
                         whenever the phone has signal: {waiting, failed, oldest_waiting_at}
  GET  /api/crew-sync/   office: each active crew member's last report, problems first

Why: crew work with no signal waits on the phone (offline.py). Without this the office can't tell "the
crew hasn't done it" from "the crew did it, the phone hasn't sent it yet". Only counts and times are
kept (never what the actions are), on the crew member's StaffProfile.
"""
import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import StaffProfile

MAX_COUNT = 500
QUIET_MINUTES = 60   # on a job and not heard from for this long: probably no signal


def _count(value):
    try:
        return max(0, min(MAX_COUNT, int(value)))
    except (TypeError, ValueError):
        return 0


def _is_office(request):
    from .views_api import IsOfficeAdmin
    return IsOfficeAdmin().has_permission(request, None)


def record(profile, data, now=None):
    """Store one report from the phone. A waiting time can't be in the future or before nothing waits."""
    now = now or timezone.now()
    waiting, failed = _count(data.get('waiting')), _count(data.get('failed'))
    oldest = None
    if waiting or failed:
        when = parse_datetime(str(data.get('oldest_waiting_at') or ''))
        if when is not None and not timezone.is_naive(when):
            oldest = min(when, now)   # a phone clock running fast can't put it in the future
    profile.sync_at, profile.sync_waiting, profile.sync_failed, profile.sync_oldest_at = now, waiting, failed, oldest
    profile.save(update_fields=['sync_at', 'sync_waiting', 'sync_failed', 'sync_oldest_at'])
    return profile


def state_of(profile, on_shift, now=None):
    """'refused' | 'unsent' | 'quiet' | 'ok' | 'never' (the order the office sees them in)."""
    now = now or timezone.now()
    if profile.sync_at is None:
        return 'never'
    if profile.sync_failed:
        return 'refused'
    if profile.sync_waiting:
        return 'unsent'
    if on_shift and now - profile.sync_at > datetime.timedelta(minutes=QUIET_MINUTES):
        return 'quiet'
    return 'ok'


ORDER = {'refused': 0, 'unsent': 1, 'quiet': 2, 'ok': 3, 'never': 4}


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def crew_sync(request):
    if request.method == 'POST':
        profile = getattr(request.user, 'staff_profile', None)
        if profile is None or profile.role != 'FIELD_CREW':
            return Response({'error': 'Field crew only.'}, status=status.HTTP_403_FORBIDDEN)
        record(profile, request.data)
        return Response({'ok': True})

    if not _is_office(request):
        return Response({'error': 'Office staff only.'}, status=status.HTTP_403_FORBIDDEN)
    from .field_work import current_shifts
    now = timezone.now()
    on_shift = set(current_shifts().values_list('staff_id', flat=True))
    rows = []
    for p in StaffProfile.objects.filter(role='FIELD_CREW', user__is_active=True).select_related('user'):
        u: User = p.user
        state = state_of(p, u.id in on_shift, now)
        rows.append({
            'user_id': u.id, 'name': u.get_full_name() or u.username, 'state': state, 'on_shift': u.id in on_shift,
            'last_sync_at': p.sync_at, 'minutes_ago': int((now - p.sync_at).total_seconds() // 60) if p.sync_at else None,
            'waiting': p.sync_waiting, 'failed': p.sync_failed, 'oldest_waiting_at': p.sync_oldest_at,
        })
    rows.sort(key=lambda r: (ORDER[r['state']], r['name'].lower()))
    return Response({'crew': rows, 'quiet_minutes': QUIET_MINUTES})
