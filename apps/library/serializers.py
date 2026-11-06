from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.models import User
from .work_queues import order_needs_refund, order_next_statuses
from .portfolio import sanitize_image
from .models import (
    InventoryItem, GardenDesign, GardenImage, BlackoutDate, ServiceBooking,
    Order, OrderItem, Attendance, ProjectMilestone, StaffAttendance, StockMovement,
    StaffProfile, Notification, ServiceReview, ProductReview,
    ProjectTracker, ProjectHistoryLog, ProjectProgressMedia,
)

def _resolve_canonical_role(user):
    """
    Return the canonical 4-tier role string for a user.

    Canonical values  →  what gets embedded in the JWT and returned on login
    ─────────────────────────────────────────────────────────────────────────
    'SUPER_ADMIN'   superuser flag
    'OFFICE_ADMIN'  is_staff + StaffProfile.role in ('OFFICE_ADMIN', 'Staff', None/missing)
    'FIELD_CREW'    is_staff + StaffProfile.role == 'FIELD_CREW'
    'CUSTOMER'      regular user, no staff flags
    """
    if user.is_superuser:
        return 'SUPER_ADMIN'
    if user.is_staff:
        raw_role = None
        try:
            raw_role = user.staff_profile.role
        except Exception:
            pass
        # Normalise legacy 'Staff' → 'OFFICE_ADMIN' at token-issuance time
        return 'FIELD_CREW' if raw_role == 'FIELD_CREW' else 'OFFICE_ADMIN'
    return 'CUSTOMER'


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['username'] = user.username
        token['first_name'] = user.first_name
        token['last_name'] = user.last_name
        token['email'] = user.email
        token['is_staff'] = user.is_staff
        token['is_superuser'] = user.is_superuser

        # Raw DB role — kept for backward compatibility (user.staff_role on frontend)
        try:
            token['staff_role'] = user.staff_profile.role
        except Exception:
            token['staff_role'] = None

        # Canonical role enum — readable as user.role on the frontend after jwtDecode()
        # Normalises legacy 'Staff' values so the frontend never sees an unexpected string
        token['role'] = _resolve_canonical_role(user)

        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data['username'] = self.user.username
        data['email'] = self.user.email
        # Login response carries the same canonical role the token contains
        data['role'] = _resolve_canonical_role(self.user)
        return data


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    first_name = serializers.CharField(required=False, allow_blank=True, default='')
    last_name = serializers.CharField(required=False, allow_blank=True, default='')

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name', 'password')

    def validate(self, attrs):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        candidate = User(username=attrs.get('username', ''), email=attrs.get('email', ''),
                         first_name=attrs.get('first_name', ''), last_name=attrs.get('last_name', ''))
        try:
            validate_password(attrs.get('password', ''), candidate)
        except DjangoValidationError as e:
            raise serializers.ValidationError({'password': list(e.messages)})
        return attrs

    def create(self, validated_data):
        user = User.objects.create(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
            is_staff=False,
            is_superuser=False
        )
        user.set_password(validated_data['password'])
        user.save()
        return user


# Change this (and the date on the frontend's /privacy page) whenever the privacy notice changes
PRIVACY_POLICY_VERSION = '2026-10-23'


class RegisterSerializer(UserSerializer):
    """Public sign-up: the customer must agree to the privacy notice, and that agreement is recorded."""
    accept_privacy = serializers.BooleanField(write_only=True, required=True)

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ('accept_privacy',)

    def validate_accept_privacy(self, value):
        if not value:
            raise serializers.ValidationError('Please read and agree to the Privacy Notice to create an account.')
        return value

    def create(self, validated_data):
        from django.db import transaction
        from .models import PrivacyConsent
        validated_data.pop('accept_privacy')
        with transaction.atomic():
            user = super().create(validated_data)
            PrivacyConsent.objects.create(user=user, policy_version=PRIVACY_POLICY_VERSION)
        return user




