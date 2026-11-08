"""
The outdoor bench model is now a three-seater: stretched along its length to 1.6 m, and drawn at
0.95 m tall (backrest), which puts the seat at 0.45 m — the usual park-bench height. real_world_size
is the height for furniture (data/modelSizing.js). Only rows still at the previous value change.
"""
from django.db import migrations

OLD, NEW = 0.9, 0.95


def forwards(apps, schema_editor):
    apps.get_model('library', 'InventoryItem').objects.filter(model_file='outdoor_bench.glb', real_world_size=OLD) \
        .update(real_world_size=NEW)


def backwards(apps, schema_editor):
    apps.get_model('library', 'InventoryItem').objects.filter(model_file='outdoor_bench.glb', real_world_size=NEW) \
        .update(real_world_size=OLD)


class Migration(migrations.Migration):
    dependencies = [('library', '0068_replace_bench_plumeria_models')]
    operations = [migrations.RunPython(forwards, backwards)]
