"""
Notifications — one place that decides who hears about what.

A notification means "something that concerns you, or needs you, just happened". The staff
work queues stay the to-do list; this is the tap on the shoulder.

  Office (office admins + superusers)
    order_new          COD order placed, or online order paid           (grouped: "3 new orders")
    booking_new        customer booked a service                        (grouped)
    booking_cancelled  customer cancelled a booking
    payment_received   customer paid a project payment request online
    job_review         crew submitted a job for sign-off
    job_problem        crew reported a problem on a job (customer not home, rain, missing materials…)
    delivery_problem   crew reported a delivery problem
  Field crew (in-app, by email and as a phone push, since they are out in the field)
    job_assigned, job_unassigned, job_rescheduled, job_sent_back, job_cancelled
    delivery_assigned, delivery_unassigned, cash_received (in-app only: the office counted their COD cash)
  Customer
    order_on_the_way, order_delivered, order_cancelled, crew_on_the_way (crew tapped "On my way")
    booking_confirmed, crew_assigned, booking_rescheduled, booking_cancelled, booking_finished
    (+ payment_request, portfolio_consent, review_reply from their own modules)

Every notification carries a `link`: the in-app page the bell opens.
"""
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import push
from .models import BusinessProfile, Notification, ServiceBooking

logger = logging.getLogger(__name__)

GROUP_WINDOW = timedelta(hours=3)   # unread events of the same kind within this window share one line


# ── Recipients and links ─────────────────────────────────────────────────────

def office_users():
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(staff_profile__role='OFFICE_ADMIN')).distinct()


def _office_link(user, path):
    # Office admins and the owner (superuser) work the same queues, so every link opens the item itself
    return path


def order_office_path(order_id=None):
    base = '/staff-dashboard?tab=orders&queue=all'
    return f'{base}&q={order_id}&open={order_id}' if order_id else base


def booking_office_path(booking_id=None):
    base = '/staff-dashboard?tab=bookings&queue=all'
    return f'{base}&open={booking_id}' if booking_id else base


CREW_JOBS = '/staff-dashboard?tab=projects'


def crew_job_link(booking):
    """The job's own page in the crew app (stages, photos, sign-off)."""
    return f'/staff-dashboard?tab=projects&job={booking.id}'
CREW_DELIVERIES = '/staff-dashboard?tab=deliveries'
MY_ORDERS = '/my-orders'
MY_BOOKINGS = '/my-bookings'


def _service(booking):
    return dict(ServiceBooking.SERVICE_CHOICES).get(booking.service_type, booking.service_type)


def _fmt_date(d):
    try:
        return f'{d:%a %b} {d.day}, {d.year}'
    except (TypeError, ValueError):
        return str(d)


def _name(user):
    return user.get_full_name() or user.username


# ── Delivery ────────────────────────────────────────────────────────────────

def business_name():
    """The name the owner set in the Business profile (falls back to the app's name)."""
    try:
        return BusinessProfile.current().name or 'Garden Studio'
    except Exception:  # noqa: BLE001 — never block a notification over this
        return 'Garden Studio'


def _email(users, subject, message, link):
    """Email after the database commit, off the request thread (SMTP can take seconds)."""
    recipients = [u.email for u in users if u.email]
    if not recipients:
        return
    brand = business_name()
    body = f'{message}\n\nOpen it in {brand}: {settings.FRONTEND_URL}{link}\n' if link else f'{message}\n'

    def send():
        for to in recipients:
            try:
                send_mail(f'{brand}: {subject}', body, settings.DEFAULT_FROM_EMAIL, [to])
            except Exception:   # a mail problem must never break the action that triggered it
                logger.exception('Notification email to %s failed', to)

    if getattr(settings, 'NOTIFY_EMAIL_ASYNC', True):
        transaction.on_commit(lambda: threading.Thread(target=send, daemon=True).start())
    else:
        transaction.on_commit(send)


def notify(users, kind, message, link='', alert=None):
    """
    One notification per user. `link` may be a function of the user (office links differ by role).
    `alert` (a short title) also reaches people outside the app: by email and as a phone push
    (used for field crew, who are out on site).
    """
    users = [u for u in users if u is not None]
    if not users:
        return
    Notification.objects.bulk_create([
        Notification(user=u, kind=kind, message=message, link=link(u) if callable(link) else link) for u in users
    ])
    if alert:
        plain_link = '' if callable(link) else link
        _email(users, alert, message, plain_link)
        push.send(users, alert, message, url=plain_link)   # untagged: two new jobs are two alerts


