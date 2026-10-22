from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0032_alter_servicebooking_service_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='projectmilestone',
            name='phase',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('design_contract',  'Design Finalized & Contract Signed'),
                    ('site_prep',        'Site Preparation Complete'),
                    ('installation',     'Installation Service (Hardscape/Softscape)'),
                    ('final_inspection', 'Final Inspection & Output Uploaded'),
                    ('hardscaping',      'Hardscaping'),
                    ('softscaping',      'Softscaping'),
                    ('cleanup',          'Cleanup & Handover'),
                ],
            ),
        ),
    ]
