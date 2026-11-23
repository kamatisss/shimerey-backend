"""
Notifications — one place that decides who hears about what.

A notification means "something that concerns you, or needs you, just happened". The staff
work queues stay the to-do list; this is the tap on the shoulder.

  Office (office admins + superusers)
    order_new          COD order placed, or online order paid           (grouped: "3 new orders";
                       also emailed, one email per order, with the items, address and phone)
    order_withdrawn    customer cancelled their COD order before it was packed
    order_late_payment an online order was paid after it had expired (stock was released: re-check it)
    booking_new        customer booked a service                        (grouped)
    booking_cancelled  customer cancelled a booking
    booking_changed    customer moved their booking date or changed its address
    payment_received   customer paid a project payment request online
    job_review         crew submitted a job for sign-off
    job_problem        crew reported a problem on a job (customer not home, rain, missing materials…)
    delivery_problem   crew reported a delivery problem
  Field crew (in-app, by email and as a phone push, since they are out in the field)
    job_assigned, job_unassigned, job_rescheduled, job_sent_back, job_cancelled
    delivery_assigned, delivery_unassigned, cash_received (in-app only: the office counted their COD cash)
  Customer — in-app, and by email for the moments a shopper or client expects one
    Orders (email to the address on the order, so guest checkouts hear too; the link is the receipt page):
      order_placed (COD), order_paid (online), order_packed (in-app only), order_on_the_way,
      order_delivered, order_cancelled, order_expired (unpaid online order released), order_refunded
    Bookings: booking_received, booking_confirmed, crew_assigned (in-app), booking_rescheduled,
      booking_cancelled, booking_finished, crew_on_the_way, crew_arrived
    Money: payment_receipt (online or recorded by the office), payment_request_withdrawn
    (+ payment_request, portfolio_consent, review_reply, design_sent from their own modules)

Every notification carries a `link`: the in-app page the bell opens.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import push
from .after_commit import run_after_commit
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
    _email_addresses([u.email for u in users if u.email], subject, message, link)


def _email_addresses(recipients, subject, message, link):
    recipients = list(dict.fromkeys(r for r in recipients if r))
    if not recipients:
        return
    brand = business_name()
    body = f'{message}\n\nOpen it in {brand}: {settings.FRONTEND_URL}{link}\n' if link else f'{message}\n'

    def send():
        # One SMTP connection for all recipients (one email each, so addresses stay private): on
        # PythonAnywhere this runs inside the request, and each new connection costs a second or two
        try:
            connection = get_connection(fail_silently=False)
            connection.open()
        except Exception:   # a mail problem must never break the action that triggered it
            logger.exception('Notification email: could not connect to the mail server')
            return
        try:
            for to in recipients:
                try:
                    EmailMessage(f'{brand}: {subject}', body, settings.DEFAULT_FROM_EMAIL, [to],
                                 connection=connection).send()
                except Exception:
                    logger.exception('Notification email to %s failed', to)
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    run_after_commit(send)   # a thread where the server runs them; in the request on PythonAnywhere


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
    office = list(office_users())
    # Also by email (one per order, never grouped): the owner is not always in the app, and an order
    # should not wait. The email carries what packing needs, so it can be acted on from the phone.
    items = '\n'.join(f'  {oi.quantity} × {oi.item.name if oi.item_id else "item no longer in the catalog"}'
                      for oi in order.items.select_related('item'))
    _email(office, f'New order #{order.id}: ₱{order.total_price:,.2f}, {how}',
           f'New order #{order.id} from {order.customer_name}: ₱{order.total_price:,.2f}, {how}.\n\n'
           + (f'Items:\n{items}\n\n' if items else '')
           + f'Deliver to: {order.customer_address or "address on the order"}\n'
           + (f'Phone: {order.customer_phone}\n' if order.customer_phone else ''),
           order_office_path(order.id))
    with transaction.atomic():
        notify_grouped(
            office, 'order_new',
            one=f'New order #{order.id} from {order.customer_name}: ₱{order.total_price:,.2f}, {how}.',
            many=lambda n: f'{n} new orders to pack. Latest: #{order.id} from {order.customer_name} (₱{order.total_price:,.2f}, {how}).',
            link_one=lambda u: _office_link(u, order_office_path(order.id)),
            link_many=lambda u: _office_link(u, order_office_path()),
        )


def order_withdrawn(order):
    """The customer cancelled their own COD order before it was packed (it stays in the list as Cancelled)."""
    if order.payment_method != 'cod':
        return   # an unpaid online order never reached the office
    notify(office_users(), 'order_withdrawn',
           f'{order.customer_name} cancelled order #{order.id} (₱{order.total_price:,.2f}, cash on delivery). '
           "Don't pack it: the items are back in stock.",
           link=lambda u: _office_link(u, order_office_path(order.id)))


def order_late_payment(order):
    """PayMongo confirmed payment for an order that had expired or been cancelled, so its stock was released."""
    notify(office_users(), 'order_late_payment',
           f'Order #{order.id} ({order.customer_name}, ₱{order.total_price:,.2f}) was paid after it had expired or been cancelled, '
           'so its items were put back in stock. Check the stock before packing it, or refund it in PayMongo.',
           link=lambda u: _office_link(u, order_office_path(order.id)))


def booking_new(booking):
    who = _name(booking.user)
    when = _fmt_date(booking.scheduled_date)
    notify([booking.user], 'booking_received',
           f'We received your request for {_service(booking)} on {when} (booking #{booking.id}). '
           "We'll confirm it soon and let you know.",
           link=MY_BOOKINGS, alert=f'Booking request #{booking.id} received')
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


def payment_received(req, balance=None):
    booking = req.project.booking
    notify(office_users(), 'payment_received',
           f'{_name(booking.user)} paid ₱{req.amount:,.2f} online for booking #{booking.id} ({req.label}).',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))
    payment_receipt(booking, req.amount, req.label, req.project.remaining_balance if balance is None else balance)


def payment_receipt(booking, amount, what, balance):
    """The customer's receipt for a project payment, paid online or recorded by the office."""
    if not booking.user:
        return
    notify([booking.user], 'payment_receipt',
           f'We received your ₱{amount:,.2f} payment for booking #{booking.id}' + (f' ({what})' if what else '')
           + (f'. Remaining balance: ₱{balance:,.2f}.' if balance > 0 else '. Your project is fully paid.')
           + ' Thank you!',
           link=MY_BOOKINGS, alert=f'Payment received: ₱{amount:,.2f}')


