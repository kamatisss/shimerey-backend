"""
My account → Privacy (Data Privacy Act, RA 10173): see your consent, get a copy of your data, close your account.

  GET  /api/me/privacy/          → consent record, whether the notice changed since, what blocks closing
  POST /api/me/privacy/accept/   → agree to the current notice (after it was updated)
  GET  /api/me/export/           → everything we hold about you, as a JSON file (right of access / portability)
  POST /api/me/close/            → {current_password, confirm: "CLOSE"} close and anonymise the account

Closing is not a hard delete: orders, bookings, payments and receipts are business records we must keep
(and other tables point at the account), so the account row stays but stops being *you*:
  • sign-in disabled, username → "deleted-<id>", email / names / mobile erased, password unusable;
  • address book, cart, phone alerts, notifications and unbooked designs deleted; share links stopped;
  • portfolio consent withdrawn (the project is unpublished); reviews stay, shown as "Verified customer";
  • phone numbers and map pins on finished orders and bookings erased (the receipt address text stays).
It is refused while something is still open: a booking not finished, a balance unpaid, an order not delivered.
Staff accounts are closed by the owner (User accounts), not here.
"""
import secrets

from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from . import activity
from .account import AccountSecurityThrottle, _role, me_data, profile_of
from .models import (
    DesignShare, GardenDesign, Notification, Order, PortfolioConsent, PrivacyConsent, ProductReview,
    PushSubscription, SavedAddress, SavedCart, ServiceBooking, ServiceReview,
)

OPEN_BOOKINGS = ('Pending', 'Preparing', 'Installing', 'For Review')
OPEN_ORDERS = ('Pending', 'Paid', 'Shipped', 'Out for Delivery')
CONFIRM_WORD = 'CLOSE'


class DataExportThrottle(ScopedRateThrottle):
    scope = 'data_export'


def _current_version():
    from .serializers import PRIVACY_POLICY_VERSION
    return PRIVACY_POLICY_VERSION


def close_blockers(user):
    """What has to be settled before this account can be closed (empty = it can be)."""
    if _role(user) != 'customer':
        return ['Staff accounts are closed by the owner under User accounts.']
    out = []
    for b in ServiceBooking.objects.filter(user=user, status__in=OPEN_BOOKINGS).order_by('scheduled_date'):
        out.append(f'Booking #{b.id} ({b.get_service_type_display()}, {b.scheduled_date:%b %d}) is still {b.get_status_display().lower()}. '
                   + ('Cancel it under My Bookings first.' if b.status in ('Pending', 'Preparing') else 'Wait until the work is finished.'))
    for b in ServiceBooking.objects.filter(user=user, tracker__remaining_balance__gt=0).exclude(status='Cancelled').select_related('tracker'):
        out.append(f'Booking #{b.id} has an unpaid balance of ₱{b.tracker.remaining_balance:,.2f}.')
    for o in Order.objects.filter(user=user, status__in=OPEN_ORDERS).exclude(payment_status='Expired'):
        out.append(f'Order #{o.id} is {o.status.lower()}: wait until it is delivered (or cancelled).')
    return out


def _consent(user):
    c = PrivacyConsent.objects.filter(user=user).first()
    return {'version': c.policy_version, 'accepted_at': c.accepted_at, 'source': c.source} if c else None


