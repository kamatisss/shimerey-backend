"""
Staff work queues for orders and bookings.

Staff process work by "what needs me now", so the lists are split into queues instead
of one ever-growing feed. Queues, search, date filters and paging run in the database
so the staff screens stay fast however many orders and bookings the business takes.

Order status rules live here too, so the API and the serializer (which tells the UI
which buttons to show) can never disagree.
"""
import csv
import datetime

from django.db import transaction
from django.db.models import Max, Q
from django.http import StreamingHttpResponse
from django.utils import timezone

from . import activity, notifications, paymongo
from .models import Order, ServiceBooking

PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
PICK_LIST_LIMIT = 200

# ── Orders ────────────────────────────────────────────────────────────────────
# Our own crew delivers, so there is no courier step. Delivered and Cancelled are final.
ORDER_TRANSITIONS = {
    'Pending':          ('Shipped', 'Cancelled'),
    'Paid':             ('Shipped', 'Cancelled'),
    'Shipped':          ('Out for Delivery', 'Delivered', 'Cancelled'),
    'Out for Delivery': ('Delivered', 'Cancelled'),
    'Delivered':        (),
    'Cancelled':        (),
}
# Field crew only handle the delivery run; packing, shipping and cancelling stay with the office
CREW_ORDER_STATUSES = ('Out for Delivery', 'Delivered')

NEEDS_REFUND = Q(status='Cancelled', payment_method='online', payment_status='Paid')

ORDER_QUEUES = {
    # Ready to pack: paid online, or COD (paid on delivery)
    'to_pack':          Q(status='Paid') | Q(status='Pending', payment_method='cod'),
    # Online checkout started but not paid yet; expires on its own, nothing to do
    'awaiting_payment': Q(status='Pending', payment_method='online'),
    'in_transit':       Q(status__in=('Shipped', 'Out for Delivery')),
    'cod_to_collect':   Q(status__in=('Shipped', 'Out for Delivery'), payment_method='cod'),
    # Paid online, then cancelled: the money must be refunded by hand in PayMongo
    'needs_refund':     NEEDS_REFUND,
    'history':          Q(status__in=('Delivered', 'Cancelled')),
}


def order_next_statuses(order):
    """Statuses staff may move this order to next."""
    allowed = ORDER_TRANSITIONS.get(order.status, ())
    if order.payment_method == 'online' and order.payment_status != 'Paid':
        # Never ship goods that haven't been paid for
        allowed = tuple(s for s in allowed if s == 'Cancelled')
    return list(allowed)


def order_needs_refund(order):
    return order.status == 'Cancelled' and order.payment_method == 'online' and order.payment_status == 'Paid'


def check_order_change(order, new_status, new_payment_status, is_office_admin):
    """Return an error message if this change isn't allowed, else None."""
    if new_status and new_status != order.status:
        if not is_office_admin and new_status not in CREW_ORDER_STATUSES:
            return 'Field crew can only mark orders Out for Delivery or Delivered.'
        if new_status not in order_next_statuses(order):
            if order.payment_method == 'online' and order.payment_status != 'Paid' and new_status != 'Cancelled':
                return f'Order #{order.id} has not been paid online yet, so it cannot be {new_status.lower()}.'
            return f'Order #{order.id} cannot go from {order.status} to {new_status}.'
    if new_payment_status and new_payment_status != order.payment_status:
        if not is_office_admin:
            return 'Only the office can change payment status.'
        # The only manual payment change: confirming a refund was sent (refunds are done in PayMongo)
        if new_payment_status != 'Refunded':
            return 'Payment status is set by PayMongo or on COD delivery, not by hand.'
        cancelled_now = new_status == 'Cancelled' or order.status == 'Cancelled'
        if not (cancelled_now and order.payment_status == 'Paid' and order.payment_method == 'online'):
            return 'Only a cancelled order that was paid online can be marked refunded.'
    return None


