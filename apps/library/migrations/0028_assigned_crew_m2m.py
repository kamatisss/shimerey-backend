from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0027_notification_model'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Add M2M with a temporary related_name to avoid clash with existing FK
        migrations.AddField(
            model_name='servicebooking',
            name='assigned_crew',
            field=models.ManyToManyField(
                blank=True,
                help_text='Field crew members assigned to this project',
                related_name='assigned_bookings_m2m_temp',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # 2. Data migration: copy FK → M2M
        migrations.RunPython(
            code=lambda apps, se: [
                booking.assigned_crew.add(booking.assigned_to)
                for booking in apps.get_model('library', 'ServiceBooking')
                    .objects.filter(assigned_to__isnull=False)
            ],
            reverse_code=migrations.RunPython.noop,
        ),
        # 3. Drop the old ForeignKey (removes related_name 'assigned_bookings')
        migrations.RemoveField(
            model_name='servicebooking',
            name='assigned_to',
        ),
        # 4. Rename M2M related_name to the canonical 'assigned_bookings'
        migrations.AlterField(
            model_name='servicebooking',
            name='assigned_crew',
            field=models.ManyToManyField(
                blank=True,
                help_text='Field crew members assigned to this project (supports 1-to-many capacity planning)',
                related_name='assigned_bookings',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