class InventoryItemSerializer(serializers.ModelSerializer):
    # The photo is an upload (full URL in responses); the 3D model is the name of a model bundled with
    # the frontend (public/models, data/modelCatalog.js), e.g. "bamboo.glb". Blank = no 3D model.
    model_file = serializers.CharField(required=False, allow_blank=True, max_length=100)
    thumbnail = serializers.ImageField(required=False, allow_null=True)
    is_low_stock = serializers.BooleanField(read_only=True)
    # Populated by InventoryItemViewSet's annotated queryset (public product reviews)
    avg_rating = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()

    class Meta:
        model = InventoryItem
        fields = ['id', 'name', 'category', 'description', 'stock_quantity',
                  'unit_price', 'image_url', 'model_file', 'thumbnail', 'real_world_size', 'spacing_cm',
                  'is_active', 'reorder_level', 'is_low_stock', 'avg_rating', 'review_count',
                  'sunlight', 'water_need', 'is_invasive', 'planting_height', 'mature_height']
        read_only_fields = ['is_active']   # archive / restore have their own actions

    def get_avg_rating(self, obj):
        avg = getattr(obj, 'avg_rating', None)
        return round(avg, 1) if avg is not None else None

    def get_review_count(self, obj):
        return getattr(obj, 'review_count', 0)

    def validate_thumbnail(self, value):
        if not value:
            return value
        # Same treatment as portfolio photos: fix orientation, strip metadata (GPS), cap the size
        clean, error = sanitize_image(value)
        if error:
            raise serializers.ValidationError(error)
        clean.name = 'product.jpg'
        return clean

    def validate_model_file(self, value):
        from .inventory import MODEL_NAME
        value = (value or '').strip().lower()
        if value and not MODEL_NAME.match(value):
            raise serializers.ValidationError('Pick one of the 3D models in the list.')
        return value

    def validate_real_world_size(self, value):
        # Metres: height for plants/furniture, longest side for hardscape (frontend data/modelSizing.js)
        if value is None or not (0.05 <= float(value) <= 30):
            raise serializers.ValidationError('Enter a size between 0.05 and 30 metres.')
        return round(float(value), 2)

    def validate_unit_price(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('Enter a price above ₱0.')
        return value

    def _height(self, value):
        if value in (None, ''):
            return None
        if not (0.05 <= float(value) <= 40):
            raise serializers.ValidationError('Enter a height between 0.05 and 40 metres, or leave it empty.')
        return round(float(value), 2)

    def validate_planting_height(self, value):
        return self._height(value)

    def validate_mature_height(self, value):
        return self._height(value)

    def validate(self, attrs):
        # Growth heights belong to plants and must read: at planting ≤ established ≤ fully grown
        inst = self.instance
        category = attrs.get('category', getattr(inst, 'category', 'plant'))
        if category != 'plant':
            attrs['planting_height'] = attrs['mature_height'] = None
            return attrs
        get = lambda k: attrs[k] if k in attrs else getattr(inst, k, None)
        planting, established, mature = get('planting_height'), get('real_world_size'), get('mature_height')
        errors = {}
        if planting is not None and established and planting > float(established):
            errors['planting_height'] = 'Should be no taller than the height after about 2 years.'
        if mature is not None and established and mature < float(established):
            errors['mature_height'] = 'Should be at least the height after about 2 years.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def update(self, instance, validated_data):
        # Stock only changes through adjust_stock (recorded, locked); an edit form must never overwrite it
        validated_data.pop('stock_quantity', None)
        return super().update(instance, validated_data)


class StockMovementSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source='get_reason_display', read_only=True)
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = ['id', 'change', 'quantity_after', 'reason', 'reason_label', 'note', 'user_name', 'created_at']

    def get_user_name(self, obj):
        if not obj.user:
            return ''
        return obj.user.get_full_name() or obj.user.username


class GardenImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GardenImage
        fields = '__all__'


class GardenDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'original_image_url', 'reference_image_url',
                  'depth_data', 'placed_items', 'plant_breakdown', 'dimensions', 'total_cost',
                  'cost_estimate', 'terrain_height', 'time_of_day', 'status',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class GardenDesignListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views (no placed_items blob)."""
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'total_cost', 'status', 'created_at', 'updated_at', 'dimensions', 'original_image_url', 'reference_image_url', 'customer_name']

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Anonymous'

class BlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlackoutDate
        fields = '__all__'


class ProjectMilestoneSerializer(serializers.ModelSerializer):
    phase_display = serializers.CharField(source='get_phase_display', read_only=True)

    class Meta:
        model = ProjectMilestone
        fields = ['id', 'phase', 'phase_display', 'completion_pct', 'notes', 'proof_photo_url', 'updated_at']
        read_only_fields = ['id', 'phase_display', 'proof_photo_url', 'updated_at']


class StaffAttendanceSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()

    class Meta:
        model = StaffAttendance
        fields = [
            'id', 'booking', 'staff', 'staff_name',
            'timestamp_checkin', 'timestamp_checkout',
            'gps_lat_checkin', 'gps_lng_checkin', 'distance_at_checkin_m',
        ]
        read_only_fields = ['id', 'staff', 'staff_name', 'timestamp_checkin', 'distance_at_checkin_m']

    def get_staff_name(self, obj):
        full_name = f"{obj.staff.first_name} {obj.staff.last_name}".strip()
        return full_name or obj.staff.username


class ServiceBookingSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    design_details = serializers.SerializerMethodField(read_only=True)
    clock_out_photo_url = serializers.SerializerMethodField(read_only=True)
    project_milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    assigned_crew_names = serializers.SerializerMethodField(read_only=True)
    tracker = serializers.SerializerMethodField(read_only=True)
    # What still blocks "submit for sign-off" on a job under way (crew UI checklist)
    sign_off_blockers = serializers.SerializerMethodField(read_only=True)
    # What to load on the truck, counted from the design (crew "What to bring" checklist)
    materials = serializers.SerializerMethodField(read_only=True)
    assigned_crew = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = ServiceBooking
        fields = [
            'id', 'user', 'customer_name', 'customer_email',
            'service_type', 'scheduled_date', 'status',
            'contact_number', 'preferred_time', 'service_address', 'notes',
            'site_lat', 'site_lng',
            'assigned_crew', 'assigned_crew_names',
            'design', 'design_details', 'clock_out_photo_url',
            'milestones', 'progress_pct', 'staff_notes',
            'project_milestones', 'tracker', 'sign_off_blockers', 'materials', 'crew_on_way_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user', 'customer_name', 'customer_email',
            'assigned_crew_names', 'created_at', 'updated_at',
            'crew_on_way_at',
            'design_details', 'clock_out_photo_url', 'tracker',
        ]

    def get_materials(self, obj):
        from .field_work import materials_for
        return materials_for(obj.design)

    def get_sign_off_blockers(self, obj):
        if obj.status != 'Installing':
            return []
        from .field_work import sign_off_blockers
        return sign_off_blockers(obj)

    def get_tracker(self, obj):
        """Project billing/stage summary — full detail lives at /api/projects/<id>/."""
        try:
            t = obj.tracker
        except ServiceBooking.tracker.RelatedObjectDoesNotExist:
            return None
        return {
            'id': t.id,
            'status': t.status,
            'status_display': t.get_status_display(),
            'progress_percentage': t.progress_percentage,
            'total_price': float(t.total_price),
            'remaining_balance': float(t.remaining_balance),
            'amount_paid': float(t.total_price - t.remaining_balance),
            'estimated_end_date': t.estimated_end_date,
            # Open payment requests the customer can pay online at /pay/<token>
            'pending_payments': [
                {'id': r.id, 'label': r.label, 'amount': float(r.amount), 'token': r.token}
                for r in t.payment_requests.all() if r.status == 'PENDING'
            ],
        }

    def validate(self, attrs):
        if 'assigned_crew' in attrs and attrs['assigned_crew']:
            request = self.context.get('request')
            if request and request.user:
                user = request.user
                is_authorized = user.is_superuser
                if not is_authorized:
                    try:
                        is_authorized = user.staff_profile.role == 'OFFICE_ADMIN'
                    except Exception:
                        pass
                if not is_authorized:
                    raise serializers.ValidationError({"assigned_crew": "Only Office Admins can assign or change crew."})
        return super().validate(attrs)

    def update(self, instance, validated_data):
        assigned_crew = validated_data.pop('assigned_crew', None)
        instance = super().update(instance, validated_data)
        if assigned_crew is not None:
            instance.assigned_crew.set(assigned_crew)
        return instance

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Unknown'

    def get_customer_email(self, obj):
        return obj.user.email if obj.user else ''

    def get_assigned_crew_names(self, obj):
        names = []
        for u in obj.assigned_crew.all():
            full = f"{u.first_name} {u.last_name}".strip()
            names.append(full or u.username)
        return names

    def get_design_details(self, obj):
        if obj.design:
            image_url = obj.design.original_image_url
            if not image_url and obj.design.garden_image:
                request = self.context.get('request')
                if request:
                    image_url = request.build_absolute_uri(obj.design.garden_image.image.url)
                else:
                    image_url = obj.design.garden_image.image.url
            return {
                'id': obj.design.id,
                'name': obj.design.name,
                'image_url': image_url,
                'reference_image_url': obj.design.reference_image_url or '',
                'placed_items': obj.design.placed_items or [],
                'plant_breakdown': obj.design.plant_breakdown or [],
                'dimensions': obj.design.dimensions or {},
                'total_cost': float(obj.design.total_cost),
            }
        return None

    def get_clock_out_photo_url(self, obj):
        attendance = obj.attendances.filter(clock_out_photo_url__isnull=False).exclude(clock_out_photo_url='').first()
        if attendance and attendance.clock_out_photo_url:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(attendance.clock_out_photo_url.url)
            return attendance.clock_out_photo_url.url
        return None

    def validate_scheduled_date(self, value):
        if BlackoutDate.objects.filter(date=value).exists():
            raise serializers.ValidationError("The selected date is unavailable.")
        return value

class OrderItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)
    item_image = serializers.SerializerMethodField()
    review = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = ['id', 'item', 'item_name', 'item_image', 'quantity', 'price_at_booking', 'review']

    def get_item_image(self, obj):
        # Same picture the shop shows: uploaded thumbnail first, else the product's image link
        item = obj.item
        if item is None:
            return None
        if item.thumbnail and hasattr(item.thumbnail, 'url'):
            request = self.context.get('request')
            return request.build_absolute_uri(item.thumbnail.url) if request else item.thumbnail.url
        return item.image_url or None

    def get_review(self, obj):
        # The customer's own review of this purchased line, or None if not yet reviewed
        review = getattr(obj, 'review', None)
        if review is None:
            return None
        return {'id': review.id, 'rating': review.rating, 'comment': review.comment}

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    # What staff may do next, from the same rules the API enforces (work_queues.py)
    next_statuses = serializers.SerializerMethodField()
    needs_refund = serializers.SerializerMethodField()
    delivery_crew_name = serializers.SerializerMethodField()
    delivered_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ['id', 'customer_name', 'customer_email', 'customer_phone', 'customer_address',
                  'booking_date', 'total_price', 'payment_method', 'status', 'payment_status', 'created_at', 'items',
                  'next_statuses', 'needs_refund',
                  'delivery_crew', 'delivery_crew_name', 'delivered_by_name', 'delivered_at']
        read_only_fields = ['delivery_crew', 'delivered_at']

    def get_next_statuses(self, obj):
        return order_next_statuses(obj)

    def get_needs_refund(self, obj):
        return order_needs_refund(obj)

    @staticmethod
    def _name(user):
        return (f'{user.first_name} {user.last_name}'.strip() or user.username) if user else None

    def get_delivery_crew_name(self, obj):
        return self._name(obj.delivery_crew)

    def get_delivered_by_name(self, obj):
        return self._name(obj.delivered_by)


class ManageUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.ChoiceField(
        choices=['SUPER_ADMIN', 'OFFICE_ADMIN', 'FIELD_CREW', 'CUSTOMER'],
        required=False
    )
    input_role = serializers.CharField(write_only=True, required=False)
    staff_role = serializers.SerializerMethodField(read_only=True)
    input_staff_role = serializers.ChoiceField(
        choices=['OFFICE_ADMIN', 'FIELD_CREW', ''],
        write_only=True, required=False, allow_blank=True,
    )

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'first_name', 'last_name',
            'is_active', 'is_staff', 'is_superuser', 'date_joined',
            'password', 'role', 'input_role', 'staff_role', 'input_staff_role',
        )
        read_only_fields = ('id', 'date_joined')

    def get_staff_role(self, obj):
        try:
            return obj.staff_profile.role
        except Exception:
            return None

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Determine 4-tier role
        if instance.is_superuser:
            ret['role'] = 'SUPER_ADMIN'
        elif instance.is_staff:
            try:
                profile = instance.staff_profile
                if profile.role == 'FIELD_CREW':
                    ret['role'] = 'FIELD_CREW'
                else:
                    ret['role'] = 'OFFICE_ADMIN'
            except Exception:
                ret['role'] = 'OFFICE_ADMIN'
        else:
            ret['role'] = 'CUSTOMER'
        return ret

    def create(self, validated_data):
        role = validated_data.pop('role', None)
        input_role = validated_data.pop('input_role', None)
        input_staff_role = validated_data.pop('input_staff_role', None)
        password = validated_data.pop('password', None)
        
        # Determine role from various inputs for backward compatibility
        if not role:
            if input_role == 'admin':
                role = 'SUPER_ADMIN'
            elif input_role == 'staff':
                if input_staff_role == 'FIELD_CREW':
                    role = 'FIELD_CREW'
                else:
                    role = 'OFFICE_ADMIN'
            else:
                role = 'CUSTOMER'

        # Map role to flags
        is_staff = False
        is_superuser = False
        if role == 'SUPER_ADMIN':
            is_superuser = True
            is_staff = True
        elif role in ('OFFICE_ADMIN', 'FIELD_CREW'):
            is_staff = True

        # Generate unique username
        if 'username' not in validated_data or not validated_data['username']:
            email = validated_data.get('email', '')
            if email:
                validated_data['username'] = email.split('@')[0]
            else:
                import random
                validated_data['username'] = 'user_' + str(random.randint(1000, 9999))
        
        base_username = validated_data['username']
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        validated_data['username'] = username

        user = User.objects.create(
            is_staff=is_staff,
            is_superuser=is_superuser,
            **validated_data
        )
        if password:
            user.set_password(password)
        else:
            from django.utils.crypto import get_random_string
            user.set_password(get_random_string(24))  # they choose their own via "Forgot password"
        user.save()

        # Save staff profile role if applicable
        if role in ('OFFICE_ADMIN', 'FIELD_CREW'):
            StaffProfile.objects.create(user=user, role=role)
        else:
            StaffProfile.objects.filter(user=user).delete()
            
        return user

    def update(self, instance, validated_data):
        role = validated_data.pop('role', None)
        input_role = validated_data.pop('input_role', None)
        input_staff_role = validated_data.pop('input_staff_role', None)
        password = validated_data.pop('password', None)

        # Determine target role
        if role is not None:
            pass
        elif input_role is not None:
            if input_role == 'admin':
                role = 'SUPER_ADMIN'
            elif input_role == 'staff':
                if input_staff_role == 'FIELD_CREW':
                    role = 'FIELD_CREW'
                elif input_staff_role == 'OFFICE_ADMIN':
                    role = 'OFFICE_ADMIN'
                else:
                    role = 'OFFICE_ADMIN'
            else:
                role = 'CUSTOMER'

        if role is not None:
            if role == 'SUPER_ADMIN':
                instance.is_superuser = True
                instance.is_staff = True
                StaffProfile.objects.filter(user=instance).delete()
            elif role == 'OFFICE_ADMIN':
                instance.is_superuser = False
                instance.is_staff = True
                profile, _ = StaffProfile.objects.get_or_create(user=instance)
                profile.role = 'OFFICE_ADMIN'
                profile.save()
            elif role == 'FIELD_CREW':
                instance.is_superuser = False
                instance.is_staff = True
                profile, _ = StaffProfile.objects.get_or_create(user=instance)
                profile.role = 'FIELD_CREW'
                profile.save()
            elif role == 'CUSTOMER':
                instance.is_superuser = False
                instance.is_staff = False
                StaffProfile.objects.filter(user=instance).delete()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'kind', 'message', 'link', 'count', 'is_read', 'timestamp']
        read_only_fields = ['id', 'kind', 'message', 'link', 'count', 'timestamp']


class AttendanceSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField(read_only=True)
    booking_label = serializers.SerializerMethodField(read_only=True)
    total_hours = serializers.ReadOnlyField()

    class Meta:
        model = Attendance
        fields = [
            'id', 'staff', 'staff_name', 'booking', 'booking_label',
            'clock_in_time', 'clock_out_time',
            'clock_in_photo_url', 'clock_out_photo_url',
            'latitude', 'longitude', 'total_hours',
            'clock_in_address', 'clock_out_address'
        ]
        read_only_fields = ['id', 'staff', 'clock_in_time', 'clock_out_time']

    def get_staff_name(self, obj):
        if obj.staff:
            full_name = f"{obj.staff.first_name} {obj.staff.last_name}".strip()
            return full_name if full_name else obj.staff.username
        return 'Unknown'

    def get_booking_label(self, obj):
        if obj.booking:
            return f"#{obj.booking.id} - {obj.booking.service_type} for {obj.booking.user.username} ({obj.booking.scheduled_date})"
        return 'General'


def public_reviewer_name(user):
    """'Maria S.' — never the username, which can be an email or a handle."""
    first = user.first_name.strip()
    last = user.last_name.strip()
    if first and last:
        return f"{first} {last[0]}."
    return first or 'Verified customer'


class ServiceReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.SerializerMethodField()
    crew_names = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    booking = serializers.PrimaryKeyRelatedField(queryset=ServiceBooking.objects.all())

    class Meta:
        model = ServiceReview
        fields = ['id', 'booking', 'rating', 'comment', 'reviewer_name', 'crew_names', 'service_type', 'is_public',
                  'reply', 'replied_at', 'created_at']
        # is_public is the customer's choice when submitting (default: shown publicly)
        read_only_fields = ['id', 'reviewer_name', 'crew_names', 'service_type', 'reply', 'replied_at', 'created_at']

    def get_reviewer_name(self, obj):
        return public_reviewer_name(obj.user)

    def get_crew_names(self, obj):
        # First names only — this is also served publicly
        return [u.first_name.strip() or 'Crew member' for u in obj.booking.assigned_crew.all()]

    def get_service_type(self, obj):
        return obj.booking.get_service_type_display()

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_booking(self, booking):
        user = self.context['request'].user
        if booking.user != user:
            raise serializers.ValidationError("You can only review your own bookings.")
        if booking.status not in ('Finished', 'Completed'):
            raise serializers.ValidationError("Only completed bookings can be reviewed.")
        if hasattr(booking, 'review'):
            raise serializers.ValidationError("You have already submitted a review for this booking.")
        return booking

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


class ProductReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.SerializerMethodField()
    product_name = serializers.SerializerMethodField()
    order_id = serializers.IntegerField(source='order_item.order_id', read_only=True)
    order_item = serializers.PrimaryKeyRelatedField(queryset=OrderItem.objects.select_related('order', 'item'))

    class Meta:
        model = ProductReview
        fields = ['id', 'order_item', 'order_id', 'product', 'product_name', 'rating', 'comment',
                  'reviewer_name', 'is_public', 'reply', 'replied_at', 'created_at']
        read_only_fields = ['id', 'order_id', 'product', 'product_name', 'reviewer_name', 'reply', 'replied_at', 'created_at']

    def get_reviewer_name(self, obj):
        return public_reviewer_name(obj.user)

    def get_product_name(self, obj):
        return obj.product.name if obj.product else 'Deleted Item'

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_order_item(self, order_item):
        user = self.context['request'].user
        if order_item.order.user != user:
            raise serializers.ValidationError("You can only review products from your own orders.")
        if order_item.order.status != 'Delivered':
            raise serializers.ValidationError("Products can be reviewed once the order is delivered.")
        if order_item.item is None:
            raise serializers.ValidationError("This product is no longer available for review.")
        if hasattr(order_item, 'review'):
            raise serializers.ValidationError("You have already reviewed this product for this order.")
        return order_item

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        validated_data['product'] = validated_data['order_item'].item
        return super().create(validated_data)


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker serializers
# ─────────────────────────────────────────────────────────────────────────────

class ProjectHistoryLogSerializer(serializers.ModelSerializer):
    trigger_user_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(
        source='get_status_reached_display', read_only=True
    )

    class Meta:
        model = ProjectHistoryLog
        fields = [
            'id', 'status_reached', 'status_display',
            'timestamp', 'trigger_user', 'trigger_user_name', 'remarks',
        ]
        read_only_fields = ['id', 'timestamp', 'status_display', 'trigger_user_name']

    def get_trigger_user_name(self, obj):
        if obj.trigger_user:
            full = f"{obj.trigger_user.first_name} {obj.trigger_user.last_name}".strip()
            return full or obj.trigger_user.username
        return 'System'


class ProjectProgressMediaSerializer(serializers.ModelSerializer):
    uploader_name = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    phase_display = serializers.CharField(
        source='get_construction_phase_display', read_only=True
    )

    class Meta:
        model = ProjectProgressMedia
        fields = [
            'id', 'project', 'construction_phase', 'phase_display',
            'file_path', 'file_url', 'uploaded_at',
            'uploader', 'uploader_name', 'description', 'location',
        ]
        read_only_fields = [
            'id', 'uploaded_at', 'uploader', 'uploader_name',
            'file_url', 'phase_display',
        ]

    def get_uploader_name(self, obj):
        if obj.uploader:
            full = f"{obj.uploader.first_name} {obj.uploader.last_name}".strip()
            return full or obj.uploader.username
        return 'Unknown'

    def get_file_url(self, obj):
        request = self.context.get('request')
        if obj.file_path and hasattr(obj.file_path, 'url'):
            return (
                request.build_absolute_uri(obj.file_path.url)
                if request else obj.file_path.url
            )
        return None


class ProjectTrackerSerializer(serializers.ModelSerializer):
    supervisor_name = serializers.SerializerMethodField()
    booking_customer = serializers.SerializerMethodField()
    booking_service = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    history_logs = ProjectHistoryLogSerializer(many=True, read_only=True)
    progress_media = ProjectProgressMediaSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectTracker
        fields = [
            'id', 'booking', 'booking_customer', 'booking_service',
            'status', 'status_display', 'progress_percentage',
            'supervisor', 'supervisor_name',
            'estimated_end_date', 'total_price', 'remaining_balance',
            'created_at', 'updated_at',
            'history_logs', 'progress_media',
        ]
        read_only_fields = [
            'id', 'progress_percentage', 'created_at', 'updated_at',
            'status_display', 'booking_customer', 'booking_service',
            'supervisor_name',
        ]

    def validate(self, attrs):
        total = attrs.get('total_price', getattr(self.instance, 'total_price', 0))
        remaining = attrs.get('remaining_balance', getattr(self.instance, 'remaining_balance', 0))
        if total < 0 or remaining < 0:
            raise serializers.ValidationError("Prices and balances cannot be negative.")
        if remaining > total:
            raise serializers.ValidationError({"remaining_balance": "Remaining balance cannot exceed the total price."})
        return attrs

    def get_supervisor_name(self, obj):
        if obj.supervisor:
            full = f"{obj.supervisor.first_name} {obj.supervisor.last_name}".strip()
            return full or obj.supervisor.username
        return None

    def get_booking_customer(self, obj):
        if obj.booking and obj.booking.user:
            u = obj.booking.user
            full = f"{u.first_name} {u.last_name}".strip()
            return full or u.username
        return 'Unknown'

    def get_booking_service(self, obj):
        if obj.booking:
            return obj.booking.get_service_type_display()
        return None


class ProjectTrackerListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views — omits nested logs and media."""
    supervisor_name = serializers.SerializerMethodField()
    booking_customer = serializers.SerializerMethodField()
    booking_service = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ProjectTracker
        fields = [
            'id', 'booking', 'booking_customer', 'booking_service',
            'status', 'status_display', 'progress_percentage',
            'supervisor', 'supervisor_name',
            'estimated_end_date', 'total_price', 'remaining_balance',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'progress_percentage', 'updated_at',
            'status_display', 'booking_customer', 'booking_service', 'supervisor_name',
        ]

    def get_supervisor_name(self, obj):
        if obj.supervisor:
            full = f"{obj.supervisor.first_name} {obj.supervisor.last_name}".strip()
            return full or obj.supervisor.username
        return None

    def get_booking_customer(self, obj):
        if obj.booking and obj.booking.user:
            u = obj.booking.user
            full = f"{u.first_name} {u.last_name}".strip()
            return full or u.username
        return 'Unknown'

    def get_booking_service(self, obj):
        if obj.booking:
            return obj.booking.get_service_type_display()
        return None