"""
Cash on delivery: from the customer's hand to the office.

A crew member who marks a COD order Delivered is holding that cash (Order.delivered_by). It stays
"with the crew" until the office records a handover: the office counts the money, and the orders it
covers are linked to one CashHandover. If the count differs from what the orders add up to, the
office must say why (the difference is kept, never silently corrected).

Orders the office itself marks Delivered (e.g. a customer picked up) are never "with the crew".
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, Sum

from . import activity, notifications
from .models import CashHandover, Order


class CashError(Exception):
    pass


def with_crew(crew=None):
    """Delivered COD orders whose cash hasn't reached the office yet (optionally for one crew member)."""
    qs = Order.objects.filter(payment_method='cod', status='Delivered', cash_handover__isnull=True,
                              delivered_by__staff_profile__role='FIELD_CREW')
    return qs.filter(delivered_by=crew) if crew is not None else qs


def crew_totals():
    """[{crew_id, name, orders, total, oldest}] — one row per crew member holding cash, most cash first."""
    rows = (with_crew().values('delivered_by').annotate(orders=Count('id'), total=Sum('total_price'))
            .order_by('-total'))
    users = User.objects.in_bulk([r['delivered_by'] for r in rows])
    out = []
    for r in rows:
        u = users[r['delivered_by']]
        oldest = with_crew(u).order_by('delivered_at').values_list('delivered_at', flat=True).first()
        out.append({'crew_id': u.id, 'name': u.get_full_name() or u.username, 'orders': r['orders'],
                    'total': Decimal(r['total']).quantize(Decimal('0.01')), 'oldest': oldest})
    return out


def receive(crew_id, order_ids, counted, note, by):
    """Record that `by` received cash from a crew member for these orders. Returns the CashHandover."""
    try:
        counted = Decimal(str(counted)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        raise CashError('Enter the amount you counted.')
    if counted < 0:
        raise CashError('The amount counted cannot be negative.')
    if not isinstance(order_ids, list) or not order_ids or not all(isinstance(i, int) for i in order_ids):
        raise CashError('Pick the orders this cash is for.')
    note = str(note or '').strip()[:300]
    crew = User.objects.filter(pk=crew_id).first()
    if crew is None:
        raise CashError('That crew member was not found.')

    with transaction.atomic():
        # Locked so two office staff can't record the same cash twice
        orders = list(with_crew(crew).select_for_update().filter(pk__in=order_ids))
        if len(orders) != len(set(order_ids)):
            raise CashError('Some of these orders are not cash this crew member is holding '
                            '(already handed over, or not theirs). Refresh and try again.')
        expected = sum((o.total_price for o in orders), Decimal('0'))
        if counted != expected and not note:
            raise CashError(f'You counted ₱{counted:,.2f} but the orders add up to ₱{expected:,.2f}. '
                            'Add a note saying why.')
        handover = CashHandover.objects.create(crew=crew, received_by=by, expected=expected, counted=counted, note=note)
        Order.objects.filter(pk__in=[o.pk for o in orders]).update(cash_handover=handover)

    name = crew.get_full_name() or crew.username
    diff = counted - expected
    summary = f'Received ₱{counted:,.2f} cash from {name} for {len(orders)} order{"s" if len(orders) != 1 else ""}'
    if diff:
        summary += f' ({"short" if diff < 0 else "over"} by ₱{abs(diff):,.2f})'
    activity.record(by, 'money', 'cash_received', summary, link='/staff-dashboard?tab=cash',
                    crew=crew.id, orders=[o.pk for o in orders], expected=str(expected), counted=str(counted), note=note)
    notifications.notify([crew], 'cash_received',
                         f'The office received your ₱{counted:,.2f} cash for {len(orders)} '
                         f'deliver{"ies" if len(orders) != 1 else "y"}. Thank you!',
                         link=notifications.CREW_DELIVERIES)
    return handover
