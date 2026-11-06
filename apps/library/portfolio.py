"""
Project Hub — the landscape business's public portfolio of finished jobs.

Lifecycle
  1. An office admin signs a booking off (Finished)  → create_portfolio_draft()
     prefills the entry from the booking, tracker, design and crew photos, and
     asks the customer for consent.
  2. The customer grants / declines consent           → PortfolioConsent
  3. An office admin curates photos & story and publishes (consent required).

Endpoints
  /api/portfolio/                         office admin: list, create {booking}, edit
  /api/portfolio/<id>/publish|unpublish|archive|request-consent|sync-media/
  /api/portfolio/<id>/media/              office admin: upload extra photo
  /api/portfolio/<id>/media/<media_id>/   office admin: edit / delete a photo
  /api/portfolio/candidates/              office admin: finished jobs without an entry
  /api/portfolio/public/                  anyone: published projects (filters)
  /api/portfolio/public/<slug>/           anyone: published detail; owner/admin preview
  /api/portfolio/mine/                    customer: their entries + consent
  /api/portfolio/mine/<id>/consent/       customer: grant / decline / withdraw

Privacy: public responses never include the street address, GPS, phone, full
customer name or exact price. Uploaded images are re-encoded to strip EXIF
(including GPS) metadata.
"""
import io
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import notifications
from .models import (
    Attendance, BusinessCredential, InventoryItem, PortfolioConsent, PortfolioMedia,
    PortfolioProject, ServiceBooking, ServiceReview, StaffAttendance,
)

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_EDGE = 2400
SERVICE_LABELS = dict(ServiceBooking.SERVICE_CHOICES)
PHASE_TO_KIND = {'BEFORE': 'before', 'DURING': 'during', 'AFTER': 'after'}

# Public price ranges — exact contract prices stay private
PRICE_BANDS = [
    (Decimal('25000'), 'Under ₱25K'),
    (Decimal('50000'), '₱25K – ₱50K'),
    (Decimal('100000'), '₱50K – ₱100K'),
    (Decimal('250000'), '₱100K – ₱250K'),
    (Decimal('500000'), '₱250K – ₱500K'),
    (None, '₱500K and up'),
]
PRICE_BAND_LABELS = [label for _, label in PRICE_BANDS]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_image(uploaded):
    """
    Validate an uploaded image and re-encode it as JPEG: applies the camera
    orientation, strips all EXIF metadata (including GPS) and caps the size.
    Returns (ContentFile, None) or (None, error message).
    """
    if uploaded.size > MAX_UPLOAD_BYTES:
        return None, 'Image is larger than 15 MB.'
    try:
        img = Image.open(uploaded)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        return None, 'File is not a valid image.'
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=86, optimize=True)  # no exif= → metadata dropped
    return ContentFile(out.getvalue(), name='photo.jpg'), None


def price_band(amount):
    if not amount:
        return ''
    for limit, label in PRICE_BANDS:
        if limit is None or amount < limit:
            return label
    return ''


_REGIONS = {
    'philippines', 'metro manila', 'ncr', 'national capital region', 'calabarzon', 'mimaropa', 'central luzon',
    'ilocos region', 'cagayan valley', 'bicol region', 'western visayas', 'central visayas', 'eastern visayas',
    'negros island region', 'zamboanga peninsula', 'northern mindanao', 'davao region', 'soccsksargen', 'caraga',
    'cordillera administrative region', 'car', 'barmm', 'bangsamoro',
}
_PROVINCES = {
    'abra', 'agusan del norte', 'agusan del sur', 'aklan', 'albay', 'antique', 'apayao', 'aurora', 'basilan',
    'bataan', 'batanes', 'batangas', 'benguet', 'biliran', 'bohol', 'bukidnon', 'bulacan', 'cagayan',
    'camarines norte', 'camarines sur', 'camiguin', 'capiz', 'catanduanes', 'cavite', 'cebu', 'cotabato',
    'compostela valley', 'davao de oro', 'davao del norte', 'davao del sur', 'davao occidental', 'davao oriental',
    'dinagat islands', 'eastern samar', 'guimaras', 'ifugao', 'ilocos norte', 'ilocos sur', 'iloilo', 'isabela',
    'kalinga', 'la union', 'laguna', 'lanao del norte', 'lanao del sur', 'leyte', 'maguindanao',
    'maguindanao del norte', 'maguindanao del sur', 'marinduque', 'masbate', 'misamis occidental',
    'misamis oriental', 'mountain province', 'negros occidental', 'negros oriental', 'northern samar',
    'nueva ecija', 'nueva vizcaya', 'occidental mindoro', 'oriental mindoro', 'palawan', 'pampanga', 'pangasinan',
    'quezon', 'quezon province', 'quirino', 'rizal', 'romblon', 'samar', 'sarangani', 'siquijor', 'sorsogon',
    'south cotabato', 'southern leyte', 'sultan kudarat', 'sulu', 'surigao del norte', 'surigao del sur', 'tarlac',
    'tawi-tawi', 'zambales', 'zamboanga del norte', 'zamboanga del sur', 'zamboanga sibugay',
}
_STREETLIKE = re.compile(
    r'\d|\b(st|street|rd|road|ave|avenue|blvd|boulevard|hwy|highway|purok|sitio|brgy|barangay|subd|subdivision|'
    r'village|vill|phase|blk|block|lot|zone|compound|cmpd|bldg|building|tower|unit)\b', re.IGNORECASE)


