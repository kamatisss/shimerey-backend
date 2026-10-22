import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0030_servicereview'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── 1. ProjectTracker ────────────────────────────────────────────────
        migrations.CreateModel(
            name='ProjectTracker',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('status', models.CharField(
                    choices=[
                        ('PENDING',                'Pending'),
                        ('CONSULTATION_SCHEDULED', 'Consultation Scheduled'),
                        ('SITE_INSPECTION',        'Site Inspection'),
                        ('QUOTATION_APPROVED',     'Quotation Approved'),
                        ('DESIGN_PREPARATION',     'Design Preparation'),
                        ('DESIGN_APPROVED',        'Design Approved'),
                        ('PROJECT_SCHEDULED',      'Project Scheduled'),
                        ('MATERIALS_PREPARATION',  'Materials Preparation'),
                        ('PROJECT_STARTED',        'Project Started'),
                        ('SITE_PREPARATION',       'Site Preparation'),
                        ('PLANTING',               'Planting'),
                        ('HARDSCAPE_INSTALLATION', 'Hardscape Installation'),
                        ('LIGHTING_INSTALLATION',  'Lighting Installation'),
                        ('DECORATION',             'Decoration'),
                        ('FINAL_INSPECTION',       'Final Inspection'),
                        ('COMPLETED',              'Completed'),
                        ('CUSTOMER_FEEDBACK',      'Customer Feedback'),
                    ],
                    default='PENDING',
                    max_length=30,
                )),
                ('progress_percentage', models.PositiveSmallIntegerField(
                    default=0,
                    help_text='Auto-computed from status — do not set manually',
                )),
                ('estimated_end_date', models.DateField(blank=True, null=True)),
                ('total_price', models.DecimalField(
                    decimal_places=2, default=0, max_digits=12,
                )),
                ('remaining_balance', models.DecimalField(
                    decimal_places=2, default=0, max_digits=12,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('booking', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='tracker',
                    to='library.servicebooking',
                )),
                ('supervisor', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='supervised_projects',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-updated_at']},
        ),

        # ── 2. ProjectHistoryLog ─────────────────────────────────────────────
        migrations.CreateModel(
            name='ProjectHistoryLog',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('status_reached', models.CharField(
                    choices=[
                        ('PENDING',                'Pending'),
                        ('CONSULTATION_SCHEDULED', 'Consultation Scheduled'),
                        ('SITE_INSPECTION',        'Site Inspection'),
                        ('QUOTATION_APPROVED',     'Quotation Approved'),
                        ('DESIGN_PREPARATION',     'Design Preparation'),
                        ('DESIGN_APPROVED',        'Design Approved'),
                        ('PROJECT_SCHEDULED',      'Project Scheduled'),
                        ('MATERIALS_PREPARATION',  'Materials Preparation'),
                        ('PROJECT_STARTED',        'Project Started'),
                        ('SITE_PREPARATION',       'Site Preparation'),
                        ('PLANTING',               'Planting'),
                        ('HARDSCAPE_INSTALLATION', 'Hardscape Installation'),
                        ('LIGHTING_INSTALLATION',  'Lighting Installation'),
                        ('DECORATION',             'Decoration'),
                        ('FINAL_INSPECTION',       'Final Inspection'),
                        ('COMPLETED',              'Completed'),
                        ('CUSTOMER_FEEDBACK',      'Customer Feedback'),
                    ],
                    max_length=30,
                )),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('remarks', models.TextField(blank=True, default='')),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='history_logs',
                    to='library.projecttracker',
                )),
                ('trigger_user', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='triggered_project_logs',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-timestamp']},
        ),

        # ── 3. ProjectProgressMedia ──────────────────────────────────────────
        migrations.CreateModel(
            name='ProjectProgressMedia',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True,
                    serialize=False, verbose_name='ID',
                )),
                ('construction_phase', models.CharField(
                    choices=[
                        ('BEFORE', 'Before'),
                        ('DURING', 'During'),
                        ('AFTER',  'After'),
                    ],
                    max_length=10,
                )),
                ('file_path', models.ImageField(
                    upload_to='project_progress/%Y/%m/',
                )),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('description', models.TextField(blank=True, default='')),
                ('location', models.CharField(
                    blank=True, default='', max_length=255,
                )),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='progress_media',
                    to='library.projecttracker',
                )),
                ('uploader', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='uploaded_progress_media',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-uploaded_at']},
        ),
    ]
