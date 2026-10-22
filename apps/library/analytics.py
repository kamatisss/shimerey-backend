"""
Admin income analytics — compares the two revenue streams:

  • Landscape services  (ServiceBooking)
  • E-commerce          (Order / OrderItem)

GET /api/analytics/income/?range=30d|90d|12m|all      [superuser only]

Revenue definitions (kept in one place so every chart agrees):

  E-commerce earned    order not Cancelled AND (payment_status == 'Paid' OR status == 'Delivered')
  E-commerce pipeline  order not Cancelled and not yet earned            dated by created_at
  Landscape earned     booking status Finished / Completed (legacy value)
  Landscape pipeline   booking status Pending / Preparing / Installing / For Review (awaiting sign-off)
                       dated by scheduled_date

A booking has no price column of its own, so its value is the ProjectTracker's
total_price when staff have set one, else the linked GardenDesign's total_cost,
else 0 (reported as `unpriced` so the admin can see the data gap).
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Avg, Count
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from .models import Order, ServiceBooking, ServiceReview, ProductReview

RANGE_DAYS = {'30d': 30, '90d': 90, '12m': 365}

LANDSCAPE_DONE = ('Finished', 'Completed')
LANDSCAPE_ACTIVE = ('Pending', 'Preparing', 'Installing', 'For Review')
SERVICE_LABELS = dict(ServiceBooking.SERVICE_CHOICES)
CATEGORY_LABELS = {'plant': 'Plants', 'hardscape': 'Hardscape', 'furniture': 'Furniture'}


class IsSuperUser(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_superuser)


def _order_earned(order):
    return order.status != 'Cancelled' and (order.payment_status == 'Paid' or order.status == 'Delivered')


def _booking_value(booking):
    try:
        tracker_price = booking.tracker.total_price
    except ServiceBooking.tracker.RelatedObjectDoesNotExist:
        tracker_price = None
    if tracker_price:
        return tracker_price
    if booking.design and booking.design.total_cost:
        return booking.design.total_cost
    return Decimal('0')


def _bucket_start(d, granularity):
    if granularity == 'week':
        return d - timedelta(days=d.weekday())  # Monday
    return d.replace(day=1)


def _bucket_sequence(start, end, granularity):
    """Every bucket between start and end, so empty periods still render as zero."""
    buckets, cur = [], _bucket_start(start, granularity)
    while cur <= end:
        buckets.append(cur)
        if granularity == 'week':
            cur += timedelta(days=7)
        else:
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return buckets


def _in_window(d, start, end):
    return (start is None or d >= start) and (end is None or d <= end)


def _pct_change(current, previous):
    if not previous:
        return None
    return round(float((current - previous) / previous * 100), 1)


def _money(value):
    return float(round(value, 2))


@api_view(['GET'])
@permission_classes([IsSuperUser])
def income_analytics(request):
    range_key = request.query_params.get('range', '12m')
    if range_key not in (*RANGE_DAYS, 'all'):
        return Response({'error': "range must be one of 30d, 90d, 12m, all."}, status=400)

    today = timezone.localdate()
    orders = list(Order.objects.prefetch_related('items__item'))
    bookings = list(ServiceBooking.objects.select_related('tracker', 'design'))

    # Normalise both streams into (date, value, state) records
    order_rows = [
        (o.created_at.date(), o.total_price,
         'cancelled' if o.status == 'Cancelled' else 'earned' if _order_earned(o) else 'pipeline', o)
        for o in orders
    ]
    booking_rows = [
        (b.scheduled_date, _booking_value(b),
         'earned' if b.status in LANDSCAPE_DONE else 'pipeline' if b.status in LANDSCAPE_ACTIVE else 'cancelled', b)
        for b in bookings
    ]

    if range_key == 'all':
        all_dates = [r[0] for r in order_rows + booking_rows if r[0] <= today]
        start = min(all_dates) if all_dates else today
        prev_start = prev_end = None
    else:
        days = RANGE_DAYS[range_key]
        start = today - timedelta(days=days - 1)
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)

    granularity = 'week' if range_key in ('30d', '90d') else 'month'

    def summarize(rows, window_start, window_end):
        earned = [r for r in rows if r[2] == 'earned' and _in_window(r[0], window_start, window_end)]
        # Open work has no upper bound for the current window: a booking scheduled
        # next week is still pipeline today. The previous window stays closed.
        pipeline_end = None if window_end == today else window_end
        pipeline = [r for r in rows if r[2] == 'pipeline' and _in_window(r[0], window_start, pipeline_end)]
        cancelled = [r for r in rows if r[2] == 'cancelled' and _in_window(r[0], window_start, window_end)]
        earned_total = sum((r[1] for r in earned), Decimal('0'))
        return {
            'earned': earned_total,
            'earned_count': len(earned),
            'pipeline': sum((r[1] for r in pipeline), Decimal('0')),
            'pipeline_count': len(pipeline),
            'cancelled_count': len(cancelled),
            'avg_value': earned_total / len(earned) if earned else Decimal('0'),
        }

    def summary_block(rows):
        cur = summarize(rows, start, today)
        prev = summarize(rows, prev_start, prev_end) if prev_start else None
        total = cur['earned_count'] + cur['pipeline_count'] + cur['cancelled_count']
        return {
            'earned': _money(cur['earned']),
            'earned_count': cur['earned_count'],
            'pipeline': _money(cur['pipeline']),
            'pipeline_count': cur['pipeline_count'],
            'cancelled_count': cur['cancelled_count'],
            'completion_rate': round(cur['earned_count'] / total * 100, 1) if total else None,
            'avg_value': _money(cur['avg_value']),
            'earned_change_pct': _pct_change(cur['earned'], prev['earned']) if prev else None,
        }

    landscape = summary_block(booking_rows)
    ecommerce = summary_block(order_rows)
    landscape['unpriced_count'] = sum(
        1 for d, v, state, _ in booking_rows
        if state != 'cancelled' and not v and _in_window(d, start, None)
    )

    # ── Trend: earned income per bucket for both streams ────────────────────
    trend = {b: {'landscape': Decimal('0'), 'ecommerce': Decimal('0')}
             for b in _bucket_sequence(start, today, granularity)}
    for key, rows in (('landscape', booking_rows), ('ecommerce', order_rows)):
        for d, value, state, _ in rows:
            if state == 'earned' and _in_window(d, start, today):
                trend[_bucket_start(d, granularity)][key] += value

    # ── Breakdowns (earned income in window) ────────────────────────────────
    by_service = defaultdict(lambda: [Decimal('0'), 0])
    for d, value, state, b in booking_rows:
        if state == 'earned' and _in_window(d, start, today):
            by_service[b.service_type][0] += value
            by_service[b.service_type][1] += 1

    by_category = defaultdict(Decimal)
    by_product = defaultdict(lambda: [Decimal('0'), 0])
    by_payment = defaultdict(lambda: [Decimal('0'), 0])
    for d, value, state, o in order_rows:
        if state != 'earned' or not _in_window(d, start, today):
            continue
        by_payment[o.payment_method][0] += value
        by_payment[o.payment_method][1] += 1
        for line in o.items.all():
            line_total = line.price_at_booking * line.quantity
            name = line.item.name if line.item else 'Deleted item'
            category = line.item.category if line.item else 'other'
            by_category[category] += line_total
            by_product[name][0] += line_total
            by_product[name][1] += line.quantity

    def ranked(mapping, label_fn):
        rows = [{'label': label_fn(k), 'value': _money(v[0] if isinstance(v, list) else v),
                 **({'count': v[1]} if isinstance(v, list) else {})}
                for k, v in mapping.items()]
        return sorted(rows, key=lambda r: r['value'], reverse=True)

    # ── Customer satisfaction (all-time; review volume is too small to slice) ─
    service_reviews = ServiceReview.objects.aggregate(avg=Avg('rating'), count=Count('id'))
    product_reviews = ProductReview.objects.aggregate(avg=Avg('rating'), count=Count('id'))

    return Response({
        'range': range_key,
        'start': start.isoformat(),
        'end': today.isoformat(),
        'granularity': granularity,
        'landscape': landscape,
        'ecommerce': ecommerce,
        'trend': [
            {'period': b.isoformat(), 'landscape': _money(v['landscape']), 'ecommerce': _money(v['ecommerce'])}
            for b, v in trend.items()
        ],
        'landscape_by_service': ranked(by_service, lambda k: SERVICE_LABELS.get(k, k)),
        'ecommerce_by_category': ranked(by_category, lambda k: CATEGORY_LABELS.get(k, 'Other')),
        'top_products': ranked(by_product, lambda k: k)[:5],
        'payment_methods': ranked(by_payment, lambda k: dict(Order.PAYMENT_METHOD_CHOICES).get(k, k)),
        'satisfaction': {
            'landscape': {'avg': round(service_reviews['avg'], 2) if service_reviews['avg'] else None,
                          'count': service_reviews['count']},
            'ecommerce': {'avg': round(product_reviews['avg'], 2) if product_reviews['avg'] else None,
                          'count': product_reviews['count']},
        },
    })
