"""
Web Push — alerts that reach a crew member's phone even when Garden Studio isn't open.

The browser gives us a subscription (an endpoint at its push service + two keys); we sign each
message with our VAPID key pair (settings.VAPID_*) and the push service delivers it to the
service worker (frontend public/sw.js), which shows the notification. Tapping it opens `url`.

  GET  /api/push/config/       {enabled, public_key}
  POST /api/push/subscribe/    {endpoint, keys: {p256dh, auth}}   one row per device (re-subscribing moves it)
  POST /api/push/unsubscribe/  {endpoint}
  POST /api/push/test/         sends a test alert to the caller's own devices

notifications.py decides who gets an alert; this module only delivers it.
"""
import json
import logging
import threading

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import PushSubscription

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600   # a phone that is off for the night still gets yesterday evening's job


def enabled():
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY)


def _deliver(sub, payload):
    """Send one message. Returns True on success; deletes subscriptions the push service says are gone."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(
            subscription_info={'endpoint': sub.endpoint, 'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}},
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={'sub': settings.VAPID_SUBJECT},
            ttl=TTL_SECONDS,
            headers={'Urgency': 'high'},
            timeout=10,
        )
    except WebPushException as exc:
        code = getattr(exc.response, 'status_code', None)
        if code in (404, 410):   # unsubscribed, uninstalled, or site data cleared
            PushSubscription.objects.filter(pk=sub.pk).delete()
        else:
            logger.warning('Push to %s failed: %s', sub.endpoint[:60], exc)
        return False
    except Exception:   # network trouble must never break the action that triggered the alert
        logger.exception('Push to %s failed', sub.endpoint[:60])
        return False
    PushSubscription.objects.filter(pk=sub.pk).update(last_sent_at=timezone.now())
    return True


def _payload(title, body, url, tag):
    return {'title': title, 'body': body, 'url': url or '/', 'tag': tag or ''}


def send(users, title, body, url='', tag=''):
    """Queue a push to every device of these users, after the current transaction commits."""
    if not enabled():
        return
    ids = [u.pk for u in users if u is not None]
    if not ids:
        return
    payload = _payload(title, body, url, tag)

    def run():
        for sub in PushSubscription.objects.filter(user_id__in=ids):
            _deliver(sub, payload)

    if getattr(settings, 'NOTIFY_EMAIL_ASYNC', True):
        transaction.on_commit(lambda: threading.Thread(target=run, daemon=True).start())
    else:
        transaction.on_commit(run)


# ── API ─────────────────────────────────────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def push_config(request):
    return Response({'enabled': enabled(), 'public_key': settings.VAPID_PUBLIC_KEY if enabled() else ''})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def push_subscribe(request):
    if not enabled():
        return Response({'error': 'Phone alerts are not set up on this server.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    endpoint = str(request.data.get('endpoint') or '')
    keys = request.data.get('keys') or {}
    p256dh, auth = str(keys.get('p256dh') or ''), str(keys.get('auth') or '')
    if not endpoint.startswith('https://') or len(endpoint) > 1000 or not p256dh or not auth or len(p256dh) > 200 or len(auth) > 100:
        return Response({'error': 'That is not a valid push subscription.'}, status=status.HTTP_400_BAD_REQUEST)
    # One device, one owner: signing in as someone else on the same phone moves its alerts to them
    PushSubscription.objects.update_or_create(endpoint=endpoint, defaults={
        'user': request.user, 'p256dh': p256dh, 'auth': auth,
        'user_agent': str(request.META.get('HTTP_USER_AGENT', ''))[:300],
    })
    return Response({'subscribed': True, 'devices': PushSubscription.objects.filter(user=request.user).count()})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def push_unsubscribe(request):
    endpoint = str(request.data.get('endpoint') or '')
    deleted, _ = PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return Response({'unsubscribed': bool(deleted)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def push_test(request):
    """Send a test alert to the caller's own devices, right now, and say how it went."""
    if not enabled():
        return Response({'error': 'Phone alerts are not set up on this server.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    subs = list(PushSubscription.objects.filter(user=request.user))
    if not subs:
        return Response({'error': 'This account has no phone with alerts turned on yet.'}, status=status.HTTP_400_BAD_REQUEST)
    payload = _payload('Garden Studio test alert', 'Alerts are working. New jobs and deliveries will show up like this.',
                       '/staff-dashboard', 'test')
    sent = sum(_deliver(s, payload) for s in subs)
    return Response({'sent': sent, 'failed': len(subs) - sent})