def change_order(pk, new_status, new_payment_status, is_office_admin, actor=None, when=None):
    """Apply a staff status change under a row lock. Returns (order, error message or None)."""
    # Locked so two staff (or staff and the PayMongo webhook) can't change it at once
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=pk).first()
        if order is None:
            return None, f'Order #{pk} was not found.'
        error = check_order_change(order, new_status, new_payment_status, is_office_admin)
        if error:
            return order, error
        old_status, old_payment = order.status, order.payment_status
        if new_status and new_status != order.status:
            if new_status == 'Cancelled':
                paymongo.release_stock(order)
            order.status = new_status
            if new_status == 'Delivered':
                order.delivered_at = when or timezone.now()   # a delivery marked offline keeps its real time
                order.delivered_by = actor  # who handed it over (and holds any COD cash)
                if order.payment_method == 'cod':
                    order.payment_status = 'Paid'  # our crew collected the cash
        if new_payment_status:
            if new_payment_status == 'Refunded' and order.payment_status != 'Refunded':
                order.refunded_at = timezone.now()
            order.payment_status = new_payment_status
        order.save()
        notifications.order_status_changed(order, old_status, order.status)
    link = notifications.order_office_path(order.id)
    if order.status == 'Cancelled' and old_status != 'Cancelled':
        activity.record(actor, 'money' if order.payment_status == 'Paid' else 'work', 'order_cancelled',
                        f'Cancelled order #{order.id} (₱{order.total_price:,.2f}, {order.customer_name})', link=link,
                        order=order.id, was=old_status)
    if order.payment_status == 'Refunded' and old_payment != 'Refunded':
        activity.record(actor, 'money', 'order_refunded',
                        f'Marked order #{order.id} refunded (₱{order.total_price:,.2f} to {order.customer_name})',
                        link=link, order=order.id)
    return order, None


def is_field_crew(user):
    return getattr(getattr(user, 'staff_profile', None), 'role', None) == 'FIELD_CREW'


def assign_delivery(order, crew):
    """Give an order to a crew member to deliver (crew=None unassigns). Returns an error or None."""
    if order.status in ('Delivered', 'Cancelled'):
        return f'Order #{order.id} is already {order.status.lower()}.'
    if crew is not None and not (crew.is_active and is_field_crew(crew)):
        return f'{crew.username} is not an active field crew member.'
    old_crew = order.delivery_crew
    if (crew.pk if crew else None) != order.delivery_crew_id:
        notifications.delivery_changed(order, crew, old_crew)
    order.delivery_crew = crew
    order.save(update_fields=['delivery_crew'])
    return None


def my_deliveries(user):
    """A crew member's delivery run: what's still to deliver, and what they handed over today."""
    today = timezone.localdate()
    active = (Order.objects.filter(delivery_crew=user, status__in=('Paid', 'Pending', 'Shipped', 'Out for Delivery'))
              .exclude(payment_method='online', payment_status__in=('Pending', 'Expired'))
              .prefetch_related('items__item').order_by('created_at'))
    done = (Order.objects.filter(delivered_by=user, delivered_at__date=today)
            .prefetch_related('items__item').order_by('-delivered_at'))
    cash = sum((o.total_price for o in done if o.payment_method == 'cod'), 0)
    return active, done, cash


def pick_list(orders):
    """What to gather from the shelves for these orders, and one packing slip per order."""
    totals, slips = {}, []
    for order in orders:
        lines = []
        for oi in order.items.all():
            name = oi.item.name if oi.item else 'Deleted item'
            row = totals.setdefault(oi.item_id or f'gone-{oi.pk}', {
                'item_id': oi.item_id, 'name': name,
                'category': oi.item.category if oi.item else '', 'quantity': 0, 'orders': []})
            row['quantity'] += oi.quantity
            row['orders'].append(order.id)
            lines.append({'name': name, 'quantity': oi.quantity})
        slips.append({
            'id': order.id, 'customer_name': order.customer_name, 'customer_phone': order.customer_phone,
            'customer_address': order.customer_address, 'payment_method': order.payment_method,
            'payment_status': order.payment_status, 'total_price': str(order.total_price),
            'created_at': order.created_at, 'items': lines,
        })
    items = sorted(totals.values(), key=lambda r: (r['category'], r['name'].lower()))
    return {'order_count': len(slips), 'items': items, 'orders': slips}


