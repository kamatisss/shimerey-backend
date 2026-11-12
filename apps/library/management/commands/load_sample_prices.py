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
    # Plants (the established size our 3D models show) — lowered 2026-11-11 to mid-range nursery prices
    'Bamboo': 650,                # a clump, ornamental
    'Banana': 180,                # a sucker / young plant
    'Christmas Palm': 850,        # Adonidia, 1.5–2 m
    'Codiaeum variegatum': 100,   # croton
    'Geranium': 85,
    'Heliconia rostrata': 250,
    'Peace Lily': 180,
    'Plumeria': 650,              # 1.5–2 m
    'Strelitzia': 400,            # bird of paradise
    # Accent boulders (60–70 cm, priced each)
    'Bluish river rock': 450,
    'Mossy rock': 650,
    'Volcanic rock': 400,
    # Small garden stones (~20 cm, priced each, ordered by the dozen) — see SMALL_STONES
    'River stones (small)': 40,
    'Mossy stones (small)': 50,
    'Volcanic stones (small)': 30,
    # Groundcover (priced per m² covered: about nine plugs or one nursery tray) — see GROUNDCOVERS
    'Mani-mani (Arachis pintoi)': 180,
    'Bangka-bangkaan (Rhoeo)': 220,
    'Mondo grass': 260,
    'Asiatic jasmine': 240,
    # Furniture
    'Bamboo bench': 3500,
    'Outdoor bench': 7500,        # 3-seater, cast iron and wood
    'Rocking chair': 3500,
}

# Small garden stones: the same three rock models drawn at ~20 cm (pricing.SMALL_STONE), so nothing new to
# download. Created when missing (name → the boulder whose model, photo and look they share); the owner can
# change or archive them in Inventory like any product.
SMALL_STONES = {
    'River stones (small)': ('Bluish river rock', 0.22, 'Smooth river stones, about 20 cm. Sold each; most gardens use a dozen or more, in clusters along the beds.'),
    'Mossy stones (small)': ('Mossy rock', 0.24, 'Moss-covered stones, about 20 cm. Sold each; lovely in clusters in shady beds.'),
    'Volcanic stones (small)': ('Volcanic rock', 0.2, 'Dark volcanic stones, about 20 cm. Sold each; good for edging and around plants.'),
}
SMALL_STONE_STOCK = 400

# Sample groundcovers (no 3D model: the 3D view draws them as a carpet over the beds). Stock = m² on hand.
GROUNDCOVERS = {
    'Mani-mani (Arachis pintoi)': ('full_sun', 'moderate', 0.15, 'Perennial peanut: a low, bright green carpet with small yellow flowers. Full sun, takes light foot traffic. Priced per m² (about nine plugs).'),
    'Bangka-bangkaan (Rhoeo)': ('partial_shade', 'low', 0.3, 'Boat lily (Tradescantia spathacea): purple-and-green rosettes, very tough, sun or part shade. Priced per m².'),
    'Mondo grass': ('partial_shade', 'moderate', 0.2, 'Dark green grassy tufts for edging and part shade, slow and neat. Priced per m².'),
    'Asiatic jasmine': ('full_shade', 'moderate', 0.15, 'Small glossy leaves; the groundcover for deep shade under trees. Priced per m².'),
}
GROUNDCOVER_STOCK = 300

# Planting distances (cm) where the catalog has none and the height rule of thumb is wrong. Bamboo: an 8 m clump
# came out 4 m apart (the rule's cap), so a design fitted two across a 10 m yard; clumping bamboo for a screen is
# planted 0.9–1.5 m apart (bamboo.org, nurseries). Only filled when empty: the owner's own value always wins.
SAMPLE_SPACING = {
    'Bamboo': 150,
}


class Command(BaseCommand):
    help = 'Load realistic sample catalog prices (demo only; the owner sets real prices in Inventory).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show the changes without saving them')

    def handle(self, *args, **options):
        dry = options['dry_run']
        changed, missing = [], []
        added = []
        with transaction.atomic():
            for name, (boulder, size, description) in SMALL_STONES.items():
                if InventoryItem.objects.filter(name=name).exists():
                    continue
                base = InventoryItem.objects.filter(name=boulder).first()
                if base is None:
                    continue
                added.append(name)
                if not dry:
                    InventoryItem.objects.create(
                        name=name, category='hardscape', description=description, unit_price=Decimal(SAMPLE_PRICES[name]),
                        stock_quantity=SMALL_STONE_STOCK, reorder_level=60, model_file=base.model_file, thumbnail=base.thumbnail,
                        image_url=base.image_url, real_world_size=size, sunlight=base.sunlight, water_need=base.water_need,
                        is_active=True)
            for name, (sun, water, size, description) in GROUNDCOVERS.items():
                if InventoryItem.objects.filter(name=name).exists():
                    continue
                added.append(name)
                if not dry:
                    InventoryItem.objects.create(
                        name=name, category='plant', is_groundcover=True, description=description,
                        unit_price=Decimal(SAMPLE_PRICES[name]), stock_quantity=GROUNDCOVER_STOCK, reorder_level=40,
                        real_world_size=size, sunlight=sun, water_need=water, is_active=True)
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
            spaced = []
            for name, cm in SAMPLE_SPACING.items():
                item = InventoryItem.objects.filter(name=name, spacing_cm__isnull=True).first()
                if item is None:
                    continue
                spaced.append((name, cm))
                if not dry:
                    item.spacing_cm = cm
                    item.save(update_fields=['spacing_cm'])
            if spaced and not dry:
                activity.record(None, 'stock', 'sample_spacing_loaded',
                                f'Set sample planting distances for {len(spaced)} products (demo; set real ones in Inventory)',
                                link='/inventory', products=[n for n, _ in spaced])
            if added and not dry:
                activity.record(None, 'stock', 'sample_stones_added',
                                f'Added {len(added)} sample products: small garden stones / groundcovers (demo; set real prices in Inventory)',
                                link='/inventory', products=added)
            if changed and not dry:
                activity.record(None, 'stock', 'sample_prices_loaded',
                                f'Loaded sample prices for {len(changed)} products (demo; set real prices in Inventory)',
                                link='/inventory', products=[c[0] for c in changed])
        for name in added:
            self.stdout.write(f'  + {name} (new product, ₱{SAMPLE_PRICES[name]} {"per m²" if name in GROUNDCOVERS else "each"})')
        for name, old, new in changed:
            self.stdout.write(f'  {name:22} ₱{old:>9,.2f} → ₱{new:>9,.2f}')
        for name, cm in spaced:
            self.stdout.write(f'  {name:22} planting distance {cm} cm (was empty)')
        missing = [m for m in missing if m not in added]
        if missing:
            self.stdout.write(f'Not in the catalog (skipped): {", ".join(missing)}')
        verb = 'Would change' if dry else 'Changed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {len(changed)} prices.'))