def notify_grouped(users, kind, one, many, link_one, link_many):
    """
    Like notify(), but an unread notification of the same kind from the last few hours absorbs the
    new event ("3 new orders · latest #42 …") and moves back to the top, so a busy day is one line.
    `many(count)` builds the grouped text; links may be functions of the user.
    """
    since = timezone.now() - GROUP_WINDOW
    for u in users:
        pick = lambda l: l(u) if callable(l) else l   # noqa: E731
        existing = (Notification.objects.select_for_update()
                    .filter(user=u, kind=kind, is_read=False, timestamp__gte=since).first())
        if existing:
            existing.count += 1
            existing.message = many(existing.count)
            existing.link = pick(link_many)
            existing.timestamp = timezone.now()
            existing.save(update_fields=['count', 'message', 'link', 'timestamp'])
        else:
            Notification.objects.create(user=u, kind=kind, message=one, link=pick(link_one))


# ── Office events ───────────────────────────────────────────────────────────

def order_new(order):
    how = 'cash on delivery' if order.payment_method == 'cod' else 'paid online'
    with transaction.atomic():
        notify_grouped(
            office_users(), 'order_new',
            one=f'New order #{order.id} from {order.customer_name}: ₱{order.total_price:,.2f}, {how}.',
            many=lambda n: f'{n} new orders to pack. Latest: #{order.id} from {order.customer_name} (₱{order.total_price:,.2f}, {how}).',
            link_one=lambda u: _office_link(u, order_office_path(order.id)),
            link_many=lambda u: _office_link(u, order_office_path()),
        )


def booking_new(booking):
    who = _name(booking.user)
    when = _fmt_date(booking.scheduled_date)
    with transaction.atomic():
        notify_grouped(
            office_users(), 'booking_new',
            one=f'New booking #{booking.id}: {_service(booking)} for {who} on {when}. It needs confirming.',
            many=lambda n: f'{n} new bookings to confirm. Latest: #{booking.id}, {_service(booking)} for {who} on {when}.',
            link_one=lambda u: _office_link(u, booking_office_path(booking.id)),
            link_many=lambda u: _office_link(u, booking_office_path()),
        )


def booking_cancelled_by_customer(booking):
    tracker = getattr(booking, 'tracker', None)
    paid = (tracker.total_price - tracker.remaining_balance) if tracker else 0
    notify(office_users(), 'booking_cancelled',
           f'{_name(booking.user)} cancelled booking #{booking.id} ({_service(booking)} on {_fmt_date(booking.scheduled_date)}).'
           + (f' They had paid ₱{paid:,.2f}: refund it in PayMongo.' if paid > 0 else ''),
           link=lambda u: _office_link(u, booking_office_path(booking.id)))
    notify(booking.assigned_crew.all(), 'job_cancelled',
           f'Booking #{booking.id} ({_service(booking)} on {_fmt_date(booking.scheduled_date)}) was cancelled by the customer. '
           'You no longer need to go.', link=crew_job_link(booking), alert=f'Job #{booking.id} cancelled')


def payment_received(req):
    booking = req.project.booking
    notify(office_users(), 'payment_received',
           f'{_name(booking.user)} paid ₱{req.amount:,.2f} online for booking #{booking.id} ({req.label}).',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))


def job_review(booking, crew_user):
    notify(office_users(), 'job_review',
           f'Booking #{booking.id} ({_service(booking)}) was submitted as finished by {_name(crew_user)} '
           'and is awaiting your sign-off.',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))


def job_problem(booking, crew_user, reason, note=''):
    notify(office_users(), 'job_problem',
           f'Problem on booking #{booking.id} ({_service(booking)}, {_name(booking.user)}): '
           f'{reason}' + (f' — "{note}"' if note else '') + f' (reported by {_name(crew_user)}).',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))


def crew_on_the_way(booking, crew_user, eta_minutes):
    if not booking.user:
        return
    where = booking.service_address.strip()
    notify([booking.user], 'crew_on_the_way',
           f'{crew_user.first_name or "Our crew"} from {business_name()} is on the way'
           + (f' to {where}' if where else '') + f', arriving in about {eta_minutes} minutes.',
           link=MY_BOOKINGS, alert='Our crew is on the way')


def delivery_problem(order, crew_user, reason, note=''):
    notify(office_users(), 'delivery_problem',
           f'Delivery problem on order #{order.id} ({order.customer_name}): {reason}'
           + (f' — "{note}"' if note else '') + f' (reported by {_name(crew_user)}).',
           link=lambda u: _office_link(u, order_office_path(order.id)))


