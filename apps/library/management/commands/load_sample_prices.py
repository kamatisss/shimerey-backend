"""
Load realistic SAMPLE prices for the catalog (demo / development only).

    python manage.py load_sample_prices            # apply
    python manage.py load_sample_prices --dry-run  # only show what would change

The test catalog had placeholder prices (a geranium dearer than a palm, a croton at ₱35), which made
every design and estimate look wrong. These are rough Philippine nursery / garden-supply retail
prices (late 2026, Quezon Province) for the sizes our models show. They are ESTIMATES: the owner sets
the real prices in Inventory. Products are matched by exact name; anything else is left alone.
Deliberately a command, not a migration, so it never runs on the live server by itself.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.library import activity
from apps.library.models import InventoryItem

SAMPLE_PRICES = {
    # Plants (the established size our 3D models show)
    'Bamboo': 900,                # a clump, ornamental
    'Banana': 250,                # a sucker / young plant
    'Christmas Palm': 1200,       # Adonidia, 1.5–2 m
    'Codiaeum variegatum': 150,   # croton
    'Geranium': 120,
    'Heliconia rostrata': 350,
    'Peace Lily': 250,
    'Plumeria': 900,              # 1.5–2 m
    'Strelitzia': 550,            # bird of paradise
    # Accent boulders
    'Bluish river rock': 900,
    'Mossy rock': 1200,
    'Volcanic rock': 800,
    # Furniture
    'Bamboo bench': 4500,
    'Outdoor bench': 9500,        # 3-seater, cast iron and wood
    'Rocking chair': 4500,
}


class Command(BaseCommand):
    help = 'Load realistic sample catalog prices (demo only; the owner sets real prices in Inventory).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show the changes without saving them')

    def handle(self, *args, **options):
        dry = options['dry_run']
        changed, missing = [], []
        with transaction.atomic():
            for name, price in SAMPLE_PRICES.items():
                item = InventoryItem.objects.filter(name=name).first()
                if item is None:
                    missing.append(name)
                    continue
                new = Decimal(price)
                if item.unit_price == new:
                    continue
                changed.append((name, item.unit_price, new))
                if not dry:
                    item.unit_price = new
                    item.save(update_fields=['unit_price'])
            if changed and not dry:
                activity.record(None, 'stock', 'sample_prices_loaded',
                                f'Loaded sample prices for {len(changed)} products (demo; set real prices in Inventory)',
                                link='/inventory', products=[c[0] for c in changed])
        for name, old, new in changed:
            self.stdout.write(f'  {name:22} ₱{old:>9,.2f} → ₱{new:>9,.2f}')
        if missing:
            self.stdout.write(f'Not in the catalog (skipped): {", ".join(missing)}')
        verb = 'Would change' if dry else 'Changed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {len(changed)} prices.'))
