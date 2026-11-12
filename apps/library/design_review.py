"""
Agreeing the final design of a booking: the version the crew builds.

A customer's own design (AI Designer or Studio) is a starting point. After the site visit the office
adjusts it to the real yard (in the Studio), then:

  office   send               → the customer sees the design with the office's note of what changed
  customer approve            → this exact version (revision) is the one to build
  customer request changes    → back to the office, with what they want changed
  office   approved in person → the customer agreed on site (the office writes down how)

State of a booking (state_of):
  none       no design on the booking
  draft      nothing sent yet: the office still has to check it and send it
  sent       waiting for the customer
  changes    the customer asked for changes: the office's turn
  changed    it was sent or approved, then the design was changed again: send it again
  approved   approved, and the design hasn't changed since

The office can't mark "Design & contract" done until the design is approved, and the crew see whether
the design they open is final. Each step is kept (DesignReview) with a snapshot of the design.
"""
from django.db import transaction

from .models import DesignReview, ProjectHistoryLog, ProjectTracker, STATUS_PROGRESS_MAP

APPROVED_KINDS = ('approved', 'approved_in_person')
NOTE_MAX = 1000


class ReviewError(Exception):
    """Not allowed right now; the message is shown to the person."""


def snapshot(design):
    """The design as it is now: what the customer is shown and what they approve."""
    return {
        'name': design.name,
        'revision': design.revision,
        'placed_items': design.placed_items or [],
        'plant_breakdown': design.plant_breakdown or [],
        'dimensions': design.dimensions or {},
        'total_cost': float(design.total_cost or 0),
    }


def _steps(booking):
    """This booking's steps for its current design, newest first (uses prefetched design_reviews if there)."""
    if not booking.design_id:
        return []
    steps = [s for s in booking.design_reviews.all() if s.design_id == booking.design_id]
    return sorted(steps, key=lambda s: (s.created_at, s.id), reverse=True)


def state_of(booking, steps=None):
    design = booking.design
    if design is None:
        return 'none'
    steps = _steps(booking) if steps is None else steps
    last = steps[0] if steps else None
    if last is None:
        return 'draft'
    if last.kind == 'changes':
        return 'changes'
    if last.revision != design.revision:
        return 'changed'
    return 'approved' if last.kind in APPROVED_KINDS else 'sent'


def is_approved(booking):
    return state_of(booking) == 'approved'


def _who(user):
    if user is None:
        return ''
    return (f'{user.first_name} {user.last_name}'.strip() or user.username)


def summary(booking):
    """What the booking serializer shows: the state, the last steps, and the approved version."""
    if not booking.design_id:
        return {'state': 'none', 'steps': []}
    steps = _steps(booking)[:10]
    approved = next((s for s in steps if s.kind in APPROVED_KINDS), None)
    sent = next((s for s in steps if s.kind == 'sent'), None)
    return {
        'state': state_of(booking, steps),
        'revision': booking.design.revision,
        'steps': [{
            'id': s.id, 'kind': s.kind, 'label': s.get_kind_display(), 'note': s.note, 'revision': s.revision,
            'by': _who(s.by), 'at': s.created_at,
        } for s in steps],
        # The office's note on the version waiting for the customer
        'sent_note': sent.note if sent else '',
        'sent_revision': sent.revision if sent else None,
        'approved_at': approved.created_at if approved else None,
        'approved_by': _who(approved.by) if approved else '',
        'approved_revision': approved.revision if approved else None,
        'approved_in_person': bool(approved and approved.kind == 'approved_in_person'),
    }


def _note(note, required, what):
    note = str(note or '').strip()[:NOTE_MAX]
    if required and not note:
        raise ReviewError(what)
    return note


def _record(booking, kind, user, note):
    design = booking.design
    return DesignReview.objects.create(booking=booking, design=design, revision=design.revision, kind=kind,
                                       note=note, by=user, snapshot=snapshot(design))


def _mark_tracker_approved(booking, user, remark):
    """Move the project to "Design Approved" unless it's already past that point."""
    tracker = ProjectTracker.objects.filter(booking=booking).first()
    if tracker is None:
        return
    if STATUS_PROGRESS_MAP.get(tracker.status, 0) < STATUS_PROGRESS_MAP['DESIGN_APPROVED']:
        tracker.status = 'DESIGN_APPROVED'
        tracker.save()
        ProjectHistoryLog.objects.create(project=tracker, status_reached='DESIGN_APPROVED', trigger_user=user, remarks=remark)


@transaction.atomic
def send(booking, user, note=''):
    """Office: send the design (as it is now) to the customer to approve."""
    if booking.design is None:
        raise ReviewError('This booking has no design to send.')
    if booking.status in ('Finished', 'Cancelled'):
        raise ReviewError(f'This booking is {booking.status.lower()}.')
    if state_of(booking) == 'sent':
        raise ReviewError('This version is already waiting for the customer.')
    step = _record(booking, 'sent', user, _note(note, False, ''))
    from . import notifications
    notifications.design_sent(booking, step.note)
    return step


@transaction.atomic
def approve(booking, user, revision):
    """Customer: approve the version they were sent. `revision` is the one on their screen."""
    current = state_of(booking)
    if current == 'approved':
        raise ReviewError('You already approved this design.')
    if current != 'sent':
        raise ReviewError("There's no design waiting for your approval.")
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        raise ReviewError('Open the design again and approve the version you see.')
    if revision != booking.design.revision:
        raise ReviewError('The design was changed after you opened it. Please look at it again before approving.')
    step = _record(booking, 'approved', user, '')
    _mark_tracker_approved(booking, user, f'Customer approved the final design (version {step.revision}).')
    from . import notifications
    notifications.design_approved(booking, in_person=False)
    return step


@transaction.atomic
def request_changes(booking, user, note):
    """Customer: say what should change. Allowed while a version waits for them, or after approving it."""
    if state_of(booking) not in ('sent', 'approved'):
        raise ReviewError("There's no design waiting for your answer.")
    if booking.status not in ('Pending', 'Preparing'):
        raise ReviewError('Work has started on your garden. Please call us to change the design.')
    step = _record(booking, 'changes', user, _note(note, True, 'Say what you would like changed.'))
    from . import notifications
    notifications.design_changes_requested(booking, step.note)
    return step


@transaction.atomic
def approve_in_person(booking, user, note):
    """Office: the customer approved this version face to face (e.g. at the site visit)."""
    if booking.design is None:
        raise ReviewError('This booking has no design.')
    if state_of(booking) == 'approved':
        raise ReviewError('This version is already approved.')
    step = _record(booking, 'approved_in_person', user,
                   _note(note, True, 'Write how the customer approved it (e.g. "Signed at the site visit, Nov 12").'))
    _mark_tracker_approved(booking, user, f'Final design approved in person (version {step.revision}): {step.note}')
    from . import notifications
    notifications.design_approved(booking, in_person=True)
    return step
