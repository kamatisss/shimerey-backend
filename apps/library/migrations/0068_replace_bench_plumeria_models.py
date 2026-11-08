"""
New catalog models (2026-11-07): the old outdoor bench was a flat 1 : 0.15 : 0.15 model that could
only look right at 0.25 m "size", and the plumeria was a flower, not a tree. The new files ship with
the frontend (public/models/...); here the products are pointed at them. Rows already changed by
the owner are left alone.
  Outdoor bench: real_world_size is the height (data/modelSizing.js: furniture = height); the new
                 park bench's backrest is 0.9 m tall, about 1.1 m long.
  Plumeria:      model_file plumeria_flower.glb → plumeria_tree.glb (the frontend also maps the old
                 name, for saved designs).
The bamboo keeps its file name (bamboo.glb now holds a clump, not one stalk) and its sizes.
"""
from django.db import migrations

BENCH_OLD_SIZE, BENCH_NEW_SIZE = 0.25, 0.9


def forwards(apps, schema_editor):
    Item = apps.get_model('library', 'InventoryItem')
    Item.objects.filter(model_file='outdoor_bench.glb', real_world_size=BENCH_OLD_SIZE).update(real_world_size=BENCH_NEW_SIZE)
    Item.objects.filter(model_file='plumeria_flower.glb').update(model_file='plumeria_tree.glb')


def backwards(apps, schema_editor):
    Item = apps.get_model('library', 'InventoryItem')
    Item.objects.filter(model_file='outdoor_bench.glb', real_world_size=BENCH_NEW_SIZE).update(real_world_size=BENCH_OLD_SIZE)
    Item.objects.filter(model_file='plumeria_tree.glb').update(model_file='plumeria_flower.glb')


class Migration(migrations.Migration):
    dependencies = [('library', '0067_attendance_closed_by_owner')]
    operations = [migrations.RunPython(forwards, backwards)]
