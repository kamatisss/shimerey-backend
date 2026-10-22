"""
Owner reports — the numbers a business is run (and taxed) on. Owner only; every report also
downloads as CSV (?format=csv) for the accountant.

  GET /api/reports/money/?from=&to=      Money in: cash actually received, by channel and by day,
                                          minus refunds. (Income analytics shows work *earned*;
                                          this is money *collected*. They differ: a finished job
                                          can still be unpaid.)
  GET /api/reports/balances/             Who owes us: project balances, aged; cash still with crew.
  GET /api/reports/crew/?from=&to=       Each field crew member: jobs, deliveries, cash, hours.

Money-in definitions (one place, so the overview, reports and CSV agree):
  Shop, online    online order paid (Paid, or later Refunded)   dated paid_at
  Shop, cash      COD order Delivered                           dated delivered_at
  Projects        ProjectPayment rows (online or recorded)      dated paid_at
  Refunds         online order marked Refunded                  dated refunded_at
Periods are local calendar days, inclusive; at most 366 days.
"""
import csv
import datetime
from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import cash
from .analytics import _booking_value
from .models import Attendance, CashHandover, Order, ProjectPayment, ProjectTracker, ServiceBooking

MAX_DAYS = 366
CHANNELS = [('shop_online', 'Shop · paid online'), ('shop_cash', 'Shop · cash on delivery'),
            ('project_online', 'Projects · paid online'), ('project_office', 'Projects · recorded by the office')]
METHOD_LABELS = dict(ProjectPayment.METHODS)


class PeriodError(ValueError):
    pass


def parse_period(params):
    """?from=YYYY-MM-DD&to=YYYY-MM-DD → (start, end) dates. Default: this month so far."""
    today = timezone.localdate()
    try:
        start = datetime.date.fromisoformat(params['from']) if params.get('from') else today.replace(day=1)
        end = datetime.date.fromisoformat(params['to']) if params.get('to') else today
    except ValueError:
        raise PeriodError('Dates must look like 2026-10-31.')
    if start > end:
        raise PeriodError('The start date is after the end date.')
    if (end - start).days >= MAX_DAYS:
        raise PeriodError(f'Pick a period of at most {MAX_DAYS} days.')
    return start, end


def _bounds(start, end):
    """Aware datetimes covering whole local days start..end."""
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.datetime.combine(start, datetime.time.min), tz)
    hi = timezone.make_aware(datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time.min), tz)
    return lo, hi


def _name(user):
    return (user.get_full_name() or user.username) if user else ''


def _local(dt):
    return timezone.localtime(dt) if dt else None


def _m(value):
    return float(round(Decimal(value or 0), 2))


# ── Money in ─────────────────────────────────────────────────────────────────

def money_transactions(start, end):
    """Every payment received (and every refund) in the period, oldest first."""
    lo, hi = _bounds(start, end)
    rows = []
    for o in Order.objects.filter(payment_method='online', payment_status__in=('Paid', 'Refunded'), paid_at__gte=lo, paid_at__lt=hi):
        rows.append({'when': _local(o.paid_at), 'channel': 'shop_online', 'reference': f'Order #{o.id}',
                     'customer': o.customer_name, 'method': 'Online (PayMongo)', 'amount': o.total_price, 'by': ''})
    for o in Order.objects.filter(payment_method='cod', status='Delivered', delivered_at__gte=lo, delivered_at__lt=hi).select_related('delivered_by'):
        rows.append({'when': _local(o.delivered_at), 'channel': 'shop_cash', 'reference': f'Order #{o.id}',
                     'customer': o.customer_name, 'method': 'Cash on delivery', 'amount': o.total_price, 'by': _name(o.delivered_by)})
    for p in ProjectPayment.objects.filter(paid_at__gte=lo, paid_at__lt=hi).select_related('project__booking__user', 'received_by'):
        b = p.project.booking
        rows.append({'when': _local(p.paid_at), 'channel': 'project_online' if p.method == 'online' else 'project_office',
                     'reference': f'Booking #{b.id}', 'customer': _name(b.user), 'method': METHOD_LABELS.get(p.method, p.method),
                     'amount': p.amount, 'by': _name(p.received_by)})
    for o in Order.objects.filter(refunded_at__gte=lo, refunded_at__lt=hi):
        rows.append({'when': _local(o.refunded_at), 'channel': 'refund', 'reference': f'Order #{o.id}',
                     'customer': o.customer_name, 'method': 'Refund (PayMongo)', 'amount': -o.total_price, 'by': ''})
    rows.sort(key=lambda r: r['when'])
    return rows


def _money_totals(rows):
    totals = {k: [Decimal('0'), 0] for k, _ in CHANNELS}
    refunds = [Decimal('0'), 0]
    for r in rows:
        bucket = refunds if r['channel'] == 'refund' else totals[r['channel']]
        bucket[0] += abs(r['amount'])
        bucket[1] += 1
    received = sum((v[0] for v in totals.values()), Decimal('0'))
    return totals, refunds, received


