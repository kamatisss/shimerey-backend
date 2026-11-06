"""
Fill in sunlight / water / aggressive-roots for the plants in the catalog (0058 added the fields with
defaults only, so every plant looked "full sun, moderate water" and no compatibility warning could fire).

Values are for Philippine (tropical lowland) gardens. Matched on the plant's name; anything not
recognised keeps the defaults and staff can set it in Inventory → Edit. Non-plant items are never
"aggressive roots" (a bamboo bench is furniture).
"""
from django.db import migrations

# keyword in the name (lower case) → (sunlight, water_need, is_invasive); first match wins
GROWING_NEEDS = [
    ('bench', None),  # guard: furniture made of bamboo etc.
    ('bamboo', ('full_sun', 'moderate', True)),          # Bambusa spreads; needs a root barrier
    ('banana', ('full_sun', 'high', False)),             # Musa: heavy feeder, wants moist soil
    ('heliconia', ('partial_shade', 'high', False)),     # sun to part shade, constantly moist
    ('christmas palm', ('full_sun', 'moderate', False)),  # Adonidia merrillii
    ('adonidia', ('full_sun', 'moderate', False)),
    ('codiaeum', ('full_sun', 'moderate', False)),       # croton: best colour in sun
    ('croton', ('full_sun', 'moderate', False)),
    ('geranium', ('full_sun', 'low', False)),            # rots in wet soil
    ('plumeria', ('full_sun', 'low', False)),            # kalachuchi: drought tolerant
    ('kalachuchi', ('full_sun', 'low', False)),
    ('strelitzia', ('full_sun', 'moderate', False)),     # bird of paradise
    ('bird of paradise', ('full_sun', 'moderate', False)),
    ('bougainvillea', ('full_sun', 'low', False)),
    ('cactus', ('full_sun', 'low', False)),
    ('succulent', ('full_sun', 'low', False)),
    ('agave', ('full_sun', 'low', False)),
    ('sansevieria', ('partial_shade', 'low', False)),
    ('snake plant', ('partial_shade', 'low', False)),
    ('fern', ('full_shade', 'high', False)),
    ('calathea', ('full_shade', 'high', False)),
    ('caladium', ('partial_shade', 'high', False)),
    ('anthurium', ('partial_shade', 'moderate', False)),
    ('philodendron', ('partial_shade', 'moderate', False)),
    ('monstera', ('partial_shade', 'moderate', False)),
    ('ixora', ('full_sun', 'moderate', False)),
    ('santan', ('full_sun', 'moderate', False)),
    ('palm', ('full_sun', 'moderate', False)),
]


def fill(apps, schema_editor):
    InventoryItem = apps.get_model('library', 'InventoryItem')
    InventoryItem.objects.exclude(category='plant').update(is_invasive=False)
    for item in InventoryItem.objects.filter(category='plant'):
        name = item.name.lower()
        for keyword, needs in GROWING_NEEDS:
            if keyword in name:
                if needs:
                    item.sunlight, item.water_need, item.is_invasive = needs
                    item.save(update_fields=['sunlight', 'water_need', 'is_invasive'])
                break


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0058_inventoryitem_is_invasive_inventoryitem_sunlight_and_more'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
