from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import User
from django.utils import timezone


class InventoryItem(models.Model):
    CATEGORY_CHOICES = [
        ('plant', 'Plant'),
        ('hardscape', 'Hardscape'),
        ('furniture', 'Furniture'),
    ]
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='plant')
    description = models.TextField(blank=True)
    stock_quantity = models.IntegerField(default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    image_url = models.CharField(max_length=500, blank=True)
    # A bundled 3D model's file name (e.g. "bamboo.glb"): the models ship with the frontend
    # (public/models/<category>/, data/modelCatalog.js). Blank = sold in the shop, not placeable in 3D.
    model_file = models.CharField(max_length=100, blank=True, default='')
    real_world_size = models.FloatField(default=1.0, help_text="Metres at established size (1–2 years after planting): height for plants and furniture, longest side for hardscape")
    spacing_cm = models.IntegerField(
        null=True, blank=True,
        help_text="Recommended on-center planting spacing in centimetres (e.g. 45 = 45 cm OC)"
    )
    thumbnail = models.ImageField(upload_to='thumbnails/', null=True, blank=True)
    # Archived items leave the shop, Studio and AI Designer but keep order history and designs intact
    is_active = models.BooleanField(default=True, db_index=True)
    # At or below this many units the item counts as low stock (cheap plants and benches differ)
    reorder_level = models.PositiveIntegerField(default=10)

    # Botanical compatibility attributes (used by AI Designer & 3D Studio to warn about companion conflicts)
    SUNLIGHT_CHOICES = [
        ('full_sun', 'Full Sun'),
        ('partial_shade', 'Partial Shade'),
        ('full_shade', 'Full Shade'),
    ]
    WATER_CHOICES = [
        ('low', 'Low / Drought Tolerant'),
        ('moderate', 'Moderate Moisture'),
        ('high', 'High Moisture'),
    ]
    sunlight = models.CharField(max_length=20, choices=SUNLIGHT_CHOICES, default='full_sun', blank=True)
    water_need = models.CharField(max_length=20, choices=WATER_CHOICES, default='moderate', blank=True)
    is_invasive = models.BooleanField(
        default=False,
        help_text="True if species has aggressive rhizomatous/root spread requiring isolation (e.g. Bamboo)"
    )
    # Growth: real_world_size is the ESTABLISHED height (~2 years after planting, what the 3D view and
    # the AI layout use). These two put it in context for the customer (Studio height label).
    planting_height = models.FloatField(null=True, blank=True,
                                        help_text="Metres tall when we plant it (nursery size). Plants only.")
    mature_height = models.FloatField(null=True, blank=True,
                                      help_text="Metres tall when fully grown, in a home garden. Plants only.")

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} (₱{self.unit_price})"

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.reorder_level


class StockMovement(models.Model):
    """Every change to an item's stock, newest first: who, why, by how much, and what was left."""
    REASONS = [
        ('opening',   'Opening stock'),
        ('restock',   'Restock / delivery received'),
        ('damaged',   'Damaged, dead or lost'),
        ('count',     'Stock count correction'),
        ('sale',      'Sold (order)'),
        ('released',  'Returned to stock (order cancelled or expired)'),
    ]
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='stock_movements')
    change = models.IntegerField()
    quantity_after = models.IntegerField()
    reason = models.CharField(max_length=10, choices=REASONS)
    note = models.CharField(max_length=200, blank=True, default='')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return f"{self.item_id}: {self.change:+d} ({self.reason}) → {self.quantity_after}"


class GardenImage(models.Model):
    image = models.ImageField(upload_to='gardens/')
    depth_map = models.ImageField(upload_to='depth/', null=True, blank=True)
    normal_map_url = models.CharField(max_length=255, blank=True)
    rock_mask_url = models.CharField(max_length=255, blank=True)
    grass_mask_url = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"GardenImage #{self.pk} ({self.uploaded_at:%Y-%m-%d})"


