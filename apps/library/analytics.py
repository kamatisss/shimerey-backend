"""
Admin income analytics — compares the two revenue streams:

  • Landscape services  (ServiceBooking)
  • E-commerce          (Order / OrderItem)

GET /api/analytics/income/?range=30d|90d|12m|all      [superuser only]

Revenue definitions (kept in one place so every chart agrees):

  E-commerce earned    order not Cancelled AND (payment_status == 'Paid' OR status == 'Delivered')
  E-commerce pipeline  order not Cancelled and not yet earned            dated by created_at
  Landscape earned     booking status Finished / Completed (legacy value), dated by finished_at (the
                       sign-off); jobs finished before that was recorded: their scheduled_date
                       (counted in `finish_date_estimated_count`)
  Landscape pipeline   booking status Pending / Preparing / Installing / For Review (awaiting sign-off)
                       dated by scheduled_date

Dates are Manila calendar days (an order placed at 7 AM belongs to that day, not the UTC day before).

Pipeline is a stock, not a flow: every open job / unpaid order *right now*, whatever its date, so it
doesn't change with the range picker.

Completion rate = done / (done + cancelled + still open) over work dated in the range up to today.
Work scheduled after today isn't due yet, so it stays out (it used to be counted as "not completed",
which made the 30-day rate collapse whenever next month was well booked).

Trend buckets are whole weeks (Mon–Sun) or months; the first and last usually only partly overlap the
range (e.g. "last 12 months" starts on the 20th, and this month isn't over). Each bucket says which
days it covers and whether it is `partial`, so a short bucket isn't read as a slump.

Earned is not collected. Landscape work is earned in full on sign-off whether or not the customer has
paid, so the response also carries:
  collected   money actually received in the range, net of refunds, from reports.money_transactions
              (the Reports "Money in" rules, so the two screens agree to the peso). Projects -> landscape;
              shop online / cash on delivery / refunds -> e-commerce. Also per trend bucket.
  owed        right now: balances on finished jobs (owed today) and on open projects (not due yet),
              cancelled bookings excluded, as in the Balances report.
  demand      new booking requests and shop orders (not cancelled) by the day they were made, up to
              today, per bucket; and what became of the requests made in the range: confirmed (any
              status past Pending), still waiting (Pending) or cancelled. That is each request's
              status *now*: a request made last week may simply not be confirmed yet. No confirmation
              time is stored, so time-to-confirm can't be measured.

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


def _bucket_end(b, granularity):
    """Last day of the bucket that starts on b."""
    if granularity == 'week':
        return b + timedelta(days=6)
    return (b.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


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


CONFIRMED = ('Preparing', 'Installing', 'For Review', 'Finished', 'Completed')
LANDSCAPE_CHANNELS = ('project_online', 'project_office')


def _collected(start, end, granularity=None):
    """Money received start..end (local days), net of refunds: totals per stream and, with a
    granularity, per trend bucket. Same rows as the Reports "Money in" report."""
    from .reports import money_transactions   # reports imports this module
    totals = {'landscape': Decimal('0'), 'ecommerce': Decimal('0')}
    buckets = defaultdict(lambda: {'landscape': Decimal('0'), 'ecommerce': Decimal('0')})
    for r in money_transactions(start, end):
        stream = 'landscape' if r['channel'] in LANDSCAPE_CHANNELS else 'ecommerce'
        amount = Decimal(r['amount'])        # refunds are already negative
        totals[stream] += amount
        if granularity:
            buckets[_bucket_start(r['when'].date(), granularity)][stream] += amount
    return totals, buckets


def owed_now():
    """Project balances right now, cancelled bookings excluded (the Balances report's rule)."""
    from django.db.models import Sum
    from .models import ProjectTracker
    owing = ProjectTracker.objects.filter(remaining_balance__gt=0).exclude(booking__status='Cancelled')
    finished = owing.filter(booking__status__in=LANDSCAPE_DONE)
    open_ = owing.filter(booking__status__in=LANDSCAPE_ACTIVE)

    def total(qs):
        return _money(qs.aggregate(t=Sum('remaining_balance'))['t'] or Decimal('0'))
    return {'finished': total(finished), 'finished_count': finished.count(),
            'open': total(open_), 'open_count': open_.count()}


def _demand(bookings, orders, start, end, prev_start, prev_end, granularity):
    """New requests and orders by the day they were made, and the outcome of the range's requests."""
    requests = [(timezone.localdate(b.created_at), b) for b in bookings]
    placed = [timezone.localdate(o.created_at) for o in orders if o.status != 'Cancelled']
    cohort = [b for d, b in requests if _in_window(d, start, end)]
    confirmed = sum(1 for b in cohort if b.status in CONFIRMED)
    waiting = sum(1 for b in cohort if b.status == 'Pending')
    cancelled = sum(1 for b in cohort if b.status == 'Cancelled')
    orders_now = sum(1 for d in placed if _in_window(d, start, end))
    buckets = defaultdict(lambda: {'requests': 0, 'orders': 0})
    for d, _ in requests:
        if _in_window(d, start, end):
            buckets[_bucket_start(d, granularity)]['requests'] += 1
    for d in placed:
        if _in_window(d, start, end):
            buckets[_bucket_start(d, granularity)]['orders'] += 1

    def share(n):
        return round(n / len(cohort) * 100, 1) if cohort else None
    block = {
        'requests': len(cohort), 'orders': orders_now,
        'confirmed': confirmed, 'waiting': waiting, 'cancelled': cancelled,
        'confirmed_pct': share(confirmed), 'waiting_pct': share(waiting), 'cancelled_pct': share(cancelled),
        'requests_change_pct': None, 'orders_change_pct': None,
    }
    if prev_start:
        prev_requests = sum(1 for d, _ in requests if _in_window(d, prev_start, prev_end))
        prev_orders = sum(1 for d in placed if _in_window(d, prev_start, prev_end))
        block['requests_change_pct'] = _pct_change(Decimal(len(cohort)), Decimal(prev_requests))
        block['orders_change_pct'] = _pct_change(Decimal(orders_now), Decimal(prev_orders))
    return block, buckets


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
        (timezone.localdate(o.created_at), o.total_price,
         'cancelled' if o.status == 'Cancelled' else 'earned' if _order_earned(o) else 'pipeline', o)
        for o in orders
    ]
    # Finished work counts on the day it was signed off (finished_at); older jobs with no recorded
    # sign-off fall back to their scheduled date. Open and cancelled work stays on its scheduled date.
    booking_rows = [
        (b.finish_date() if b.status in LANDSCAPE_DONE else b.scheduled_date, _booking_value(b),
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
        cancelled = [r for r in rows if r[2] == 'cancelled' and _in_window(r[0], window_start, window_end)]
        # Open work dated in the window up to its end: due, but not done (the completion rate's 3rd part)
        open_due = [r for r in rows if r[2] == 'pipeline' and _in_window(r[0], window_start, window_end)]
        earned_total = sum((r[1] for r in earned), Decimal('0'))
        return {
            'earned': earned_total,
            'earned_count': len(earned),
            'cancelled_count': len(cancelled),
            'open_due_count': len(open_due),
            'avg_value': earned_total / len(earned) if earned else Decimal('0'),
        }

    def summary_block(rows):
        cur = summarize(rows, start, today)
        prev = summarize(rows, prev_start, prev_end) if prev_start else None
        # Pipeline: everything open right now, whatever its date (a stock; the range doesn't change it)
        pipeline = [r for r in rows if r[2] == 'pipeline']
        due = cur['earned_count'] + cur['cancelled_count'] + cur['open_due_count']
        return {
            'earned': _money(cur['earned']),
            'earned_count': cur['earned_count'],
            'pipeline': _money(sum((r[1] for r in pipeline), Decimal('0'))),
            'pipeline_count': len(pipeline),
            'open_due_count': cur['open_due_count'],          # open, dated in the range up to today
            'cancelled_count': cur['cancelled_count'],
            'completion_rate': round(cur['earned_count'] / due * 100, 1) if due else None,
            'avg_value': _money(cur['avg_value']),
            'earned_change_pct': _pct_change(cur['earned'], prev['earned']) if prev else None,
        }

    landscape = summary_block(booking_rows)
    ecommerce = summary_block(order_rows)
    # Finished jobs in the range dated by their scheduled date, because no sign-off date was recorded
    landscape['finish_date_estimated_count'] = sum(
        1 for d, _, state, b in booking_rows
        if state == 'earned' and b.finished_at is None and _in_window(d, start, today)
    )
    # Bookings counted as ₱0 in the numbers above: earned in the range, or open now (the pipeline)
    landscape['unpriced_count'] = sum(
        1 for d, v, state, _ in booking_rows
        if not v and ((state == 'earned' and _in_window(d, start, today)) or state == 'pipeline')
    )

    # ── Trend: earned income per bucket for both streams ────────────────────
    trend = {b: {'landscape': Decimal('0'), 'ecommerce': Decimal('0')}
             for b in _bucket_sequence(start, today, granularity)}
    for key, rows in (('landscape', booking_rows), ('ecommerce', order_rows)):
        for d, value, state, _ in rows:
            if state == 'earned' and _in_window(d, start, today):
                trend[_bucket_start(d, granularity)][key] += value

    # ── Collected (money received), owed now, demand ────────────────────────
    collected, collected_buckets = _collected(start, today, granularity)
    prev_collected = _collected(prev_start, prev_end)[0] if prev_start else None
    collected_block = {
        stream: {'amount': _money(collected[stream]),
                 'change_pct': _pct_change(collected[stream], prev_collected[stream]) if prev_collected else None}
        for stream in ('landscape', 'ecommerce')
    }
    total_collected = collected['landscape'] + collected['ecommerce']
    collected_block['total'] = _money(total_collected)
    collected_block['total_change_pct'] = (
        _pct_change(total_collected, prev_collected['landscape'] + prev_collected['ecommerce']) if prev_collected else None)
    demand, demand_buckets = _demand(bookings, orders, start, today, prev_start, prev_end, granularity)

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
        'collected': collected_block,
        'owed': owed_now(),
        'demand': demand,
        'trend': [
            {'period': b.isoformat(), 'landscape': _money(v['landscape']), 'ecommerce': _money(v['ecommerce']),
             'landscape_collected': _money(collected_buckets[b]['landscape']),
             'ecommerce_collected': _money(collected_buckets[b]['ecommerce']),
             'requests': demand_buckets[b]['requests'], 'orders': demand_buckets[b]['orders'],
             # the days of this bucket inside the range; partial = fewer than a whole week / month
             'from': max(b, start).isoformat(), 'to': min(_bucket_end(b, granularity), today).isoformat(),
             'partial': b < start or _bucket_end(b, granularity) > today}
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
