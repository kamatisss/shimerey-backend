"""
Live crew locations and arrival — the office's dispatch map, and "we're at your gate".

  POST /api/crew-location/          field crew: the phone's position (sent every ~45 s by the crew app)
  GET  /api/crew-location/live/     office: each crew member's latest position (last 15 min) and job
  POST /api/crew-location/arrived/  field crew: "I've arrived" at a job site or delivery pin

Privacy (RA 10173, see the Privacy Notice): positions are taken only while the crew member is ON SHIFT —
clocked in, or carrying an order that is Out for Delivery — and only while the crew app is open (a web
app can't track in the background). Only office staff see them; they are deleted after 30 days.
"""
import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import notifications
from .models import CrewLocation, Order, ServiceBooking

KEEP_DAYS = 30
MIN_GAP_S = 20          # store at most one position per 20 s per crew member
LIVE_MINUTES = 15       # older than this, the crew member isn't shown as "live"
ARRIVAL_RADIUS_M = 150  # a motorcycle stops at the gate or the barangay road, not on the pin
MAX_ACCURACY_ALLOWANCE_M = 50   # GPS error we forgive on top of the radius


def _is_field_crew(user):
    return getattr(getattr(user, 'staff_profile', None), 'role', None) == 'FIELD_CREW'


def _is_office(request):
    from .views_api import IsOfficeAdmin
    return IsOfficeAdmin().has_permission(request, None)


def on_shift(user):
    """Clocked in, or out on a delivery: the only times a crew member's position is taken. A shift left
    open for days (forgot to clock out) doesn't count: field_work.current_shifts."""
    from .field_work import current_shifts
    return (current_shifts().filter(staff=user).exists()
            or Order.objects.filter(delivery_crew=user, status='Out for Delivery').exists())


def _coords(data):
    try:
        lat, lng = float(data.get('lat')), float(data.get('lng'))
    except (TypeError, ValueError):
        return None
    # The Philippines, generously: a position anywhere else is a broken reading
    if not (4.0 <= lat <= 22.0 and 116.0 <= lng <= 127.5):
        return None
    try:
        acc = float(data.get('accuracy')) if data.get('accuracy') is not None else None
    except (TypeError, ValueError):
        acc = None
    return lat, lng, (round(acc, 1) if acc is not None and acc >= 0 else None)


def _prune(user=None):
    old = CrewLocation.objects.filter(recorded_at__lt=timezone.now() - datetime.timedelta(days=KEEP_DAYS))
    (old.filter(user=user) if user else old).delete()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def report_location(request):
    if not _is_field_crew(request.user):
        return Response({'error': 'Only field crew share their location.'}, status=status.HTTP_403_FORBIDDEN)
    if not on_shift(request.user):
        # The app stops sending when it sees this (clocked out, no delivery out)
        return Response({'tracking': False, 'reason': 'Not on shift.'}, status=status.HTTP_409_CONFLICT)
    point = _coords(request.data)
    if point is None:
        return Response({'error': 'Send lat and lng from the phone.'}, status=status.HTTP_400_BAD_REQUEST)
    last = CrewLocation.objects.filter(user=request.user).first()
    if last and (timezone.now() - last.recorded_at).total_seconds() < MIN_GAP_S:
        return Response({'tracking': True, 'stored': False})
    CrewLocation.objects.create(user=request.user, lat=point[0], lng=point[1], accuracy_m=point[2])
    _prune(request.user)
    return Response({'tracking': True, 'stored': True}, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def live_locations(request):
    if not _is_office(request):
        return Response({'error': 'Office staff only.'}, status=status.HTTP_403_FORBIDDEN)
    _prune()
    since = timezone.now() - datetime.timedelta(minutes=LIVE_MINUTES)
    latest = {}
    for p in CrewLocation.objects.filter(recorded_at__gte=since).select_related('user').order_by('-recorded_at'):
        latest.setdefault(p.user_id, p)
    from .field_work import current_shifts
    shifts = {a.staff_id: a for a in current_shifts().filter(staff_id__in=latest).select_related('booking', 'booking__user')}
    out = []
    for uid, p in latest.items():
        a = shifts.get(uid)
        delivering = Order.objects.filter(delivery_crew_id=uid, status='Out for Delivery').count()
        out.append({
            'user_id': uid, 'name': p.user.get_full_name() or p.user.username,
            'lat': p.lat, 'lng': p.lng, 'accuracy_m': p.accuracy_m, 'at': p.recorded_at,
            'minutes_ago': int((timezone.now() - p.recorded_at).total_seconds() // 60),
            'job': ({'id': a.booking_id, 'customer': (a.booking.user.get_full_name() or a.booking.user.username)
                     if a and a.booking and a.booking.user else ''} if a and a.booking_id else None),
            'deliveries_out': delivering,
        })
    out.sort(key=lambda r: r['name'])
    return Response({'crew': out, 'live_minutes': LIVE_MINUTES})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def arrived(request):
    """{kind: 'job' | 'delivery', id, lat, lng, accuracy}: within ARRIVAL_RADIUS_M of the pin → the
    arrival time is kept and the customer told (once). Without a pin on file, the phone's word is taken."""
    from .views_api import _haversine
    user = request.user
    if not _is_field_crew(user):
        return Response({'error': 'Only field crew mark arrival.'}, status=status.HTTP_403_FORBIDDEN)
    kind = request.data.get('kind')
    try:
        ref = int(request.data.get('id'))
    except (TypeError, ValueError):
        return Response({'error': 'Which job or delivery?'}, status=status.HTTP_400_BAD_REQUEST)
    if kind == 'job':
        target = ServiceBooking.objects.filter(pk=ref, assigned_crew=user).first()
        pin = (target.site_lat, target.site_lng) if target else None
    elif kind == 'delivery':
        target = Order.objects.filter(pk=ref, delivery_crew=user, status__in=('Shipped', 'Out for Delivery')).first()
        pin = (target.delivery_lat, target.delivery_lng) if target else None
    else:
        return Response({'error': "kind must be 'job' or 'delivery'."}, status=status.HTTP_400_BAD_REQUEST)
    if target is None:
        return Response({'error': 'That is not one of your jobs or deliveries.'}, status=status.HTTP_404_NOT_FOUND)
    point = _coords(request.data)
    if pin[0] is not None:
        if point is None:
            return Response({'error': 'Turn on location so we can confirm you are there.'}, status=status.HTTP_400_BAD_REQUEST)
        distance = _haversine(point[0], point[1], pin[0], pin[1])
        allowance = min(point[2] or 0, MAX_ACCURACY_ALLOWANCE_M)
        if distance - allowance > ARRIVAL_RADIUS_M:
            return Response({'error': f'You are about {round(distance)} m from the pin. Get a little closer, then try again.',
                             'distance_m': round(distance)}, status=status.HTTP_400_BAD_REQUEST)
    now = timezone.now()
    first = target.crew_arrived_at is None or timezone.localtime(target.crew_arrived_at).date() != timezone.localdate()
    if first:
        target.crew_arrived_at = now
        target.save(update_fields=['crew_arrived_at'])
        if kind == 'job':
            notifications.crew_arrived_at_site(target, user)
        else:
            notifications.rider_arrived(target, user)
    return Response({'crew_arrived_at': target.crew_arrived_at, 'notified': first})
