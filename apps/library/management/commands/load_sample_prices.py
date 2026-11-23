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
    # Products for 3D models that already ship with the frontend — see MODEL_PRODUCTS
    'Hibiscus': 150,              # gumamela, ~1 m in a 12" bag
    'Livistona palm': 950,        # fan palm, ~1.5–2 m
    'Coral stone': 500,           # quarried coral limestone accent, ~60 cm
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

# Products for catalog models that ship with the frontend (public/models/..., modelCatalog.js) but had no product,
# so the AI Designer and the Studio never used them. Created when missing — by name, or when some product already
# uses the model (the owner made it under their own name). Sizes in metres, as Inventory: real_world_size is the
# established height (hardscape: longest side); planting / mature heights and the three spreads as 0071/0072.
# Hibiscus is kept pruned as a shrub (2 m grown → the 'mid' layer, not the tall back row).
MODEL_PRODUCTS = {
    'Hibiscus': dict(
        category='plant', model_file='hibiscus.glb', sunlight='full_sun', water_need='moderate',
        real_world_size=1.2, planting_height=0.5, mature_height=2.0,
        planting_spread=0.4, established_spread=0.9, mature_spread=1.5, stock_quantity=40,
        description='Gumamela (Hibiscus rosa-sinensis): large red or pink flowers almost all year. Full sun, '
                    'kept pruned as a 1–2 m shrub or hedge.'),
    'Livistona palm': dict(
        category='plant', model_file='livistona.glb', sunlight='partial_shade', water_need='moderate',
        real_world_size=2.0, planting_height=1.5, mature_height=6.0,
        planting_spread=1.2, established_spread=1.8, mature_spread=3.0, stock_quantity=10,
        description='Fan palm with round, pleated leaves: a slow-growing specimen for the back of a bed. Sun or '
                    'part shade (young palms prefer some shade).'),
    'Coral stone': dict(
        category='hardscape', model_file='coral_stone.glb', sunlight='full_sun', water_need='moderate',
        real_world_size=0.6, stock_quantity=20,
        description='Pale, porous accent boulder of quarried coral limestone, about 60 cm. Never collected from '
                    'the sea or the beach.'),
}

# Planting distances (cm) where the catalog has none and the height rule of thumb is wrong. Bamboo: an 8 m clump
# came out 4 m apart (the rule's cap), so a design fitted two across a 10 m yard; clumping bamboo for a screen is
# planted 0.9–1.5 m apart (bamboo.org, nurseries). Only filled when empty: the owner's own value always wins.
SAMPLE_SPACING = {
    'Bamboo': 150,
}

# Looks (flower colour, leaf colour, shape, design role) for the sample catalog: how the AI Designer combines plants
# for a pleasing design (layout_engine Looks). The usual nursery forms; the owner corrects them in Inventory (a pink
# hibiscus or geranium changes its colour family). Only empty fields are filled: the owner's own values always win.
SAMPLE_LOOKS = {
    #                             flower    leaves        shape        role
    'Bamboo':                     ('none',   'green',      'upright',   'structure'),
    'Banana':                     ('none',   'lime',       'bold',      'filler'),
    'Christmas Palm':             ('none',   'green',      'palm',      'structure'),
    'Codiaeum variegatum':        ('none',   'variegated', 'upright',   'accent'),
    'Geranium':                   ('red',    'green',      'mounding',  'edging'),
    'Heliconia rostrata':         ('red',    'green',      'bold',      'accent'),
    'Hibiscus':                   ('red',    'dark_green', 'mounding',  'filler'),
    'Livistona palm':             ('none',   'green',      'palm',      'specimen'),
    'Peace Lily':                 ('white',  'dark_green', 'mounding',  'filler'),
    'Plumeria':                   ('white',  'green',      'spreading', 'specimen'),
    'Strelitzia':                 ('orange', 'silver',     'bold',      'accent'),
    # Groundcovers: carpets, never placed one by one, so no role
    'Mani-mani (Arachis pintoi)': ('yellow', 'green',      'spreading', ''),
    'Bangka-bangkaan (Rhoeo)':    ('none',   'red_purple', 'mounding',  ''),
    'Mondo grass':                ('none',   'dark_green', 'grassy',    ''),
    'Asiatic jasmine':            ('none',   'dark_green', 'spreading', ''),
}
LOOK_FIELDS = ('flower_color', 'foliage_color', 'plant_form', 'design_role')

# Years after planting until the sample plants reach their fully grown size (mature_height), in a lowland
# Philippine home garden from our usual nursery size. Shown on the 3D "Fully grown" card and in the proposal, so
# customers don't read the fully grown view as next year's garden. Rough horticultural estimates: fast soft plants
# ~2, shrubs 3–4, palms and trees 8–15 (Livistona is famously slow). Only filled when empty.
SAMPLE_YEARS = {
    'Banana': 2,                # a pseudostem is full height in about a year; the clump fills out by year 2
    'Geranium': 2,
    'Peace Lily': 2,
    'Heliconia rostrata': 3,
    'Hibiscus': 3,              # kept pruned at 1.5–2 m
    'Codiaeum variegatum': 4,
    'Strelitzia': 4,
    'Bamboo': 5,                # clumping bamboo: full-height culms once the clump is established
    'Christmas Palm': 8,
    'Plumeria': 12,
    'Livistona palm': 15,
    # Groundcovers: years until the plugs close into a carpet (the 3D view fills each in at its own pace)
    'Mani-mani (Arachis pintoi)': 1,   # runs fast: a closed carpet in about 6–9 months
    'Bangka-bangkaan (Rhoeo)': 2,
    'Mondo grass': 3,                  # slow clumps
    'Asiatic jasmine': 3,              # slow to start, then runs
}

