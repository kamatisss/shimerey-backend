"""
Crew actions sent from a phone that may have been offline (frontend src/crew/outbox.js).

  Idempotency-Key header   The same action sent twice (a retry after a dropped connection) is done once:
                           the second request gets the first answer back, marked "Idempotent-Replayed: true".
                           Keys belong to the signed-in user and are kept for KEEP_DAYS.
  X-Action-Time header     When the crew member actually tapped it (ISO 8601 with a time zone). Used for
                           clock-in/out and delivery times when believable: not in the future, at most
                           MAX_DELAY old. Otherwise the server's own time is used.
"""
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KEY_RE = re.compile(r'[A-Za-z0-9_-]{16,64}')
WRITE_METHODS = {'POST', 'PATCH', 'PUT', 'DELETE'}
KEEP_DAYS = 3
MAX_DELAY = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=2)
# Not worth remembering: the same request may well succeed later (signed out, rate limited)
TRANSIENT = {401, 429}


def action_time(request):
    """When the action really happened (X-Action-Time), or now if missing or not believable."""
    now = timezone.now()
    raw = request.headers.get('X-Action-Time', '')
    when = parse_datetime(raw) if raw else None
    if when is None or timezone.is_naive(when):
        return now
    if when > now + CLOCK_SKEW or now - when > MAX_DELAY:
        return now
    return min(when, now)


def delay_note(request):
    """' (happened at 10:05 AM, sent when the signal came back)' for actions sent late, else ''."""
    when = action_time(request)
    if timezone.now() - when < timedelta(minutes=2):
        return ''
    return f' (happened at {timezone.localtime(when):%I:%M %p}, sent when the signal came back)'.replace(' 0', ' ', 1)


def _user_id(request):
    """The JWT user, resolved here because this middleware runs before DRF authentication."""
    try:
        from .jwt_session import SessionJWTAuthentication as JWTAuthentication   # signed-out sessions count as no user
        result = JWTAuthentication().authenticate(request)
    except Exception:  # noqa: BLE001 — bad or expired token: the view answers 401 itself
        return None
    return result[0].pk if result else None


class IdempotencyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = request.headers.get('Idempotency-Key', '').strip()
        if not key or request.method not in WRITE_METHODS or not request.path.startswith('/api/'):
            return self.get_response(request)
        if not KEY_RE.fullmatch(key):
            return JsonResponse({'error': 'Invalid Idempotency-Key.'}, status=400)
        user_id = _user_id(request)
        if user_id is None:
            return self.get_response(request)

        from .models import IdempotentRequest
        if not IdempotentRequest.objects.filter(created_at__gte=timezone.now() - timedelta(minutes=10)).exists():
            # Cheap housekeeping, at most every few minutes of activity
            IdempotentRequest.objects.filter(created_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()
        try:
            with transaction.atomic():
                record = IdempotentRequest.objects.create(key=key, user_id=user_id, method=request.method,
                                                          path=request.path, status_code=0)
        except IntegrityError:
            record = IdempotentRequest.objects.filter(key=key).first()
            if record is None or record.user_id != user_id or record.method != request.method or record.path != request.path:
                return JsonResponse({'error': 'This Idempotency-Key was already used for a different request.'}, status=422)
            if record.status_code == 0:
                response = JsonResponse({'error': 'Still being processed. Try again in a moment.'}, status=409)
                response['Retry-After'] = '5'
                return response
            response = HttpResponse(record.body, status=record.status_code, content_type=record.content_type)
            response['Idempotent-Replayed'] = 'true'
            return response

        response = self.get_response(request)
        if response.status_code >= 500 or response.status_code in TRANSIENT or getattr(response, 'streaming', False):
            record.delete()   # let a retry run the action for real
        else:
            record.status_code = response.status_code
            record.content_type = response.get('Content-Type', 'application/json')
            record.body = response.content.decode('utf-8', 'replace')[:200_000]
            record.save(update_fields=['status_code', 'content_type', 'body'])
        return response
