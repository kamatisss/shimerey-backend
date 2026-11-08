"""
Field-work rules for crew jobs — one place, used by the views (and mirrored in the crew UI).

  Start a job      = clock in to it (attendance clock_in). Allowed when the office has confirmed the
                     booking (Preparing) and its day has come, or when work is already under way
                     (Installing: multi-day jobs continue on later days). Starting moves it to Installing.
  Crew stages      = site_prep, installation, final_inspection: each needs a proof photo, and the crew
                     member must be clocked in to that job. design_contract is the office's stage.
  Sign-off         = crew may submit "For Review" only when every crew stage is done and at least one
                     After photo exists (final inspection's proof is an After photo).
"""
import datetime

from django.db.models import Q
from django.utils import timezone

from collections import Counter

from .models import Attendance, InventoryItem, ProjectMilestone, ProjectProgressMedia

CREW_STAGES = ['site_prep', 'installation', 'final_inspection']
OFFICE_STAGES = ['design_contract']
# A stage's proof photo is also a progress photo, so it reaches the office and the portfolio
STAGE_PHOTO_PHASE = {'site_prep': 'DURING', 'installation': 'DURING', 'final_inspection': 'AFTER'}
STAGE_LABELS = {'site_prep': 'Site prep', 'installation': 'Installation', 'final_inspection': 'Final inspection'}


def _day(d):
    try:
        return f'{d:%a %b} {d.day}'
    except (TypeError, ValueError):
        return str(d)


def start_job_error(booking, today=None):
    """Why this booking can't be started (clocked in to) right now, or None."""
    today = today or timezone.localdate()
    if booking.status == 'Pending':
        return f'Booking #{booking.id} is not confirmed by the office yet.'
    if booking.status in ('For Review', 'Finished', 'Cancelled'):
        return f'Booking #{booking.id} is {booking.status} and no longer open for work.'
    if booking.status == 'Preparing' and booking.scheduled_date and booking.scheduled_date > today:
        return f'Booking #{booking.id} is scheduled for {_day(booking.scheduled_date)}. You can start it that day.'
    return None


OPEN_SHIFT_MAX_HOURS = 16   # a shift still open after this was forgotten, not worked


def current_shifts():
    """Shifts genuinely under way: clocked in, not out, and started within OPEN_SHIFT_MAX_HOURS."""
    since = timezone.now() - datetime.timedelta(hours=OPEN_SHIFT_MAX_HOURS)
    return Attendance.objects.filter(clock_in_time__isnull=False, clock_out_time__isnull=True, clock_in_time__gte=since)


def forgotten_shifts():
    """Still open long after any real shift would have ended: the crew member forgot to clock out."""
    since = timezone.now() - datetime.timedelta(hours=OPEN_SHIFT_MAX_HOURS)
    return Attendance.objects.filter(clock_in_time__isnull=False, clock_out_time__isnull=True, clock_in_time__lt=since)


def is_clocked_in_to(user, booking, at=None):
    """On this job now, or (at=) at that moment: a stage photo taken offline may arrive after clock-out."""
    shifts = Attendance.objects.filter(staff=user, booking=booking)
    if at is None:
        return shifts.filter(clock_out_time__isnull=True).exists()
    return shifts.filter(clock_in_time__lte=at).filter(Q(clock_out_time__isnull=True) | Q(clock_out_time__gte=at)).exists()


def sign_off_blockers(booking):
    """What still stands between this job and "submit for sign-off" (empty list = ready)."""
    done = set(ProjectMilestone.objects.filter(booking=booking, phase__in=CREW_STAGES, completion_pct=100)
               .values_list('phase', flat=True))
    missing = [STAGE_LABELS.get(p, p) for p in CREW_STAGES if p not in done]
    blockers = [f'Finish: {", ".join(missing)}.'] if missing else []
    if not ProjectProgressMedia.objects.filter(project__booking=booking, construction_phase='AFTER').exists():
        blockers.append('Add at least one After photo of the finished garden.')
    return blockers


ON_MY_WAY_ETAS = (15, 30, 45, 60)
ON_MY_WAY_REPEAT_MINUTES = 20
JOB_PROBLEMS = ('Customer not home', 'Weather (rain or storm)', 'Missing plants or materials',
                'Site is different from the plan', 'Safety concern', 'Other')


def materials_for(design):
    """
    What to bring for a design: what is actually placed in it, counted per catalog item (items marked
    as already on site are skipped). Names and photos come from the catalog, so every design format
    works (AI layouts store plant_id, Studio designs store productId). Plants first, then the rest.
    """
    if not design or not design.placed_items:
        return []
    from .reviews import display_name   # here, not at the top: reviews → views_api → field_work would loop
    counts = Counter()
    for item in design.placed_items:
        if not isinstance(item, dict) or item.get('is_existing'):
            continue
        pid = item.get('plant_id') or item.get('productId') or item.get('product_id')
        if pid not in (None, ''):
            counts[str(pid)] += 1
    numeric = [int(p) for p in counts if str(p).isdigit()]
    catalog = {str(i.id): i for i in InventoryItem.objects.filter(id__in=numeric)}
    names = {str(b.get('plant_id')): b.get('name') for b in (design.plant_breakdown or []) if isinstance(b, dict)}
    rows = []
    for pid, qty in counts.items():
        item = catalog.get(pid)
        name = item.name if item else names.get(pid)
        if not name:
            continue
        rows.append({
            'product_id': pid, 'name': display_name(name), 'quantity': qty,
            'category': item.category if item else 'plant',
            'thumbnail': (item.thumbnail.url if item and item.thumbnail else (item.image_url if item else '')) or '',
        })
    order = {'plant': 0, 'hardscape': 1, 'furniture': 2}
    return sorted(rows, key=lambda r: (order.get(r['category'], 3), -r['quantity'], r['name']))