class GardenDesign(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100, default='Untitled Design')
    garden_image = models.ForeignKey(GardenImage, on_delete=models.SET_NULL, null=True, blank=True)
    original_image_url = models.CharField(max_length=500, blank=True)
    reference_image_url = models.CharField(
        max_length=1000, blank=True, default='',
        help_text="Garden background photo URL used for AR overlay in staff preview"
    )
    depth_data = models.JSONField(default=dict, blank=True,
                                  help_text="Stores depth/normal/mask URLs")
    placed_items = models.JSONField(default=list,
                                    help_text="[{product_id, position, rotation, scale}]")
    plant_breakdown = models.JSONField(
        default=list, blank=True,
        help_text="[{plant_id, name, quantity, unit_price, subtotal}] — AI-generated procurement list"
    )
    dimensions = models.JSONField(default=dict, blank=True,
                                  help_text="{width, length, terrain_type}")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Snapshot of the estimate (lines + rates) the customer saw — rate changes never rewrite it
    cost_estimate = models.JSONField(default=dict, blank=True)
    terrain_height = models.FloatField(default=1.5)
    time_of_day = models.FloatField(default=14.0)
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.name} ({self.created_at:%Y-%m-%d})"


class BlackoutDate(models.Model):
    date = models.DateField(unique=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f"Blackout: {self.date}"


def _default_milestones():
    return [
        {"id": "site_prep",      "label": "Site Preparation",  "completed": False, "completed_at": None},
        {"id": "plant_sourcing", "label": "Plant Sourcing",     "completed": False, "completed_at": None},
        {"id": "installation",   "label": "Installation",       "completed": False, "completed_at": None},
        {"id": "cleanup",        "label": "Cleanup & Handover", "completed": False, "completed_at": None},
    ]


class ServiceBooking(models.Model):
    SERVICE_CHOICES = [
        ('maintenance', 'Maintenance'),
        ('consultation', 'Consultation'),
        ('hardscaping', 'Full Hardscaping'),
        ('Softscape', 'Softscape / Planting Services'),
    ]
    STATUS_CHOICES = [
        ('Pending',    'Pending'),
        ('Preparing',  'Preparing'),
        ('Installing', 'Installing'),
        # Field crew submit finished work here; an office admin signs it off → Finished
        ('For Review', 'Awaiting Sign-off'),
        ('Finished',   'Finished'),
        ('Cancelled',  'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service_type = models.CharField(max_length=50, choices=SERVICE_CHOICES)
    scheduled_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    contact_number = models.CharField(max_length=20, blank=True, default='')
    preferred_time = models.CharField(max_length=50, blank=True, default='')
    service_address = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True)
    design = models.ForeignKey('GardenDesign', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    # GPS coordinates of the project site (set by staff for geofenced check-in)
    site_lat = models.FloatField(null=True, blank=True, help_text="Project site GPS latitude")
    site_lng = models.FloatField(null=True, blank=True, help_text="Project site GPS longitude")
    # Last "on my way" sent to the customer by the crew (shown on the job; stops repeat sends)
    crew_on_way_at = models.DateTimeField(null=True, blank=True)

    # Multi-crew assignment for capacity planning and RBAC data filtering
    assigned_crew = models.ManyToManyField(
        User,
        blank=True,
        related_name='assigned_bookings',
        help_text="Field crew members assigned to this project (supports 1-to-many capacity planning)",
    )

    # Project management fields
    milestones = models.JSONField(
        default=_default_milestones, blank=True,
        help_text="[{id, label, completed, completed_at}]"
    )
    progress_pct = models.PositiveSmallIntegerField(
        default=0,
        help_text="Overall project progress 0-100 set manually by staff"
    )
    staff_notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_date']
        # Staff work queues filter by status and sort by visit date (work_queues.py)
        indexes = [models.Index(fields=['status', 'scheduled_date'])]

    def __str__(self):
        return f"{self.service_type} for {self.user.username} on {self.scheduled_date}"


class ProjectMilestone(models.Model):
    PHASE_CHOICES = [
        ('design_contract',  'Design Finalized & Contract Signed'),
        ('site_prep',        'Site Preparation Complete'),
        ('installation',     'Installation Service (Hardscape/Softscape)'),
        ('final_inspection', 'Final Inspection & Output Uploaded'),
        # Legacy phases kept for backward compatibility
        ('hardscaping',      'Hardscaping'),
        ('softscaping',      'Softscaping'),
        ('cleanup',          'Cleanup & Handover'),
    ]
    ACTIVE_PHASES = ['design_contract', 'site_prep', 'installation', 'final_inspection']

    booking = models.ForeignKey(ServiceBooking, on_delete=models.CASCADE, related_name='project_milestones')
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES)
    completion_pct = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True, default='')
    proof_photo_url = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Clock-out proof photo URL bound when phase is marked 100% complete"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('booking', 'phase')
        ordering = ['booking', 'phase']

    def __str__(self):
        return f"{self.get_phase_display()} for Booking #{self.booking_id} ({self.completion_pct}%)"


class StaffProfile(models.Model):
    ROLE_CHOICES = [
        ('OFFICE_ADMIN', 'Office Admin'),
        ('FIELD_CREW',   'Field Crew'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='OFFICE_ADMIN')

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"


class StaffAttendance(models.Model):
    booking = models.ForeignKey(ServiceBooking, on_delete=models.CASCADE, related_name='staff_attendances')
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='field_attendances')
    timestamp_checkin = models.DateTimeField(null=True, blank=True)
    timestamp_checkout = models.DateTimeField(null=True, blank=True)
    gps_lat_checkin = models.FloatField(null=True, blank=True)
    gps_lng_checkin = models.FloatField(null=True, blank=True)
    distance_at_checkin_m = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp_checkin']

    def __str__(self):
        return f"StaffAttendance: {self.staff.username} at Booking #{self.booking_id}"


class Notification(models.Model):
    """In-app notification; created through notifications.notify() (see that module for the events)."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=30, default='general', db_index=True)
    message = models.TextField()
    link = models.CharField(max_length=300, blank=True, default='')   # in-app path the bell opens
    count = models.PositiveIntegerField(default=1)                     # grouped events ("3 new orders")
    is_read = models.BooleanField(default=False)
    # Not auto_now_add: a grouped notification moves back to the top when another event joins it
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp', '-id']
        indexes = [models.Index(fields=['user', 'is_read', '-timestamp'], name='notif_user_unread')]

    def __str__(self):
        return f"Notification({self.user.username}): {self.message[:60]}"


class PushSubscription(models.Model):
    """A phone or browser that agreed to receive push alerts (Web Push). One row per device."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='push_subscriptions')
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"PushSubscription({self.user.username}, {self.endpoint[:40]}…)"


class Order(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Paid',    'Paid'),
        ('Shipped', 'Shipped'),
        ('Out for Delivery', 'Out for Delivery'),
        ('Delivered', 'Delivered'),
        ('Cancelled', 'Cancelled'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('online', 'Online Payment (QR Ph / GCash / Maya / Card)'),
        ('cod',    'Cash on Delivery'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('Pending',  'Pending'),
        ('Paid',     'Paid'),
        ('Expired',  'Expired'),   # online order not paid in time; stock returned
        ('Refunded', 'Refunded'),
    ]
    user             = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    customer_name    = models.CharField(max_length=150)
    customer_email   = models.EmailField()
    customer_phone   = models.CharField(max_length=20, blank=True, default='')
    customer_address = models.TextField()
    booking_date     = models.DateField(null=True, blank=True)
    total_price      = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method   = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default='online')
    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    payment_status   = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='Pending')
    # PayMongo checkout session (cs_...) and payment (pay_...) for online orders
    paymongo_checkout_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    paymongo_payment_id  = models.CharField(max_length=64, blank=True, default='')
    paid_at          = models.DateTimeField(null=True, blank=True)
    # Lets a guest open their own receipt page without logging in
    receipt_token    = models.CharField(max_length=43, blank=True, default='')
    created_at       = models.DateTimeField(auto_now_add=True)
    # Our own field crew deliver; whoever marks it delivered also collected any COD cash
    delivery_crew    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='delivery_orders')
    delivered_by     = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='delivered_orders')
    delivered_at     = models.DateTimeField(null=True, blank=True)
    # COD only: set when the crew member hands the cash to the office (see CashHandover)
    cash_handover    = models.ForeignKey('CashHandover', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    refunded_at      = models.DateTimeField(null=True, blank=True)  # when staff marked the online refund done

    class Meta:
        # Staff work queues filter by status / payment and sort by age (work_queues.py)
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['payment_method', 'status']),
        ]

    def __str__(self):
        return f"Order #{self.pk} - {self.customer_name} [{self.status}]"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    item = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField(default=1)
    price_at_booking = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        item_name = self.item.name if self.item else "Deleted Item"
        return f"{self.quantity}x {item_name} (Order #{self.order.pk})"


