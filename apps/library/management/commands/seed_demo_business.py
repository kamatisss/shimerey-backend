"""
Seed realistic dummy business history for demos, the Business overview, Reports and the Projects page.

    python manage.py seed_demo_business              # 24 months of history (default)
    python manage.py seed_demo_business --months 12
    python manage.py seed_demo_business --reset      # remove previous demo data, then seed
    python manage.py seed_demo_business --clear      # remove demo data only

Everything created belongs to demo accounts whose email ends in @demo.gardenstudio.test (customers
and six field crew, all with unusable passwords, so nobody can log in as them), plus anonymous
Studio visits whose session id starts with "demo-". --clear deletes exactly those: the demo crew's
cash handovers first (they protect the crew), then the demo users, whose bookings, trackers,
payments, history, crew stages, clock-ins, orders, order items and reviews cascade with them.
Real customers, staff, inventory and stock levels are never modified. Nothing is sent: rows are
written straight to the database, so no e-mail, push alert or payment ever goes out.

Shape of the data (Quezon Province, PHP):
  • Customers live in the service area (Lucena, Pagbilao, Tayabas, Sariaya …) with real map pins
    from the app's own town list, so the areas report and the crew's site pins work.
  • Landscape bookings peak in the dry season (Dec–May) and dip in the rainy season (Jun–Oct);
    shop orders peak for Mother's Day (May) and the holidays (Nov–Dec). Both grow ~2% a month.
  • Dates are consistent and never in the future: a request is made 4–25 days before its visit,
    a job is signed off (finished_at) after a realistic length of work, online orders are paid
    minutes after ordering, delivered orders carry their delivery day and crew member.
  • Money is recorded the way the app records it: project payments (a downpayment on confirming,
    the balance after sign-off) add up exactly to each project's price minus its balance; cash on
    delivery is handed over to the office in batches (the last couple of days' cash is still with
    the crew); a few paid online orders are refunded after cancelling.
  • Crew work: crew assigned to confirmed jobs (a few left for the office to assign), crew stages
    by job status, clock-in / clock-out with GPS on site, and the office stage history.
  • Recent and upcoming work is left open, so the pipeline, queues and Projects page have content.
"""
import random
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.library.addresses import PROVINCE, areas, format_address
from apps.library.models import (
    Attendance, CashHandover, FunnelEvent, InventoryItem, Order, OrderItem, ProductReview, ProjectHistoryLog,
    ProjectMilestone, ProjectPayment, ProjectTracker, STATUS_PROGRESS_MAP, ServiceBooking, ServiceReview,
    StaffProfile,
)

DEMO_DOMAIN = 'demo.gardenstudio.test'
DEMO_SESSION_PREFIX = 'demo-'
SHIPPING_FEE = Decimal('150')

FIRST_NAMES = ['Maria', 'Jose', 'Ana', 'Juan', 'Carmela', 'Miguel', 'Liza', 'Ramon', 'Grace', 'Paolo',
               'Andrea', 'Mark', 'Kristine', 'Rafael', 'Joy', 'Carlo', 'Patricia', 'Enrico', 'Bea', 'Luis',
               'Rowena', 'Dennis', 'Camille', 'Arnel', 'Trisha', 'Gabriel', 'Nicole', 'Ronaldo', 'Faye', 'Jerome']
LAST_NAMES = ['Santos', 'Reyes', 'Cruz', 'Bautista', 'Ocampo', 'Garcia', 'Mendoza', 'Torres', 'Villanueva',
              'Ramos', 'Aquino', 'Castillo', 'Navarro', 'Dela Cruz', 'Gonzales', 'Lim', 'Tan', 'Flores',
              'Domingo', 'Salazar']
CREW = [('Rodel', 'Manalo'), ('Jun', 'Pascual'), ('Edwin', 'Abrigo'), ('Noel', 'Racelis'), ('Ariel', 'Dimaculangan'),
        ('Benjie', 'Salvacion')]
# The service area around Pagbilao: the busiest towns first (weights = share of customers)
TOWNS = {'Lucena City': 30, 'Pagbilao': 18, 'Tayabas City': 14, 'Sariaya': 10, 'Candelaria': 6, 'Lucban': 5,
         'Tiaong': 4, 'Mauban': 4, 'Atimonan': 3, 'Padre Burgos': 3, 'Unisan': 2, 'Agdangan': 1}
STREETS = ['Rizal St.', 'Mabini St.', 'Quezon Ave.', 'Gomez St.', 'Purok 1', 'Purok 2', 'Purok 3', 'Purok 4',
           'Sitio Malinis', 'Sampaguita St.', 'Narra St.', 'Acacia Lane', 'Maharlika Hwy.', 'Diversion Rd.']