def filter_orders(qs, params):
    queue = params.get('queue')
    if queue in ORDER_QUEUES:
        qs = qs.filter(ORDER_QUEUES[queue])
    if params.get('payment') in dict(Order.PAYMENT_METHOD_CHOICES):
        qs = qs.filter(payment_method=params['payment'])
    if params.get('status') in dict(Order.STATUS_CHOICES):
        qs = qs.filter(status=params['status'])
    query = (params.get('q') or '').strip().lstrip('#')
    if query:
        if query.isdigit():  # a number means an order number or a phone number
            search = Q(pk=int(query)) | Q(customer_phone__icontains=query)
        else:
            search = (Q(customer_name__icontains=query) | Q(customer_email__icontains=query)
                      | Q(customer_address__icontains=query))
        qs = qs.filter(search)
    qs = _date_range(qs, params, 'created_at__date')
    # Work queues: oldest first, so nothing waits forever. History: newest first.
    oldest_first = queue in ORDER_QUEUES and queue != 'history'
    return qs.order_by('created_at' if oldest_first else '-created_at')


def order_counts(qs):
    return {key: qs.filter(q).count() for key, q in ORDER_QUEUES.items()}


# ── Bookings ──────────────────────────────────────────────────────────────────
_has_crew = Q(assigned_crew__isnull=False)
BOOKING_QUEUES = {
    'awaiting':  Q(status='Pending'),
    'dispatch':  Q(status__in=('Preparing', 'Installing')) & ~Q(pk__in=ServiceBooking.objects.filter(_has_crew).values('pk')),
    'active':    Q(status__in=('Preparing', 'Installing'), pk__in=ServiceBooking.objects.filter(_has_crew).values('pk')),
    'review':    Q(status='For Review'),
    'completed': Q(status__in=('Finished', 'Cancelled')),
}


def filter_bookings(qs, params):
    queue = params.get('queue')
    if queue in BOOKING_QUEUES:
        qs = qs.filter(BOOKING_QUEUES[queue])
    if params.get('status') in dict(ServiceBooking.STATUS_CHOICES):
        qs = qs.filter(status=params['status'])
    if params.get('service') in dict(ServiceBooking.SERVICE_CHOICES):
        qs = qs.filter(service_type=params['service'])
    query = (params.get('q') or '').strip().lstrip('#')
    if query:
        if query.isdigit():  # a number means a booking number or a phone number
            search = Q(pk=int(query)) | Q(contact_number__icontains=query)
        else:
            search = (Q(user__first_name__icontains=query) | Q(user__last_name__icontains=query)
                      | Q(user__username__icontains=query) | Q(user__email__icontains=query)
                      | Q(service_address__icontains=query))
        qs = qs.filter(search)
    qs = _date_range(qs, params, 'scheduled_date')
    # Open work: soonest visit first. Finished work and the full list: most recent first.
    soonest_first = queue in BOOKING_QUEUES and queue != 'completed'
    return qs.order_by(*(('scheduled_date', 'id') if soonest_first else ('-scheduled_date', '-id')))


SCHEDULE_MAX_DAYS = 42


def booking_schedule(qs, start, end, crew):
    """Light rows for the dispatch calendar: who is where on which day."""
    labels = dict(ServiceBooking.SERVICE_CHOICES)
    rows = (qs.filter(scheduled_date__range=(start, end)).exclude(status='Cancelled')
            .select_related('user').prefetch_related('assigned_crew').order_by('scheduled_date', 'id'))
    return {
        'from': start, 'to': end,
        'crew': [{'id': u.id, 'name': f'{u.first_name} {u.last_name}'.strip() or u.username} for u in crew],
        'bookings': [{
            'id': b.id, 'date': b.scheduled_date, 'time': b.preferred_time, 'status': b.status,
            'service_type': b.service_type, 'service_label': labels.get(b.service_type, b.service_type),
            'customer_name': f'{b.user.first_name} {b.user.last_name}'.strip() or b.user.username,
            'address': b.service_address, 'crew': [u.id for u in b.assigned_crew.all()],
            'contact': b.contact_number, 'notes': b.notes,  # for the printed day run sheet
        } for b in rows],
    }


def booking_counts(qs):
    today = timezone.localdate()  # business day (settings.TIME_ZONE), same as deliveries and expiry
    counts = {key: qs.filter(q).count() for key, q in BOOKING_QUEUES.items()}
    counts['today'] = qs.filter(scheduled_date=today).exclude(status__in=('Finished', 'Cancelled')).count()
    counts['all'] = qs.count()
    return counts