class Attendance(models.Model):
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    booking = models.ForeignKey(ServiceBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='attendances')
    clock_in_time = models.DateTimeField(null=True, blank=True)
    clock_out_time = models.DateTimeField(null=True, blank=True)
    clock_in_photo_url = models.ImageField(upload_to='attendance_proofs/', null=True, blank=True)
    clock_out_photo_url = models.ImageField(upload_to='attendance_proofs/', null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    clock_in_address = models.CharField(max_length=500, null=True, blank=True)
    clock_out_address = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        ordering = ['-clock_in_time']

    @property
    def total_hours(self):
        if self.clock_in_time and self.clock_out_time:
            duration = self.clock_out_time - self.clock_in_time
            return round(duration.total_seconds() / 3600.0, 2)
        return None

    def __str__(self):
        return f"Attendance for {self.staff.username} on {self.clock_in_time}"


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker pipeline
# ─────────────────────────────────────────────────────────────────────────────

STATUS_PROGRESS_MAP = {
    'PENDING':                  0,
    'CONSULTATION_SCHEDULED':   5,
    'SITE_INSPECTION':         10,
    'QUOTATION_APPROVED':      15,
    'DESIGN_PREPARATION':      20,
    'DESIGN_APPROVED':         25,
    'PROJECT_SCHEDULED':       30,
    'MATERIALS_PREPARATION':   40,
    'PROJECT_STARTED':         50,
    'SITE_PREPARATION':        60,
    'PLANTING':                70,
    'HARDSCAPE_INSTALLATION':  75,
    'LIGHTING_INSTALLATION':   85,
    'DECORATION':              90,
    'FINAL_INSPECTION':        95,
    'COMPLETED':              100,
    'CUSTOMER_FEEDBACK':      100,
}


class ProjectTracker(models.Model):
    STATUS_CHOICES = [
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
    ]

    booking = models.OneToOneField(
        ServiceBooking, on_delete=models.CASCADE, related_name='tracker'
    )
    status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default='PENDING'
    )
    progress_percentage = models.PositiveSmallIntegerField(
        default=0,
        help_text='Auto-computed from status — do not set manually'
    )
    supervisor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supervised_projects'
    )
    estimated_end_date = models.DateField(null=True, blank=True)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    remaining_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def save(self, *args, **kwargs):
        self.progress_percentage = STATUS_PROGRESS_MAP.get(self.status, 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Tracker#{self.pk} — Booking#{self.booking_id} "
            f"[{self.status} {self.progress_percentage}%]"
        )


