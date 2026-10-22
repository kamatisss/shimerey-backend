"""
Seed realistic dummy business history for demos and the admin Income Analytics.

    python manage.py seed_demo_business              # 24 months of history (default)
    python manage.py seed_demo_business --months 12
    python manage.py seed_demo_business --reset      # remove previous demo data, then seed
    python manage.py seed_demo_business --clear      # remove demo data only

Everything created belongs to demo customer accounts whose email ends in
@demo.gardenstudio.test (unusable passwords, so nobody can log in as them).
--clear deletes those users; their bookings, trackers, orders, order items and
reviews cascade with them. Real customers, staff, inventory and stock levels
are never modified.

Shape of the data (Philippines, PHP):
  • Landscape bookings peak in the dry season (Dec–May) and dip in the rainy
    season (Jun–Oct); projects are priced via ProjectTracker.total_price.
  • Shop orders peak for Mother's Day (May) and the holidays (Nov–Dec).
  • Both streams grow ~2% per month so period-over-period deltas are visible.
  • Recent and upcoming work is left open (pending/shipped/installing) so the
    pending pipeline is populated.
"""
import random
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.library.models import (
    InventoryItem, Order, OrderItem, ProductReview, ProjectTracker,
    STATUS_PROGRESS_MAP, ServiceBooking, ServiceReview,
)

DEMO_DOMAIN = 'demo.gardenstudio.test'
SHIPPING_FEE = Decimal('150')

FIRST_NAMES = ['Maria', 'Jose', 'Ana', 'Juan', 'Carmela', 'Miguel', 'Liza', 'Ramon', 'Grace', 'Paolo',
               'Andrea', 'Mark', 'Kristine', 'Rafael', 'Joy', 'Carlo', 'Patricia', 'Enrico', 'Bea', 'Luis',
               'Rowena', 'Dennis', 'Camille', 'Arnel', 'Trisha', 'Gabriel', 'Nicole', 'Ronaldo', 'Faye', 'Jerome']
LAST_NAMES = ['Santos', 'Reyes', 'Cruz', 'Bautista', 'Ocampo', 'Garcia', 'Mendoza', 'Torres', 'Villanueva',
              'Ramos', 'Aquino', 'Castillo', 'Navarro', 'Dela Cruz', 'Gonzales', 'Lim', 'Tan', 'Flores',
              'Domingo', 'Salazar']
CITIES = ['Quezon City', 'Makati', 'Pasig', 'Taguig', 'Parañaque', 'Las Piñas', 'Muntinlupa', 'Marikina',
          'Antipolo', 'Cainta', 'Bacoor', 'Imus', 'Santa Rosa', 'Biñan', 'San Juan']