LANDMARKS = ['', '', '', 'near the chapel', 'beside the sari-sari store', 'across the barangay hall',
             'near the elementary school', 'blue gate', 'after the basketball court']
TIMES = ['8:00 AM - 10:00 AM', '10:00 AM - 12:00 PM', '1:00 PM - 3:00 PM', '3:00 PM - 5:00 PM']

# Seasonal demand multipliers by calendar month (1 = January)
LANDSCAPE_SEASON = {1: 1.15, 2: 1.2, 3: 1.3, 4: 1.25, 5: 1.1, 6: 0.85,
                    7: 0.7, 8: 0.7, 9: 0.75, 10: 0.85, 11: 0.95, 12: 1.0}
SHOP_SEASON = {1: 0.8, 2: 0.9, 3: 1.0, 4: 1.05, 5: 1.25, 6: 1.0,
               7: 1.05, 8: 0.95, 9: 0.95, 10: 1.0, 11: 1.2, 12: 1.45}

# service_type → (mix weight, min price, typical price, max price)
SERVICES = {
    'consultation': (0.30, 1500, 2500, 4500),
    'maintenance':  (0.35, 3500, 6500, 15000),
    'Softscape':    (0.20, 18000, 38000, 95000),
    'hardscaping':  (0.15, 45000, 95000, 280000),
}
# Days of work after the visit date (min, max), and how many crew it takes (min, max)
WORK_DAYS = {'consultation': (0, 0), 'maintenance': (0, 1), 'Softscape': (2, 5), 'hardscaping': (4, 14)}
CREW_SIZE = {'consultation': (1, 1), 'maintenance': (1, 1), 'Softscape': (2, 2), 'hardscaping': (2, 3)}
# Hours on site per day of work (min, max)
SHIFT_HOURS = {'consultation': (1.0, 2.0), 'maintenance': (2.5, 4.0), 'Softscape': (7.0, 8.5), 'hardscaping': (8.0, 9.0)}
# The office pipeline each kind of job goes through (ProjectTracker.status), in order
PATHS = {
    'consultation': ['CONSULTATION_SCHEDULED', 'SITE_INSPECTION', 'FINAL_INSPECTION', 'COMPLETED'],
    'maintenance': ['PROJECT_SCHEDULED', 'PROJECT_STARTED', 'FINAL_INSPECTION', 'COMPLETED'],
    'Softscape': ['CONSULTATION_SCHEDULED', 'SITE_INSPECTION', 'QUOTATION_APPROVED', 'DESIGN_APPROVED',
                  'PROJECT_SCHEDULED', 'MATERIALS_PREPARATION', 'SITE_PREPARATION', 'PLANTING',
                  'FINAL_INSPECTION', 'COMPLETED'],
    'hardscaping': ['CONSULTATION_SCHEDULED', 'SITE_INSPECTION', 'QUOTATION_APPROVED', 'DESIGN_APPROVED',
                    'PROJECT_SCHEDULED', 'MATERIALS_PREPARATION', 'SITE_PREPARATION', 'HARDSCAPE_INSTALLATION',
                    'FINAL_INSPECTION', 'COMPLETED'],
}
# How far along the office pipeline a booking status sits (fraction of the path)
STAGE_SPAN = {'Pending': (0.0, 0.3), 'Preparing': (0.35, 0.6), 'Installing': (0.65, 0.85)}
DESIGNED = ('Softscape', 'hardscaping')   # jobs that start with a design and contract
PAYMENT_METHODS = [('online', 30), ('cash', 30), ('ewallet', 25), ('bank', 15)]

SERVICE_COMMENTS = {
    5: ['The crew was on time, professional, and the garden looks amazing!',
        'Exceeded our expectations. The 3D preview matched the final result.',
        'Very neat work and they cleaned up everything after. Highly recommended.'],
    4: ['Great result overall, just a small delay on the second day.',
        'Good workmanship and friendly team. Will book maintenance again.'],
    3: ['Decent job but communication about the schedule could be better.',
        'Okay result. Some plants needed replacing after a few weeks.'],
    2: ['The project took longer than promised and the site was left messy.'],
    1: ['Very disappointed with the delays and the quality of the work.'],
}
PRODUCT_COMMENTS = {
    5: ['Healthy and well-packed. Arrived in perfect condition!',
        'Exactly as pictured. Already thriving in our garden.',
        'Great quality for the price, will order again.'],
    4: ['Good quality, delivery took a day longer than expected.',
        'Nice item, packaging could be a bit better.'],
    3: ['A few leaves were damaged on arrival but it recovered.',
        'Okay, smaller than I expected.'],
    2: ['Arrived with some damage. Customer service was helpful though.'],
    1: ['Item arrived in poor condition.'],
}
RATING_WEIGHTS = [(5, 50), (4, 32), (3, 11), (2, 5), (1, 2)]