def city_from_address(address):
    """
    City (or town) only — never the street. Handles typed addresses
    ('12 Mabini St., Makati, Metro Manila' → 'Makati') and reverse-geocoded ones
    ('Pinag-isa, Cotta, Lucena, 2nd District, Calabarzon, 4301, Philippines' → 'Lucena').
    """
    parts = [p.strip() for p in (address or '').split(',') if p.strip()]
    places, provinces = [], []
    for part in parts:
        low = part.lower()
        if low in _REGIONS or low.startswith('region ') or re.fullmatch(r'\d{4}', part) or re.search(r'\bdistrict\b', low):
            continue
        if low in _PROVINCES:
            provinces.append(part)
        elif not _STREETLIKE.search(part):
            places.append(part)
    if places:
        return places[-1][:80]
    return provinces[-1] if provinces else ''


def _unique_slug(title, pk_hint=''):
    base = slugify(title)[:110] or 'project'
    slug, n = f"{base}-{pk_hint}" if pk_hint else base, 2
    while PortfolioProject.objects.filter(slug=slug).exists():
        slug = f"{base}-{pk_hint}-{n}" if pk_hint else f"{base}-{n}"
        n += 1
    return slug


def _is_office_admin(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    role = getattr(getattr(user, 'staff_profile', None), 'role', None)
    return user.is_staff and role != 'FIELD_CREW'


def import_progress_media(project):
    """Copy crew progress photos into the portfolio (hidden until the admin picks them)."""
    try:
        tracker = project.booking.tracker
    except ServiceBooking.tracker.RelatedObjectDoesNotExist:
        return 0
    existing = set(project.media.exclude(source=None).values_list('source_id', flat=True))
    next_order = (project.media.order_by('-sort_order').values_list('sort_order', flat=True).first() or 0) + 1
    created = 0
    for pm in tracker.progress_media.order_by('uploaded_at'):
        if pm.id in existing or not pm.file_path:
            continue
        media = PortfolioMedia(
            project=project, kind=PHASE_TO_KIND.get(pm.construction_phase, 'detail'),
            caption=(pm.description or '')[:200], is_public=False, sort_order=next_order, source=pm,
        )
        media.image.name = pm.file_path.name  # share the stored file, no copy
        media.save()
        next_order += 1
        created += 1
    if created and not project.cover_id:
        project.cover = project.media.filter(kind='after').first() or project.media.first()
        project.save(update_fields=['cover'])
    return created


def create_portfolio_draft(booking, user=None):
    """Idempotently draft a portfolio entry for a finished booking and ask the customer for consent."""
    existing = PortfolioProject.objects.filter(booking=booking).first()
    if existing:
        return existing

    city = city_from_address(booking.service_address)
    label = SERVICE_LABELS.get(booking.service_type, booking.service_type.title())
    title = f"{label} in {city}" if city else label

    try:
        amount = booking.tracker.total_price
    except ServiceBooking.tracker.RelatedObjectDoesNotExist:
        amount = None
    if not amount and booking.design:
        amount = booking.design.total_cost

    lot_area = None
    dims = (booking.design.dimensions or {}) if booking.design else {}
    try:
        lot_area = round(float(dims['width']) * float(dims['length']), 1)
    except (KeyError, TypeError, ValueError):
        pass

    first_clock_in = (
        Attendance.objects.filter(booking=booking, clock_in_time__isnull=False)
        .order_by('clock_in_time').values_list('clock_in_time', flat=True).first()
    )

    with transaction.atomic():
        project = PortfolioProject.objects.create(
            booking=booking, title=title, slug=_unique_slug(title, booking.id),
            city=city, lot_area_sqm=lot_area, price_band=price_band(amount),
            started_on=first_clock_in.date() if first_clock_in else booking.scheduled_date,
            completed_on=timezone.localdate(), created_by=user,
        )
        PortfolioConsent.objects.create(project=project, customer=booking.user)
        import_progress_media(project)
        notifications.notify([booking.user], 'booking_finished', (
            f"Your {label.lower()} project is complete! We'd love to feature your garden in our "
            f"project portfolio — open My Bookings to choose your privacy options."
        ), link=notifications.MY_BOOKINGS)
    return project


def unpublish_for_consent(project, reason):
    if project.status == 'published':
        project.status = 'draft'
        project.save(update_fields=['status', 'updated_at'])
    notifications.notify(notifications.office_users(), 'portfolio', f'Portfolio "{project.title}": {reason}',
                         link=lambda u: '/portfolio-manager')


def _media_url(request, field):
    if not field:
        return None
    return request.build_absolute_uri(field.url) if request else field.url


def public_media(project):
    """Photos visitors may see: marked public, and no house shots unless the customer allowed them."""
    qs = project.media.filter(is_public=True)
    consent = getattr(project, 'consent', None)
    if not (consent and consent.status == 'granted' and consent.show_house):
        qs = qs.filter(shows_house=False)
    return qs


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def design_layout(design):
    """The 3D layout for the read-only viewer. Never the reference photo (usually the customer's house)."""
    if not design or not design.placed_items:
        return None
    dims = design.dimensions if isinstance(design.dimensions, dict) else {}
    return {'placed_items': design.placed_items, 'width': dims.get('width'), 'length': dims.get('length')}


FINISHED_STATUSES = ('Finished', 'Completed')


def build_track_record():
    """Live track-record numbers, computed from signed-off jobs — nothing typed in by hand."""
    finished = ServiceBooking.objects.filter(status__in=FINISHED_STATUSES)
    area, plants = 0.0, 0
    for dims, breakdown in finished.filter(design__isnull=False).values_list('design__dimensions', 'design__plant_breakdown'):
        if isinstance(dims, dict):
            try:
                area += float(dims['width']) * float(dims['length'])
            except (KeyError, TypeError, ValueError):
                pass
        for row in breakdown if isinstance(breakdown, list) else []:
            try:
                if row.get('category', 'plant') != 'plant':
                    continue   # benches and rocks aren't plants planted
                plants += max(0, int(row.get('quantity') or 0))
            except (AttributeError, TypeError, ValueError):
                pass
    reviews = ServiceReview.objects.filter(is_public=True).aggregate(avg=Avg('rating'), n=Count('id'))
    per_customer = finished.values('user').annotate(n=Count('id'))
    founded = getattr(settings, 'BUSINESS_FOUNDED_YEAR', None)
    return {
        'projects_completed': finished.count(),
        'published_projects': PortfolioProject.objects.filter(status='published').count(),
        'area_sqm': round(area),
        'plants_installed': plants,
        'average_rating': round(reviews['avg'], 1) if reviews['avg'] else None,
        'review_count': reviews['n'],
        'customers': per_customer.count(),
        'repeat_customers': per_customer.filter(n__gte=2).count(),
        'founded_year': founded,
        'years_in_business': (timezone.localdate().year - founded) if founded else None,
    }


def _is_verified_on_site(booking):
    return (
        StaffAttendance.objects.filter(booking=booking, gps_lat_checkin__isnull=False).exists()
        or Attendance.objects.filter(booking=booking, latitude__isnull=False).exists()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────────────────────────────────────

class PortfolioMediaSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = PortfolioMedia
        fields = ['id', 'url', 'kind', 'caption', 'is_public', 'shows_house', 'sort_order', 'source']
        read_only_fields = ['id', 'url', 'source']

    def get_url(self, obj):
        return _media_url(self.context.get('request'), obj.image)


class PortfolioConsentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = PortfolioConsent
        fields = ['status', 'status_display', 'name_display', 'show_house', 'include_review',
                  'requested_at', 'responded_at']
        read_only_fields = fields


class PortfolioAdminSerializer(serializers.ModelSerializer):
    media = PortfolioMediaSerializer(many=True, read_only=True)
    consent = PortfolioConsentSerializer(read_only=True)
    cover_url = serializers.SerializerMethodField()
    booking_info = serializers.SerializerMethodField()
    stage = serializers.SerializerMethodField()
    design = serializers.SerializerMethodField()

    class Meta:
        model = PortfolioProject
        fields = ['id', 'booking', 'booking_info', 'title', 'slug', 'summary', 'story', 'style_tags',
                  'city', 'lot_area_sqm', 'price_band', 'started_on', 'completed_on', 'status', 'stage',
                  'is_featured', 'cover', 'cover_url', 'view_count', 'published_at', 'media', 'consent',
                  'design', 'created_at', 'updated_at']
        read_only_fields = ['id', 'booking', 'booking_info', 'slug', 'status', 'stage', 'cover_url',
                            'view_count', 'published_at', 'media', 'consent', 'design', 'created_at', 'updated_at']

    def get_design(self, obj):
        return design_layout(obj.booking.design)

    def validate_price_band(self, value):
        if value and value not in PRICE_BAND_LABELS:
            raise serializers.ValidationError(f"Choose one of: {', '.join(PRICE_BAND_LABELS)}")
        return value

    def validate_style_tags(self, value):
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            raise serializers.ValidationError('style_tags must be a list of strings.')
        return [t.strip()[:30] for t in value if t.strip()][:8]

    def validate_cover(self, media):
        if media and media.project_id != self.instance.id:
            raise serializers.ValidationError('Cover must be one of this project\'s photos.')
        return media

    def get_cover_url(self, obj):
        return _media_url(self.context.get('request'), obj.cover.image if obj.cover else None)

    def get_stage(self, obj):
        """Admin workflow stage, combining publish status with customer consent."""
        if obj.status != 'draft':
            return obj.status
        consent = getattr(obj, 'consent', None)
        if consent is None or consent.status == 'pending':
            return 'awaiting_consent'
        if consent.status == 'granted':
            return 'ready'
        return 'consent_refused'

    def get_booking_info(self, obj):
        b = obj.booking
        review = getattr(b, 'review', None)
        return {
            'id': b.id,
            'service_type': SERVICE_LABELS.get(b.service_type, b.service_type),
            'customer_name': b.user.get_full_name() or b.user.username,
            'scheduled_date': b.scheduled_date,
            'crew': [u.get_full_name() or u.username for u in b.assigned_crew.all()],
            'review': {'rating': review.rating, 'comment': review.comment} if review else None,
            'has_design': bool(b.design_id),
        }


class PortfolioPublicListSerializer(serializers.ModelSerializer):
    cover_url = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    service_key = serializers.CharField(source='booking.service_type', read_only=True)

    class Meta:
        model = PortfolioProject
        fields = ['title', 'slug', 'summary', 'style_tags', 'service_type', 'service_key', 'city',
                  'lot_area_sqm', 'price_band', 'completed_on', 'is_featured', 'cover_url']

    def get_cover_url(self, obj):
        visible = public_media(obj)
        cover = obj.cover if obj.cover_id and visible.filter(pk=obj.cover_id).exists() else visible.first()
        return _media_url(self.context.get('request'), cover.image if cover else None)

    def get_service_type(self, obj):
        return SERVICE_LABELS.get(obj.booking.service_type, obj.booking.service_type)


class PortfolioPublicDetailSerializer(PortfolioPublicListSerializer):
    media = serializers.SerializerMethodField()
    duration_days = serializers.SerializerMethodField()
    client = serializers.SerializerMethodField()
    testimonial = serializers.SerializerMethodField()
    crew = serializers.SerializerMethodField()
    plants = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()
    design = serializers.SerializerMethodField()

    class Meta(PortfolioPublicListSerializer.Meta):
        fields = PortfolioPublicListSerializer.Meta.fields + [
            'story', 'started_on', 'duration_days', 'media', 'client', 'testimonial', 'crew', 'plants', 'verified',
            'design',
        ]

    def get_design(self, obj):
        return design_layout(obj.booking.design)

    def get_media(self, obj):
        request = self.context.get('request')
        return [{'id': m.id, 'url': _media_url(request, m.image), 'kind': m.kind, 'caption': m.caption}
                for m in public_media(obj)]

    def get_duration_days(self, obj):
        if obj.started_on and obj.completed_on:
            return max(1, (obj.completed_on - obj.started_on).days + 1)
        return None

    def _consent(self, obj):
        consent = getattr(obj, 'consent', None)
        return consent if consent and consent.status == 'granted' else None

    def get_client(self, obj):
        consent = self._consent(obj)
        user = obj.booking.user
        if consent and consent.name_display == 'initials' and user.first_name:
            last = user.last_name.strip()
            return f"{user.first_name.strip()} {last[0]}." if last else user.first_name.strip()
        return 'Private client'

    def get_testimonial(self, obj):
        consent = self._consent(obj)
        review = getattr(obj.booking, 'review', None)
        if consent and consent.include_review and review and review.is_public:
            return {'rating': review.rating, 'comment': review.comment}
        return None

    def get_crew(self, obj):
        return [u.first_name or u.username for u in obj.booking.assigned_crew.all()]

    def get_plants(self, obj):
        design = obj.booking.design
        if not design:
            return []
        request = self.context.get('request')
        items = list(InventoryItem.objects.only('id', 'name', 'category', 'unit_price', 'image_url', 'thumbnail', 'stock_quantity'))
        by_id = {i.id: i for i in items}
        by_name = {i.name.lower(): i for i in items}
        plants = []
        for row in design.plant_breakdown if isinstance(design.plant_breakdown, list) else []:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name') or '').strip()
            if not name:
                continue
            try:
                item = by_id.get(int(row.get('plant_id')))
            except (TypeError, ValueError):
                item = None
            item = item or by_name.get(name.lower())
            plants.append({
                'name': name,
                'quantity': row.get('quantity'),
                'category': item.category if item else row.get('category', 'plant'),
                'product_id': item.id if item else None,
                # Shop the look — current catalog price, not what this customer paid
                'product': {
                    'price': float(item.unit_price),
                    'image_url': _media_url(request, item.thumbnail) if item.thumbnail else (item.image_url or None),
                    'in_stock': item.stock_quantity > 0,
                } if item else None,
            })
        return plants

    def get_verified(self, obj):
        booking = obj.booking
        return {
            'on_site_gps': _is_verified_on_site(booking),
            'signed_off': booking.status in ('Finished', 'Completed'),
            'customer_reviewed': hasattr(booking, 'review'),
        }


class PortfolioMineSerializer(serializers.ModelSerializer):
    consent = PortfolioConsentSerializer(read_only=True)
    cover_url = serializers.SerializerMethodField()

    class Meta:
        model = PortfolioProject
        fields = ['id', 'booking', 'title', 'slug', 'status', 'consent', 'cover_url']

    def get_cover_url(self, obj):
        return _media_url(self.context.get('request'), obj.cover.image if obj.cover else None)


CREDENTIAL_EXTENSIONS = ('.pdf', '.jpg', '.jpeg', '.png', '.webp')
MAX_CREDENTIAL_BYTES = 10 * 1024 * 1024


class BusinessCredentialSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)
    document_url = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    expires_soon = serializers.SerializerMethodField()

    class Meta:
        model = BusinessCredential
        fields = ['id', 'kind', 'kind_display', 'title', 'issuer', 'reference_no', 'show_reference_no',
                  'issued_on', 'expires_on', 'document', 'document_url', 'show_document', 'is_public',
                  'sort_order', 'is_expired', 'expires_soon', 'updated_at']
        read_only_fields = ['id', 'kind_display', 'document_url', 'is_expired', 'expires_soon', 'updated_at']
        extra_kwargs = {'document': {'write_only': True, 'required': False, 'allow_null': True}}

    def validate_document(self, upload):
        if upload is None:
            return upload
        if not upload.name.lower().endswith(CREDENTIAL_EXTENSIONS):
            raise serializers.ValidationError('Upload a PDF or an image (JPG, PNG, WEBP).')
        if upload.size > MAX_CREDENTIAL_BYTES:
            raise serializers.ValidationError('The file is larger than 10 MB.')
        return upload

    def validate(self, attrs):
        issued = attrs.get('issued_on', getattr(self.instance, 'issued_on', None))
        expires = attrs.get('expires_on', getattr(self.instance, 'expires_on', None))
        if issued and expires and expires < issued:
            raise serializers.ValidationError({'expires_on': 'The expiry date is before the issue date.'})
        return attrs

    def get_document_url(self, obj):
        return _media_url(self.context.get('request'), obj.document)

    def get_is_expired(self, obj):
        return bool(obj.expires_on and obj.expires_on < timezone.localdate())

    def get_expires_soon(self, obj):
        today = timezone.localdate()
        return bool(obj.expires_on and today <= obj.expires_on <= today + timedelta(days=30))


def public_credential(obj, request):
    return {
        'kind': obj.kind,
        'kind_display': obj.get_kind_display(),
        'title': obj.title,
        'issuer': obj.issuer,
        'reference_no': obj.reference_no if obj.show_reference_no else '',
        'issued_on': obj.issued_on,
        'expires_on': obj.expires_on,
        'document_url': _media_url(request, obj.document) if obj.show_document and obj.document else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

class IsPortfolioAdmin(IsAuthenticated):
    def has_permission(self, request, view):
        return _is_office_admin(request.user)


class PortfolioViewSet(viewsets.ModelViewSet):
    serializer_class = PortfolioAdminSerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_permissions(self):
        if self.action in ('public_list', 'public_detail', 'public_showcase', 'track_record'):
            return [AllowAny()]
        if self.action in ('mine', 'consent'):
            return [IsAuthenticated()]
        return [IsPortfolioAdmin()]

    def get_queryset(self):
        qs = PortfolioProject.objects.select_related(
            'booking', 'booking__user', 'booking__design', 'cover', 'consent',
        ).prefetch_related('media', 'booking__assigned_crew')
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    # ── admin: create / edit ────────────────────────────────────────────────
    def create(self, request, *args, **kwargs):
        booking = get_object_or_404(ServiceBooking, pk=request.data.get('booking'))
        if booking.status not in ('Finished', 'Completed'):
            return Response({'error': 'Only signed-off (Finished) projects can be added to the portfolio.'},
                            status=status.HTTP_400_BAD_REQUEST)
        project = create_portfolio_draft(booking, request.user)
        return Response(self.get_serializer(project).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        project = self.get_object()
        if project.status == 'published':
            return Response({'error': 'Unpublish the project before deleting it.'}, status=status.HTTP_400_BAD_REQUEST)
        project.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'])
    def candidates(self, request):
        """Finished jobs that don't have a portfolio entry yet (most recent first)."""
        bookings = (ServiceBooking.objects.filter(status__in=('Finished', 'Completed'), portfolio__isnull=True)
                    .select_related('user').order_by('-scheduled_date')[:50])
        return Response([{
            'id': b.id,
            'service_type': SERVICE_LABELS.get(b.service_type, b.service_type),
            'customer_name': b.user.get_full_name() or b.user.username,
            'city': city_from_address(b.service_address),
            'scheduled_date': b.scheduled_date,
        } for b in bookings])

    # ── admin: workflow ─────────────────────────────────────────────────────
    @action(detail=True, methods=['post'])
    def publish(self, request, pk=None):
        project = self.get_object()
        problems = []
        if getattr(project, 'consent', None) is None or project.consent.status != 'granted':
            problems.append('the customer has not given consent')
        if not public_media(project).exists():
            problems.append('no photos are marked public' + (
                ' (photos showing the house stay hidden — the customer did not allow them)'
                if project.media.filter(is_public=True).exists() else ''))
        if not project.title.strip():
            problems.append('the title is empty')
        if problems:
            return Response({'error': 'Cannot publish: ' + '; '.join(problems) + '.'}, status=status.HTTP_400_BAD_REQUEST)
        project.status = 'published'
        project.published_at = project.published_at or timezone.now()
        project.save()
        return Response(self.get_serializer(project).data)

    @action(detail=True, methods=['post'])
    def unpublish(self, request, pk=None):
        project = self.get_object()
        project.status = 'draft'
        project.save()
        return Response(self.get_serializer(project).data)

    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        project = self.get_object()
        project.status = 'archived'
        project.is_featured = False
        project.save()
        return Response(self.get_serializer(project).data)

    @action(detail=True, methods=['post'], url_path='request-consent')
    def request_consent(self, request, pk=None):
        project = self.get_object()
        consent, _ = PortfolioConsent.objects.get_or_create(project=project, defaults={'customer': project.booking.user})
        if consent.status == 'granted':
            return Response({'error': 'Consent has already been granted.'}, status=status.HTTP_400_BAD_REQUEST)
        consent.status = 'pending'
        consent.save()
        notifications.notify([consent.customer], 'portfolio_consent', (
            f'Reminder: may we feature your garden ("{project.title}") in our project portfolio? '
            f'Open My Bookings to choose your privacy options.'
        ), link=notifications.MY_BOOKINGS)
        return Response(self.get_serializer(project).data)

    @action(detail=True, methods=['post'], url_path='sync-media')
    def sync_media(self, request, pk=None):
        project = self.get_object()
        added = import_progress_media(project)
        data = self.get_serializer(project).data
        data['imported'] = added
        return Response(data)

    @action(detail=True, methods=['post'], url_path='media')
    def upload_media(self, request, pk=None):
        project = self.get_object()
        upload = request.FILES.get('image')
        if not upload:
            return Response({'error': 'An image file is required (field name: image).'}, status=status.HTTP_400_BAD_REQUEST)
        clean, error = sanitize_image(upload)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
        kind = request.data.get('kind', 'after')
        if kind not in dict(PortfolioMedia.KIND_CHOICES):
            kind = 'after'
        next_order = (project.media.order_by('-sort_order').values_list('sort_order', flat=True).first() or 0) + 1
        media = PortfolioMedia(project=project, kind=kind, caption=(request.data.get('caption') or '')[:200],
                               is_public=True, sort_order=next_order)
        media.image.save(f'portfolio_{project.id}.jpg', clean, save=False)
        media.save()
        if not project.cover_id:
            project.cover = media
            project.save(update_fields=['cover'])
        return Response(PortfolioMediaSerializer(media, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'], url_path=r'media/(?P<media_id>\d+)')
    def media_item(self, request, pk=None, media_id=None):
        project = self.get_object()
        media = get_object_or_404(PortfolioMedia, pk=media_id, project=project)
        if request.method == 'DELETE':
            # Imported crew photos keep their file (it still belongs to the progress record)
            if media.source_id is None:
                media.image.delete(save=False)
            media.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = PortfolioMediaSerializer(media, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    # ── public ──────────────────────────────────────────────────────────────
    @action(detail=False, methods=['get'], url_path='public')
    def public_list(self, request):
        published = list(PortfolioProject.objects.filter(status='published').select_related('booking'))
        qs = (PortfolioProject.objects.filter(status='published')
              .select_related('booking', 'cover', 'consent'))
        params = request.query_params
        if params.get('featured') in ('1', 'true'):
            qs = qs.filter(is_featured=True)
        if params.get('service'):
            label_to_key = {v.lower(): k for k, v in SERVICE_LABELS.items()}
            qs = qs.filter(booking__service_type=label_to_key.get(params['service'].lower(), params['service']))
        if params.get('city'):
            qs = qs.filter(city__iexact=params['city'])
        if params.get('price_band'):
            qs = qs.filter(price_band=params['price_band'])
        projects = list(qs)
        if params.get('style'):
            style = params['style'].lower()
            projects = [p for p in projects if style in [t.lower() for t in p.style_tags]]
        projects.sort(key=lambda p: (not p.is_featured, -(p.completed_on.toordinal() if p.completed_on else 0)))
        try:
            limit = max(1, min(int(params.get('limit', 60)), 60))
        except ValueError:
            limit = 60
        data = PortfolioPublicListSerializer(projects[:limit], many=True, context={'request': request}).data
        return Response({
            'results': data,
            # Options come from every published project so choosing one filter doesn't hide the others
            'filters': {
                'services': sorted({SERVICE_LABELS.get(p.booking.service_type, p.booking.service_type) for p in published}),
                'cities': sorted({p.city for p in published if p.city}),
                'styles': sorted({t for p in published for t in p.style_tags}),
                'price_bands': [b for b in PRICE_BAND_LABELS if any(p.price_band == b for p in published)],
            },
        })

    # Not under public/: that prefix is taken by public/<slug> (a project called "showcase" would clash)
    @action(detail=False, methods=['get'], url_path='showcase')
    def public_showcase(self, request):
        """Landing page proof: published projects with a Before/After pair and/or a 3D design/Built pair.
        Same visibility rules as the project page (public photos only, house shots only with consent)."""
        try:
            limit = max(1, min(int(request.query_params.get('limit', 3)), 6))
        except ValueError:
            limit = 3
        projects = (PortfolioProject.objects.filter(status='published')
                    .select_related('booking', 'cover', 'consent').order_by('-is_featured', '-completed_on', '-id'))
        out = []
        for project in projects:
            photos = list(public_media(project))
            first = lambda kind: next((m for m in photos if m.kind == kind), None)  # noqa: E731
            cover = next((m for m in photos if project.cover_id == m.id and m.kind == 'after'), None)
            before, after, render = first('before'), cover or first('after'), first('render')
            if not after or not (before or render):
                continue
            url = lambda m: _media_url(request, m.image) if m else None  # noqa: E731
            out.append({
                'slug': project.slug, 'title': project.title, 'city': project.city,
                'service_type': SERVICE_LABELS.get(project.booking.service_type, project.booking.service_type),
                'completed_on': project.completed_on,
                'duration_days': max(1, (project.completed_on - project.started_on).days + 1)
                if project.started_on and project.completed_on else None,
                'before_url': url(before), 'after_url': url(after), 'render_url': url(render),
            })
            if len(out) == limit:
                break
        return Response({'results': out})

    @action(detail=False, methods=['get'], url_path='track-record')
    def track_record(self, request):
        """Public: live numbers + the business's current (non-expired) credentials."""
        today = timezone.localdate()
        credentials = BusinessCredential.objects.filter(is_public=True).filter(
            Q(expires_on__isnull=True) | Q(expires_on__gte=today))
        return Response({
            'stats': build_track_record(),
            'credentials': [public_credential(c, request) for c in credentials],
            'as_of': today,
        })

    @action(detail=False, methods=['get'], url_path=r'public/(?P<slug>[-\w]+)')
    def public_detail(self, request, slug=None):
        project = get_object_or_404(
            PortfolioProject.objects.select_related('booking', 'booking__user', 'booking__design', 'cover', 'consent'),
            slug=slug,
        )
        user = request.user
        is_owner = user.is_authenticated and project.booking.user_id == user.id
        is_admin = _is_office_admin(user)
        if project.status != 'published' and not (is_owner or is_admin):
            return Response({'error': 'Project not found.'}, status=status.HTTP_404_NOT_FOUND)
        if project.status == 'published' and not (is_owner or is_admin):
            PortfolioProject.objects.filter(pk=project.pk).update(view_count=project.view_count + 1)
        data = PortfolioPublicDetailSerializer(project, context={'request': request}).data
        data['is_preview'] = project.status != 'published'
        return Response(data)

    # ── customer ────────────────────────────────────────────────────────────
    @action(detail=False, methods=['get'])
    def mine(self, request):
        projects = (PortfolioProject.objects.filter(booking__user=request.user)
                    .exclude(status='archived').select_related('consent', 'cover'))
        return Response(PortfolioMineSerializer(projects, many=True, context={'request': request}).data)

    @action(detail=False, methods=['post'], url_path=r'mine/(?P<project_id>\d+)/consent')
    def consent(self, request, project_id=None):
        project = get_object_or_404(PortfolioProject, pk=project_id, booking__user=request.user)
        consent, _ = PortfolioConsent.objects.get_or_create(project=project, defaults={'customer': request.user})
        decision = request.data.get('decision')
        if decision not in ('grant', 'decline', 'withdraw'):
            return Response({'error': "decision must be 'grant', 'decline' or 'withdraw'."}, status=status.HTTP_400_BAD_REQUEST)

        if decision == 'grant':
            name_display = request.data.get('name_display', 'initials')
            if name_display not in dict(PortfolioConsent.NAME_CHOICES):
                return Response({'error': 'Invalid name_display.'}, status=status.HTTP_400_BAD_REQUEST)
            consent.status = 'granted'
            consent.name_display = name_display
            consent.show_house = _as_bool(request.data.get('show_house'), True)
            consent.include_review = _as_bool(request.data.get('include_review'), True)
        else:
            consent.status = 'declined' if decision == 'decline' else 'withdrawn'
        consent.responded_at = timezone.now()
        consent.save()

        if decision == 'withdraw':
            unpublish_for_consent(project, 'the customer withdrew consent, so it was unpublished.')
        elif decision == 'decline':
            unpublish_for_consent(project, 'the customer declined to be featured.')
        return Response(PortfolioMineSerializer(project, context={'request': request}).data)


class BusinessCredentialViewSet(viewsets.ModelViewSet):
    """
    /api/business-credentials/   office admin: permits, registrations, awards for the Track Record page.
    Multipart for document uploads; send document=null (JSON) to remove a file.
    """
    serializer_class = BusinessCredentialSerializer
    permission_classes = [IsPortfolioAdmin]
    queryset = BusinessCredential.objects.all()

    def perform_update(self, serializer):
        old = serializer.instance.document
        replaced = 'document' in serializer.validated_data and old
        old_name = old.name if replaced else None
        instance = serializer.save()
        if old_name and instance.document.name != old_name:
            instance.document.storage.delete(old_name)

    def perform_destroy(self, instance):
        if instance.document:
            instance.document.delete(save=False)
        instance.delete()
