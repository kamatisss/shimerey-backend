"""
Projects board: every landscaping project on one screen for the office and the owner
(GET /api/projects/board/, office admins and admins only).

A project is a service booking plus its ProjectTracker. Two progress measures exist and the board
shows both, side by side, without merging them:
  • the office stage: ProjectTracker.status and its percentage, set by the office;
  • the crew stages: site preparation, installation and final inspection (ProjectMilestone at 100%),
    completed by the crew on their phones (field_work.CREW_STAGES).

Delay rules live here, in one place, so the flag, the "delayed" filter and the totals always agree:
  • overdue:  open, and its target end date (or the visit date when there is none) has passed;
  • stalled:  crew assigned and the job under way, but no crew activity (stage, clock-in or out,
              progress photo, arrival) for STALL_DAYS;
  • review:   awaiting sign-off for more than REVIEW_DAYS. The booking does not store when it was
              submitted, so the wait counts from the crew's last activity (the submission follows it),
              else from the booking's last change.
Finished and cancelled projects are never delayed.

Open projects are few (a small business has tens, not thousands), so the open list is read in full,
flagged in Python and sorted most urgent first. Finished, cancelled and "all" lists page in the database.
Either way the number of queries does not grow with the number of rows.
"""
import datetime
from decimal import Decimal

from django.db.models import Count, IntegerField, Max, OuterRef, Q, Subquery, Sum
from django.utils import timezone

from . import work_queues
from .field_work import CREW_STAGES
from .models import Attendance, ProjectMilestone, ProjectProgressMedia, ServiceBooking

STALL_DAYS = 3
REVIEW_DAYS = 2

OPEN = ('Pending', 'Preparing', 'Installing', 'For Review')
SCOPES = {
    'open':      Q(status__in=OPEN),
    'finished':  Q(status__in=ServiceBooking.DONE_STATUSES),
    'cancelled': Q(status='Cancelled'),
    'all':       Q(),
}
UNDER_WAY = ('Preparing', 'Installing')

DELAY_LABELS = {
    'overdue': 'Overdue',
    'stalled': 'No crew update',
    'review':  'Waiting for sign-off',
}


def _latest(model, booking_path, field):
    return Subquery(model.objects.filter(**{booking_path: OuterRef('pk')})
                    .order_by().values(booking_path).annotate(x=Max(field)).values('x')[:1])


def _annotated(qs):
    done = (ProjectMilestone.objects.filter(booking=OuterRef('pk'), phase__in=CREW_STAGES, completion_pct=100)
            .order_by().values('booking').annotate(n=Count('id')).values('n')[:1])
    return (qs.select_related('user', 'tracker').prefetch_related('assigned_crew')
            .annotate(crew_stages_done=Subquery(done, output_field=IntegerField()),
                      last_stage_at=_latest(ProjectMilestone, 'booking', 'updated_at'),
                      last_clock_in_at=_latest(Attendance, 'booking', 'clock_in_time'),
                      last_clock_out_at=_latest(Attendance, 'booking', 'clock_out_time'),
                      last_photo_at=_latest(ProjectProgressMedia, 'project__booking', 'uploaded_at')))


def _name(user):
    return (f'{user.first_name} {user.last_name}'.strip() or user.username) if user else ''


def _tracker(b):
    # A booking without a tracker raises RelatedObjectDoesNotExist, an AttributeError
    return getattr(b, 'tracker', None)


def _day(dt):
    return timezone.localdate(dt) if dt else None


def crew_activity(b):
    """The crew's latest sign of work on the job, or None."""
    times = [b.last_stage_at, b.last_clock_in_at, b.last_clock_out_at, b.last_photo_at, b.crew_arrived_at]
    return max((t for t in times if t), default=None)


def due_date(b):
    t = _tracker(b)
    return (t.estimated_end_date if t and t.estimated_end_date else None) or b.scheduled_date


def delays(b, today):
    """Why an open project is late, as a list of DELAY_LABELS keys (empty when on track)."""
    if b.status not in OPEN:
        return []
    out = []
    if due_date(b) < today:
        out.append('overdue')
    activity = crew_activity(b)
    crew = list(b.assigned_crew.all())
    if crew and b.status in UNDER_WAY and b.scheduled_date <= today:
        # Counted from the latest crew activity, or from the visit date when the crew never started
        since = max(_day(activity), b.scheduled_date) if activity else b.scheduled_date
        if (today - since).days >= STALL_DAYS:
            out.append('stalled')
    if b.status == 'For Review':
        since = _day(activity) or _day(b.updated_at)
        if since and (today - since).days > REVIEW_DAYS:
            out.append('review')
    return out


def _money(value):
    return float(value) if value is not None else None


def row(b, today):
    t = _tracker(b)
    activity = crew_activity(b)
    flags = delays(b, today)
    return {
        'id': b.id,
        'tracker_id': t.id if t else None,
        'customer': _name(b.user),
        'contact_number': b.contact_number,
        'service_type': b.service_type,
        'service_label': b.get_service_type_display(),
        'address': b.service_address,
        'status': b.status,
        'status_label': b.get_status_display(),
        'office_stage': t.status if t else None,
        'office_stage_label': t.get_status_display() if t else None,
        'office_pct': t.progress_percentage if t else None,
        'crew_stages_done': b.crew_stages_done or 0,
        'crew_stages_total': len(CREW_STAGES),
        'crew': [{'id': u.id, 'name': _name(u)} for u in b.assigned_crew.all()],
        'scheduled_date': b.scheduled_date,
        'target_end_date': t.estimated_end_date if t else None,
        'due_date': due_date(b),
        'days_late': max(0, (today - due_date(b)).days) if 'overdue' in flags else 0,
        'finished_at': b.finished_at,
        'total_price': _money(t.total_price) if t else None,
        'paid': _money(t.total_price - t.remaining_balance) if t else None,
        'balance': _money(t.remaining_balance) if t else None,
        'last_crew_activity': activity,
        'delays': flags,
        'delay_labels': [DELAY_LABELS[f] for f in flags],
    }