def privacy_summary(user):
    version = _current_version()
    consent = _consent(user)
    blockers = close_blockers(user)
    return {
        'consent': consent, 'current_version': version,
        'up_to_date': bool(consent and consent['version'] == version),
        'can_close': not blockers, 'blockers': blockers, 'confirm_word': CONFIRM_WORD,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def privacy(request):
    return Response(privacy_summary(request.user))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def accept_notice(request):
    """Agree to the notice as it is now (a new row: earlier agreements stay on record)."""
    version = _current_version()
    if not PrivacyConsent.objects.filter(user=request.user, policy_version=version).exists():
        PrivacyConsent.objects.create(user=request.user, policy_version=version, source='account')
    return Response(privacy_summary(request.user))


def _rows(qs, fields):
    return list(qs.values(*fields))   # dates and decimals: DjangoJSONEncoder in export()


def export_data(user):
    """Everything held about this person, in plain JSON-friendly values."""
    return {
        'exported_at': timezone.now(),
        'account': me_data(user),
        'privacy_consents': _rows(PrivacyConsent.objects.filter(user=user), ['policy_version', 'accepted_at', 'source']),
        'addresses': _rows(SavedAddress.objects.filter(user=user),
                           ['label', 'contact_name', 'contact_phone', 'city_code', 'barangay_code', 'street', 'landmark',
                            'lat', 'lng', 'is_default', 'created_at']),
        'bookings': _rows(ServiceBooking.objects.filter(user=user),
                          ['id', 'service_type', 'scheduled_date', 'preferred_time', 'status', 'contact_number',
                           'service_address', 'site_lat', 'site_lng', 'notes', 'design_id', 'created_at']),
        'orders': [{
            **{k: v for k, v in o.items()},
            'items': _rows(Order.objects.get(pk=o['id']).items.all(), ['item__name', 'quantity', 'price_at_booking']),
        } for o in _rows(Order.objects.filter(user=user),
                         ['id', 'customer_name', 'customer_email', 'customer_phone', 'customer_address', 'delivery_lat',
                          'delivery_lng', 'total_price', 'payment_method', 'payment_status', 'status', 'created_at'])],
        'garden_designs': _rows(GardenDesign.objects.filter(user=user), ['id', 'name', 'total_cost', 'status', 'created_at', 'updated_at']),
        'service_reviews': _rows(ServiceReview.objects.filter(user=user), ['booking_id', 'rating', 'comment', 'created_at']),
        'product_reviews': _rows(ProductReview.objects.filter(user=user), ['product__name', 'rating', 'comment', 'created_at']),
        'portfolio_consents': _rows(PortfolioConsent.objects.filter(customer=user),
                                    ['project__title', 'status', 'name_display', 'show_house', 'include_review', 'responded_at']),
        'notifications': _rows(Notification.objects.filter(user=user)[:500], ['message', 'timestamp', 'is_read']),
        'phone_alert_devices': PushSubscription.objects.filter(user=user).count(),
        'saved_cart': list(SavedCart.objects.filter(user=user).values_list('items', flat=True)[:1]),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@throttle_classes([DataExportThrottle])
def export(request):
    import json
    from django.core.serializers.json import DjangoJSONEncoder
    user = request.user
    body = json.dumps(export_data(user), cls=DjangoJSONEncoder, indent=2, ensure_ascii=False)
    response = HttpResponse(body, content_type='application/json; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="my-garden-studio-data-{timezone.localdate():%Y-%m-%d}.json"'
    response['Access-Control-Expose-Headers'] = 'Content-Disposition'
    return response


@transaction.atomic
def close_account(user):
    """Anonymise the account (see the module notes). Returns the old username for the log."""
    from .portfolio import unpublish_for_consent
    old_username = user.username
    now = timezone.now()
    for consent in PortfolioConsent.objects.filter(customer=user, status='granted').select_related('project'):
        consent.status, consent.responded_at = 'withdrawn', now
        consent.save(update_fields=['status', 'responded_at'])
        unpublish_for_consent(consent.project, 'the customer closed their account, so it was unpublished.')
    DesignShare.objects.filter(created_by=user, revoked_at__isnull=True).update(revoked_at=now)
    GardenDesign.objects.filter(user=user, bookings__isnull=True).delete()
    SavedAddress.objects.filter(user=user).delete()
    SavedCart.objects.filter(user=user).delete()
    PushSubscription.objects.filter(user=user).delete()
    Notification.objects.filter(user=user).delete()
    Order.objects.filter(user=user).update(customer_phone='', delivery_lat=None, delivery_lng=None)
    ServiceBooking.objects.filter(user=user).update(contact_number='', site_lat=None, site_lng=None)
    profile = profile_of(user)
    profile.phone = ''
    profile.closed_at = now   # the owner's User accounts → Closed
    profile.save(update_fields=['phone', 'closed_at', 'updated_at'])
    user.username = f'deleted-{user.id}-{secrets.token_hex(3)}'
    user.email = user.first_name = user.last_name = ''
    user.is_active = False
    user.set_unusable_password()
    user.save()
    from .jwt_session import end_all_sessions
    end_all_sessions(user)   # every device it was signed in on, at once
    return old_username


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AccountSecurityThrottle])
def close(request):
    user = request.user
    if not user.check_password(request.data.get('current_password') or ''):
        return Response({'current_password': 'That isn’t your current password.'}, status=status.HTTP_400_BAD_REQUEST)
    if str(request.data.get('confirm') or '').strip().upper() != CONFIRM_WORD:
        return Response({'confirm': f'Type {CONFIRM_WORD} to confirm.'}, status=status.HTTP_400_BAD_REQUEST)
    blockers = close_blockers(user)
    if blockers:
        return Response({'error': 'Your account can’t be closed yet.', 'blockers': blockers}, status=status.HTTP_409_CONFLICT)
    user_id = user.id
    old = close_account(user)
    activity.record(None, 'people', 'account_closed', f'Customer {old} closed their account (#{user_id}); personal details erased',
                    user=user_id)
    return Response({'closed': True})