class ProjectPaymentRequest(models.Model):
    """
    Staff ask the customer for part of a project's price (e.g. a 50% downpayment).
    The customer pays through a link to our /pay/<token> page, which opens a fresh
    PayMongo checkout each time, so the link itself never expires.
    """
    STATUS_CHOICES = [
        ('PENDING',   'Pending'),
        ('PAID',      'Paid'),
        ('CANCELLED', 'Cancelled'),
    ]
    project = models.ForeignKey(ProjectTracker, on_delete=models.CASCADE, related_name='payment_requests')
    label = models.CharField(max_length=120, default='Downpayment')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='PENDING')
    token = models.CharField(max_length=43, unique=True)
    paymongo_checkout_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    paymongo_payment_id = models.CharField(max_length=64, blank=True, default='')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.label} ₱{self.amount} for project #{self.project_id} [{self.status}]"


class ProjectHistoryLog(models.Model):
    """
    Immutable append-only audit trail for ProjectTracker pipeline transitions.
    Each status change creates exactly one log entry; updates are blocked at
    the model level via the save() override.
    """
    project = models.ForeignKey(
        ProjectTracker, on_delete=models.CASCADE, related_name='history_logs'
    )
    status_reached = models.CharField(
        max_length=30, choices=ProjectTracker.STATUS_CHOICES
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    trigger_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='triggered_project_logs'
    )
    remarks = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-timestamp']

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "ProjectHistoryLog entries are immutable — updates are not permitted."
            )
        super().save(*args, **kwargs)

    def __str__(self):
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M') if self.timestamp else '—'
        return f"Log#{self.pk} {self.status_reached} @ {ts}"


