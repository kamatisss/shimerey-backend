"""
Planting and fully-grown heights for the plants in the catalog (0060 added the fields).

real_world_size stays the ESTABLISHED height (~2 years after planting) that the 3D view, the AI
layout and the labour pricing already use; these two put it in context for the customer.
Typical values for Philippine home gardens; staff can change them in Inventory → Edit.
Only empty fields are filled, and only when they fit around the established height.
"""
from django.db import migrations

# keyword in the name (lower case) → (at planting m, fully grown m); first match wins
HEIGHTS = [
    ('bench', None),                           # guard: furniture made of bamboo etc.
    ('christmas palm', (2.5, 6.5)),            # Adonidia merrillii: slow, ~0.3 m a year; 5–8 m mature
    ('adonidia', (2.5, 6.5)),
    ('bamboo', (2.5, 8.0)),                    # landscape clumping bamboo; species vary (6–10 m+)
    ('banana', (1.2, 4.0)),                    # Lakatan / Latundan type (Saba grows to 5–6 m)
    ('plumeria', (1.5, 6.0)),                  # kalachuchi grows into a 4–8 m tree
    ('kalachuchi', (1.5, 6.0)),
    ('heliconia', (0.8, 2.5)),
    ('codiaeum', (0.5, 1.8)),                  # croton
    ('croton', (0.5, 1.8)),
    ('strelitzia', (0.6, 1.5)),                # bird of paradise
    ('bird of paradise', (0.6, 1.5)),
    ('geranium', (0.25, 0.5)),
    ('peace lily', (0.35, 0.7)),
    ('spathiphyllum', (0.35, 0.7)),
]


def fill(apps, schema_editor):
    InventoryItem = apps.get_model('library', 'InventoryItem')
    for item in InventoryItem.objects.filter(category='plant'):
        name = item.name.lower()
        for keyword, heights in HEIGHTS:
            if keyword not in name:
                continue
            if heights:
                planting, mature = heights
                established = float(item.real_world_size or 0)
                changed = []
                if item.planting_height is None and (not established or planting <= established):
                    item.planting_height = planting
                    changed.append('planting_height')
                if item.mature_height is None and (not established or mature >= established):
                    item.mature_height = mature
                    changed.append('mature_height')
                if changed:
                    item.save(update_fields=changed)
            break


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0060_plant_growth_heights'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