# ── Shared ────────────────────────────────────────────────────────────────────
def arrivals(qs, since):
    """What arrived after the newest record a screen has seen, so it can offer "3 new orders"."""
    latest = qs.aggregate(latest=Max('pk'))['latest'] or 0
    try:
        since = int(since)
    except (TypeError, ValueError):
        return {'latest_id': latest}
    return {'latest_id': latest, 'new': qs.filter(pk__gt=since).count()}


# ── CSV export ────────────────────────────────────────────────────────────────
EXPORT_LIMIT = 20000


class _Echo:
    def write(self, value):
        return value


def _cell(value):
    # A cell starting with = + - @ runs as a formula in Excel/Sheets; customer-typed text must not
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


def csv_response(header, rows, filename):
    writer = csv.writer(_Echo())

    def stream():
        yield '﻿'  # BOM so Excel reads ₱ and ñ correctly
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow([_cell(v) for v in row])

    response = StreamingHttpResponse(stream(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def order_csv(qs):
    methods = dict(Order.PAYMENT_METHOD_CHOICES)
    header = ['Order #', 'Placed', 'Customer', 'Email', 'Phone', 'Address', 'Payment method',
              'Payment status', 'Status', 'Total (PHP)', 'Items', 'Delivery crew', 'Delivered by', 'Delivered at']
    rows = ([o.id, timezone.localtime(o.created_at).strftime('%Y-%m-%d %H:%M'), o.customer_name, o.customer_email,
             o.customer_phone, o.customer_address, methods.get(o.payment_method, o.payment_method), o.payment_status,
             o.status, o.total_price,
             '; '.join(f'{oi.quantity}x {oi.item.name if oi.item else "Deleted item"}' for oi in o.items.all()),
             _name(o.delivery_crew), _name(o.delivered_by),
             timezone.localtime(o.delivered_at).strftime('%Y-%m-%d %H:%M') if o.delivered_at else '']
            for o in qs.select_related('delivery_crew', 'delivered_by').prefetch_related('items__item')
            [:EXPORT_LIMIT].iterator(chunk_size=500))
    return header, rows


def _name(user):
    return (f'{user.first_name} {user.last_name}'.strip() or user.username) if user else ''


def booking_csv(qs):
    labels = dict(ServiceBooking.SERVICE_CHOICES)

    def money(b, field):
        tracker = getattr(b, 'tracker', None)
        if tracker is None:
            return ''
        return tracker.total_price - tracker.remaining_balance if field == 'paid' else getattr(tracker, field)

    header = ['Booking #', 'Service', 'Visit date', 'Time', 'Status', 'Customer', 'Email', 'Phone', 'Address',
              'Crew', 'Project total (PHP)', 'Paid (PHP)', 'Balance (PHP)']
    rows = ([b.id, labels.get(b.service_type, b.service_type), b.scheduled_date, b.preferred_time, b.status,
             f'{b.user.first_name} {b.user.last_name}'.strip() or b.user.username, b.user.email, b.contact_number,
             b.service_address,
             ', '.join(f'{u.first_name} {u.last_name}'.strip() or u.username for u in b.assigned_crew.all()),
             money(b, 'total_price'), money(b, 'paid'), money(b, 'remaining_balance')]
            for b in qs.select_related('user', 'tracker').prefetch_related('assigned_crew')[:EXPORT_LIMIT].iterator(chunk_size=500))
    return header, rows


def _date_range(qs, params, field):
    for key, lookup in (('from', 'gte'), ('to', 'lte')):
        try:
            day = datetime.date.fromisoformat(params.get(key, ''))
        except ValueError:
            continue
        qs = qs.filter(**{f'{field}__{lookup}': day})
    return qs


def wants_page(params):
    """Paging is opt-in: customer and crew screens still get the plain list they expect."""
    return 'page' in params


def paginate(qs, params):
    """Slice a queryset into one page. Returns (rows, meta)."""
    try:
        size = min(MAX_PAGE_SIZE, max(1, int(params.get('page_size', PAGE_SIZE))))
    except ValueError:
        size = PAGE_SIZE
    count = qs.count()
    pages = max(1, -(-count // size))
    try:
        page = min(pages, max(1, int(params.get('page', 1))))
    except ValueError:
        page = 1
    rows = qs[(page - 1) * size: page * size]
    return rows, {'count': count, 'page': page, 'pages': pages, 'page_size': size}
