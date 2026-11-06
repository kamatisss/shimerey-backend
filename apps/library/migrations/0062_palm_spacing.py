"""
Christmas palm spacing. Plants are now spaced for their fully grown size (layout_engine.specs_from_inventory),
using a height rule of thumb (~0.55 × height) unless spacing_cm is set. Palms are columnar: a 6.5 m Adonidia
has a crown only ~2.5 m wide, so the height rule (3.8 m) would space them far wider than landscapers do.
Sets the usual 2.5 m on-centre where staff haven't set a spacing yet.
"""
from django.db import migrations


def fill(apps, schema_editor):
    InventoryItem = apps.get_model('library', 'InventoryItem')
    for item in InventoryItem.objects.filter(category='plant', spacing_cm__isnull=True):
        name = item.name.lower()
        if 'christmas palm' in name or 'adonidia' in name:
            item.spacing_cm = 250
            item.save(update_fields=['spacing_cm'])


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0061_fill_plant_growth_heights'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
