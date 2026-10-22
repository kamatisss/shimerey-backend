from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0021_inventoryitem_real_world_size'),
    ]

    operations = [
        migrations.AddField(
            model_name='gardendesign',
            name='reference_image_url',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Garden background photo URL used for AR overlay in staff preview',
                max_length=1000,
            ),
        ),
    ]
