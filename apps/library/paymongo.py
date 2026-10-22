"""
PayMongo integration (QR Ph, GCash, Maya, cards, GrabPay) for shop orders and
project payment requests (downpayments etc.).

Flow: /checkout/ creates the Order -> create_checkout_session() returns PayMongo's
hosted checkout_url -> the customer pays -> PayMongo calls our webhook
(checkout_session.payment.paid) and/or the receipt page asks sync_order_payment().
Either path ends in mark_order_paid(), which is idempotent.
Project payment requests work the same way through /pay/<token>, ending in
mark_project_request_paid().

Docs: https://developers.paymongo.com/reference/checkout-session-resource
"""
import hashlib
import hmac
import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import notifications
from .models import InventoryItem, Order, ProjectHistoryLog, ProjectPayment, ProjectPaymentRequest, ProjectTracker

logger = logging.getLogger(__name__)

API_BASE = 'https://api.paymongo.com/v1'
TIMEOUT = 15  # seconds


class PayMongoError(Exception):
    pass


def _centavos(amount):
    """₱ Decimal -> integer centavos (PayMongo's amount unit)."""
    return int((Decimal(amount) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _request(method, path, payload=None):
    secret = settings.PAYMONGO_SECRET_KEY
    if not secret:
        raise PayMongoError('Online payment is not configured (PAYMONGO_SECRET_KEY is missing).')
    try:
        res = requests.request(method, f'{API_BASE}{path}', json=payload,
                               auth=(secret, ''), timeout=TIMEOUT)
    except requests.RequestException as e:
        logger.warning('PayMongo %s %s failed: %s', method, path, e)
        raise PayMongoError('Could not reach the payment provider. Please try again.') from e
    if res.status_code >= 400:
        try:
            detail = res.json()['errors'][0]['detail']
        except (ValueError, KeyError, IndexError, TypeError):
            detail = res.text[:200]
        logger.warning('PayMongo %s %s -> %s: %s', method, path, res.status_code, detail)
        raise PayMongoError(f'Payment provider error: {detail}')
    return res.json()['data']


def _create_session(*, line_items, billing, description, reference, metadata, success_url, cancel_url):
    """Create a hosted checkout session. Returns (session_id, checkout_url)."""
    attributes = {
        'line_items': line_items,
        'payment_method_types': settings.PAYMONGO_PAYMENT_METHODS,
        'success_url': success_url,
        'cancel_url': cancel_url,
        'description': description,
        'reference_number': reference,
        'send_email_receipt': True,
        'show_description': True,
        'show_line_items': True,
        'metadata': metadata,
    }
    if billing:
        attributes['billing'] = billing
    data = _request('POST', '/checkout_sessions', {'data': {'attributes': attributes}})
    return data['id'], data['attributes']['checkout_url']


def open_session_url(session_id):
    """checkout_url of a session that can still be paid, else None (so a new one is made)."""
    if not session_id:
        return None
    session = retrieve_checkout_session(session_id)
    return session['attributes']['checkout_url'] if session['attributes'].get('status') == 'active' else None


def create_checkout_session(order, success_url, cancel_url):
    """Create a hosted checkout session for the order. Returns (session_id, checkout_url)."""
    line_items = [{
        'currency': 'PHP',
        'amount': _centavos(oi.price_at_booking),
        'name': (oi.item.name if oi.item else 'Garden item')[:255],
        'quantity': oi.quantity,
    } for oi in order.items.select_related('item')]
    shipping = Decimal(order.total_price) - sum(Decimal(oi.price_at_booking) * oi.quantity
                                                for oi in order.items.all())
    if shipping > 0:
        line_items.append({'currency': 'PHP', 'amount': _centavos(shipping),
                           'name': 'Delivery fee', 'quantity': 1})

    # No phone: PayMongo disables its Pay button when a prefilled number fails its own
    # validation (e.g. a short "09856857"), and the phone is already on our order.
    return _create_session(
        line_items=line_items,
        billing={'name': order.customer_name, 'email': order.customer_email},
        description=f'{notifications.business_name()} Order #{order.id}',
        reference=str(order.id),
        metadata={'order_id': str(order.id)},
        success_url=success_url, cancel_url=cancel_url,
    )


def create_project_payment_session(req, success_url, cancel_url):
    """Checkout session for a ProjectPaymentRequest (e.g. a downpayment)."""
    booking = req.project.booking
    customer = booking.user
    service = booking.get_service_type_display()
    return _create_session(
        line_items=[{'currency': 'PHP', 'amount': _centavos(req.amount), 'quantity': 1,
                     'name': f'{req.label} - {service} (Booking #{booking.id})'[:255]}],
        billing={'name': customer.get_full_name() or customer.username, 'email': customer.email}
        if customer.email else None,
        description=f'{notifications.business_name()} project #{req.project_id}: {req.label}',
        reference=f'P{req.project_id}-R{req.id}',
        metadata={'project_payment_request_id': str(req.id)},
        success_url=success_url, cancel_url=cancel_url,
    )


def retrieve_checkout_session(session_id):
    return _request('GET', f'/checkout_sessions/{session_id}')


def expire_checkout_session(session_id):
    """Close the hosted page so an expired order can't be paid any more. Best effort."""
    try:
        _request('POST', f'/checkout_sessions/{session_id}/expire')
    except PayMongoError:
        pass


def paid_payment(session):
    """The successful payment on a checkout session resource, or None."""
    for payment in session.get('attributes', {}).get('payments') or []:
        if payment.get('attributes', {}).get('status') == 'paid':
            return payment
    return None


def mark_order_paid(order, payment):
    """Record a PayMongo payment on the order. Safe to call more than once."""
    amount = payment['attributes'].get('amount')
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.payment_status == 'Paid':
            return order
        if amount is not None and amount < _centavos(order.total_price):
            logger.error('PayMongo payment %s for order #%s is short: %s < %s centavos',
                         payment.get('id'), order.id, amount, _centavos(order.total_price))
            return order
        if order.payment_status == 'Expired':
            # Paid at the last second after we released the stock: keep the money, flag it for staff
            logger.error('Order #%s was paid after it expired; stock must be re-checked by staff.', order.id)
        order.payment_status = 'Paid'
        order.paymongo_payment_id = payment.get('id', '')
        order.paid_at = timezone.now()
        if order.status in ('Pending', 'Cancelled'):
            order.status = 'Paid'
        order.save(update_fields=['payment_status', 'paymongo_payment_id', 'paid_at', 'status'])
        notifications.order_new(order)   # online orders reach the office once they are paid
    return order


def sync_order_payment(order):
    """Ask PayMongo whether an unpaid online order has been paid (webhook fallback)."""
    if order.payment_method != 'online' or order.payment_status == 'Paid' or not order.paymongo_checkout_id:
        return order
    try:
        session = retrieve_checkout_session(order.paymongo_checkout_id)
    except PayMongoError:
        return order
    payment = paid_payment(session)
    return mark_order_paid(order, payment) if payment else order


def mark_project_request_paid(req, payment):
    """
    Record a PayMongo payment on a project payment request: lower the project balance
    and log it in the project history. Safe to call more than once.
    """
    amount = payment['attributes'].get('amount')
    with transaction.atomic():
        req = ProjectPaymentRequest.objects.select_for_update().select_related('project__booking__user').get(pk=req.pk)
        if req.status == 'PAID':
            return req
        if amount is not None and amount < _centavos(req.amount):
            logger.error('PayMongo payment %s for payment request #%s is short: %s < %s centavos',
                         payment.get('id'), req.id, amount, _centavos(req.amount))
            return req
        if req.status == 'CANCELLED':
            logger.error('Payment request #%s was paid after staff cancelled it; recording it anyway.', req.id)

        tracker = ProjectTracker.objects.select_for_update().get(pk=req.project_id)
        applied = min(req.amount, tracker.remaining_balance)
        tracker.remaining_balance -= applied
        tracker.save()

        req.status = 'PAID'
        req.paymongo_payment_id = payment.get('id', '')
        req.paid_at = timezone.now()
        req.save(update_fields=['status', 'paymongo_payment_id', 'paid_at'])
        # The full amount the customer paid (any overpayment shows in the history note below)
        ProjectPayment.objects.create(project=tracker, amount=req.amount, method='online', request=req,
                                      note=req.label, paid_at=req.paid_at)

        over = req.amount - applied
        ProjectHistoryLog.objects.create(
            project=tracker, status_reached=tracker.status, trigger_user=tracker.booking.user,
            remarks=f"Payment received online: ₱{req.amount:,.2f} — {req.label} "
                    f"(balance ₱{tracker.remaining_balance:,.2f}, PayMongo {req.paymongo_payment_id})"
                    + (f" — ₱{over:,.2f} over the balance: refund or credit it" if over > 0 else ''),
        )
        notifications.payment_received(req)
    return req


def sync_project_request_payment(req):
    """Ask PayMongo whether a payment request's checkout was paid (webhook fallback)."""
    if req.status == 'PAID' or not req.paymongo_checkout_id:
        return req
    try:
        session = retrieve_checkout_session(req.paymongo_checkout_id)
    except PayMongoError:
        return req
    payment = paid_payment(session)
    return mark_project_request_paid(req, payment) if payment else req


def verify_webhook_signature(raw_body, header):
    """
    Paymongo-Signature: t=<timestamp>,te=<test signature>,li=<live signature>
    signature = HMAC-SHA256(webhook_secret, f"{t}.{raw_body}")
    """
    secret = settings.PAYMONGO_WEBHOOK_SECRET
    if not secret or not header:
        return False
    parts = dict(p.split('=', 1) for p in header.split(',') if '=' in p)
    timestamp = parts.get('t')
    if not timestamp:
        return False
    expected = hmac.new(secret.encode(), f'{timestamp}.'.encode() + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, parts.get(k, '')) for k in ('te', 'li') if parts.get(k))


def expire_unpaid_online_orders():
    """
    Cancel online orders left unpaid past PAYMONGO_ORDER_TTL_MINUTES and return their stock.
    Runs lazily from checkout and the order list, and from `manage.py expire_unpaid_orders`.
    """
    cutoff = timezone.now() - timedelta(minutes=settings.PAYMONGO_ORDER_TTL_MINUTES)
    stale = Order.objects.filter(payment_method='online', payment_status='Pending',
                                 status='Pending', created_at__lt=cutoff)
    expired = 0
    for order in stale:
        if order.paymongo_checkout_id:
            # Paid but the webhook never arrived? Then record it instead of expiring.
            if sync_order_payment(order).payment_status == 'Paid':
                continue
            expire_checkout_session(order.paymongo_checkout_id)
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.payment_status != 'Pending' or locked.status != 'Pending':
                continue
            release_stock(locked)
            locked.status = 'Cancelled'
            locked.payment_status = 'Expired'
            locked.save(update_fields=['status', 'payment_status'])
            expired += 1
    return expired



def release_stock(order):
    """Put an order's items back on the shelf (call inside a transaction), recorded in the stock history."""
    from .inventory import record
    for oi in order.items.select_related('item'):
        if oi.item_id:
            InventoryItem.objects.filter(pk=oi.item_id).update(stock_quantity=F('stock_quantity') + oi.quantity)
            oi.item.refresh_from_db(fields=['stock_quantity'])
            record(oi.item, oi.quantity, 'released', note=f'Order #{order.id} {order.status.lower()}')