def payment_request_withdrawn(req):
    booking = req.project.booking
    if not booking.user:
        return
    notify([booking.user], 'payment_request_withdrawn',
           f'The ₱{req.amount:,.2f} payment request ({req.label}) for booking #{booking.id} was withdrawn. '
           "You don't need to pay it.", link=MY_BOOKINGS)


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


def crew_arrived_at_site(booking, crew_user):
    """The crew reached the site pin (crew_location.arrived)."""
    if not booking.user:
        return
    notify([booking.user], 'crew_arrived',
           f'{crew_user.first_name or "Our crew"} from {business_name()} has arrived at your address.',
           link=MY_BOOKINGS, alert='Our crew has arrived')


def rider_arrived(order, crew_user):
    """The rider reached the delivery pin with order #id."""
    if not order.user:
        return
    pay = ' Please have the cash ready.' if order.payment_method == 'cod' and order.payment_status != 'Paid' else ''
    notify([order.user], 'rider_arrived',
           f'{crew_user.first_name or "Our rider"} from {business_name()} is at your address with order #{order.id}.{pay}',
           link=MY_ORDERS, alert='Your order has arrived')


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


def booking_rescheduled(booking, old_date, by_customer=False):
    msg = (f'Booking #{booking.id} ({_service(booking)}) moved from {_fmt_date(old_date)} '
           f'to {_fmt_date(booking.scheduled_date)}.')
    notify(booking.assigned_crew.all(), 'job_rescheduled', msg, link=crew_job_link(booking),
           alert=f'Booking #{booking.id} moved to {_fmt_date(booking.scheduled_date)}')
    if by_customer:
        # The office plans crews and materials around the date: they must hear when the customer moves it
        notify(office_users(), 'booking_changed', f'{_name(booking.user)} moved booking #{booking.id} '
               f'({_service(booking)}) from {_fmt_date(old_date)} to {_fmt_date(booking.scheduled_date)}.',
               link=lambda u: _office_link(u, booking_office_path(booking.id)))
    elif booking.user:
        notify([booking.user], 'booking_rescheduled', f'Your {msg[0].lower()}{msg[1:]}', link=MY_BOOKINGS,
               alert=f'Booking #{booking.id} moved to {_fmt_date(booking.scheduled_date)}')


def booking_address_changed(booking, old_address):
    """The customer changed where the work happens (allowed until work starts)."""
    msg = (f'{_name(booking.user)} changed the address of booking #{booking.id} ({_service(booking)}) '
           f'to {booking.service_address or "a new pin"}' + (f' (was {old_address})' if old_address else '') + '.')
    notify(office_users(), 'booking_changed', msg,
           link=lambda u: _office_link(u, booking_office_path(booking.id)))
    notify(booking.assigned_crew.all(), 'job_rescheduled',
           f'The address for booking #{booking.id} changed to {booking.service_address or "a new pin"}.',
           link=crew_job_link(booking), alert=f'New address for booking #{booking.id}')