class ProjectProgressMedia(models.Model):
    PHASE_CHOICES = [
        ('BEFORE', 'Before'),
        ('DURING', 'During'),
        ('AFTER',  'After'),
    ]

    project = models.ForeignKey(
        ProjectTracker, on_delete=models.CASCADE, related_name='progress_media'
    )
    construction_phase = models.CharField(max_length=10, choices=PHASE_CHOICES)
    file_path = models.ImageField(upload_to='project_progress/%Y/%m/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploader = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='uploaded_progress_media'
    )
    description = models.TextField(blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return (
            f"Media#{self.pk} [{self.construction_phase}] "
            f"Tracker#{self.project_id}"
        )


class PortfolioProject(models.Model):
    """
    A finished landscape job curated for the public Project Hub (portfolio).
    Drafted automatically when an office admin signs a booking off; only
    published once the customer has given consent (see PortfolioConsent).
    """
    STATUS_CHOICES = [
        ('draft',     'Draft'),
        ('published', 'Published'),
        ('archived',  'Archived'),
    ]

    booking = models.OneToOneField(ServiceBooking, on_delete=models.CASCADE, related_name='portfolio')
    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    summary = models.CharField(max_length=300, blank=True, default='')
    story = models.TextField(blank=True, default='')
    style_tags = models.JSONField(default=list, blank=True, help_text='e.g. ["Tropical", "Low maintenance"]')

    # Public display facts — never the street address or the exact price
    city = models.CharField(max_length=80, blank=True, default='')
    lot_area_sqm = models.FloatField(null=True, blank=True)
    price_band = models.CharField(max_length=40, blank=True, default='')
    started_on = models.DateField(null=True, blank=True)
    completed_on = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='draft')
    is_featured = models.BooleanField(default=False)
    cover = models.ForeignKey('PortfolioMedia', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    view_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-completed_on', '-created_at']

    def __str__(self):
        return f"Portfolio: {self.title} [{self.status}]"


class PortfolioMedia(models.Model):
    KIND_CHOICES = [
        ('before', 'Before'),
        ('during', 'During'),
        ('after',  'After'),
        ('detail', 'Detail'),
        ('render', '3D design render'),  # snapshot of the customer's 3D design, for Design vs Reality
    ]

    project = models.ForeignKey(PortfolioProject, on_delete=models.CASCADE, related_name='media')
    image = models.ImageField(upload_to='portfolio/%Y/%m/')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='after')
    caption = models.CharField(max_length=200, blank=True, default='')
    # Imported crew photos start hidden; the admin chooses what goes public
    is_public = models.BooleanField(default=False)
    # Flagged by the admin; hidden publicly unless the customer allowed house photos
    shows_house = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    source = models.OneToOneField(
        ProjectProgressMedia, on_delete=models.SET_NULL, null=True, blank=True, related_name='portfolio_copy',
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f"PortfolioMedia#{self.pk} [{self.kind}] for {self.project_id}"


class PortfolioConsent(models.Model):
    """The customer's permission to feature their garden (Data Privacy Act, RA 10173)."""
    STATUS_CHOICES = [
        ('pending',   'Awaiting response'),
        ('granted',   'Granted'),
        ('declined',  'Declined'),
        ('withdrawn', 'Withdrawn'),
    ]
    NAME_CHOICES = [
        ('initials',  'First name and last initial'),
        ('anonymous', 'Anonymous'),
    ]

    project = models.OneToOneField(PortfolioProject, on_delete=models.CASCADE, related_name='consent')
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='portfolio_consents')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    name_display = models.CharField(max_length=10, choices=NAME_CHOICES, default='initials')
    show_house = models.BooleanField(default=True, help_text='Customer allows photos where the house is visible')
    include_review = models.BooleanField(default=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Consent[{self.status}] for {self.project_id}"


class BusinessCredential(models.Model):
    """
    A permit, registration, certification or award shown on the public Track
    Record page. Entered by an office admin — nothing here is generated.
    """
    KIND_CHOICES = [
        ('registration',  'Business registration'),
        ('permit',        'Permit / license'),
        ('certification', 'Certification'),
        ('award',         'Award'),
        ('partnership',   'Partnership / membership'),
    ]

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='permit')
    title = models.CharField(max_length=150, help_text="e.g. Mayor's Permit, DTI Business Name Registration")
    issuer = models.CharField(max_length=150, blank=True, default='', help_text='e.g. City of Lucena, BIR, DTI')
    reference_no = models.CharField(max_length=80, blank=True, default='')
    show_reference_no = models.BooleanField(default=False)
    issued_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    document = models.FileField(upload_to='credentials/', null=True, blank=True)
    show_document = models.BooleanField(default=False, help_text='Let visitors view the uploaded document')
    is_public = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'kind', 'title']

    def __str__(self):
        return f"{self.get_kind_display()}: {self.title}"