def _urgency(r):
    # Delayed first (most reasons, then most days late), then sign-offs, then soonest due
    return (-len(r['delays']), -r['days_late'], r['status'] != 'For Review', r['due_date'], r['id'])


def _filter(qs, params):
    # Search and service use the same rules as the Bookings queues
    qs = work_queues.filter_bookings(qs, {k: params.get(k) for k in ('q', 'service') if params.get(k)})
    crew = params.get('crew')
    if crew and str(crew).isdigit():
        qs = qs.filter(pk__in=ServiceBooking.objects.filter(assigned_crew=int(crew)).values('pk'))
    if params.get('owing') == '1':
        qs = qs.filter(tracker__remaining_balance__gt=0)
    return qs


def _page_params(params):
    try:
        size = min(work_queues.MAX_PAGE_SIZE, max(1, int(params.get('page_size', work_queues.PAGE_SIZE))))
    except ValueError:
        size = work_queues.PAGE_SIZE
    try:
        page = max(1, int(params.get('page', 1)))
    except ValueError:
        page = 1
    return page, size


def totals(today):
    """The tiles at the top: always the whole business, whatever the filters."""
    open_rows = [row(b, today) for b in _annotated(ServiceBooking.objects.filter(SCOPES['open']))]
    owed = (ServiceBooking.objects.exclude(status='Cancelled').filter(tracker__remaining_balance__gt=0)
            .aggregate(s=Sum('tracker__remaining_balance'))['s'] or Decimal('0'))
    return {
        'open': len(open_rows),
        'delayed': sum(1 for r in open_rows if r['delays']),
        'overdue': sum(1 for r in open_rows if 'overdue' in r['delays']),
        'stalled': sum(1 for r in open_rows if 'stalled' in r['delays']),
        'review_waiting': sum(1 for r in open_rows if 'review' in r['delays']),
        'in_field': sum(1 for r in open_rows if r['status'] in UNDER_WAY and r['crew']),
        'needs_crew': sum(1 for r in open_rows if r['status'] in UNDER_WAY and not r['crew']),
        'review': sum(1 for r in open_rows if r['status'] == 'For Review'),
        'balance_owed': float(owed),
    }


def board(params, today=None):
    today = today or timezone.localdate()
    scope = params.get('scope') if params.get('scope') in SCOPES else 'open'
    delayed_only = params.get('delayed') == '1'
    qs = _filter(ServiceBooking.objects.filter(SCOPES[scope]), params)
    page, size = _page_params(params)

    if scope == 'open' or delayed_only:
        # Small set: flag everything, then sort and page in Python
        rows = [row(b, today) for b in _annotated(qs.filter(SCOPES['open']).order_by())]
        if delayed_only:
            rows = [r for r in rows if r['delays']]
        rows.sort(key=_urgency)
        count = len(rows)
        pages = max(1, -(-count // size))
        page = min(page, pages)
        rows = rows[(page - 1) * size: page * size]
    else:
        qs = qs.order_by('-scheduled_date', '-id')
        count = qs.count()
        pages = max(1, -(-count // size))
        page = min(page, pages)
        rows = [row(b, today) for b in _annotated(qs)[(page - 1) * size: page * size]]

    return {
        'scope': scope,
        'results': rows,
        'count': count, 'page': page, 'pages': pages, 'page_size': size,
        'totals': totals(today),
        'rules': {'stall_days': STALL_DAYS, 'review_days': REVIEW_DAYS},
        'today': today,
    }


CSV_HEADER = ['Booking #', 'Customer', 'Phone', 'Service', 'Address', 'Booking status', 'Office stage', 'Office %',
              'Crew stages done', 'Crew', 'Visit date', 'Target end date', 'Days late', 'Delays',
              'Project total (PHP)', 'Paid (PHP)', 'Balance (PHP)', 'Last crew activity']


def board_csv(params, today=None):
    """Same rows and filters as the screen, without paging."""
    today = today or timezone.localdate()
    scope = params.get('scope') if params.get('scope') in SCOPES else 'open'
    qs = _filter(ServiceBooking.objects.filter(SCOPES[scope]), params)
    rows = [row(b, today) for b in _annotated(qs.order_by())[:work_queues.EXPORT_LIMIT]]
    if params.get('delayed') == '1':
        rows = [r for r in rows if r['delays']]
    rows.sort(key=_urgency if scope == 'open' else (lambda r: (r['scheduled_date'], r['id'])),
              reverse=scope != 'open')

    def fmt(dt):
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M') if isinstance(dt, datetime.datetime) else ''

    out = ([r['id'], r['customer'], r['contact_number'], r['service_label'], r['address'], r['status_label'],
            r['office_stage_label'] or '', '' if r['office_pct'] is None else r['office_pct'],
            f"{r['crew_stages_done']}/{r['crew_stages_total']}", ', '.join(c['name'] for c in r['crew']),
            r['scheduled_date'], r['target_end_date'] or '', r['days_late'] or '', '; '.join(r['delay_labels']),
            '' if r['total_price'] is None else r['total_price'], '' if r['paid'] is None else r['paid'],
            '' if r['balance'] is None else r['balance'], fmt(r['last_crew_activity'])]
           for r in rows)
    return CSV_HEADER, out