def booking_status_changed(booking, old, new, actor, note=''):
    """Office status changes (crew submissions are job_review)."""
    customer = [booking.user] if booking.user else []
    crew = list(booking.assigned_crew.all())
    when = _fmt_date(booking.scheduled_date)
    if new == 'Preparing' and old == 'Pending':
        notify(customer, 'booking_confirmed', f'Your {_service(booking)} booking #{booking.id} is confirmed for {when}.',
               link=MY_BOOKINGS, alert=f'Booking #{booking.id} confirmed for {when}')
    elif new == 'Finished' and old != 'Finished':
        notify(customer, 'booking_finished',
               f'Your {_service(booking)} project (booking #{booking.id}) is complete. Thank you for choosing '
               f'{business_name()}!', link=MY_BOOKINGS, alert=f'Your project #{booking.id} is complete')
    elif new == 'Installing' and old == 'For Review':
        notify(crew, 'job_sent_back',
               f'Booking #{booking.id} was sent back for more work' + (f': {note}' if note else '.'),
               link=crew_job_link(booking), alert=f'Booking #{booking.id} needs more work')
    elif new == 'Cancelled':
        notify(customer, 'booking_cancelled', f'Your booking #{booking.id} ({_service(booking)} on {when}) was cancelled'
               + (f': {note}' if note else '. Contact us if you have questions.'), link=MY_BOOKINGS,
               alert=f'Booking #{booking.id} cancelled')
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

def order_receipt_link(order):
    """The order's receipt page: opens for the buyer, and for a guest through the token in the link."""
    if not order.receipt_token:
        return MY_ORDERS
    return f'/order-success?order_id={order.id}&token={order.receipt_token}'


def _tell_buyer(order, kind, message, subject=None):
    """
    In the buyer's bell when they have an account, and (with a subject) by email to the address
    typed at checkout: guest checkouts have no bell, and the email is where shoppers look.
    """
    if order.user_id:
        notify([order.user], kind, message, link=MY_ORDERS)
    if subject:
        _email_addresses([order.customer_email], f'{subject} (order #{order.id})', message, order_receipt_link(order))


def _cash_note(order):
    unpaid_cod = order.payment_method == 'cod' and order.payment_status != 'Paid'
    return f' Please prepare ₱{order.total_price:,.2f} cash.' if unpaid_cod else ''


def order_placed(order):
    """A COD order was placed: the buyer's confirmation (online orders are confirmed when paid)."""
    _tell_buyer(order, 'order_placed',
                f"We received your order #{order.id}: ₱{order.total_price:,.2f}, cash on delivery. "
                "We'll let you know when it's on the way.", subject='Order received')


def order_paid(order):
    _tell_buyer(order, 'order_paid',
                f"Payment received for your order #{order.id}: ₱{order.total_price:,.2f}. "
                "We're preparing it now and will let you know when it's on the way.", subject='Payment received')


def order_expired(order, minutes):
    _tell_buyer(order, 'order_expired',
                f'Your order #{order.id} was cancelled because the online payment was not completed within '
                f'{minutes} minutes. You were not charged. You can order again any time.', subject='Order cancelled')


def order_refunded(order):
    _tell_buyer(order, 'order_refunded',
                f'We sent the ₱{order.total_price:,.2f} refund for your cancelled order #{order.id}. '
                'It can take a few days to show in your account.', subject='Refund sent')


def order_status_changed(order, old, new):
    if old == new:
        return
    msg = {
        'Shipped': f"Your order #{order.id} is packed and ready for delivery.",
        'Out for Delivery': f'Your order #{order.id} is on its way.' + _cash_note(order),
        'Delivered': f'Your order #{order.id} was delivered. Thank you for shopping with us!',
        'Cancelled': f'Your order #{order.id} was cancelled.'
                     + (' Your online payment will be refunded.' if order.payment_status in ('Paid', 'Refunded') and order.payment_method == 'online' else ''),
    }.get(new)
    if msg:
        kind = {'Shipped': 'order_packed', 'Out for Delivery': 'order_on_the_way', 'Delivered': 'order_delivered',
                'Cancelled': 'order_cancelled'}[new]
        # Packed is a small step: the bell only. The rest are the emails a shopper expects.
        subject = {'Out for Delivery': 'Your order is on its way', 'Delivered': 'Order delivered',
                   'Cancelled': 'Order cancelled'}.get(new)
        _tell_buyer(order, kind, msg, subject)


# ─── Final design (design_review.py) ─────────────────────────────────────────

def design_review_link(booking):
    """The customer's booking page with the design review open."""
    return f'{MY_BOOKINGS}?review={booking.id}'


def design_sent(booking, note=''):
    notify([booking.user], 'design_sent',
           f'Your final garden design for booking #{booking.id} is ready. Please check it and approve it'
           + (f'. Our note: {note}' if note else '.'),
           link=design_review_link(booking), alert='Your final design is ready to approve')


def design_approved(booking, in_person=False):
    how = 'was approved in person' if in_person else f'was approved by {_name(booking.user)}'
    notify(office_users(), 'design_approved', f'The final design for booking #{booking.id} {how}.',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))
    notify(booking.assigned_crew.all(), 'design_approved',
           f'The design for booking #{booking.id} is final. Build from this version.', link=crew_job_link(booking))


def design_changes_requested(booking, note):
    notify(office_users(), 'design_changes',
           f'{_name(booking.user)} asked for changes to the design for booking #{booking.id}: {note}',
           link=lambda u: _office_link(u, booking_office_path(booking.id)))
