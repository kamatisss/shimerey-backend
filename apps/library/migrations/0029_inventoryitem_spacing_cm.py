from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0028_assigned_crew_m2m'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryitem',
            name='spacing_cm',
            field=models.IntegerField(
                blank=True, null=True,
                help_text='Recommended on-center planting spacing in centimetres (e.g. 45 = 45 cm OC)',
            ),
        ),
    ]
