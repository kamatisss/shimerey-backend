"""
Widths (spread) at planting, after ~2 years and fully grown, for the plants in the catalog (0071 added
the fields). The 3D growth view scales width and height separately with them: a Christmas palm's crown
hardly widens while it grows taller, a plumeria spreads into an umbrella, a bamboo clump widens.
Typical values for Philippine home gardens (estimates, 2026-11-08); staff can change them in
Inventory → Edit. Only empty fields are filled, and only when all three read small ≤ medium ≤ large.
"""
from django.db import migrations

# keyword in the name (lower case) → (at planting, after ~2 years, fully grown) in metres wide; first match wins
SPREADS = [
    ('bench', None),                              # guard: furniture made of bamboo etc.
    ('christmas palm', (1.5, 2.0, 2.5)),          # Adonidia: a compact crown on a single trunk
    ('adonidia', (1.5, 2.0, 2.5)),
    ('bamboo', (0.6, 1.5, 3.0)),                  # clumping bamboo widens as new culms come up
    ('banana', (0.8, 2.5, 3.0)),                  # wide leaves; the clump adds suckers
    ('plumeria', (0.8, 1.5, 5.0)),                # kalachuchi: an umbrella-shaped canopy
    ('kalachuchi', (0.8, 1.5, 5.0)),
    ('heliconia', (0.5, 1.2, 2.0)),               # spreads by rhizomes into a clump
    ('codiaeum', (0.4, 0.8, 1.2)),                # croton
    ('croton', (0.4, 0.8, 1.2)),
    ('strelitzia', (0.6, 1.0, 1.5)),              # bird of paradise: a fan that fills out
    ('bird of paradise', (0.6, 1.0, 1.5)),
    ('geranium', (0.25, 0.35, 0.5)),
    ('peace lily', (0.35, 0.5, 0.7)),
    ('spathiphyllum', (0.35, 0.5, 0.7)),
]
FIELDS = ('planting_spread', 'established_spread', 'mature_spread')


def fill(apps, schema_editor):
    InventoryItem = apps.get_model('library', 'InventoryItem')
    for item in InventoryItem.objects.filter(category='plant'):
        name = item.name.lower()
        for keyword, spreads in SPREADS:
            if keyword not in name:
                continue
            if spreads:
                values = [getattr(item, f) if getattr(item, f) is not None else v for f, v in zip(FIELDS, spreads)]
                changed = [f for f in FIELDS if getattr(item, f) is None]
                if changed and values[0] <= values[1] <= values[2]:
                    for f, v in zip(FIELDS, values):
                        setattr(item, f, v)
                    item.save(update_fields=changed)
            break


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0071_plant_spreads'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