STREETS = ['Mabini St.', 'Rizal Ave.', 'Bonifacio St.', 'Acacia Lane', 'Narra St.', 'Sampaguita St.',
           'Molave Rd.', 'Ilang-Ilang St.', 'Kamagong St.', 'Banaba Drive']
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
            count = demo_users.count()
            demo_users.delete()
            self.stdout.write(f'Removed {count} demo customers and all of their data.')
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
        self.today = timezone.localdate()
        with transaction.atomic(), explicit_timestamps(
            ServiceBooking, ProjectTracker, Order, ServiceReview, ProductReview,
        ):
            customers = self._create_customers(140)
            bookings = self._create_bookings(customers, opts['months'])
            orders = self._create_orders(customers, products, opts['months'])

        self._report(bookings, orders)

    # ── helpers ──────────────────────────────────────────────────────────
    def _aware(self, day, hour=None):
        hour = self.rng.randint(8, 21) if hour is None else hour
        naive = datetime.combine(day, time(hour, self.rng.randint(0, 59)))
        return timezone.make_aware(naive, timezone.get_current_timezone())

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

    def _address(self):
        return f"{self.rng.randint(1, 250)} {self.rng.choice(STREETS)}, {self.rng.choice(CITIES)}, Metro Manila"

    def _phone(self):
        return f"09{self.rng.randint(10, 99)}{self.rng.randint(1000000, 9999999)}"

    # ── customers ────────────────────────────────────────────────────────
    def _create_customers(self, n):
        customers = []
        for i in range(1, n + 1):
            first, last = self.rng.choice(FIRST_NAMES), self.rng.choice(LAST_NAMES)
            user = User(username=f'demo_customer_{i:03d}', first_name=first, last_name=last,
                        email=f'demo_customer_{i:03d}@{DEMO_DOMAIN}')
            user.set_unusable_password()
            customers.append(user)
        User.objects.bulk_create(customers)
        return list(User.objects.filter(email__iendswith=f'@{DEMO_DOMAIN}'))

    # ── landscape bookings + project trackers + service reviews ─────────
    def _create_bookings(self, customers, months):
        start = self.today - timedelta(days=round(months * 30.4))
        end = self.today + timedelta(days=45)  # upcoming scheduled work = pipeline
        service_types = list(SERVICES)
        weights = [SERVICES[s][0] for s in service_types]

        planned = []  # (booking, price, tracker_status, remaining)
        day = start
        while day <= end:
            rate = 0.75 * LANDSCAPE_SEASON[day.month] * self._growth(min(day, self.today), start)
            for _ in range(self._poisson(rate)):
                service = self.rng.choices(service_types, weights)[0]
                _, lo, mode, hi = SERVICES[service]
                price = Decimal(round(self.rng.triangular(lo, hi, mode), -2))
                age = (self.today - day).days

                if age > 14:
                    status = 'Cancelled' if self.rng.random() < 0.10 else 'Finished'
                elif age >= 0:
                    status = self.rng.choices(['Finished', 'For Review', 'Installing', 'Preparing', 'Cancelled'],
                                              [32, 12, 28, 20, 8])[0]
                else:
                    status = self.rng.choices(['Pending', 'Preparing', 'Cancelled'], [62, 33, 5])[0]

                if status == 'Finished':
                    tracker_status = 'COMPLETED'
                    remaining = price * Decimal('0.3') if self.rng.random() < 0.06 else Decimal('0')
                elif status == 'For Review':
                    tracker_status = 'FINAL_INSPECTION'
                    remaining = price * Decimal('0.2')
                elif status == 'Installing':
                    tracker_status = self.rng.choice(['PLANTING', 'HARDSCAPE_INSTALLATION', 'SITE_PREPARATION'])
                    remaining = price * Decimal('0.5')
                elif status == 'Preparing':
                    tracker_status = self.rng.choice(['MATERIALS_PREPARATION', 'PROJECT_SCHEDULED', 'DESIGN_APPROVED'])
                    remaining = price * Decimal('0.7')
                elif status == 'Pending':
                    tracker_status = self.rng.choice(['CONSULTATION_SCHEDULED', 'SITE_INSPECTION', 'QUOTATION_APPROVED'])
                    remaining = price
                else:
                    tracker_status = None
                    remaining = None

                created = self._aware(day - timedelta(days=self.rng.randint(4, 25)))
                planned.append((
                    ServiceBooking(
                        user=self.rng.choice(customers), service_type=service, scheduled_date=day,
                        status=status, contact_number=self._phone(), preferred_time=self.rng.choice(TIMES),
                        service_address=self._address(), notes='Demo booking',
                        progress_pct=100 if status == 'Finished' else 0,
                        created_at=created,
                        updated_at=self._aware(min(day, self.today)) if status == 'Finished' else created,
                    ),
                    price, tracker_status, remaining,
                ))
            day += timedelta(days=1)

        bookings = ServiceBooking.objects.bulk_create([p[0] for p in planned])

        trackers, reviews = [], []
        for booking, (_, price, tracker_status, remaining) in zip(bookings, planned):
            if tracker_status:
                trackers.append(ProjectTracker(
                    booking=booking, status=tracker_status,
                    progress_percentage=STATUS_PROGRESS_MAP.get(tracker_status, 0),
                    total_price=price, remaining_balance=remaining.quantize(Decimal('1')),
                    estimated_end_date=booking.scheduled_date + timedelta(days=self.rng.randint(1, 14)),
                    created_at=booking.created_at, updated_at=booking.updated_at,
                ))
            if booking.status == 'Finished' and self.rng.random() < 0.45:
                rating = self._rating()
                reviews.append(ServiceReview(
                    booking=booking, user=booking.user, rating=rating,
                    comment=self.rng.choice(SERVICE_COMMENTS[rating]),
                    created_at=self._aware(min(booking.scheduled_date + timedelta(days=self.rng.randint(1, 10)), self.today)),
                ))
        ProjectTracker.objects.bulk_create(trackers)
        ServiceReview.objects.bulk_create(reviews)
        return bookings

    # ── shop orders + items + product reviews ───────────────────────────
    def _create_orders(self, customers, products, months):
        start = self.today - timedelta(days=round(months * 30.4))
        # Cheaper plants sell in bigger quantities; furniture mostly one at a time
        max_qty = {'plant': 6, 'hardscape': 4, 'furniture': 2}

        planned = []  # (order, [(product, qty)])
        day = start
        while day <= self.today:
            rate = 1.7 * SHOP_SEASON[day.month] * self._growth(day, start)
            for _ in range(self._poisson(rate)):
                customer = self.rng.choice(customers)
                picks = self.rng.sample(products, k=min(len(products), self.rng.choices([1, 2, 3, 4], [45, 30, 17, 8])[0]))
                lines = [(p, self.rng.randint(1, max_qty.get(p.category, 3))) for p in picks]
                total = sum((p.unit_price * q for p, q in lines), Decimal('0')) + SHIPPING_FEE
                method = self.rng.choices(['cod', 'online'], [55, 45])[0]

                age = (self.today - day).days
                if age > 10:
                    status = 'Cancelled' if self.rng.random() < 0.08 else 'Delivered'
                elif age > 3:
                    status = self.rng.choices(['Delivered', 'Out for Delivery', 'Shipped', 'Cancelled'], [50, 20, 25, 5])[0]
                else:
                    status = self.rng.choices(['Pending', 'Shipped'], [75, 25])[0]
                if status == 'Pending' and method == 'online':
                    status = 'Paid'

                paid = status != 'Cancelled' and (method == 'online' or status == 'Delivered')
                planned.append((
                    Order(
                        user=customer, customer_name=f'{customer.first_name} {customer.last_name}',
                        customer_email=customer.email, customer_phone=self._phone(),
                        customer_address=self._address(), total_price=total, payment_method=method,
                        status=status, payment_status='Paid' if paid else 'Pending',
                        created_at=self._aware(day),
                    ),
                    lines,
                ))
            day += timedelta(days=1)

        orders = Order.objects.bulk_create([p[0] for p in planned])
        items = OrderItem.objects.bulk_create([
            OrderItem(order=order, item=product, quantity=qty, price_at_booking=product.unit_price)
            for order, (_, lines) in zip(orders, planned)
            for product, qty in lines
        ])

        reviews = []
        for line in items:
            order = line.order
            if order.status == 'Delivered' and self.rng.random() < 0.22:
                rating = self._rating()
                reviews.append(ProductReview(
                    order_item=line, product=line.item, user=order.user, rating=rating,
                    comment=self.rng.choice(PRODUCT_COMMENTS[rating]),
                    created_at=min(order.created_at + timedelta(days=self.rng.randint(4, 14)), timezone.now()),
                ))
        ProductReview.objects.bulk_create(reviews)
        return orders

    def _report(self, bookings, orders):
        finished = [b for b in bookings if b.status == 'Finished']
        delivered = [o for o in orders if o.status == 'Delivered']
        self.stdout.write(self.style.SUCCESS(
            f"\nSeeded demo business data ({DEMO_DOMAIN}):\n"
            f"  {len(bookings)} landscape bookings ({len(finished)} finished) with project trackers\n"
            f"  {len(orders)} shop orders ({len(delivered)} delivered)\n"
            f"  {ServiceReview.objects.filter(user__email__iendswith=DEMO_DOMAIN).count()} service reviews, "
            f"{ProductReview.objects.filter(user__email__iendswith=DEMO_DOMAIN).count()} product reviews\n"
            f"Remove it any time with: python manage.py seed_demo_business --clear"
        ))
