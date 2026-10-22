"""
Owner overview — GET /api/admin/overview/ (office admins and the owner).

Two questions an owner opens the app with:
  today      What came in today: orders placed, money received, today's visits, who is working.
  attention  What is waiting for someone, each with a count and a link to the exact queue.
The numbers use the same queue rules as the staff screens (work_queues.py), so "3 to pack" here is
the same 3 the Orders tab shows.
"""
from django.db.models import F, Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import cash, work_queues
from .models import (
    Attendance, GardenDesign, InventoryItem, Order, ProjectPayment, ProjectTracker, ServiceBooking,
)
from .views_api import IsOfficeAdmin

ORDERS = '/staff-dashboard?tab=orders'
BOOKINGS = '/staff-dashboard?tab=bookings'


def _money(qs, field):
    return float(qs.aggregate(total=Sum(field))['total'] or 0)


def today_numbers():
    today = timezone.localdate()
    placed = Order.objects.filter(created_at__date=today).exclude(status='Cancelled')
    received = (
        _money(Order.objects.filter(payment_method='online', payment_status='Paid', paid_at__date=today), 'total_price')
        + _money(Order.objects.filter(payment_method='cod', status='Delivered', delivered_at__date=today), 'total_price')
        + _money(ProjectPayment.objects.filter(paid_at__date=today), 'amount')
    )
    return {
        'date': today,
        'orders': placed.count(),
        'orders_value': _money(placed, 'total_price'),
        'money_received': received,
        'visits': ServiceBooking.objects.filter(scheduled_date=today).exclude(status__in=('Finished', 'Cancelled')).count(),
        'crew_on_shift': Attendance.objects.filter(clock_out_time__isnull=True).count(),
    }


def attention():
    """Waiting work, most urgent first; only what has something in it."""
    oc = work_queues.order_counts(Order.objects.all())
    bc = work_queues.booking_counts(ServiceBooking.objects.all())
    low = InventoryItem.objects.filter(is_active=True, stock_quantity__lte=F('reorder_level'))
    unpaid = ProjectTracker.objects.filter(booking__status='Finished', remaining_balance__gt=0)
    held = cash.with_crew()
    held_crew = held.order_by().values('delivered_by').distinct().count()
    items = [
        ('needs_refund', 'red', 'Refunds to make in PayMongo', 'Paid online, then cancelled', oc['needs_refund'], f'{ORDERS}&queue=needs_refund'),
        ('review', 'violet', 'Jobs to sign off', 'The crew marked them finished', bc['review'], f'{BOOKINGS}&queue=review'),
        ('awaiting', 'amber', 'Bookings to confirm', 'New requests from customers', bc['awaiting'], f'{BOOKINGS}&queue=awaiting'),
        ('dispatch', 'amber', 'Bookings that need a crew', 'Confirmed but nobody assigned', bc['dispatch'], f'{BOOKINGS}&queue=dispatch'),
        ('cash', 'amber', 'Cash with crew to hand in',
         f'₱{_money(held, "total_price"):,.0f} from {held_crew} crew member{"s" if held_crew != 1 else ""}', held.count(),
         '/staff-dashboard?tab=cash'),
        ('to_pack', 'blue', 'Orders to pack', 'Paid online or cash on delivery', oc['to_pack'], f'{ORDERS}&queue=to_pack'),
        ('designs', 'blue', 'Designs to review', 'Customers submitted them for a quote',
         GardenDesign.objects.filter(status='submitted').count(), '/staff-dashboard?tab=designs'),
        ('low_stock', 'amber', 'Items low on stock', 'At or below their low-stock level', low.count(), '/inventory'),
        ('unpaid', 'red', 'Unpaid balances on finished jobs',
         f'₱{_money(unpaid, "remaining_balance"):,.0f} still to collect', unpaid.count(), f'{BOOKINGS}&queue=completed'),
    ]
    return [{'key': k, 'tone': tone, 'label': label, 'hint': hint, 'count': n, 'link': link}
            for k, tone, label, hint, n, link in items if n]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def business_overview(request):
    if not IsOfficeAdmin().has_permission(request, None):
        return Response({'error': 'Office admins only.'}, status=status.HTTP_403_FORBIDDEN)
    return Response({'today': today_numbers(), 'attention': attention()})