# Specimen size: bigger nursery stock a customer may choose per plant (planted by our crew), as
# (price ₱, planted height m). Rough mid-range Quezon nursery prices for older stock; the owner sets the real
# ones in Inventory. Only filled when the plant has no specimen size yet.
SAMPLE_SPECIMENS = {
    'Hibiscus': (450, 1.2),            # a bushy 2-year shrub instead of a 0.5 m pot
    'Codiaeum variegatum': (300, 1.0),
    'Plumeria': (2500, 3.0),           # a branched young tree instead of a 1.5 m cutting
    'Christmas Palm': (2200, 3.5),     # a palm with a trunk already showing
}


class Command(BaseCommand):
    help = 'Load realistic sample catalog prices (demo only; the owner sets real prices in Inventory).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show the changes without saving them')

    def handle(self, *args, **options):
        dry = options['dry_run']
        # A Windows console (cp1252) can't print ₱ / m²: without this the report crashed after the changes were saved
        reconfigure = getattr(self.stdout, 'reconfigure', None)
        if reconfigure:
            reconfigure(errors='replace')
        changed, missing = [], []
        added = []
        with transaction.atomic():
            for name, fields in MODEL_PRODUCTS.items():
                if InventoryItem.objects.filter(name=name).exists() or \
                        InventoryItem.objects.filter(model_file=fields['model_file']).exists():
                    continue
                added.append(name)
                if not dry:
                    InventoryItem.objects.create(name=name, unit_price=Decimal(SAMPLE_PRICES[name]),
                                                 reorder_level=5, is_active=True, **fields)
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
            aged = []
            for name, years in SAMPLE_YEARS.items():
                item = InventoryItem.objects.filter(name=name, category='plant', years_to_mature__isnull=True).first()
                if item is None:
                    continue
                aged.append((name, years))
                if not dry:
                    item.years_to_mature = years
                    item.save(update_fields=['years_to_mature'])
            if aged and not dry:
                activity.record(None, 'stock', 'sample_years_loaded',
                                f'Set sample "fully grown after" years for {len(aged)} plants (demo; correct them in Inventory)',
                                link='/inventory', products=[n for n, _ in aged])
            specimens = []
            for name, (sp, sh) in SAMPLE_SPECIMENS.items():
                item = InventoryItem.objects.filter(name=name, category='plant', specimen_price__isnull=True,
                                                    specimen_height__isnull=True).first()
                if item is None or Decimal(sp) <= item.unit_price:
                    continue
                specimens.append((name, sp, sh))
                if not dry:
                    item.specimen_price, item.specimen_height = Decimal(sp), sh
                    item.save(update_fields=['specimen_price', 'specimen_height'])
            if specimens and not dry:
                activity.record(None, 'stock', 'sample_specimens_loaded',
                                f'Set sample specimen sizes for {len(specimens)} plants (demo; correct them in Inventory)',
                                link='/inventory', products=[n for n, _, _ in specimens])
            looked = []
            for name, values in SAMPLE_LOOKS.items():
                item = InventoryItem.objects.filter(name=name, category='plant').first()
                if item is None:
                    continue
                fill = {f: v for f, v in zip(LOOK_FIELDS, values) if v and not getattr(item, f)}
                if not fill:
                    continue
                looked.append(name)
                if not dry:
                    for f, v in fill.items():
                        setattr(item, f, v)
                    item.save(update_fields=list(fill))
            if looked and not dry:
                activity.record(None, 'stock', 'sample_looks_loaded',
                                f'Set sample flower colour, leaf colour, shape and design role for {len(looked)} plants '
                                f'(demo; correct them in Inventory)',
                                link='/inventory', products=looked)
            if spaced and not dry:
                activity.record(None, 'stock', 'sample_spacing_loaded',
                                f'Set sample planting distances for {len(spaced)} products (demo; set real ones in Inventory)',
                                link='/inventory', products=[n for n, _ in spaced])
            if added and not dry:
                activity.record(None, 'stock', 'sample_stones_added',
                                f'Added {len(added)} sample products: small garden stones / groundcovers / catalog models (demo; set real prices in Inventory)',
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
        for name, years in aged:
            self.stdout.write(f'  {name:22} fully grown after about {years} year{"" if years == 1 else "s"} (was empty)')
        for name, sp, sh in specimens:
            self.stdout.write(f'  {name:22} specimen size ₱{sp:,} planted at {sh:g} m (was empty)')
        for name in looked:
            self.stdout.write(f'  {name:22} looks: {", ".join(v for v in SAMPLE_LOOKS[name] if v)}')
        missing = [m for m in missing if m not in added]
        if missing:
            self.stdout.write(f'Not in the catalog (skipped): {", ".join(missing)}')
        verb = 'Would change' if dry else 'Changed'
        self.stdout.write(self.style.SUCCESS(f'{verb} {len(changed)} prices.'))