# ── Field crew events (also emailed) ────────────────────────────────────────

def crew_changed(booking, added, removed):
    where = booking.service_address.strip() or 'location to be confirmed'
    what = f'{_service(booking)} at {where} (booking #{booking.id}, {_fmt_date(booking.scheduled_date)})'
    notify(added, 'job_assigned', f'You have been assigned to {what}.',
           link=crew_job_link(booking), alert=f'New job: booking #{booking.id} on {_fmt_date(booking.scheduled_date)}')
    notify(removed, 'job_unassigned', f'You were taken off {what}. You no longer need to go.',
           link=CREW_JOBS, alert=f'Removed from booking #{booking.id}')
    if added and booking.user:
        names = ', '.join(u.first_name or u.username for u in booking.assigned_crew.all())
        notify([booking.user], 'crew_assigned',
               f'Our crew ({names}) will handle your {_service(booking)} on {_fmt_date(booking.scheduled_date)}.',
               link=MY_BOOKINGS)


def booking_rescheduled(booking, old_date):
    msg = (f'Booking #{booking.id} ({_service(booking)}) moved from {_fmt_date(old_date)} '
           f'to {_fmt_date(booking.scheduled_date)}.')
    notify(booking.assigned_crew.all(), 'job_rescheduled', msg, link=crew_job_link(booking),
           alert=f'Booking #{booking.id} moved to {_fmt_date(booking.scheduled_date)}')
    if booking.user:
        notify([booking.user], 'booking_rescheduled', f'Your {msg[0].lower()}{msg[1:]}', link=MY_BOOKINGS)


def booking_status_changed(booking, old, new, actor, note=''):
    """Office status changes (crew submissions are job_review)."""
    customer = [booking.user] if booking.user else []
    crew = list(booking.assigned_crew.all())
    when = _fmt_date(booking.scheduled_date)
    if new == 'Preparing' and old == 'Pending':
        notify(customer, 'booking_confirmed', f'Your {_service(booking)} booking #{booking.id} is confirmed for {when}.',
               link=MY_BOOKINGS)
    elif new == 'Installing' and old == 'For Review':
        notify(crew, 'job_sent_back',
               f'Booking #{booking.id} was sent back for more work' + (f': {note}' if note else '.'),
               link=crew_job_link(booking), alert=f'Booking #{booking.id} needs more work')
    elif new == 'Cancelled':
        notify(customer, 'booking_cancelled', f'Your booking #{booking.id} ({_service(booking)} on {when}) was cancelled'
               + (f': {note}' if note else '. Contact us if you have questions.'), link=MY_BOOKINGS)
        notify(crew, 'job_cancelled', f'Booking #{booking.id} ({_service(booking)} on {when}) was cancelled. '
               'You no longer need to go.', link=crew_job_link(booking), alert=f'Job #{booking.id} cancelled')


def delivery_changed(order, new_crew, old_crew):
    if new_crew is not None:
        notify([new_crew], 'delivery_assigned',
               f'Deliver order #{order.id} to {order.customer_name}, {order.customer_address or "address on file"}.'
               + (f' Collect ₱{order.total_price:,.2f} cash on delivery.' if order.payment_method == 'cod' else ' Already paid online.'),
               link=CREW_DELIVERIES, alert=f'New delivery: order #{order.id}')
    if old_crew is not None and old_crew != new_crew:
        notify([old_crew], 'delivery_unassigned', f'Order #{order.id} ({order.customer_name}) was given to another driver. '
               'You no longer need to deliver it.', link=CREW_DELIVERIES, alert=f'Order #{order.id} reassigned')


# ── Customer order events ──────────────────────────────────────────────────

def order_status_changed(order, old, new):
    if not order.user_id or old == new:
        return
    msg = {
        'Out for Delivery': f'Your order #{order.id} is on its way.'
                            + (f' Please prepare ₱{order.total_price:,.2f} cash.' if order.payment_method == 'cod' and order.payment_status != 'Paid' else ''),
        'Delivered': f'Your order #{order.id} was delivered. Thank you for shopping with us!',
        'Cancelled': f'Your order #{order.id} was cancelled.'
                     + (' Your online payment will be refunded.' if order.payment_status in ('Paid', 'Refunded') and order.payment_method == 'online' else ''),
    }.get(new)
    if msg:
        kind = {'Out for Delivery': 'order_on_the_way', 'Delivered': 'order_delivered', 'Cancelled': 'order_cancelled'}[new]
        notify([order.user], kind, msg, link=MY_ORDERS)