# Studio funnel: visits per day, and the chance of each next step (the funnel card's order)
STUDIO_DAYS = 90
STUDIO_VISITS_PER_DAY = 9


@contextmanager
def explicit_timestamps(*models):
    """Let bulk_create keep the historical created_at/updated_at values we set."""
    saved = []
    for model in models:
        for field in model._meta.fields:
            if getattr(field, 'auto_now_add', False) or getattr(field, 'auto_now', False):
                saved.append((field, field.auto_now_add, field.auto_now))
                field.auto_now_add = field.auto_now = False
    try:
        yield
    finally:
        for field, auto_now_add, auto_now in saved:
            field.auto_now_add, field.auto_now = auto_now_add, auto_now


def clear_demo():
    """Delete every demo record. Returns (users removed, Studio funnel events removed)."""
    demo_users = User.objects.filter(email__iendswith=f'@{DEMO_DOMAIN}')
    CashHandover.objects.filter(crew__in=demo_users).delete()        # PROTECT on crew
    visits = FunnelEvent.objects.filter(session_id__startswith=DEMO_SESSION_PREFIX).delete()[0]
    return demo_users.delete()[1].get('auth.User', 0), visits


class Command(BaseCommand):
    help = 'Seed realistic dummy landscaping + shop history (removable with --clear)'

    def add_arguments(self, parser):
        parser.add_argument('--months', type=int, default=24, help='Months of history to generate (default 24)')
        parser.add_argument('--seed', type=int, default=42, help='Random seed for reproducible data')
        parser.add_argument('--clear', action='store_true', help='Delete demo data and exit')
        parser.add_argument('--reset', action='store_true', help='Delete existing demo data before seeding')

    def handle(self, *args, **opts):
        demo_users = User.objects.filter(email__iendswith=f'@{DEMO_DOMAIN}')

        if opts['clear'] or opts['reset']:
            users, visits = clear_demo()
            self.stdout.write(f'Removed {users} demo accounts and all of their data, and {visits} demo Studio funnel events.')
            if opts['clear']:
                return
        elif demo_users.exists():
            raise CommandError('Demo data already exists. Use --reset to regenerate it or --clear to remove it.')

        if opts['months'] < 1:
            raise CommandError('--months must be at least 1.')
        products = list(InventoryItem.objects.all())
        if not products:
            raise CommandError('No inventory items found. Add shop products first.')

        self.rng = random.Random(opts['seed'])
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.towns = self._towns()
        # Payments and cash handovers are received by a real office account when there is one
        self.office = (User.objects.filter(staff_profile__role='OFFICE_ADMIN', is_active=True)
                       .exclude(email__iendswith=f'@{DEMO_DOMAIN}').order_by('id').first()
                       or User.objects.filter(is_superuser=True, is_active=True).order_by('id').first())
        with transaction.atomic(), explicit_timestamps(
            ServiceBooking, ProjectTracker, Order, ServiceReview, ProductReview, ProjectHistoryLog,
            ProjectMilestone, ProjectPayment, CashHandover, FunnelEvent,
        ):
            customers = self._create_customers(140)
            self.crew = self._create_crew()
            bookings = self._create_bookings(customers, opts['months'])
            orders = self._create_orders(customers, products, opts['months'])
            visits = self._create_studio_visits()

        self._report(bookings, orders, visits)

    # ── helpers ──────────────────────────────────────────────────────────
    def _aware(self, day, hour=None, minute=None):
        """A moment on a local day; never later than now (today's times are pulled back)."""
        hour = self.rng.randint(8, 21) if hour is None else hour
        minute = self.rng.randint(0, 59) if minute is None else minute
        moment = timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())
        return self._past(moment)

    def _past(self, moment):
        if moment <= self.now:
            return moment
        return self.now - timedelta(minutes=self.rng.randint(5, 90))

    def _growth(self, day, start):
        return 1.022 ** ((day - start).days / 30.4)

    def _poisson(self, lam):
        # Knuth — fine for the small daily rates used here
        limit, k, p = pow(2.718281828, -lam), 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1

    def _rating(self):
        return self.rng.choices([r for r, _ in RATING_WEIGHTS], [w for _, w in RATING_WEIGHTS])[0]

    def _phone(self):
        return f"09{self.rng.randint(10, 99)}{self.rng.randint(1000000, 9999999)}"

    def _towns(self):
        by_name = {c['name']: c for c in areas()['cities']}
        towns = [(by_name[name], weight) for name, weight in TOWNS.items() if name in by_name]
        if not towns:
            raise CommandError('The Quezon town list is missing (apps/library/data/quezon_areas.json).')
        return towns

    def _address(self):
        """A structured address in the service area with a map pin near the barangay (or town) centre."""
        town = self.rng.choices([t for t, _ in self.towns], [w for _, w in self.towns])[0]
        barangay = self.rng.choice(town['barangays'])
        base_lat, base_lng = barangay.get('lat') or town['lat'], barangay.get('lng') or town['lng']
        spread = 0.004 if barangay.get('lat') else 0.012
        detail = {
            'province': PROVINCE, 'city_code': town['code'], 'city': town['name'],
            'barangay_code': barangay['code'], 'barangay': barangay['name'],
            'street': f"{self.rng.randint(1, 250)} {self.rng.choice(STREETS)}",
            'landmark': self.rng.choice(LANDMARKS),
            'lat': round(base_lat + self.rng.uniform(-spread, spread), 6),
            'lng': round(base_lng + self.rng.uniform(-spread, spread), 6),
            'source': 'pin',
        }
        return detail, format_address(detail)

    def _near(self, lat, lng):
        """A GPS fix within ~30 m of a site (inside the crew's 50 m check-in radius)."""
        return round(lat + self.rng.uniform(-0.00025, 0.00025), 6), round(lng + self.rng.uniform(-0.00025, 0.00025), 6)

    # ── people ───────────────────────────────────────────────────────────
    def _create_customers(self, n):
        customers = []
        for i in range(1, n + 1):
            first, last = self.rng.choice(FIRST_NAMES), self.rng.choice(LAST_NAMES)
            user = User(username=f'demo_customer_{i:03d}', first_name=first, last_name=last,
                        email=f'demo_customer_{i:03d}@{DEMO_DOMAIN}')
            user.set_unusable_password()
            customers.append(user)
        User.objects.bulk_create(customers)
        return list(User.objects.filter(email__iendswith=f'@{DEMO_DOMAIN}', is_staff=False))

    def _create_crew(self):
        crew = []
        for i, (first, last) in enumerate(CREW, start=1):
            user = User(username=f'demo_crew_{i}', first_name=first, last_name=last, is_staff=True,
                        email=f'demo_crew_{i}@{DEMO_DOMAIN}')
            user.set_unusable_password()
            crew.append(user)
        User.objects.bulk_create(crew)
        crew = list(User.objects.filter(email__iendswith=f'@{DEMO_DOMAIN}', is_staff=True).order_by('username'))
        StaffProfile.objects.bulk_create([StaffProfile(user=u, role='FIELD_CREW') for u in crew])
        return crew

    # ── landscape bookings, projects, payments, crew work, reviews ──────
    def _booking_status(self, age):
        if age > 14:
            return 'Cancelled' if self.rng.random() < 0.10 else 'Finished'
        if age >= 0:
            return self.rng.choices(['Finished', 'For Review', 'Installing', 'Preparing', 'Cancelled'],
                                    [40, 6, 26, 20, 8])[0]
        return self.rng.choices(['Pending', 'Preparing', 'Cancelled'], [62, 33, 5])[0]

    def _tracker_status(self, service, status):
        path = PATHS[service]
        if status == 'Finished':
            return path[-1]
        if status == 'For Review':
            return path[-2]
        lo, hi = STAGE_SPAN[status]
        index = round(self.rng.uniform(lo, hi) * (len(path) - 1))
        return path[min(max(index, 0), len(path) - 2)]

    def _create_bookings(self, customers, months):
        start = self.today - timedelta(days=round(months * 30.4))
        end = self.today + timedelta(days=45)  # upcoming scheduled work = pipeline
        service_types = list(SERVICES)
        weights = [SERVICES[s][0] for s in service_types]

        planned = []
        day = start
        while day <= end:
            rate = 0.75 * LANDSCAPE_SEASON[day.month] * self._growth(min(day, self.today), start)
            for _ in range(self._poisson(rate)):
                service = self.rng.choices(service_types, weights)[0]
                _, lo, mode, hi = SERVICES[service]
                price = Decimal(round(self.rng.triangular(lo, hi, mode), -2))
                status = self._booking_status((self.today - day).days)
                detail, text = self._address()
                # Requested 4–25 days before the visit, and never later than now
                created = self._aware(min(day - timedelta(days=self.rng.randint(4, 25)), self.today))
                work_days = self.rng.randint(*WORK_DAYS[service])
                finished_at = None
                if status == 'Finished':
                    signed = day + timedelta(days=work_days + self.rng.randint(0, 2))
                    finished_at = self._aware(min(signed, self.today), self.rng.randint(14, 18))
                    finished_at = max(finished_at, created + timedelta(hours=1))
                planned.append({
                    'booking': ServiceBooking(
                        user=self.rng.choice(customers), service_type=service, scheduled_date=day,
                        status=status, contact_number=self._phone(), preferred_time=self.rng.choice(TIMES),
                        service_address=text, address_detail=detail, site_lat=detail['lat'], site_lng=detail['lng'],
                        notes='Demo booking', created_at=created, finished_at=finished_at,
                        updated_at=finished_at or created,
                    ),
                    'price': price, 'work_days': work_days,
                })
            day += timedelta(days=1)

        bookings = ServiceBooking.objects.bulk_create([p['booking'] for p in planned])
        trackers, reviews = [], []
        for booking, p in zip(bookings, planned):
            p['booking'] = booking
            p['crew'] = self._pick_crew(booking)
            p['stages'] = self._stages(booking, p)
            if booking.status != 'Cancelled':
                tracker_status = self._tracker_status(booking.service_type, booking.status)
                p['remaining'] = self._remaining(booking.status, p['price'])
                trackers.append(ProjectTracker(
                    booking=booking, status=tracker_status,
                    progress_percentage=STATUS_PROGRESS_MAP.get(tracker_status, 0),
                    total_price=p['price'], remaining_balance=p['remaining'],
                    estimated_end_date=booking.scheduled_date + timedelta(days=p['work_days'] + self.rng.randint(1, 4)),
                    created_at=booking.created_at, updated_at=booking.updated_at,
                ))
            if booking.status == 'Finished' and self.rng.random() < 0.45:
                rating = self._rating()
                reviews.append(ServiceReview(
                    booking=booking, user=booking.user, rating=rating,
                    comment=self.rng.choice(SERVICE_COMMENTS[rating]),
                    created_at=self._past(booking.finished_at + timedelta(days=self.rng.randint(1, 10))),
                ))
        ProjectTracker.objects.bulk_create(trackers)
        ServiceReview.objects.bulk_create(reviews)

        by_booking = {t.booking_id: t for t in ProjectTracker.objects.filter(booking__in=bookings)}
        self._assign_crew(planned)
        self._create_stages(planned)
        self._create_clock_ins(planned)
        self._create_history(planned, by_booking)
        self._create_project_payments(planned, by_booking)
        return bookings

    def _remaining(self, status, price):
        """What the customer still owes. Finished jobs are mostly paid up; open ones paid a downpayment."""
        if status == 'Finished':
            share = Decimal('0.3') if self.rng.random() < 0.06 else Decimal('0')
        elif status == 'For Review':
            share = Decimal('0.2')
        elif status == 'Installing':
            share = Decimal('0.5')
        elif status == 'Preparing':
            share = Decimal('0.5') if self.rng.random() < 0.7 else Decimal('1')   # some haven't paid yet
        else:
            share = Decimal('1')
        return (price * share).quantize(Decimal('1'))

    def _pick_crew(self, booking):
        if booking.status in ('Pending', 'Cancelled'):
            return []
        # A few confirmed jobs are left for the office to assign (the "Needs crew" queue)
        if booking.status == 'Preparing' and self.rng.random() < 0.2:
            return []
        lo, hi = CREW_SIZE[booking.service_type]
        return self.rng.sample(self.crew, k=self.rng.randint(lo, hi))

    def _assign_crew(self, planned):
        through = ServiceBooking.assigned_crew.through
        through.objects.bulk_create([through(servicebooking_id=p['booking'].id, user_id=u.id)
                                     for p in planned for u in p['crew']])

    def _work_moment(self, day, hour):
        return self._aware(day, hour, self.rng.randint(0, 59))

    def _stages(self, booking, p):
        """[(phase, completed moment)] for the crew stages (and the office's design step) done so far."""
        day, last = booking.scheduled_date, booking.scheduled_date + timedelta(days=p['work_days'])
        done = []
        if booking.service_type in DESIGNED and booking.status in ('Preparing', 'Installing', 'For Review', 'Finished'):
            done.append(('design_contract', self._past(booking.created_at + timedelta(days=self.rng.randint(1, 3)))))
        if booking.status in ('Installing', 'For Review', 'Finished') and day <= self.today:
            done.append(('site_prep', self._work_moment(day, 11)))
            mid = min(day + timedelta(days=max(0, p['work_days'] // 2)), self.today)
            if booking.status != 'Installing' or self.rng.random() < 0.4:
                done.append(('installation', self._work_moment(mid, 15)))
            if booking.status in ('For Review', 'Finished'):
                end = booking.finished_at or self._work_moment(min(last, self.today), 16)
                done.append(('final_inspection', min(end, self._work_moment(min(last, self.today), 16))))
        return done

    def _create_stages(self, planned):
        rows = []
        for p in planned:
            b = p['booking']
            for phase, when in p['stages']:
                rows.append(ProjectMilestone(booking=b, phase=phase, completion_pct=100, updated_at=when,
                                             notes='Demo' if phase == 'design_contract' else ''))
            # Booking progress = share of the four active phases done (the crew app's rule); finished = 100
            active = sum(1 for phase, _ in p['stages'] if phase in ProjectMilestone.ACTIVE_PHASES)
            b.progress_pct = 100 if b.status == 'Finished' else int(active / len(ProjectMilestone.ACTIVE_PHASES) * 100)
        ProjectMilestone.objects.bulk_create(rows)
        ServiceBooking.objects.bulk_update([p['booking'] for p in planned], ['progress_pct'], batch_size=500)

    def _create_clock_ins(self, planned):
        """Each crew member clocks in and out on site for each day of work done so far. A crew member's
        day is filled in order (7:30 AM to 6 PM, 30 minutes' travel between sites), so nobody is ever on
        two jobs at once; a visit that no longer fits that day isn't clocked. Today's shift is still open
        if it hasn't ended yet; no older shift is ever left open (those are flagged as forgotten)."""
        tz = timezone.get_current_timezone()
        free_from = {}                     # (crew id, day) → when that crew member is next free
        day_start, day_end, travel = time(7, 30), time(18, 0), timedelta(minutes=30)
        rows = []
        for p in sorted(planned, key=lambda p: (p['booking'].scheduled_date, p['booking'].id)):
            b = p['booking']
            if not p['crew'] or b.status not in ('Installing', 'For Review', 'Finished') or b.scheduled_date > self.today:
                continue
            last = min(b.scheduled_date + timedelta(days=p['work_days']), self.today,
                       timezone.localdate(b.finished_at) if b.finished_at else self.today)
            hours = SHIFT_HOURS[b.service_type]
            for i in range((last - b.scheduled_date).days + 1):
                day = b.scheduled_date + timedelta(days=i)
                for member in p['crew']:
                    start = free_from.get((member.id, day)) or timezone.make_aware(datetime.combine(day, day_start), tz)
                    start += timedelta(minutes=self.rng.randint(0, 20))
                    out = start + timedelta(hours=self.rng.uniform(*hours))
                    if out > timezone.make_aware(datetime.combine(day, day_end), tz) or start > self.now:
                        continue
                    free_from[(member.id, day)] = out + travel
                    lat, lng = self._near(b.site_lat, b.site_lng)
                    done = out <= self.now
                    rows.append(Attendance(
                        staff=member, booking=b, clock_in_time=start, clock_out_time=out if done else None,
                        latitude=lat, longitude=lng, clock_in_address=b.service_address,
                        clock_out_address=b.service_address if done else None,
                    ))
        Attendance.objects.bulk_create(rows, batch_size=1000)

    def _create_history(self, planned, trackers):
        """The office stage history: one entry per pipeline step reached, spread between the request and
        the sign-off (or now)."""
        rows = []
        for p in planned:
            b = p['booking']
            t = trackers.get(b.id)
            if not t:
                continue
            path = PATHS[b.service_type]
            steps = path[:path.index(t.status) + 1]
            begin = b.created_at
            end = b.finished_at or self._past(self._aware(min(b.scheduled_date, self.today), 17))
            end = min(max(end, begin + timedelta(hours=1)), self.now)   # a request made in the last hour
            span = max(0.0, (end - begin).total_seconds())
            for i, step in enumerate(steps):
                when = begin + timedelta(seconds=span * (i / max(1, len(steps) - 1)))
                rows.append(ProjectHistoryLog(project=t, status_reached=step, trigger_user=self.office,
                                              remarks='Signed off by office admin.' if step == 'COMPLETED' else '',
                                              timestamp=when))
        ProjectHistoryLog.objects.bulk_create(rows, batch_size=1000)

    def _create_project_payments(self, planned, trackers):
        """Payment rows that add up exactly to price − balance: a downpayment when the booking is confirmed,
        the rest after sign-off (or on the visit day for jobs still under way)."""
        rows = []
        methods = [m for m, _ in PAYMENT_METHODS]
        weights = [w for _, w in PAYMENT_METHODS]
        for p in planned:
            b = p['booking']
            t = trackers.get(b.id)
            if not t:
                continue
            paid = t.total_price - t.remaining_balance
            if paid <= 0:
                continue
            down = min(paid, (t.total_price * Decimal('0.5')).quantize(Decimal('1')))
            if b.service_type in ('consultation', 'maintenance'):
                down = paid                       # small jobs are paid in one go
            first = self._past(b.created_at + timedelta(days=self.rng.randint(1, 3), hours=self.rng.randint(0, 8)))
            parts = [(down, first)]
            if paid - down > 0:
                after = b.finished_at + timedelta(days=self.rng.randint(0, 5)) if b.finished_at \
                    else self._aware(min(b.scheduled_date, self.today), 16)
                parts.append((paid - down, self._past(max(after, first + timedelta(hours=1)))))
            for amount, when in parts:
                method = self.rng.choices(methods, weights)[0]
                rows.append(ProjectPayment(
                    project=t, amount=amount, method=method, received_by=None if method == 'online' else self.office,
                    note='Demo payment', paid_at=when, created_at=when,
                ))
        ProjectPayment.objects.bulk_create(rows, batch_size=1000)

    # ── shop orders, deliveries, cash handovers, refunds, reviews ───────
    def _create_orders(self, customers, products, months):
        start = self.today - timedelta(days=round(months * 30.4))
        # Cheaper plants sell in bigger quantities; furniture mostly one at a time
        max_qty = {'plant': 6, 'hardscape': 4, 'furniture': 2}

        planned = []  # (order, [(product, qty)])
        day = start
        while day <= self.today:
            rate = 1.7 * SHOP_SEASON[day.month] * self._growth(day, start)
            for _ in range(self._poisson(rate)):
                planned.append(self._order(day, customers, products, max_qty))
            day += timedelta(days=1)

        orders = Order.objects.bulk_create([p[0] for p in planned])
        items = OrderItem.objects.bulk_create([
            OrderItem(order=order, item=product, quantity=qty, price_at_booking=product.unit_price)
            for order, (_, lines) in zip(orders, planned)
            for product, qty in lines
        ])
        self._create_cash_handovers(orders)

        reviews = []
        for line in items:
            order = line.order
            if order.status == 'Delivered' and self.rng.random() < 0.22:
                rating = self._rating()
                reviews.append(ProductReview(
                    order_item=line, product=line.item, user=order.user, rating=rating,
                    comment=self.rng.choice(PRODUCT_COMMENTS[rating]),
                    created_at=self._past(order.delivered_at + timedelta(days=self.rng.randint(2, 12))),
                ))
        ProductReview.objects.bulk_create(reviews)
        return orders

    def _order(self, day, customers, products, max_qty):
        customer = self.rng.choice(customers)
        picks = self.rng.sample(products, k=min(len(products), self.rng.choices([1, 2, 3, 4], [45, 30, 17, 8])[0]))
        lines = [(p, self.rng.randint(1, max_qty.get(p.category, 3))) for p in picks]
        total = sum((p.unit_price * q for p, q in lines), Decimal('0')) + SHIPPING_FEE
        method = self.rng.choices(['cod', 'online'], [55, 45])[0]
        created = self._aware(day)

        age = (self.today - day).days
        if age > 10:
            status = 'Cancelled' if self.rng.random() < 0.08 else 'Delivered'
        elif age > 3:
            status = self.rng.choices(['Delivered', 'Out for Delivery', 'Shipped', 'Cancelled'], [50, 20, 25, 5])[0]
        else:
            status = self.rng.choices(['Pending', 'Shipped'], [75, 25])[0]
        if status == 'Pending' and method == 'online':
            status = 'Paid'

        paid_at = delivered_at = refunded_at = crew = None
        payment_status = 'Pending'
        if method == 'online':
            if status == 'Cancelled':
                # Most cancelled online orders were never paid (expired); some were paid, then refunded.
                # A paid one cancelled in the last few days still waits for its refund (the refund queue).
                if self.rng.random() < 0.35:
                    paid_at = self._past(created + timedelta(minutes=self.rng.randint(2, 30)))
                    if age > 3:
                        payment_status = 'Refunded'
                        refunded_at = self._past(paid_at + timedelta(days=self.rng.randint(1, 3)))
                    else:
                        payment_status = 'Paid'
                else:
                    payment_status = 'Expired'
            else:
                payment_status = 'Paid'
                paid_at = self._past(created + timedelta(minutes=self.rng.randint(2, 30)))
        if status in ('Delivered', 'Out for Delivery') or (status == 'Shipped' and self.rng.random() < 0.5):
            crew = self.rng.choice(self.crew)
        if status == 'Delivered':
            delivered_at = self._aware(min(day + timedelta(days=self.rng.randint(1, 5)), self.today),
                                       self.rng.randint(9, 17))
            delivered_at = max(delivered_at, created + timedelta(hours=1))
            if method == 'cod':
                payment_status = 'Paid'
        detail, text = self._address()
        order = Order(
            user=customer, customer_name=f'{customer.first_name} {customer.last_name}',
            customer_email=customer.email, customer_phone=self._phone(),
            customer_address=text, address_detail=detail, delivery_lat=detail['lat'], delivery_lng=detail['lng'],
            total_price=total, payment_method=method, status=status, payment_status=payment_status,
            paid_at=paid_at, delivered_at=delivered_at, refunded_at=refunded_at,
            delivery_crew=crew, delivered_by=crew if status == 'Delivered' else None, created_at=created,
        )
        return order, lines

    def _create_cash_handovers(self, orders):
        """Cash on delivery reaches the office in batches every 2–4 days; the last two days' cash is still
        with the crew. One count in twenty is a little short, with the reason written down."""
        cutoff = self.today - timedelta(days=2)
        by_crew = {}
        for o in orders:
            if o.payment_method == 'cod' and o.status == 'Delivered' and o.delivered_by_id:
                by_crew.setdefault(o.delivered_by_id, []).append(o)
        for crew_id, crew_orders in by_crew.items():
            crew_orders.sort(key=lambda o: o.delivered_at)
            batch, batch_end = [], None
            for o in crew_orders:
                day = timezone.localdate(o.delivered_at)
                if day > cutoff:
                    break                                       # still with the crew
                if batch and day > batch_end:
                    self._handover(crew_id, batch, batch_end)
                    batch = []
                if not batch:
                    batch_end = min(day + timedelta(days=self.rng.randint(1, 3)), cutoff)
                batch.append(o)
            if batch:
                self._handover(crew_id, batch, batch_end)

    def _handover(self, crew_id, batch, day):
        expected = sum((o.total_price for o in batch), Decimal('0'))
        short = Decimal(self.rng.choice([50, 100, 150, 200])) if self.rng.random() < 0.05 else Decimal('0')
        handover = CashHandover.objects.create(
            crew_id=crew_id, received_by=self.office, expected=expected, counted=expected - short,
            note='Customer was short on change; will settle on the next delivery.' if short else '',
            created_at=self._aware(day, 17, self.rng.randint(0, 59)),
        )
        Order.objects.filter(pk__in=[o.pk for o in batch]).update(cash_handover=handover)

    # ── free 3D Studio visits (anonymous funnel events) ──────────────────
    def _create_studio_visits(self):
        rows = []
        n = 0
        for back in range(STUDIO_DAYS, -1, -1):
            day = self.today - timedelta(days=back)
            for _ in range(self._poisson(STUDIO_VISITS_PER_DAY * (1.15 if day.weekday() >= 5 else 1.0))):
                n += 1
                session = f'{DEMO_SESSION_PREFIX}{self.rng.getrandbits(64):016x}'
                moment = self._aware(day)
                steps = ['studio_opened']
                if self.rng.random() < 0.62:
                    steps.append('first_item_placed')
                    if self.rng.random() < 0.40:
                        steps.append('ai_teaser_clicked')
                    if self.rng.random() < 0.38:
                        steps.append('save_clicked')
                        roll = self.rng.random()
                        if roll < 0.42:
                            steps.append('account_created')
                        elif roll < 0.55:
                            steps.append('signed_in')
                for i, name in enumerate(steps):
                    rows.append(FunnelEvent(name=name, session_id=session,
                                            created_at=self._past(moment + timedelta(minutes=2 * i))))
        FunnelEvent.objects.bulk_create(rows, batch_size=1000)
        return n

    def _report(self, bookings, orders, visits):
        finished = sum(1 for b in bookings if b.status == 'Finished')
        delivered = sum(1 for o in orders if o.status == 'Delivered')
        demo = f'@{DEMO_DOMAIN}'
        self.stdout.write(self.style.SUCCESS(
            f"\nSeeded demo business data ({DEMO_DOMAIN}):\n"
            f"  {len(self.crew)} field crew (can't sign in)\n"
            f"  {len(bookings)} landscape bookings ({finished} finished) with projects, "
            f"{ProjectPayment.objects.filter(project__booking__user__email__iendswith=demo).count()} project payments, "
            f"{Attendance.objects.filter(staff__email__iendswith=demo).count()} clock-ins\n"
            f"  {len(orders)} shop orders ({delivered} delivered), "
            f"{CashHandover.objects.filter(crew__email__iendswith=demo).count()} cash handovers\n"
            f"  {ServiceReview.objects.filter(user__email__iendswith=DEMO_DOMAIN).count()} service reviews, "
            f"{ProductReview.objects.filter(user__email__iendswith=DEMO_DOMAIN).count()} product reviews\n"
            f"  {visits} Studio visits\n"
            f"Remove it any time with: python manage.py seed_demo_business --clear"
        ))