def money_report(start, end):
    rows = money_transactions(start, end)
    totals, refunds, received = _money_totals(rows)

    # Same-length period just before, for "vs previous"
    span = end - start
    prev_end = start - datetime.timedelta(days=1)
    _, prev_refunds, prev_received = _money_totals(money_transactions(prev_end - span, prev_end))
    prev_net = prev_received - prev_refunds[0]
    net = received - refunds[0]

    days = {start + datetime.timedelta(days=i): defaultdict(Decimal) for i in range(span.days + 1)}
    by_method = defaultdict(lambda: [Decimal('0'), 0])
    for r in rows:
        if r['channel'] != 'refund':
            days[r['when'].date()][r['channel']] += r['amount']
        if r['channel'] == 'project_office':
            by_method[r['method']][0] += r['amount']
            by_method[r['method']][1] += 1

    lo, hi = _bounds(start, end)
    handovers = CashHandover.objects.filter(created_at__gte=lo, created_at__lt=hi)
    short = sum((h.difference for h in handovers if h.difference < 0), Decimal('0'))
    over = sum((h.difference for h in handovers if h.difference > 0), Decimal('0'))
    return {
        'from': start, 'to': end,
        'received': _m(received), 'refunds': {'amount': _m(refunds[0]), 'count': refunds[1]}, 'net': _m(net),
        'previous_net': _m(prev_net),
        'change_pct': round(float((net - prev_net) / prev_net * 100), 1) if prev_net else None,
        'channels': [{'key': k, 'label': label, 'amount': _m(totals[k][0]), 'count': totals[k][1]} for k, label in CHANNELS],
        'office_methods': sorted(({'label': k, 'amount': _m(v[0]), 'count': v[1]} for k, v in by_method.items()),
                                 key=lambda r: -r['amount']),
        'days': [{'date': d, **{k: _m(v.get(k)) for k, _ in CHANNELS}, 'total': _m(sum(v.values(), Decimal('0')))}
                 for d, v in days.items()],
        'cash_handovers': {'count': handovers.count(), 'short': _m(-short), 'over': _m(over)},
    }


# ── Balances ─────────────────────────────────────────────────────────────────

AGE_BUCKETS = [(30, '0–30 days'), (60, '31–60 days'), (90, '61–90 days'), (None, 'Over 90 days')]


def balance_rows():
    """Every project with money still owed (cancelled bookings excluded), most owed first."""
    today = timezone.localdate()
    trackers = (ProjectTracker.objects.filter(remaining_balance__gt=0).exclude(booking__status='Cancelled')
                .select_related('booking__user').prefetch_related('payments', 'payment_requests'))
    rows = []
    for t in trackers:
        b = t.booking
        finished = b.status in ('Finished', 'Completed')
        last = max((p.paid_at for p in t.payments.all()), default=None)
        rows.append({
            'booking_id': b.id, 'customer': _name(b.user),
            'service': b.get_service_type_display(), 'status': b.status, 'finished': finished,
            'job_date': b.scheduled_date, 'total': _m(t.total_price), 'paid': _m(t.total_price - t.remaining_balance),
            'balance': _m(t.remaining_balance),
            'requested': _m(sum((r.amount for r in t.payment_requests.all() if r.status == 'PENDING'), Decimal('0'))),
            'last_payment': _local(last),
            'days_since_job': (today - b.scheduled_date).days if finished else None,
        })
    rows.sort(key=lambda r: (not r['finished'], -r['balance']))
    return rows


def balances_report(limit=100):
    rows = balance_rows()
    owed = [r for r in rows if r['finished']]
    open_jobs = [r for r in rows if not r['finished']]
    buckets = []
    lower = -1
    for upper, label in AGE_BUCKETS:
        hit = [r for r in owed if r['days_since_job'] > lower and (upper is None or r['days_since_job'] <= upper)]
        buckets.append({'label': label, 'amount': _m(sum(r['balance'] for r in hit)), 'count': len(hit)})
        lower = upper if upper is not None else lower
    held = cash.with_crew()
    return {
        'as_of': timezone.localdate(),
        'owed_total': _m(sum(r['balance'] for r in owed)), 'owed_count': len(owed),
        'open_total': _m(sum(r['balance'] for r in open_jobs)), 'open_count': len(open_jobs),
        'aging': buckets,
        'cash_with_crew': {'amount': _m(sum((o.total_price for o in held), Decimal('0'))), 'count': held.count()},
        'owed': owed[:limit], 'open': open_jobs[:limit],
    }


# ── Crew output ──────────────────────────────────────────────────────────────

