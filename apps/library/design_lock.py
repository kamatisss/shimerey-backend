"""
Booked designs are locked.

Once a booking uses a design, the office quotes from it and the crew builds from it, so it has to stay
the same version everyone agreed on. While any booking that isn't cancelled points at a design:
  • the customer can't change or delete it (they can save a copy and keep designing that);
  • field crew can only view it (crew never change a customer's design, booked or not);
  • the office can still adjust it (site visit changes), and each change is written to the activity log;
  • nobody can delete it, because the booking would lose its design.
Cancelling the booking unlocks the design again. Finished bookings keep it locked: it's the record of
what was built.
"""
from django.db.models import Exists, OuterRef

from .models import ServiceBooking
from .work_queues import is_field_crew


def open_bookings(design):
    """Bookings that hold this design (everything except cancelled), soonest visit first."""
    return design.bookings.exclude(status='Cancelled').order_by('scheduled_date', 'id')


def locked_annotation():
    """Queryset annotation: True when a booking that isn't cancelled uses the design."""
    return Exists(ServiceBooking.objects.filter(design=OuterRef('pk')).exclude(status='Cancelled'))


def is_office(user):
    """Office admins and the owner (same people IsOfficeAdmin lets in; never field crew)."""
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser) and not is_field_crew(user))


def lock_info(design):
    """The booking holding the design, as {booking, service, date, status}, or None when it's free."""
    booking = open_bookings(design).first()
    if booking is None:
        return None
    return {
        'booking': booking.id,
        'service': booking.get_service_type_display(),
        'date': booking.scheduled_date.isoformat(),
        'status': booking.status,
    }


def _when(info):
    from datetime import date
    return date.fromisoformat(info['date']).strftime('%b %d, %Y').replace(' 0', ' ')


def write_error(user, design, deleting=False):
    """Why this user can't change (or delete) this design, as (http_status, body), or None if they can."""
    if is_field_crew(user) and design.user_id != user.id:
        return 403, {'error': "Crew can view a job's design but not change it. Ask the office if something needs to change."}
    info = lock_info(design)
    if info is None:
        return None
    if deleting:
        msg = (f'Booking #{info["booking"]} uses this design, so it can\'t be deleted. '
               'Cancel the booking or give it another design first.') if is_office(user) else (
               f'This design is booked ({info["service"]}, {_when(info)}). It can\'t be deleted while the booking is open.')
        return 409, {'error': msg, 'locked': True, 'locked_for': info}
    if is_office(user):
        return None
    return 409, {
        'error': (f'This design is booked ({info["service"]}, {_when(info)}). Our crew builds from it, so it can\'t '
                  'be changed. Save it as a new design to keep working, or contact us to change the booked design.'),
        'locked': True, 'locked_for': info,
    }


def attach_error(user, design):
    """Why this user can't put this design on a booking, or None. Customers attach only their own designs."""
    if design is None or is_office(user) or design.user_id == user.id:
        return None
    return 'Pick one of your own saved designs.'