class PricingSettings(models.Model):
    """
    The business's rate card (one row). Every estimate — AI Designer, Studio, shop
    delivery — is built from these rates. The defaults are SAMPLE rates: `is_sample`
    stays True until an admin saves real ones, and the admin screen says so.
    """
    # Planting labour per plant, by size class (see layout_engine.PlantSpec.layer)
    planting_tall = models.DecimalField(max_digits=10, decimal_places=2, default=350,
                                        help_text='Trees & palms (2.5 m+), per plant')
    planting_mid = models.DecimalField(max_digits=10, decimal_places=2, default=120,
                                       help_text='Shrubs (0.8–2.5 m), per plant')
    planting_low = models.DecimalField(max_digits=10, decimal_places=2, default=40,
                                       help_text='Flowers & groundcover, per plant')
    install_other = models.DecimalField(max_digits=10, decimal_places=2, default=100,
                                        help_text='Placing stones, furniture and other items, per item')
    # Per square metre of the garden area
    site_prep_per_sqm = models.DecimalField(max_digits=10, decimal_places=2, default=150,
                                            help_text='Clearing, levelling, soil preparation')
    materials_per_sqm = models.DecimalField(max_digits=10, decimal_places=2, default=120,
                                            help_text='Garden soil, compost, mulch')
    # Per project
    mobilization_fee = models.DecimalField(max_digits=10, decimal_places=2, default=1500,
                                           help_text='Crew transport & delivery within the service area')
    service_area = models.CharField(max_length=200, blank=True, default='Lucena City and nearby towns')
    design_fee_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10,
                                             help_text='Design & project management, % of the subtotal')
    minimum_project = models.DecimalField(max_digits=12, decimal_places=2, default=15000)
    estimate_range_percent = models.DecimalField(max_digits=5, decimal_places=2, default=10,
                                                 help_text='Estimates are shown as ± this %')
    vat_registered = models.BooleanField(default=False)
    vat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=12)
    # Shop
    shipping_fee = models.DecimalField(max_digits=10, decimal_places=2, default=150,
                                       help_text='Flat delivery fee per shop order')
    cod_max_amount = models.DecimalField(max_digits=12, decimal_places=2, default=5000,
                                         help_text='Largest order total allowed as Cash on Delivery (0 = no limit)')

    is_sample = models.BooleanField(default=True, help_text='True until an admin saves real rates')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'pricing settings'
        verbose_name_plural = 'pricing settings'

    def __str__(self):
        return 'Rate card' + (' (sample rates)' if self.is_sample else '')

    @classmethod
    def current(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            obj.refresh_from_db()  # defaults are plain numbers until read back as Decimals
        return obj


class LayoutGenerationLog(models.Model):
    """
    One AI Designer request: did Gemini answer, or did the smart layout engine
    cover for it? Feeds the admin "AI Designer health" view.
    """
    AI_STATUS_CHOICES = [
        ('ok',          'AI design generated'),
        ('cached',      'Served from cache'),
        ('error',       'AI failed — smart layouts used'),
        ('quota',       'Gemini daily limit reached — smart layouts used'),
        ('unavailable', 'AI paused after recent failures — smart layouts used'),
        ('user_quota',  "Customer's daily AI designs used — smart layouts used"),
        ('disabled',    'AI off (engine-only mode or no API key)'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    ai_status = models.CharField(max_length=12, choices=AI_STATUS_CHOICES)
    zone_scan = models.CharField(max_length=16, blank=True, default='')  # ok / none / failed / customer
    adjustments = models.PositiveSmallIntegerField(default=0)            # repair notes on the AI design
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Layout run {self.ai_status} ({self.duration_ms} ms)"


class FunnelEvent(models.Model):
    """
    One step of the guest 3D Studio → sign-up funnel, counted once per browser
    session. Anonymous by design: a random session id, no IP or personal data.
    """
    EVENT_CHOICES = [
        ('studio_opened',      'Opened the free 3D Studio'),
        ('first_item_placed',  'Placed a first item'),
        ('ai_teaser_clicked',  'Clicked "Let AI design it"'),
        ('save_clicked',       'Tried to save / photo / checkout'),
        ('account_created',    'Created an account from the Studio'),
        ('signed_in',          'Signed in from the Studio'),
    ]

    name = models.CharField(max_length=30, choices=EVENT_CHOICES)
    session_id = models.CharField(max_length=40)
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('session_id', 'name')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} [{self.session_id[:8]}]"


class ReviewModeration(models.Model):
    """
    Office moderation shared by service and product reviews. `is_public` stays the
    customer's own choice; staff can hide a public review but never publish a private one.
    """
    is_hidden = models.BooleanField(default=False, help_text='Hidden by an office admin (spam, abuse, personal data)')
    is_featured = models.BooleanField(default=False, help_text='Pinned to the landing page carousel')
    reply = models.TextField(blank=True, default='', help_text='Public response from the business')
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True


class ServiceReview(ReviewModeration):
    booking = models.OneToOneField(
        ServiceBooking, on_delete=models.CASCADE, related_name='review'
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, default='')
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Review by {self.user.username} for Booking #{self.booking_id} ({self.rating}★)"


class ProductReview(ReviewModeration):
    """Customer review of one purchased product, allowed once the order is Delivered."""
    # One review per purchased line — buying the same product again allows a new review
    order_item = models.OneToOneField(
        OrderItem, on_delete=models.CASCADE, related_name='review'
    )
    # Denormalized from order_item.item so product ratings can be aggregated directly
    product = models.ForeignKey(
        InventoryItem, on_delete=models.SET_NULL, null=True, related_name='reviews'
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='product_reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, default='')
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        product_name = self.product.name if self.product else "Deleted Item"
        return f"Review by {self.user.username} for {product_name} ({self.rating}★)"


class CashHandover(models.Model):
    """
    Cash on delivery money a crew member handed to the office. The orders it covers point at it
    (Order.cash_handover); a delivered COD order with no handover is cash still with the crew.
    """
    crew = models.ForeignKey(User, on_delete=models.PROTECT, related_name='cash_handovers')
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    expected = models.DecimalField(max_digits=12, decimal_places=2)   # sum of the orders' totals
    counted = models.DecimalField(max_digits=12, decimal_places=2)    # what the office actually counted
    note = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def difference(self):
        return self.counted - self.expected

    def __str__(self):
        return f'Cash from {self.crew.username}: ₱{self.counted:,.2f}'


class BusinessProfile(models.Model):
    """The business's public details (site footer, emails). One row, edited by the owner."""
    name = models.CharField(max_length=80, default='Garden Studio')
    tagline = models.CharField(max_length=160, blank=True, default='Design your garden in 3D, then let our own crew build it.')
    founded_year = models.PositiveSmallIntegerField(null=True, blank=True)
    phone = models.CharField(max_length=30, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    address = models.CharField(max_length=200, blank=True, default='')
    service_area = models.CharField(max_length=160, blank=True, default='Quezon Province and nearby areas')
    facebook = models.URLField(blank=True, default='')
    instagram = models.URLField(blank=True, default='')
    tiktok = models.URLField(blank=True, default='')
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @classmethod
    def current(cls):
        return cls.objects.get_or_create(pk=1)[0]


class ActivityLog(models.Model):
    """
    Who changed what, for the decisions an owner wants to be able to check later: prices, stock,
    refunds and cancellations, cash received, staff roles and business settings. Written by
    activity.record(); never edited.
    """
    AREAS = [('money', 'Money'), ('stock', 'Prices & stock'), ('people', 'People'), ('work', 'Orders & jobs'),
             ('settings', 'Settings')]
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    area = models.CharField(max_length=10, choices=AREAS, db_index=True)
    action = models.CharField(max_length=40)
    summary = models.CharField(max_length=300)
    link = models.CharField(max_length=200, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at', '-id']

    def __str__(self):
        return self.summary


class ProjectPayment(models.Model):
    """
    Money received on a project, one row per payment: online through a payment request, or
    recorded by the office (cash, bank transfer, a direct GCash/Maya transfer). The reports and
    "money received" read these rows; the project's remaining_balance is lowered at the same time.
    """
    METHODS = [('online', 'Online (PayMongo)'), ('cash', 'Cash'), ('bank', 'Bank transfer'),
               ('ewallet', 'GCash / Maya transfer'), ('other', 'Other')]
    project = models.ForeignKey(ProjectTracker, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=METHODS, default='cash')
    request = models.OneToOneField(ProjectPaymentRequest, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='payment')
    received_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=300, blank=True, default='')
    paid_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-paid_at']

    def __str__(self):
        return f'₱{self.amount:,.2f} on project #{self.project_id} ({self.method})'


class IdempotentRequest(models.Model):
    """One crew action, by its Idempotency-Key, so a retried request is done once (offline.py)."""
    key = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='+')
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=255)
    status_code = models.PositiveSmallIntegerField(default=0)   # 0 = still being processed
    content_type = models.CharField(max_length=100, blank=True, default='')
    body = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f'{self.method} {self.path} [{self.key[:8]}…]'


class PrivacyConsent(models.Model):
    """
    A customer agreed to the privacy notice (Data Privacy Act, RA 10173): which version and when,
    so the business can show that consent was given. Written at sign-up; never edited.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='privacy_consents')
    policy_version = models.CharField(max_length=20)
    accepted_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=20, default='sign_up')

    class Meta:
        ordering = ['-accepted_at']

    def __str__(self):
        return f'{self.user_id} accepted privacy notice {self.policy_version}'