def crew_report(start, end):
    lo, hi = _bounds(start, end)
    crew = list(User.objects.filter(staff_profile__role='FIELD_CREW').order_by('first_name', 'username'))
    ids = [u.id for u in crew]
    stats = {u.id: {'crew_id': u.id, 'name': _name(u), 'active': u.is_active, 'jobs_finished': 0, 'jobs_value': Decimal('0'),
                    'jobs_open': 0, 'deliveries': 0, 'cash_collected': Decimal('0'), 'cash_short': Decimal('0'),
                    'hours': 0.0, 'days_worked': set()} for u in crew}

    jobs = (ServiceBooking.objects.filter(assigned_crew__in=ids, scheduled_date__gte=start, scheduled_date__lte=end)
            .exclude(status='Cancelled').select_related('tracker', 'design').prefetch_related('assigned_crew').distinct())
    for b in jobs:
        done = b.status in ('Finished', 'Completed')
        value = _booking_value(b) if done else 0
        for u in b.assigned_crew.all():
            if u.id in stats:
                s = stats[u.id]
                if done:
                    s['jobs_finished'] += 1
                    s['jobs_value'] += value   # the whole job's value: crews share jobs
                else:
                    s['jobs_open'] += 1
    for o in Order.objects.filter(delivered_by__in=ids, delivered_at__gte=lo, delivered_at__lt=hi):
        s = stats[o.delivered_by_id]
        s['deliveries'] += 1
        if o.payment_method == 'cod':
            s['cash_collected'] += o.total_price
    for h in CashHandover.objects.filter(crew__in=ids, created_at__gte=lo, created_at__lt=hi):
        if h.difference < 0:
            stats[h.crew_id]['cash_short'] += -h.difference
    for a in Attendance.objects.filter(staff__in=ids, clock_in_time__gte=lo, clock_in_time__lt=hi):
        s = stats[a.staff_id]
        s['hours'] += float(a.total_hours or 0)
        s['days_worked'].add(_local(a.clock_in_time).date())

    out = []
    for s in stats.values():
        if not s['active'] and not (s['jobs_finished'] or s['jobs_open'] or s['deliveries'] or s['hours']):
            continue  # former crew with nothing in this period
        out.append({**s, 'jobs_value': _m(s['jobs_value']), 'cash_collected': _m(s['cash_collected']),
                    'cash_short': _m(s['cash_short']), 'hours': round(s['hours'], 1), 'days_worked': len(s['days_worked'])})
    out.sort(key=lambda r: (-r['jobs_finished'], -r['deliveries'], r['name']))
    return {'from': start, 'to': end, 'crew': out}


# ── Endpoints ────────────────────────────────────────────────────────────────

def _csv(filename, header, rows):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('﻿')  # so Excel opens ₱ and ñ correctly
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


def _owner_only(view):
    def wrapped(request):
        if not request.user.is_superuser:
            return Response({'error': 'Only the owner can see reports.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            return view(request)
        except PeriodError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def _wants_csv(request):
    return request.query_params.get('export') == 'csv'


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@_owner_only
def money_in(request):
    start, end = parse_period(request.query_params)
    if _wants_csv(request):
        labels = {**dict(CHANNELS), 'refund': 'Refund'}
        return _csv(f'money-in_{start}_to_{end}.csv', ['Date', 'Time', 'Channel', 'Reference', 'Customer', 'Method', 'Amount (PHP)', 'Received by'],
                    [[r['when'].date(), r['when'].strftime('%H:%M'), labels[r['channel']], r['reference'], r['customer'],
                      r['method'], f"{r['amount']:.2f}", r['by']] for r in money_transactions(start, end)])
    return Response(money_report(start, end))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@_owner_only
def balances(request):
    if _wants_csv(request):
        return _csv(f'balances_{timezone.localdate()}.csv',
                    ['Booking', 'Customer', 'Service', 'Status', 'Job date', 'Days since job', 'Contract price', 'Paid',
                     'Balance', 'Payment requested', 'Last payment'],
                    [[f"#{r['booking_id']}", r['customer'], r['service'], r['status'], r['job_date'],
                      '' if r['days_since_job'] is None else r['days_since_job'], f"{r['total']:.2f}", f"{r['paid']:.2f}",
                      f"{r['balance']:.2f}", f"{r['requested']:.2f}", r['last_payment'].date() if r['last_payment'] else '']
                     for r in balance_rows()])
    return Response(balances_report())


@api_view(['GET'])
@permission_classes([IsAuthenticated])
@_owner_only
def crew_output(request):
    start, end = parse_period(request.query_params)
    data = crew_report(start, end)
    if _wants_csv(request):
        return _csv(f'crew-output_{start}_to_{end}.csv',
                    ['Crew member', 'Jobs finished', 'Value of finished jobs', 'Jobs still open', 'Deliveries',
                     'COD cash collected', 'Cash short', 'Hours on shift', 'Days worked'],
                    [[r['name'], r['jobs_finished'], f"{r['jobs_value']:.2f}", r['jobs_open'], r['deliveries'],
                      f"{r['cash_collected']:.2f}", f"{r['cash_short']:.2f}", r['hours'], r['days_worked']] for r in data['crew']])
    return Response(data)
