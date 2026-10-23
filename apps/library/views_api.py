import os
import re
import secrets
import json
import logging
import time
import math
import datetime
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# Crew must be this close to a booking's GPS pin to clock in / check in there
SITE_RADIUS_M = 50


def _complete_tracker(booking, user, remarks):
    """Move the booking's project tracker to COMPLETED (creating it for legacy bookings)."""
    estimate = booking.design.total_cost if booking.design else 0
    tracker, _ = ProjectTracker.objects.get_or_create(
        booking=booking, defaults={'total_price': estimate, 'remaining_balance': estimate},
    )
    if tracker.status not in ('COMPLETED', 'CUSTOMER_FEEDBACK'):
        tracker.status = 'COMPLETED'
        tracker.save()
        ProjectHistoryLog.objects.create(
            project=tracker, status_reached='COMPLETED', trigger_user=user, remarks=remarks,
        )


def _draft_portfolio(booking, user):
    """Draft the Project Hub entry on sign-off; never let a portfolio problem block the sign-off."""
    try:
        create_portfolio_draft(booking, user)
    except Exception:
        logger.exception("Could not draft portfolio entry for booking #%s", booking.pk)


def bind_proof_to_milestone(booking_id: int, phase: str, photo_url: str) -> None:
    """Attach a proof photo URL to the matching ProjectMilestone record."""
    ProjectMilestone.objects.filter(booking_id=booking_id, phase=phase).update(proof_photo_url=photo_url)

# ───────────────────────────────────────────────────────────────
from django.http import HttpResponse

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action, api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser, BasePermission
from rest_framework.response import Response

class IsOfficeAdmin(BasePermission):
    """
    Grants access to superusers and staff members whose role resolves to OFFICE_ADMIN.

    Transition-phase strategy
    ─────────────────────────
    • Accepts 'OFFICE_ADMIN'  — current canonical enum value
    • Accepts 'Staff'         — legacy string used before the role enum migration
    • Falls back to is_staff  — catches manually-created admin accounts that have no
                                StaffProfile row yet (never locked out silently)
    • Explicitly denies 'FIELD_CREW' even when is_staff is True
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        # Customers can never have a StaffProfile — early exit
        if not user.is_staff:
            return False

        # Resolve the raw role string from StaffProfile (may be absent on legacy accounts)
        resolved_role = None
        try:
            resolved_role = user.staff_profile.role
        except Exception:
            pass

        # ── DEBUG: print exactly what the permission class sees ──────────────
        print(
            f"DEBUG assign_crew permission | "
            f"email={user.email!r} "
            f"is_staff={user.is_staff} "
            f"user.role (direct attr)={getattr(user, 'role', 'NO_ROLE')!r} "
            f"resolved_role (staff_profile.role)={resolved_role!r}"
        )
        # ─────────────────────────────────────────────────────────────────────

        # FIELD_CREW is staff but must never be able to assign crew
        if resolved_role == 'FIELD_CREW':
            return False

        # Accept current canonical value, legacy value, or any un-profiled staff account
        return resolved_role in ('OFFICE_ADMIN', 'Staff') or user.is_staff

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Avg, Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework_simplejwt.views import TokenObtainPairView

from .portfolio import create_portfolio_draft, sanitize_image
from . import activity, cash, field_work, inventory, notifications, offline, paymongo, work_queues
from .emails import send_payment_request_email

from .models import (
    InventoryItem, GardenDesign, BlackoutDate, ServiceBooking,
    Order, OrderItem, Attendance, ProjectMilestone, StaffAttendance,
    StaffProfile, Notification, ServiceReview, ProductReview,
    ProjectTracker, ProjectHistoryLog, ProjectProgressMedia, PricingSettings,
    ProjectPayment, ProjectPaymentRequest,
)
from .serializers import (
    InventoryItemSerializer,
    GardenDesignSerializer,
    GardenDesignListSerializer,
    UserSerializer,
    CustomTokenObtainPairSerializer,
    BlackoutDateSerializer,
    ServiceBookingSerializer,
    OrderSerializer,
    ManageUserSerializer,
    AttendanceSerializer,
    ProjectMilestoneSerializer,
    StaffAttendanceSerializer,
    StockMovementSerializer,
    NotificationSerializer,
    ServiceReviewSerializer,
    ProductReviewSerializer,
    ProjectTrackerSerializer,
    ProjectTrackerListSerializer,
    ProjectHistoryLogSerializer,
    ProjectProgressMediaSerializer,
)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
    # Slows password guessing: LOGINS_PER_MINUTE attempts per IP (settings: 'login')
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (AllowAny,)
    serializer_class = UserSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'register'


class InventoryItemViewSet(viewsets.ModelViewSet):
    """
    GET /api/inventory/          → list (filterable by ?category=plant)
    GET /api/inventory/:id/      → detail
    POST, PUT, PATCH, DELETE     → admin/staff only
    """
    queryset = InventoryItem.objects.all()
    serializer_class = InventoryItemSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            self.permission_classes = [AllowAny]
        else:
            self.permission_classes = [IsOfficeAdmin]
        return super().get_permissions()

    def get_queryset(self):
        public_reviews = Q(reviews__is_public=True)
        qs = super().get_queryset().annotate(
            avg_rating=Avg('reviews__rating', filter=public_reviews),
            review_count=Count('reviews', filter=public_reviews),
        )
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)
        # Archived items are only listed for the office (?archived=1 on the Inventory page)
        if self.action == 'list' and not (self.request.query_params.get('archived') == '1'
                                          and IsOfficeAdmin().has_permission(self.request, self)):
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        item = serializer.save()
        if item.stock_quantity:
            inventory.record(item, item.stock_quantity, 'opening', user=self.request.user, note='New item')
        activity.record(self.request.user, 'stock', 'item_added',
                        f'Added {item.name} at ₱{item.unit_price:,.2f} ({item.stock_quantity} in stock)',
                        link='/inventory', item=item.id)

    def perform_update(self, serializer):
        old_price, old_name = serializer.instance.unit_price, serializer.instance.name
        item = serializer.save()
        if item.unit_price != old_price:
            activity.record(self.request.user, 'stock', 'price_changed',
                            f'Changed the price of {item.name}: ₱{old_price:,.2f} → ₱{item.unit_price:,.2f}',
                            link='/inventory', item=item.id, old=str(old_price), new=str(item.unit_price))
        if item.name != old_name:
            activity.record(self.request.user, 'stock', 'item_renamed', f'Renamed {old_name} to {item.name}',
                            link='/inventory', item=item.id)

    def destroy(self, request, *args, **kwargs):
        """Archive, never delete: past orders, saved designs and estimates still point at the item."""
        item = self.get_object()
        item.is_active = False
        item.save(update_fields=['is_active'])
        activity.record(request.user, 'stock', 'item_archived', f'Took {item.name} off sale (archived)',
                        link='/inventory', item=item.id)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=['post'])
    def restore(self, request, pk=None):
        item = self.get_object()
        item.is_active = True
        item.save(update_fields=['is_active'])
        activity.record(request.user, 'stock', 'item_restored', f'Put {item.name} back on sale',
                        link='/inventory', item=item.id)
        return Response(self.get_serializer(item).data)

    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        """POST {reason: restock|damaged|count, quantity, note} → the movement and the item."""
        try:
            movement = inventory.adjust(pk, request.data.get('reason'), request.data.get('quantity'),
                                        request.user, request.data.get('note', ''))
        except inventory.StockError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except InventoryItem.DoesNotExist:
            return Response({'error': 'Item not found.'}, status=status.HTTP_404_NOT_FOUND)
        verb = {'restock': 'Restocked', 'damaged': 'Wrote off', 'count': 'Corrected the count of'}.get(movement.reason, 'Adjusted')
        activity.record(request.user, 'stock', 'stock_adjusted',
                        f'{verb} {movement.item.name}: {movement.change:+d} (now {movement.quantity_after})'
                        + (f' — {movement.note}' if movement.note else ''),
                        link='/inventory', item=movement.item_id, reason=movement.reason, change=movement.change)
        return Response({'movement': StockMovementSerializer(movement).data,
                         'item': self.get_serializer(movement.item).data})

    @action(detail=True, methods=['get'])
    def stock_history(self, request, pk=None):
        item = self.get_object()
        return Response(StockMovementSerializer(item.stock_movements.select_related('user')[:50], many=True).data)


class GardenDesignViewSet(viewsets.ModelViewSet):
    """
    GET    /api/designs/        → list saved designs
    POST   /api/designs/        → create new design
    GET    /api/designs/:id/    → load specific design
    PUT    /api/designs/:id/    → save/update design
    DELETE /api/designs/:id/    → delete design
    """
    queryset = GardenDesign.objects.all()
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if work_queues.is_field_crew(user):
            # Crew open the design of a job they are on (and any of their own), nobody else's
            return GardenDesign.objects.filter(Q(bookings__assigned_crew=user) | Q(user=user)).distinct()
        if user.is_staff or user.is_superuser:
            return GardenDesign.objects.all()
        return GardenDesign.objects.filter(user=user)

    def get_serializer_class(self):
        if self.action == 'list':
            return GardenDesignListSerializer
        return GardenDesignSerializer

    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None
        serializer.save(user=user)

    def perform_update(self, serializer):
        serializer.save()

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """POST /api/designs/:id/duplicate/ → clone a design."""
        original = self.get_object()
        copy = GardenDesign.objects.create(
            user=request.user if request.user.is_authenticated else None,
            name=f"{original.name} (Copy)",
            original_image_url=original.original_image_url,
            depth_data=original.depth_data,
            placed_items=original.placed_items,
            dimensions=original.dimensions,
            total_cost=original.total_cost,
            terrain_height=original.terrain_height,
            time_of_day=original.time_of_day,
        )
        return Response(GardenDesignSerializer(copy).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'])
    def submit(self, request, pk=None):
        design = self.get_object()
        design.status = 'submitted'
        design.save()
        return Response({'status': 'design submitted'})

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def submitted_designs(self, request):
        qs = GardenDesign.objects.filter(status='submitted')
        serializer = GardenDesignListSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def update_status(self, request, pk=None):
        design = self.get_object()
        new_status = request.data.get('status')
        if new_status in ['approved', 'rejected']:
            design.status = new_status
            design.save()
            return Response({'status': new_status})
        return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['patch'], url_path='items/(?P<item_id>[^/.]+)')
    def patch_item(self, request, pk=None, item_id=None):
        """
        PATCH /api/designs/:id/items/:item_id/
        Partially updates a single placed item inside the design's placed_items JSON array.
        Accepts any subset of: position, rotation, scale.
        """
        design = self.get_object()
        placed = design.placed_items or []

        # Find the item by its client-side id (stored as a number or string)
        target = None
        for item in placed:
            if str(item.get('id')) == str(item_id):
                target = item
                break

        if target is None:
            return Response({'error': f'Item {item_id} not found in design.'}, status=status.HTTP_404_NOT_FOUND)

        # Merge only the fields provided
        allowed_fields = {'position', 'rotation', 'scale', 'rotation_y'}
        for field, value in request.data.items():
            if field in allowed_fields:
                if field == 'rotation_y':
                    # Convenience: update just the Y axis of rotation
                    if 'rotation' not in target or not isinstance(target['rotation'], dict):
                        target['rotation'] = {'x': 0, 'y': 0, 'z': 0}
                    target['rotation']['y'] = float(value)
                else:
                    target[field] = value

        design.placed_items = placed
        design.save(update_fields=['placed_items', 'updated_at'])
        return Response({'status': 'updated', 'item': target})


class BlackoutDateViewSet(viewsets.ModelViewSet):
    queryset = BlackoutDate.objects.all()
    serializer_class = BlackoutDateSerializer
    
    def get_permissions(self):
        if self.request.method in ['GET', 'OPTIONS', 'HEAD']:
            self.permission_classes = [AllowAny]
        else:
            self.permission_classes = [IsOfficeAdmin]
        return super().get_permissions()


class ServiceBookingViewSet(viewsets.ModelViewSet):
    queryset = ServiceBooking.objects.all()
    serializer_class = ServiceBookingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        base = ServiceBooking.objects.select_related('design', 'design__garden_image', 'tracker').prefetch_related('assigned_crew', 'tracker__payment_requests')

        if not (user.is_staff or user.is_superuser):
            return base.filter(user=user)

        # Use getattr to avoid StaffProfile.DoesNotExist silently falling through to base.all()
        role = getattr(getattr(user, 'staff_profile', None), 'role', None)

        if role == 'FIELD_CREW':
            # M2M filter — .distinct() prevents duplicate rows from the join
            return base.filter(assigned_crew=user).distinct()

        # OFFICE_ADMIN / SUPER_ADMIN / legacy staff accounts
        return base.all()

    def list(self, request, *args, **kwargs):
        params = request.query_params
        qs = work_queues.filter_bookings(self.get_queryset(), params)
        if not work_queues.wants_page(params):
            return Response(self.get_serializer(qs, many=True).data)
        rows, meta = work_queues.paginate(qs, params)
        return Response({**meta, 'results': self.get_serializer(rows, many=True).data})

    @action(detail=False, methods=['get'])
    def counts(self, request):
        """Badge numbers for every staff booking queue, without downloading the bookings."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        qs = self.get_queryset()
        return Response({**work_queues.booking_counts(qs), **work_queues.arrivals(qs, request.query_params.get('since'))})

    @action(detail=False, methods=['get'], permission_classes=[IsOfficeAdmin])
    def export(self, request):
        """CSV of the bookings matching the current queue and filters (same params as the list)."""
        header, rows = work_queues.booking_csv(work_queues.filter_bookings(self.get_queryset(), request.query_params))
        return work_queues.csv_response(header, rows, f'bookings-{datetime.date.today():%Y%m%d}.csv')

    @action(detail=False, methods=['get'], permission_classes=[IsOfficeAdmin])
    def schedule(self, request):
        """Dispatch calendar: bookings and crew for a date range (?from=&to=, at most 6 weeks)."""
        try:
            start = datetime.date.fromisoformat(request.query_params.get('from', ''))
            end = datetime.date.fromisoformat(request.query_params.get('to', ''))
        except ValueError:
            return Response({'error': 'from and to must be dates (YYYY-MM-DD).'}, status=status.HTTP_400_BAD_REQUEST)
        if end < start or (end - start).days >= work_queues.SCHEDULE_MAX_DAYS:
            return Response({'error': f'Pick a range of 1–{work_queues.SCHEDULE_MAX_DAYS} days.'},
                            status=status.HTTP_400_BAD_REQUEST)
        crew = User.objects.filter(staff_profile__role='FIELD_CREW', is_active=True).order_by('first_name', 'username')
        return Response(work_queues.booking_schedule(self.get_queryset(), start, end, crew))

    def perform_create(self, serializer):
        design_id = self.request.data.get('design_id') or self.request.data.get('design')
        if design_id:
            try:
                from .models import GardenDesign
                design = GardenDesign.objects.get(id=design_id)
                booking = serializer.save(user=self.request.user, design=design)
            except Exception:
                booking = serializer.save(user=self.request.user)
        else:
            booking = serializer.save(user=self.request.user)

        # Every booking gets a project tracker so billing and stages exist from day one.
        # The design estimate is the starting contract price; staff can adjust it.
        estimate = booking.design.total_cost if booking.design else 0
        tracker = ProjectTracker.objects.create(
            booking=booking, total_price=estimate, remaining_balance=estimate,
        )
        ProjectHistoryLog.objects.create(
            project=tracker, status_reached=tracker.status, trigger_user=self.request.user,
            remarks='Project created from customer booking.',
        )
        notifications.booking_new(booking)

    # Customers may change their own request (date, time, address, notes), never its status or the
    # staff side. Their "cancel" keeps the booking as Cancelled (it used to be deleted outright).
    CUSTOMER_LOCKED = ('status', 'assigned_crew', 'staff_notes', 'milestones', 'progress_pct')

    def perform_update(self, serializer):
        booking = serializer.instance
        user = self.request.user
        old_status, old_date = booking.status, booking.scheduled_date
        if not (user.is_staff or user.is_superuser):
            for field in self.CUSTOMER_LOCKED:
                serializer.validated_data.pop(field, None)
        booking = serializer.save()
        if booking.scheduled_date != old_date:
            notifications.booking_rescheduled(booking, old_date)
        if booking.status != old_status and (user.is_staff or user.is_superuser):
            notifications.booking_status_changed(booking, old_status, booking.status, user)
            if booking.status == 'Cancelled':
                activity.record(user, 'work', 'booking_cancelled', f'Cancelled booking #{booking.id} ({booking.service_type})',
                                link=notifications.booking_office_path(booking.id), booking=booking.id, was=old_status)

    def destroy(self, request, *args, **kwargs):
        booking = self.get_object()
        user = request.user
        if user.is_staff or user.is_superuser:
            return super().destroy(request, *args, **kwargs)
        if booking.status not in ('Pending', 'Preparing'):
            return Response({'error': f'This booking is already {booking.status.lower()}; contact us to change it.'},
                            status=status.HTTP_400_BAD_REQUEST)
        booking.status = 'Cancelled'
        booking.save(update_fields=['status'])
        notifications.booking_cancelled_by_customer(booking)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def update_status(self, request, pk=None):
        """
        Field crew  → may only submit their work for sign-off ('For Review').
        Office admin → any transition; approving 'Finished' is the sign-off and
                       also completes the project tracker.
        """
        user = request.user
        if not (user.is_staff or user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)

        new_status = request.data.get('status')
        if new_status not in dict(ServiceBooking.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

        booking = self.get_object()  # field crew only ever see their assigned bookings
        is_office_admin = IsOfficeAdmin().has_permission(request, self)

        if not is_office_admin:
            if new_status != 'For Review':
                return Response(
                    {'error': 'Field crew can only submit work for sign-off. An office admin approves completion.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if booking.status not in ('Preparing', 'Installing'):
                return Response(
                    {'error': f'Only active jobs can be submitted for sign-off (this one is {booking.status}).'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            blockers = field_work.sign_off_blockers(booking)
            if blockers:
                return Response({'error': 'Not ready for sign-off. ' + ' '.join(blockers), 'blockers': blockers},
                                status=status.HTTP_400_BAD_REQUEST)

        old_status = booking.status
        booking.status = new_status
        booking.save()

        if new_status == 'For Review':
            notifications.job_review(booking, user)
        elif old_status != new_status:
            # Sending a job back can carry a short reason for the crew
            note = str(request.data.get('note') or '').strip()[:300]
            notifications.booking_status_changed(booking, old_status, new_status, user, note)
        if new_status == 'Finished':
            _complete_tracker(booking, request.user, 'Signed off by office admin.')
            _draft_portfolio(booking, request.user)
        if old_status != new_status and new_status in ('Finished', 'Cancelled'):
            done = new_status == 'Finished'
            activity.record(user, 'work', 'job_signed_off' if done else 'booking_cancelled',
                            f'{"Signed off" if done else "Cancelled"} booking #{booking.id} ({booking.service_type})',
                            link=notifications.booking_office_path(booking.id), booking=booking.id, was=old_status)

        return Response({'status': new_status})

    @action(detail=True, methods=['get', 'post'], url_path='progress-media')
    def progress_media(self, request, pk=None):
        """
        GET  /api/bookings/<id>/progress-media/   before/during/after photos (crew, office, the customer)
        POST /api/bookings/<id>/progress-media/   multipart: file, construction_phase, description  [staff]
        Images are re-encoded to strip EXIF/GPS metadata before they are stored.
        """
        booking = self.get_object()  # crew: assigned only · customers: own only

        if request.method == 'GET':
            media = ProjectProgressMedia.objects.filter(project__booking=booking).select_related('uploader')
            return Response(ProjectProgressMediaSerializer(media, many=True, context={'request': request}).data)

        user = request.user
        if not (user.is_staff or user.is_superuser):
            return Response({'error': 'Only staff can upload progress photos.'}, status=status.HTTP_403_FORBIDDEN)
        if booking.status in ('Finished', 'Cancelled') and not IsOfficeAdmin().has_permission(request, self):
            return Response({'error': f'Booking #{booking.id} is {booking.status}; photos can no longer be added.'},
                            status=status.HTTP_400_BAD_REQUEST)

        phase = (request.data.get('construction_phase') or '').strip().upper()
        if phase not in dict(ProjectProgressMedia.PHASE_CHOICES):
            return Response({'error': "construction_phase must be BEFORE, DURING or AFTER."}, status=status.HTTP_400_BAD_REQUEST)
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': 'A photo is required (field name: file).'}, status=status.HTTP_400_BAD_REQUEST)
        clean, error = sanitize_image(upload)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        estimate = booking.design.total_cost if booking.design else 0
        tracker, _ = ProjectTracker.objects.get_or_create(
            booking=booking, defaults={'total_price': estimate, 'remaining_balance': estimate},
        )
        media = ProjectProgressMedia(
            project=tracker, construction_phase=phase, uploader=user,
            description=(request.data.get('description') or '').strip()[:500],
        )
        media.file_path.save(f'booking_{booking.id}_{phase.lower()}.jpg', clean, save=False)
        media.save()
        return Response(ProjectProgressMediaSerializer(media, context={'request': request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['delete'], url_path=r'progress-media/(?P<media_id>\d+)')
    def delete_progress_media(self, request, pk=None, media_id=None):
        booking = self.get_object()
        media = get_object_or_404(ProjectProgressMedia, pk=media_id, project__booking=booking)
        if not (media.uploader_id == request.user.id or IsOfficeAdmin().has_permission(request, self)):
            return Response({'error': 'You can only delete photos you uploaded.'}, status=status.HTTP_403_FORBIDDEN)
        if hasattr(media, 'portfolio_copy'):
            return Response({'error': 'This photo is used in the project portfolio. Remove it there first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        media.file_path.delete(save=False)
        media.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def update_milestones(self, request, pk=None):
        booking = self.get_object()
        milestones   = request.data.get('milestones')
        progress_pct = request.data.get('progress_pct')
        staff_notes  = request.data.get('staff_notes')

        if milestones is not None:
            booking.milestones = milestones
        if progress_pct is not None:
            try:
                booking.progress_pct = max(0, min(100, int(progress_pct)))
            except (ValueError, TypeError):
                return Response({'error': 'progress_pct must be an integer 0-100'},
                                status=status.HTTP_400_BAD_REQUEST)
        if staff_notes is not None:
            booking.staff_notes = staff_notes

        booking.save()
        serializer = ServiceBookingSerializer(booking, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def on_my_way(self, request, pk=None):
        """Crew: tell the customer we're coming, with an arrival estimate (15/30/45/60 min)."""
        from django.utils import timezone
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        booking = self.get_object()   # crew: only their own jobs
        try:
            eta = int(request.data.get('eta_minutes'))
        except (TypeError, ValueError):
            eta = 0
        if eta not in field_work.ON_MY_WAY_ETAS:
            return Response({'error': 'Pick an arrival time: 15, 30, 45 or 60 minutes.'}, status=status.HTTP_400_BAD_REQUEST)
        not_now = field_work.start_job_error(booking)
        if not_now:
            return Response({'error': not_now}, status=status.HTTP_400_BAD_REQUEST)
        now = timezone.now()
        if booking.crew_on_way_at and now - booking.crew_on_way_at < datetime.timedelta(minutes=field_work.ON_MY_WAY_REPEAT_MINUTES):
            told = timezone.localtime(booking.crew_on_way_at).strftime('%I:%M %p').lstrip('0')
            return Response({'error': f'The customer was already told at {told}.'}, status=status.HTTP_400_BAD_REQUEST)
        booking.crew_on_way_at = now
        booking.save(update_fields=['crew_on_way_at'])
        notifications.crew_on_the_way(booking, request.user, eta)
        return Response({'crew_on_way_at': booking.crew_on_way_at})

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated])
    def report_problem(self, request, pk=None):
        """Crew: something stops or delays the job. The office is notified and it's kept in the project history."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        booking = self.get_object()
        reason = str(request.data.get('reason') or '').strip()[:120]
        note = str(request.data.get('note') or '').strip()[:500]
        if not reason:
            return Response({'error': 'Say what went wrong.'}, status=status.HTTP_400_BAD_REQUEST)
        note = (note + offline.delay_note(request)).strip()
        tracker = getattr(booking, 'tracker', None)
        if tracker:
            ProjectHistoryLog.objects.create(
                project=tracker, status_reached=tracker.status, trigger_user=request.user,
                remarks=f'Problem reported by the crew: {reason}' + (f' — {note}' if note else ''),
            )
        notifications.job_problem(booking, request.user, reason, note)
        return Response({'ok': True})

    @action(detail=True, methods=['post'], url_path='check_in_at_site', permission_classes=[IsAuthenticated])
    def check_in_at_site(self, request, pk=None):
        from django.utils import timezone
        booking = self.get_object()
        try:
            lat = float(request.data['latitude'])
            lng = float(request.data['longitude'])
        except (KeyError, TypeError, ValueError):
            return Response({'error': 'latitude and longitude are required numeric fields.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Enforce: staff must have an active Attendance clock-in session for THIS booking.
        active_att = Attendance.objects.filter(
            staff=request.user, clock_out_time__isnull=True
        ).first()
        if active_att is None:
            return Response(
                {'error': f'You are not clocked in. Clock in to Booking #{booking.id} via Attendance Tracking first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if active_att.booking_id != booking.id:
            return Response(
                {'error': (
                    f'Your active attendance session is linked to Booking #{active_att.booking_id}, '
                    f'not Booking #{booking.id}. Switch your attendance session first.'
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if booking.site_lat is not None and booking.site_lng is not None:
            dist_m = _haversine(lat, lng, booking.site_lat, booking.site_lng)
            if dist_m > SITE_RADIUS_M:
                return Response(
                    {'error': f'You are {dist_m:.0f} m from the site. Must be within {SITE_RADIUS_M} m to check in.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            dist_m = None

        attendance = StaffAttendance.objects.create(
            booking=booking,
            staff=request.user,
            timestamp_checkin=timezone.now(),
            gps_lat_checkin=lat,
            gps_lng_checkin=lng,
            distance_at_checkin_m=dist_m,
        )
        if booking.status in ['Pending', 'Preparing']:
            booking.status = 'Installing'
            booking.save()
        return Response(StaffAttendanceSerializer(attendance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='update_milestone', permission_classes=[IsAdminUser])
    def update_milestone(self, request, pk=None):
        booking = self.get_object()
        phase = request.data.get('phase')
        completion_pct = request.data.get('completion_pct')
        notes = request.data.get('notes', '')
        proof_photo_url_direct = request.data.get('proof_photo_url', '')

        valid_phases = dict(ProjectMilestone.PHASE_CHOICES)
        if phase not in valid_phases:
            return Response(
                {'error': f'Invalid phase. Choices: {list(valid_phases.keys())}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if completion_pct is None:
            return Response({'error': 'completion_pct is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pct = max(0, min(100, int(completion_pct)))
        except (ValueError, TypeError):
            return Response({'error': 'completion_pct must be an integer 0-100.'}, status=status.HTTP_400_BAD_REQUEST)

        if getattr(getattr(request.user, 'staff_profile', None), 'role', None) == 'FIELD_CREW':
            if phase in field_work.OFFICE_STAGES:
                return Response({'error': 'The office confirms the design and contract.'}, status=status.HTTP_403_FORBIDDEN)
            if phase not in field_work.CREW_STAGES:
                return Response({'error': 'That stage is not used any more.'}, status=status.HTTP_400_BAD_REQUEST)
            if not field_work.is_clocked_in_to(request.user, booking, at=offline.action_time(request)):
                return Response({'error': f'Start booking #{booking.id} (clock in) before updating its stages.'},
                                status=status.HTTP_400_BAD_REQUEST)
            if pct == 100 and not proof_photo_url_direct:
                return Response({'error': 'Take a photo to finish this stage.'}, status=status.HTTP_400_BAD_REQUEST)

        milestone, _ = ProjectMilestone.objects.get_or_create(booking=booking, phase=phase)
        milestone.completion_pct = pct
        if notes:
            milestone.notes = notes

        # Accept a directly-supplied proof URL (e.g. from field crew image uploads)
        if proof_photo_url_direct:
            milestone.proof_photo_url = proof_photo_url_direct
        # Auto-bind proof photo from the most recent clock-out when phase completes
        elif pct == 100 and not milestone.proof_photo_url:
            latest_checkout = (
                Attendance.objects.filter(booking=booking, clock_out_time__isnull=False)
                .exclude(clock_out_photo_url='').exclude(clock_out_photo_url__isnull=True)
                .order_by('-clock_out_time').first()
            )
            if latest_checkout and latest_checkout.clock_out_photo_url:
                try:
                    milestone.proof_photo_url = latest_checkout.clock_out_photo_url.url
                except Exception:
                    pass

        milestone.save()

        # Progress is based on the 4 active pipeline phases only (not legacy phases)
        active_phases = ProjectMilestone.ACTIVE_PHASES
        completed_count = ProjectMilestone.objects.filter(
            booking=booking, phase__in=active_phases, completion_pct=100
        ).count()
        booking.progress_pct = int(completed_count / len(active_phases) * 100)
        
        # Auto-update status to Installing if not already there, since active work is happening
        if booking.status in ['Pending', 'Preparing']:
            booking.status = 'Installing'
        booking.save()

        serializer = ServiceBookingSerializer(booking, context={'request': request})
        return Response({
            'milestone': ProjectMilestoneSerializer(milestone).data,
            'progress_pct': booking.progress_pct,
            'booking': serializer.data,
        })


    @action(detail=False, methods=['get'], url_path='field_crew_members', permission_classes=[IsAdminUser])
    def field_crew_members(self, request):
        """GET /api/bookings/field_crew_members/ — list active FIELD_CREW users for assignment dropdowns."""
        crew = User.objects.filter(
            staff_profile__role='FIELD_CREW', is_active=True
        ).select_related('staff_profile').order_by('first_name', 'username')
        return Response([{
            'id': u.id,
            'username': u.username,
            'name': f"{u.first_name} {u.last_name}".strip() or u.username,
            'email': u.email,
        } for u in crew])

    @action(detail=True, methods=['post', 'patch', 'put'], url_path='assign_crew', permission_classes=[IsOfficeAdmin])
    def assign_crew(self, request, pk=None):
        """POST/PATCH/PUT /api/bookings/:id/assign_crew/
        Accepts crew_ids (array) to support multi-crew capacity planning.
        Also accepts legacy crew_id (single int) for backward compatibility.
        Sends notifications only to newly added crew members.
        """
        booking = self.get_object()

        # Resolve crew_ids — accept array or single legacy value
        crew_ids = request.data.get('crew_ids')
        if crew_ids is None:
            single = request.data.get('crew_id') or request.data.get('assigned_crew')
            crew_ids = [single] if single else []

        # Empty list → clear all crew
        if not crew_ids:
            booking.assigned_crew.clear()
            return Response(ServiceBookingSerializer(booking, context={'request': request}).data)

        # Validate each crew member
        crew_users = []
        for cid in crew_ids:
            try:
                crew_user = User.objects.select_related('staff_profile').get(pk=cid, is_active=True)
            except User.DoesNotExist:
                return Response({'error': f'User {cid} not found.'}, status=status.HTTP_404_NOT_FOUND)
            try:
                if crew_user.staff_profile.role != 'FIELD_CREW':
                    return Response(
                        {'error': f'{crew_user.username} is not a Field Crew member.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception:
                return Response(
                    {'error': f'{crew_user.username} does not have a Field Crew profile.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            crew_users.append(crew_user)

        # Capture previously assigned IDs so we only notify newcomers
        previously_assigned = set(booking.assigned_crew.values_list('id', flat=True))
        booking.assigned_crew.set(crew_users)

        added = [u for u in crew_users if u.id not in previously_assigned]
        kept = {u.id for u in crew_users}
        removed = list(User.objects.filter(id__in=previously_assigned - kept))
        notifications.crew_changed(booking, added, removed)

        return Response(ServiceBookingSerializer(booking, context={'request': request}).data)

    @action(detail=True, methods=['get'], url_path='generate_pdf', permission_classes=[IsAuthenticated])
    def generate_pdf(self, request, pk=None):
        """GET /api/bookings/<id>/generate_pdf/  →  Download a printable Work Order PDF."""
        booking = self.get_object()

        user = request.user
        role = getattr(getattr(user, 'staff_profile', None), 'role', None)
        if not (user.is_staff or user.is_superuser):
            if not booking.assigned_crew.filter(pk=user.pk).exists():
                return Response(
                    {'error': 'You are not assigned to this booking.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import cm
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                HRFlowable, Image as RLImage, Flowable, PageBreak,
            )
            from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
            from reportlab.graphics.shapes import (
                Drawing, Circle, Rect, Line, String as GrStr,
            )
            from reportlab.graphics import renderPDF as _renderPDF
            import io as _io
            import math as _math
        except ImportError:
            return Response(
                {'error': 'PDF library (reportlab) is not installed on this server.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        buffer = _io.BytesIO()
        PAGE_W, PAGE_H = A4
        MARGIN = 1.8 * cm

        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            rightMargin=MARGIN, leftMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN,
        )

        # ── Colour palette ────────────────────────────────────────────
        DARK  = colors.HexColor('#0f172a')
        MID   = colors.HexColor('#475569')
        LIGHT = colors.HexColor('#94a3b8')
        RULE  = colors.HexColor('#e2e8f0')
        GREEN = colors.HexColor('#059669')
        ORANGE = colors.HexColor('#ea580c')
        BG    = colors.HexColor('#f8fafc')
        BG2   = colors.HexColor('#f0fdf4')

        H1    = ParagraphStyle('H1',    fontName='Helvetica-Bold', fontSize=20, textColor=DARK, alignment=TA_CENTER, spaceAfter=2)
        H2    = ParagraphStyle('H2',    fontName='Helvetica-Bold', fontSize=10, textColor=DARK, spaceBefore=10, spaceAfter=5)
        SUB   = ParagraphStyle('SUB',   fontName='Helvetica',      fontSize=9,  textColor=MID,  alignment=TA_CENTER, spaceAfter=4)
        LBL   = ParagraphStyle('LBL',   fontName='Helvetica-Bold', fontSize=8,  textColor=LIGHT)
        VAL   = ParagraphStyle('VAL',   fontName='Helvetica',      fontSize=9,  textColor=DARK)
        SMALL = ParagraphStyle('SMALL', fontName='Helvetica',      fontSize=7,  textColor=LIGHT, alignment=TA_CENTER)
        CELL  = ParagraphStyle('CELL',  fontName='Helvetica',      fontSize=8,  textColor=DARK)
        CELLR = ParagraphStyle('CELLR', fontName='Helvetica',      fontSize=8,  textColor=DARK,  alignment=TA_RIGHT)
        CELLC = ParagraphStyle('CELLC', fontName='Helvetica',      fontSize=8,  textColor=MID,   alignment=TA_CENTER)
        HEAD  = ParagraphStyle('HEAD',  fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white)
        HEADR = ParagraphStyle('HEADR', fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white, alignment=TA_RIGHT)
        HEADC = ParagraphStyle('HEADC', fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white, alignment=TA_CENTER)

        col_full = PAGE_W - 2 * MARGIN

        # ── Inline flowable for ReportLab vector drawings ─────────────
        class _DrawingFlowable(Flowable):
            def __init__(self, drawing):
                Flowable.__init__(self)
                self._d = drawing
                self.width  = drawing.width
                self.height = drawing.height
            def draw(self):
                _renderPDF.draw(self._d, self.canv, 0, 0)

        # ── Pre-parse Design Placements & Dimensions ──────────────────
        placed_raw = (booking.design.placed_items or []) if booking.design else []
        dims       = (booking.design.dimensions or {}) if booking.design else {}
        plant_num  = {}  # pid str → 1-based number; shared by the BOM No. column
        plant_groups = {}  # pid str → {'label': str, 'positions': [(x,z)]}

        if placed_raw and isinstance(placed_raw, list):
            # Parse plant positions from both AI-format and 3D-studio-format items
            for item in placed_raw:
                if 'x' in item and 'z' in item:
                    pid   = str(item.get('plant_id', item.get('productId', '?')))
                    label = item.get('name') or pid
                    x, z  = float(item['x']), float(item['z'])
                elif 'position' in item:
                    pid   = str(item.get('productId', item.get('modelType', '?')))
                    label = item.get('name') or pid
                    x = float(item['position'].get('x', 0))
                    z = float(item['position'].get('z', 0))
                else:
                    continue
                plant_groups.setdefault(pid, {'label': label, 'positions': []})['positions'].append((x, z))

            if plant_groups:
                # Assign stable 1-based numbers so the legend and BOM can cross-reference
                for seq, pid in enumerate(plant_groups.keys(), 1):
                    plant_num[pid] = seq

        story = []

        # ── Page 1: Administrative Details & Render ───────────────────
        story.append(Paragraph('WORK ORDER', H1))
        story.append(Paragraph(
            f'Booking #{booking.id}  ·  {booking.get_service_type_display()}  ·  {booking.scheduled_date}  ·  Status: {booking.status}',
            SUB,
        ))
        story.append(HRFlowable(width='100%', thickness=2, color=GREEN, spaceAfter=8))

        # Project details table in a clean 2-column layout to save vertical space
        story.append(Paragraph('PROJECT DETAILS', H2))
        customer = booking.user
        cname = f"{customer.first_name} {customer.last_name}".strip() or customer.username
        scheduled_val = f"{booking.scheduled_date}  ·  {booking.preferred_time or 'Morning'}"
        meta_data = [
            [
                Paragraph('CUSTOMER', LBL), Paragraph(cname, VAL),
                Paragraph('SCHEDULED', LBL), Paragraph(scheduled_val, VAL)
            ],
            [
                Paragraph('CONTACT', LBL), Paragraph(booking.contact_number or '—', VAL),
                Paragraph('STATUS', LBL), Paragraph(booking.status, VAL)
            ],
            [
                Paragraph('SITE ADDRESS', LBL), Paragraph(booking.service_address or '—', VAL),
                Paragraph('PROGRESS', LBL), Paragraph(f"{booking.progress_pct}%", VAL)
            ]
        ]
        meta_tbl = Table(meta_data, colWidths=[3.0*cm, col_full/2 - 3.0*cm, 3.0*cm, col_full/2 - 3.0*cm])
        meta_tbl.setStyle(TableStyle([
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [BG, colors.white]),
            ('TOPPADDING',     (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',  (0, 0), (-1, -1), 5),
            ('LEFTPADDING',    (0, 0), (-1, -1), 7),
            ('RIGHTPADDING',   (0, 0), (-1, -1), 7),
            ('GRID',           (0, 0), (-1, -1), 0.3, RULE),
            ('VALIGN',         (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(meta_tbl)

        # Assigned crew
        crew_names = []
        for u in booking.assigned_crew.all():
            full = f"{u.first_name} {u.last_name}".strip()
            crew_names.append(full or u.username)
        if crew_names:
            story.append(Paragraph('ASSIGNED CREW', H2))
            story.append(Paragraph('  ·  '.join(crew_names), VAL))
            story.append(Spacer(1, 4))

        # AI Design Photo (on Page 1 if present)
        ai_url = None
        if booking.design:
            ai_url = booking.design.original_image_url or None
        if ai_url:
            try:
                import requests as _req
                from PIL import Image as PILImage
                resp = _req.get(ai_url, timeout=8)
                if resp.status_code == 200:
                    img_buf = _io.BytesIO(resp.content)
                    pil = PILImage.open(img_buf).convert('RGB')
                    pil.thumbnail((700, 300), PILImage.LANCZOS)
                    out = _io.BytesIO()
                    pil.save(out, format='JPEG', quality=85)
                    out.seek(0)
                    story.append(Paragraph('AI DESIGN RENDER', H2))
                    story.append(RLImage(out, width=col_full, height=5.5*cm, kind='proportional'))
                    story.append(Spacer(1, 4))
            except Exception:
                pass

        # ── Page 2: Dedicated Blueprint & Field Reference ─────────────
        ref_url = None
        if booking.design:
            ref_url = booking.design.reference_image_url or None

        # Build Page 2 if blueprint design or site reference photo exists
        if plant_groups or (ref_url and ref_url != ai_url):
            story.append(PageBreak())

            if plant_groups:
                story.append(Paragraph('GARDEN LAYOUT BLUEPRINT  (Overhead / Orthographic View)', H2))

                # Redesigned 25m x 10m grid scaling
                lot_w = 25.0
                lot_l = 10.0

                DIAG_W  = float(col_full)
                DIAG_H  = 13.5 * cm  # Fulfills min 50% vertical page real estate requirement
                D_MARG  = 35.0

                usable_w = DIAG_W - 2 * D_MARG
                usable_h = DIAG_H - 2 * D_MARG
                scale    = min(usable_w / lot_w, usable_h / lot_l)

                lot_px_w = lot_w * scale
                lot_px_h = lot_l * scale
                ox = (DIAG_W - lot_px_w) / 2
                oy = (DIAG_H - lot_px_h) / 2

                drw = Drawing(DIAG_W, DIAG_H)

                # Lot background + green boundary frame
                drw.add(Rect(ox, oy, lot_px_w, lot_px_h,
                             fillColor=colors.HexColor('#f0fdf4'),
                             strokeColor=GREEN, strokeWidth=1.5))

                # Minor grid lines (every 1 m) — very faint
                GRID_MINOR = colors.HexColor('#d1fae5')
                for xi in range(1, int(lot_w)):
                    gx = ox + xi * scale
                    drw.add(Line(gx, oy, gx, oy + lot_px_h,
                                 strokeColor=GRID_MINOR, strokeWidth=0.3))
                for zi in range(1, int(lot_l)):
                    gz = oy + zi * scale
                    drw.add(Line(ox, gz, ox + lot_px_w, gz,
                                 strokeColor=GRID_MINOR, strokeWidth=0.3))

                # Major grid lines (every 5 m) — slightly more visible
                GRID_MAJOR = colors.HexColor('#6ee7b7')
                for xi in range(5, int(lot_w), 5):
                    gx = ox + xi * scale
                    drw.add(Line(gx, oy, gx, oy + lot_px_h,
                                 strokeColor=GRID_MAJOR, strokeWidth=0.8))
                for zi in range(5, int(lot_l), 5):
                    gz = oy + zi * scale
                    drw.add(Line(ox, gz, ox + lot_px_w, gz,
                                 strokeColor=GRID_MAJOR, strokeWidth=0.8))

                # Tick labels every 5 m along width axis (bottom)
                for xi in range(0, int(lot_w) + 1, 5):
                    gx = ox + xi * scale
                    drw.add(GrStr(gx, oy - 12, f'{xi}m',
                                  textAnchor='middle', fontSize=7, fillColor=LIGHT))

                # Tick labels every 5 m along length axis (left)
                for zi in range(0, int(lot_l) + 1, 5):
                    gz = oy + zi * scale
                    drw.add(GrStr(ox - 12, gz - 2.5, f'{zi}m',
                                  textAnchor='end', fontSize=7, fillColor=LIGHT))

                # Clear orientation markers (House Side / Fence Side)
                drw.add(GrStr(ox + lot_px_w / 2, oy + lot_px_h + 15, "FENCE SIDE",
                              textAnchor='middle', fontSize=9, fontName='Helvetica-Bold', fillColor=DARK))
                drw.add(GrStr(ox + lot_px_w / 2, oy - 25, "HOUSE SIDE",
                              textAnchor='middle', fontSize=9, fontName='Helvetica-Bold', fillColor=DARK))

                # North indicator (top-right corner, outside grid but inside canvas margin)
                drw.add(GrStr(ox + lot_px_w, oy + lot_px_h + 15,
                              'N ↑', textAnchor='end', fontSize=9, fontName='Helvetica-Bold', fillColor=MID))

                # Dimension span labels
                drw.add(GrStr(ox + lot_px_w / 2, oy - 38,
                              f'Width: {lot_w:.0f} m',
                              textAnchor='middle', fontSize=8,
                              fillColor=MID, fontName='Helvetica-Bold'))

                from reportlab.graphics.shapes import Group as _GrGrp
                h_lbl_grp = _GrGrp()
                h_lbl_grp.add(GrStr(0, 0, f'Length: {lot_l:.0f} m',
                                    textAnchor='middle', fontSize=8,
                                    fillColor=MID, fontName='Helvetica-Bold'))
                h_lbl_grp.transform = (0, 1, -1, 0, ox - 26, oy + lot_px_h / 2)
                drw.add(h_lbl_grp)

                # Plant markers (e.g. 1.24, 2.25) rendered as circles with inner labels
                PALETTE = [
                    '#059669', '#0ea5e9', '#f59e0b', '#8b5cf6',
                    '#ef4444', '#06b6d4', '#84cc16', '#f97316',
                ]
                legend_entries = []   # [(seq, label, hex_color)]
                r_pt = 11.0  # Larger marker size for clear text legibility

                for pid, data in plant_groups.items():
                    seq     = plant_num[pid]
                    hex_col = PALETTE[(seq - 1) % len(PALETTE)]
                    rl_col  = colors.HexColor(hex_col)
                    legend_entries.append((seq, data['label'], hex_col))

                    for idx, (x, z) in enumerate(data['positions']):
                        # Use abs to guard against negative coordinates
                        cx = ox + abs(float(x)) * scale
                        cy = oy + abs(float(z)) * scale
                        cx = max(ox + r_pt, min(ox + lot_px_w - r_pt, cx))
                        cy = max(oy + r_pt, min(oy + lot_px_h - r_pt, cy))
                        drw.add(Circle(cx, cy, r_pt,
                                       fillColor=rl_col,
                                       strokeColor=colors.white, strokeWidth=0.8))
                        # Format label as Species.Instance (e.g. 1.24, 2.25)
                        marker_str = f"{seq}.{idx+1}"
                        drw.add(GrStr(cx, cy - r_pt * 0.25,
                                      marker_str,
                                      textAnchor='middle',
                                      fontSize=6.5,
                                      fontName='Helvetica-Bold',
                                      fillColor=colors.white))

                story.append(_DrawingFlowable(drw))
                story.append(Spacer(1, 5))

                # Plant Legend immediately adjacent to/below the Blueprint
                HLEG = ParagraphStyle('HLEG', fontName='Helvetica-Bold', fontSize=8,
                                      textColor=MID, spaceAfter=3)
                story.append(Paragraph('PLANT LEGEND  (Markers format: Species.Instance, e.g. 1.24 is instance 24 of species 1)', HLEG))
                legend_entries.sort(key=lambda e: e[0])
                leg_cols = 3
                leg_row_data = [legend_entries[i:i+leg_cols]
                                for i in range(0, len(legend_entries), leg_cols)]
                for row in leg_row_data:
                    cells = []
                    for seq, lbl, hex_col in row:
                        cells.append(Paragraph(
                            f'<font color="{hex_col}"><b>&#9679;</b></font>'
                            f'  <b>{seq}.</b>  {lbl}',
                            CELL,
                        ))
                    while len(cells) < leg_cols:
                        cells.append(Paragraph('', CELL))
                    story.append(Table(
                        [cells],
                        colWidths=[col_full / leg_cols] * leg_cols,
                        style=TableStyle([
                            ('TOPPADDING',    (0, 0), (-1, -1), 2),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
                        ]),
                    ))
                story.append(Spacer(1, 6))

            # Site Reference — "Before" Photo (Grouped with blueprint and legend on Page 2)
            if ref_url and ref_url != ai_url:
                try:
                    import requests as _req
                    from PIL import Image as PILImage, ImageEnhance as _PILEnh
                    resp = _req.get(ref_url, timeout=8)
                    if resp.status_code == 200:
                        img_buf = _io.BytesIO(resp.content)
                        pil = PILImage.open(img_buf).convert('RGB')
                        # Boost contrast and sharpness for high-contrast field print
                        pil = _PILEnh.Contrast(pil).enhance(1.25)
                        pil = _PILEnh.Sharpness(pil).enhance(1.3)
                        pil.thumbnail((700, 300), PILImage.LANCZOS)
                        out = _io.BytesIO()
                        pil.save(out, format='JPEG', quality=88)
                        out.seek(0)
                        story.append(Paragraph('SITE REFERENCE  —  "Before" Photo  (use as visual anchor on site)', H2))
                        story.append(RLImage(out, width=col_full, height=5.5*cm, kind='proportional'))
                        story.append(Spacer(1, 4))
                except Exception:
                    pass

        # ── Page 3: Technical Specifications & Workflow Sign-off ─────
        bom = []
        if booking.design and booking.design.plant_breakdown:
            bom = booking.design.plant_breakdown

        if bom or booking.milestones or booking.staff_notes:
            # Force technical details and acknowledgement to a new page to remain the final workflow step
            if plant_groups or (ref_url and ref_url != ai_url):
                story.append(PageBreak())

            if bom:
                # ── Build spacing lookup ───────────────────────────────────
                spacing_map = {}   # plant_id (int) → spacing label string

                # Compute nearest-neighbour average spacing from placed_items
                nn_spacing = {}   # pid str → avg metres (always >= 0)
                if placed_raw:
                    pos_by_pid = {}
                    for item in placed_raw:
                        pid_key = str(item.get('plant_id', item.get('productId', '')))
                        if not pid_key:
                            continue
                        if 'x' in item and 'z' in item:
                            pos_by_pid.setdefault(pid_key, []).append(
                                (abs(float(item['x'])), abs(float(item['z']))))
                        elif 'position' in item:
                            pos_by_pid.setdefault(pid_key, []).append((
                                abs(float(item['position'].get('x', 0))),
                                abs(float(item['position'].get('z', 0))),
                            ))
                    for pid_key, positions in pos_by_pid.items():
                        if len(positions) < 2:
                            continue
                        dists = []
                        for i, (x1, z1) in enumerate(positions):
                            min_d = min(
                                _math.sqrt((x2-x1)**2 + (z2-z1)**2)
                                for j, (x2, z2) in enumerate(positions) if i != j
                            )
                            dists.append(min_d)
                        avg = sum(dists) / len(dists)
                        if avg > 0:
                            nn_spacing[pid_key] = avg

                # Priority: explicit spacing_cm → computed NN → derived from real_world_size
                # Sanitized: No prefix characters like '~', formatted as positive integers
                plant_ids = [int(r['plant_id']) for r in bom if r.get('plant_id')]
                if plant_ids:
                    from .models import InventoryItem as _Inv
                    for inv in _Inv.objects.filter(id__in=plant_ids).values('id', 'spacing_cm', 'real_world_size'):
                        pid_int = inv['id']
                        if inv['spacing_cm']:
                            cm_val = abs(int(inv['spacing_cm']))
                            spacing_map[pid_int] = f"{cm_val} cm OC"
                        elif str(pid_int) in nn_spacing:
                            cm_val = abs(round(nn_spacing[str(pid_int)] * 100))
                            if cm_val > 0:
                                spacing_map[pid_int] = f"{cm_val} cm OC"
                        elif inv['real_world_size']:
                            cm_val = abs(round(float(inv['real_world_size']) * 150))
                            if cm_val > 0:
                                spacing_map[pid_int] = f"{cm_val} cm OC"

                # ── BOM table — columns: No. | Plant / Item | Qty | Spacing | Unit | Total ──
                story.append(Paragraph('PLANT BILL OF MATERIALS', H2))
                # Width proportions: No.(0.05) | Name(0.33) | Qty(0.08) | Spacing(0.18) | Unit(0.18) | Total(0.18)
                cw = [col_full * r for r in (0.05, 0.33, 0.08, 0.18, 0.18, 0.18)]
                bom_headers = [
                    Paragraph('No.', HEADC),
                    Paragraph('Plant / Item', HEAD),
                    Paragraph('Qty', HEADC),
                    Paragraph('Spacing', HEADC),
                    Paragraph('Unit (₱)', HEADR),
                    Paragraph('Total (₱)', HEADR),
                ]
                bom_rows = [bom_headers]

                grand = 0.0
                for r in bom:
                    pid_int    = int(r['plant_id']) if r.get('plant_id') else None
                    pid_str    = str(r.get('plant_id', ''))
                    qty        = float(r.get('quantity', 0))
                    unit_price = float(r.get('unit_price', 0))
                    line_total = qty * unit_price
                    grand     += line_total
                    spacing_lbl = spacing_map.get(pid_int, '100 cm OC') if pid_int else '—'
                    num_lbl    = str(plant_num.get(pid_str, '')) if pid_str in plant_num else '—'
                    bom_rows.append([
                        Paragraph(num_lbl, CELLC),
                        Paragraph(r.get('name', ''), CELL),
                        Paragraph(str(int(qty)) if qty == int(qty) else f'{qty:.1f}', CELLC),
                        Paragraph(spacing_lbl, CELLC),
                        Paragraph(f"{unit_price:,.2f}", CELLR),
                        Paragraph(f"{line_total:,.2f}", CELLR),
                    ])

                GT  = ParagraphStyle('GT',  fontName='Helvetica-Bold', fontSize=8,  textColor=DARK)
                GTR = ParagraphStyle('GTR', fontName='Helvetica-Bold', fontSize=10, textColor=GREEN, alignment=TA_RIGHT)
                bom_rows.append([
                    Paragraph('', CELLC),
                    Paragraph('GRAND TOTAL', GT),
                    Paragraph('', CELLC),
                    Paragraph('', CELLC),
                    Paragraph('', CELLR),
                    Paragraph(f"₱ {grand:,.2f}", GTR),
                ])
                bom_tbl = Table(bom_rows, colWidths=cw)
                bom_tbl.setStyle(TableStyle([
                    ('BACKGROUND',     (0, 0),  (-1, 0),  DARK),
                    ('ROWBACKGROUNDS', (0, 1),  (-1, -2), [BG, colors.white]),
                    ('BACKGROUND',     (0, -1), (-1, -1), BG2),
                    ('LINEABOVE',      (0, -1), (-1, -1), 1.0, GREEN),
                    ('LINEBELOW',      (0, -1), (-1, -1), 1.0, GREEN),
                    ('TOPPADDING',     (0, 0),  (-1, -1), 5),
                    ('BOTTOMPADDING',  (0, 0),  (-1, -1), 5),
                    ('LEFTPADDING',    (0, 0),  (-1, -1), 6),
                    ('RIGHTPADDING',   (0, 0),  (-1, -1), 6),
                    ('GRID',           (0, 0),  (-1, -1), 0.2, RULE),
                    ('VALIGN',         (0, 0),  (-1, -1), 'MIDDLE'),
                    ('LINEAFTER',      (0, 0),  (0, -1),  0.5, RULE),
                ]))
                story.append(bom_tbl)
                story.append(Spacer(1, 6))

            # ── Project milestones ────────────────────────────────────
            if booking.milestones:
                story.append(Paragraph('PROJECT MILESTONES', H2))
                mc = [col_full * r for r in (0.55, 0.15, 0.30)]
                DONE_STYLE  = ParagraphStyle('DONE',  fontName='Helvetica-Bold', fontSize=9,
                                             textColor=GREEN, alignment=TA_CENTER)
                PEND_STYLE  = ParagraphStyle('PEND',  fontName='Helvetica',      fontSize=9,
                                             textColor=LIGHT, alignment=TA_CENTER)
                m_rows = [[Paragraph(h, HEAD) for h in ['Milestone', 'Status', 'Completed At']]]
                for m in booking.milestones:
                    done = bool(m.get('completed', False))
                    m_rows.append([
                        Paragraph(m.get('label', ''), CELL),
                        Paragraph('✔  Done' if done else '○  Pending', DONE_STYLE if done else PEND_STYLE),
                        Paragraph(str(m.get('completed_at') or '—')[:10], CELL),
                    ])
                m_tbl = Table(m_rows, colWidths=mc)
                m_tbl.setStyle(TableStyle([
                    ('BACKGROUND',     (0, 0), (-1, 0),  DARK),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BG, colors.white]),
                    ('TOPPADDING',     (0, 0), (-1, -1), 6),
                    ('BOTTOMPADDING',  (0, 0), (-1, -1), 6),
                    ('LEFTPADDING',    (0, 0), (-1, -1), 8),
                    ('RIGHTPADDING',   (0, 0), (-1, -1), 8),
                    ('GRID',           (0, 0), (-1, -1), 0.2, RULE),
                    ('BOX',            (0, 0), (-1, -1), 1.5, DARK),
                    ('LINEBEFORE',     (0, 1), (0, -1),  3.0, GREEN),
                ]))
                story.append(m_tbl)
                story.append(Spacer(1, 6))

            # ── Field notes ───────────────────────────────────────────
            if booking.staff_notes:
                story.append(Paragraph('FIELD NOTES', H2))
                story.append(Paragraph(booking.staff_notes, VAL))
                story.append(Spacer(1, 6))

            # ── Client Acknowledgement — bordered box ─────────────────
            ACK_HDR = ParagraphStyle('ACKHDR', fontName='Helvetica-Bold', fontSize=9,
                                      textColor=colors.white)
            ACK_NOTE = ParagraphStyle('ACKNOTE', fontName='Helvetica', fontSize=8,
                                       textColor=MID, spaceAfter=6)
            SIG_LBL  = ParagraphStyle('SIGLBL', fontName='Helvetica-Bold', fontSize=7,
                                       textColor=LIGHT, spaceAfter=2)
            SIG_NOTE = ParagraphStyle('SIGNOTE', fontName='Helvetica', fontSize=6,
                                       textColor=LIGHT, alignment=TA_CENTER)

            ack_content = [
                [Paragraph('CLIENT ACKNOWLEDGEMENT', ACK_HDR), ''],
                [Paragraph(
                    'By signing below the client confirms that the work described in this Work Order '
                    'has been completed to a satisfactory standard.',
                    ACK_NOTE,
                ), ''],
                [
                    [
                        Paragraph('Client Signature:', SIG_LBL),
                        Spacer(1, 0.85 * cm),
                        HRFlowable(width='100%', thickness=0.8, color=DARK, spaceAfter=3),
                        Paragraph('Signature over Printed Name', SIG_NOTE),
                    ],
                    [
                        Paragraph('Date:', SIG_LBL),
                        Spacer(1, 0.85 * cm),
                        HRFlowable(width='100%', thickness=0.8, color=DARK, spaceAfter=3),
                        Paragraph('MM / DD / YYYY', SIG_NOTE),
                    ],
                ],
            ]

            ack_tbl = Table(
                ack_content,
                colWidths=[col_full * 0.65, col_full * 0.35],
            )
            ack_tbl.setStyle(TableStyle([
                ('BACKGROUND',    (0, 0), (-1, 0),  DARK),
                ('SPAN',          (0, 0), (-1, 0)),
                ('TOPPADDING',    (0, 0), (-1, 0),  6),
                ('BOTTOMPADDING', (0, 0), (-1, 0),  6),
                ('LEFTPADDING',   (0, 0), (-1, 0),  10),
                ('BACKGROUND',    (0, 1), (-1, 1),  BG),
                ('SPAN',          (0, 1), (-1, 1)),
                ('TOPPADDING',    (0, 1), (-1, 1),  6),
                ('BOTTOMPADDING', (0, 1), (-1, 1),  4),
                ('LEFTPADDING',   (0, 1), (-1, 1),  10),
                ('RIGHTPADDING',  (0, 1), (-1, 1),  10),
                ('VALIGN',        (0, 2), (-1, 2),  'BOTTOM'),
                ('TOPPADDING',    (0, 2), (-1, 2),  8),
                ('BOTTOMPADDING', (0, 2), (-1, 2),  10),
                ('LEFTPADDING',   (0, 2), (-1, 2),  10),
                ('RIGHTPADDING',  (0, 2), (-1, 2),  10),
                ('BOX',           (0, 0), (-1, -1), 1.5, DARK),
                ('LINEAFTER',     (0, 2), (0, 2),   0.5, RULE),
            ]))
            story.append(ack_tbl)

        # ── Footer ───────────────────────────────────────────────────
        from django.utils import timezone as _tz
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width='100%', thickness=0.3, color=RULE, spaceAfter=4))
        story.append(Paragraph(
            f'Generated {_tz.localdate().strftime("%B %d, %Y")}  ·  Garden Studio  ·  '
            f'Work Order #{booking.id}  ·  CONFIDENTIAL — INTERNAL FIELD USE ONLY',
            SMALL,
        ))

        # ── Diagonal watermark on every page ─────────────────────────
        _WM_W, _WM_H = PAGE_W, PAGE_H

        def _watermark(canv, _doc):
            canv.saveState()
            canv.setFont('Helvetica-Bold', 52)
            canv.setFillColor(colors.Color(0.72, 0.72, 0.72, alpha=0.09))
            canv.translate(_WM_W / 2, _WM_H / 2)
            canv.rotate(42)
            canv.drawCentredString(0, 30, 'CONFIDENTIAL')
            canv.setFont('Helvetica', 18)
            canv.setFillColor(colors.Color(0.72, 0.72, 0.72, alpha=0.07))
            canv.drawCentredString(0, -22, 'INTERNAL — FIELD USE ONLY')
            canv.restoreState()

        doc.build(story, onFirstPage=_watermark, onLaterPages=_watermark)
        buffer.seek(0)

        from django.http import HttpResponse as DjangoHttpResponse
        pdf_response = DjangoHttpResponse(buffer.getvalue(), content_type='application/pdf')
        pdf_response['Content-Disposition'] = f'attachment; filename="work-order-{booking.id}.pdf"'
        pdf_response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return pdf_response


class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = (Order.objects.select_related('delivery_crew', 'delivered_by')
              .prefetch_related('items__item', 'items__review').order_by('-created_at'))
        user = self.request.user
        # Field crew only see the orders they were given to deliver
        if work_queues.is_field_crew(user):
            return qs.filter(delivery_crew=user)
        # Admins and office staff can see all orders
        if user.is_staff or user.is_superuser:
            return qs
        # Standard users can only see their own orders
        return qs.filter(user=user)

    def list(self, request, *args, **kwargs):
        paymongo.expire_unpaid_online_orders()
        params = request.query_params
        qs = work_queues.filter_orders(self.get_queryset(), params)
        if not work_queues.wants_page(params):
            return Response(self.get_serializer(qs, many=True).data)
        rows, meta = work_queues.paginate(qs, params)
        return Response({**meta, 'results': self.get_serializer(rows, many=True).data})

    @action(detail=False, methods=['get'])
    def counts(self, request):
        """Badge numbers for every staff order queue, without downloading the orders."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        paymongo.expire_unpaid_online_orders()
        qs = self.get_queryset()
        return Response({**work_queues.order_counts(qs), **work_queues.arrivals(qs, request.query_params.get('since'))})

    @action(detail=False, methods=['get'], permission_classes=[IsOfficeAdmin])
    def export(self, request):
        """CSV of the orders matching the current queue and filters (same params as the list)."""
        header, rows = work_queues.order_csv(work_queues.filter_orders(self.get_queryset(), request.query_params))
        return work_queues.csv_response(header, rows, f'orders-{datetime.date.today():%Y%m%d}.csv')

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        is_staff = request.user.is_staff or request.user.is_superuser
        if not is_staff and (instance.status != 'Pending' or instance.payment_status == 'Paid'):
            # A paid order needs a refund from staff, not a silent delete
            return Response({'error': 'This order can no longer be cancelled here. Please contact us.'},
                            status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            if instance.status not in ('Cancelled', 'Delivered'):
                paymongo.release_stock(instance)
            instance.delete()
        if instance.paymongo_checkout_id:
            paymongo.expire_checkout_session(instance.paymongo_checkout_id)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def update_status(self, request, pk=None):
        order = self.get_object()
        new_payment_status = request.data.get('payment_status')

        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)
        if new_payment_status and new_payment_status not in dict(Order.PAYMENT_STATUS_CHOICES):
            return Response({'error': f'Invalid payment status: {new_payment_status}'}, status=status.HTTP_400_BAD_REQUEST)
        new_status = _normalize_order_status(request.data.get('status'))
        if new_status is False:
            return Response({'error': f"Invalid status: {request.data.get('status')}"}, status=status.HTTP_400_BAD_REQUEST)

        order, error = work_queues.change_order(
            order.pk, new_status, new_payment_status, IsOfficeAdmin().has_permission(request, self), actor=request.user,
            when=offline.action_time(request))
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(OrderSerializer(order, context={'request': request}).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['post'])
    def bulk_status(self, request):
        """Move many orders at once (e.g. ship today's packed orders). Each order follows the usual rules."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        new_status = _normalize_order_status(request.data.get('status'))
        ids = request.data.get('ids')
        if not new_status:
            return Response({'error': 'A valid status is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(ids, list) or not ids or len(ids) > work_queues.MAX_PAGE_SIZE \
                or not all(isinstance(i, int) for i in ids):
            return Response({'error': f'Send 1–{work_queues.MAX_PAGE_SIZE} order ids.'}, status=status.HTTP_400_BAD_REQUEST)

        is_office_admin = IsOfficeAdmin().has_permission(request, self)
        # Optional: who delivers this batch (e.g. "ship these 5 with Eddie")
        crew = None
        if request.data.get('delivery_crew'):
            if not is_office_admin:
                return Response({'error': 'Only the office assigns deliveries.'}, status=status.HTTP_403_FORBIDDEN)
            crew = User.objects.filter(pk=request.data['delivery_crew']).first()
            if crew is None:
                return Response({'error': 'That crew member was not found.'}, status=status.HTTP_400_BAD_REQUEST)

        visible = set(self.get_queryset().filter(pk__in=ids).values_list('pk', flat=True))  # crew: only their own
        updated, failed = [], []
        for pk in dict.fromkeys(ids):
            if pk not in visible:
                failed.append({'id': pk, 'error': f'Order #{pk} was not found.'})
                continue
            order, error = work_queues.change_order(pk, new_status, None, is_office_admin, actor=request.user)
            if not error and crew is not None:
                error = work_queues.assign_delivery(order, crew)
            if error:
                failed.append({'id': pk, 'error': error})
            else:
                updated.append(pk)
        return Response({'updated': updated, 'failed': failed})

    @action(detail=True, methods=['post'], permission_classes=[IsOfficeAdmin])
    def assign_delivery(self, request, pk=None):
        """Choose which crew member delivers this order ({crew_id: id} or {crew_id: null} to unassign)."""
        order = self.get_object()
        crew_id = request.data.get('crew_id')
        crew = User.objects.filter(pk=crew_id).first() if crew_id else None
        if crew_id and crew is None:
            return Response({'error': 'That crew member was not found.'}, status=status.HTTP_400_BAD_REQUEST)
        error = work_queues.assign_delivery(order, crew)
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
        return Response(OrderSerializer(order, context={'request': request}).data)

    @action(detail=False, methods=['get'])
    def my_deliveries(self, request):
        """The signed-in crew member's delivery run and the COD cash they collected today."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        active, done, cash_total = work_queues.my_deliveries(request.user)
        holding = list(cash.with_crew(request.user))
        ctx = {'request': request}
        return Response({
            'active': OrderSerializer(active, many=True, context=ctx).data,
            'delivered_today': OrderSerializer(done, many=True, context=ctx).data,
            'cash_today': str(cash_total),
            # Not yet handed to the office (any day), so yesterday's forgotten cash still shows
            'cash_to_hand_in': str(sum((o.total_price for o in holding), 0)),
            'cash_orders': len(holding),
        })

    @action(detail=True, methods=['post'])
    def report_problem(self, request, pk=None):
        """Crew can't complete a delivery: tell the office (the order stays with the crew member)."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        order = self.get_object()  # crew: only orders assigned to them
        reason = str(request.data.get('reason') or '').strip()[:120]
        note = str(request.data.get('note') or '').strip()[:500]
        if not reason:
            return Response({'error': 'Say what went wrong.'}, status=status.HTTP_400_BAD_REQUEST)
        notifications.delivery_problem(order, request.user, reason, (note + offline.delay_note(request)).strip())
        return Response({'ok': True})

    @action(detail=False, methods=['get'])
    def pick_list(self, request):
        """Items to gather for a batch of orders, plus one packing slip per order."""
        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)
        params = request.query_params
        qs = self.get_queryset()
        ids = [int(i) for i in params.get('ids', '').split(',') if i.strip().isdigit()]
        qs = qs.filter(pk__in=ids) if ids else work_queues.filter_orders(qs, {**params.dict(), 'queue': params.get('queue') or 'to_pack'})
        return Response(work_queues.pick_list(qs.order_by('created_at')[:work_queues.PICK_LIST_LIMIT]))


def _normalize_order_status(value):
    """None when no status was sent, False when it isn't a real status, else the canonical name."""
    if not value:
        return None
    # Accept the snake_case variants older clients send
    value = {'shipped': 'Shipped', 'out_for_delivery': 'Out for Delivery',
             'delivered': 'Delivered', 'cancelled': 'Cancelled'}.get(value, value)
    return value if value in dict(Order.STATUS_CHOICES) else False


def normalize_ph_mobile(raw):
    """'0917 123 4567', '+63 917-123-4567', '639171234567' -> '09171234567'; None if not a PH mobile."""
    digits = re.sub(r'[\s\-().]', '', str(raw or ''))
    match = re.fullmatch(r'(?:\+?63|0)?(9\d{9})', digits)
    return f'0{match.group(1)}' if match else None


@api_view(['POST'])
def checkout(request):
    data = request.data
    customer_name = data.get('customer_name')
    customer_email = data.get('customer_email')
    customer_phone = data.get('customer_phone', '')
    customer_address = data.get('customer_address')
    payment_method = data.get('payment_method', 'online')
    items_data = data.get('items', [])
    # Any client-sent total_price is ignored: the server prices the order from the catalog.
    # (Trusting it let a modified request pay ₱1 for any order.)

    if payment_method not in dict(Order.PAYMENT_METHOD_CHOICES):
        return Response({'error': 'Invalid payment method.'}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(items_data, list) or not items_data:
        return Response({'error': 'Your cart is empty.'}, status=status.HTTP_400_BAD_REQUEST)
    quantities = {}
    for item_data in items_data:
        try:
            item_id = int(item_data['id'])
            quantity = int(item_data.get('quantity', 1))
        except (KeyError, TypeError, ValueError):
            return Response({'error': 'Each item needs a numeric id and quantity.'}, status=status.HTTP_400_BAD_REQUEST)
        if not 1 <= quantity <= 999:
            return Response({'error': 'Quantities must be between 1 and 999.'}, status=status.HTTP_400_BAD_REQUEST)
        quantities[item_id] = quantities.get(item_id, 0) + quantity

    if not customer_name or not customer_email or not customer_address:
        return Response({'error': 'Name, email and delivery address are required.'}, status=status.HTTP_400_BAD_REQUEST)
    # The rider calls this number on delivery
    customer_phone = normalize_ph_mobile(customer_phone)
    if not customer_phone:
        return Response({'error': 'Enter a valid PH mobile number, e.g. 09171234567.'}, status=status.HTTP_400_BAD_REQUEST)
    if payment_method == 'cod' and not request.user.is_authenticated:
        return Response({'error': 'Please log in to use Cash on Delivery, or pay online.'}, status=status.HTTP_400_BAD_REQUEST)

    # Return stock held by abandoned online payments before checking availability
    paymongo.expire_unpaid_online_orders()

    pricing = PricingSettings.current()
    shipping_fee = pricing.shipping_fee
    try:
        with transaction.atomic():
            order = Order.objects.create(
                user=request.user if request.user.is_authenticated else None,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_address=customer_address,
                payment_method=payment_method,
                receipt_token=secrets.token_urlsafe(32),
                total_price=0,
            )

            subtotal = 0
            for item_id, quantity in quantities.items():
                # select_for_update() locks the row until transaction ends, preventing race conditions
                try:
                    inventory_item = InventoryItem.objects.select_for_update().get(id=item_id)
                except InventoryItem.DoesNotExist:
                    raise ValueError(f"Item with ID {item_id} does not exist.")

                if not inventory_item.is_active:
                    raise ValueError(f"{inventory_item.name} is no longer sold. Remove it from your cart.")
                if inventory_item.stock_quantity < quantity:
                    raise ValueError(f"Insufficient stock for {inventory_item.name}. Available: {inventory_item.stock_quantity}")

                inventory_item.stock_quantity -= quantity
                inventory_item.save(update_fields=['stock_quantity'])
                inventory.record(inventory_item, -quantity, 'sale', user=request.user if request.user.is_authenticated else None,
                                 note=f'Order #{order.id}')

                OrderItem.objects.create(
                    order=order,
                    item=inventory_item,
                    quantity=quantity,
                    price_at_booking=inventory_item.unit_price
                )
                subtotal += inventory_item.unit_price * quantity

            order.total_price = subtotal + shipping_fee
            if payment_method == 'cod' and pricing.cod_max_amount and order.total_price > pricing.cod_max_amount:
                # Raising rolls back the order and the stock it took
                raise ValueError(f'Cash on Delivery is available for orders up to ₱{pricing.cod_max_amount:,.0f}. '
                                 'Please pay online with QR Ph, GCash, Maya or card.')
            order.save(update_fields=['total_price'])

        if order.payment_method == 'cod':
            notifications.order_new(order)
        return Response({'message': 'Order successfully placed!', 'order_id': order.id,
                         'receipt_token': order.receipt_token,
                         'subtotal': float(subtotal), 'shipping_fee': float(shipping_fee),
                         'total_price': float(order.total_price)},
                        status=status.HTTP_201_CREATED)

    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# PayMongo Payment Endpoints (QR Ph / GCash / Maya / Card)
# ─────────────────────────────────────────────────────────────────────────────

def _can_view_order(request, order, token=''):
    """Owner, staff, or anyone holding the order's receipt token (guest checkouts)."""
    user = request.user
    if user.is_authenticated and (user.is_staff or user.is_superuser or order.user_id == user.id):
        return True
    return bool(token and order.receipt_token and secrets.compare_digest(token, order.receipt_token))


@api_view(['POST'])
def create_checkout_session(request):
    """
    Accept an order_id and return a PayMongo hosted checkout_url for it.
    Also used by "Pay now" on My Orders: an open session is reused instead of making a new one.
    """
    order_id = request.data.get('order_id')
    if not order_id:
        return Response({'error': 'order_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        order = Order.objects.get(id=order_id)
    except (Order.DoesNotExist, ValueError, TypeError):
        return Response({'error': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
    # Only the customer who placed an order can pay for it (guest orders have no owner)
    if order.user_id and order.user_id != getattr(request.user, 'id', None):
        return Response({'error': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

    order = paymongo.sync_order_payment(order)
    if order.payment_status == 'Paid' or order.status == 'Cancelled':
        return Response({'error': f'Order #{order.id} is already {order.payment_status if order.payment_status == "Paid" else "cancelled"}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    if order.payment_method != 'online':
        return Response({'error': 'This order is Cash on Delivery.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        if order.paymongo_checkout_id:
            session = paymongo.retrieve_checkout_session(order.paymongo_checkout_id)
            if session['attributes'].get('status') == 'active':
                return Response({'checkout_url': session['attributes']['checkout_url']})

        receipt = f"{settings.FRONTEND_URL}/order-success?order_id={order.id}&token={order.receipt_token}"
        session_id, checkout_url = paymongo.create_checkout_session(
            order, success_url=receipt, cancel_url=f"{receipt}&cancelled=1")
    except paymongo.PayMongoError as e:
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

    order.paymongo_checkout_id = session_id
    order.save(update_fields=['paymongo_checkout_id'])
    return Response({'checkout_url': checkout_url})


@api_view(['GET'])
@permission_classes([AllowAny])
def order_receipt(request, pk):
    """
    GET /api/order-receipt/<id>/?token=...
    The order-success page reads the real payment state from here, never from its URL.
    Unpaid online orders are checked against PayMongo, so this works without webhooks (localhost).
    """
    order = Order.objects.filter(pk=pk).first()
    if not order or not _can_view_order(request, order, request.query_params.get('token', '')):
        return Response({'error': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
    order = paymongo.sync_order_payment(order)
    return Response({
        'id': order.id,
        'customer_name': order.customer_name,
        'total_price': float(order.total_price),
        'payment_method': order.payment_method,
        'payment_status': order.payment_status,
        'status': order.status,
        'created_at': order.created_at,
        'paid_at': order.paid_at,
        'items': [{'name': oi.item.name if oi.item else 'Deleted item', 'quantity': oi.quantity}
                  for oi in order.items.select_related('item')],
    })


@csrf_exempt
def paymongo_webhook(request):
    """
    PayMongo sends a signed POST here when a checkout session is paid.
    Register https://<domain>/api/paymongo-webhook/ for event checkout_session.payment.paid.
    """
    if request.method != 'POST':
        return HttpResponse(status=405)
    if not paymongo.verify_webhook_signature(request.body, request.META.get('HTTP_PAYMONGO_SIGNATURE', '')):
        return HttpResponse(status=400)
    try:
        event = json.loads(request.body)['data']['attributes']
    except (ValueError, KeyError, TypeError):
        return HttpResponse(status=400)

    if event.get('type') == 'checkout_session.payment.paid':
        session = event.get('data') or {}
        metadata = session.get('attributes', {}).get('metadata') or {}
        payment = paymongo.paid_payment(session)

        def by_session_or_metadata(model, key):
            obj_id = metadata.get(key)
            return (model.objects.filter(paymongo_checkout_id=session.get('id')).first()
                    or (model.objects.filter(pk=obj_id).first() if str(obj_id or '').isdigit() else None))

        order = by_session_or_metadata(Order, 'order_id')
        req = None if order else by_session_or_metadata(ProjectPaymentRequest, 'project_payment_request_id')
        if payment and order:
            paymongo.mark_order_paid(order, payment)
        elif payment and req:
            paymongo.mark_project_request_paid(req, payment)
        else:
            logger.warning('PayMongo webhook: no order/payment request/payment for session %s', session.get('id'))

    # Always 200 for verified events so PayMongo stops retrying
    return HttpResponse(status=200)


def payment_request_data(req):
    return {
        'id': req.id,
        'label': req.label,
        'amount': float(req.amount),
        'status': req.status,
        'status_display': req.get_status_display(),
        'created_at': req.created_at,
        'paid_at': req.paid_at,
        'pay_url': f"{settings.FRONTEND_URL}/pay/{req.token}",
    }


def _payment_request_by_token(token):
    return (ProjectPaymentRequest.objects.select_related('project__booking__user')
            .filter(token=token).first())


@api_view(['GET'])
@permission_classes([AllowAny])
def project_payment_detail(request, token):
    """
    GET /api/project-payments/<token>/
    What the /pay/<token> page shows. Anyone with the link can see and pay it
    (staff send it over Messenger/SMS; a family member may pay), so only show what the payer needs.
    """
    req = _payment_request_by_token(token)
    if not req:
        return Response({'error': 'This payment link is not valid.'}, status=status.HTTP_404_NOT_FOUND)
    req = paymongo.sync_project_request_payment(req)
    booking = req.project.booking
    return Response({
        **{k: v for k, v in payment_request_data(req).items() if k != 'pay_url'},
        'customer_first_name': booking.user.first_name or booking.user.username,
        'service': booking.get_service_type_display(),
        'booking_id': booking.id,
        'project_total': float(req.project.total_price),
        'project_balance': float(req.project.remaining_balance),
    })


@api_view(['POST'])
@permission_classes([AllowAny])
def project_payment_checkout(request, token):
    """POST /api/project-payments/<token>/checkout/ -> {checkout_url} on PayMongo."""
    req = _payment_request_by_token(token)
    if not req:
        return Response({'error': 'This payment link is not valid.'}, status=status.HTTP_404_NOT_FOUND)
    req = paymongo.sync_project_request_payment(req)
    if req.status != 'PENDING':
        return Response({'error': f'This payment is already {req.get_status_display().lower()}.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        url = paymongo.open_session_url(req.paymongo_checkout_id)
        if not url:
            page = f"{settings.FRONTEND_URL}/pay/{req.token}"
            session_id, url = paymongo.create_project_payment_session(
                req, success_url=f"{page}?done=1", cancel_url=page)
            req.paymongo_checkout_id = session_id
            req.save(update_fields=['paymongo_checkout_id'])
    except paymongo.PayMongoError as e:
        return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response({'checkout_url': url})


# ─────────────────────────────────────────────────────────────────────────────
# Forgot / Reset Password (no email required)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
def upload_design_image(request):
    """
    POST /api/upload-design-image/
    Accepts a single image file, saves it to media/design_refs/, and returns its absolute URL.
    Used by the AI Designer to persist the garden background photo before creating a GardenDesign.
    """
    if not request.user or not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
    if 'image' not in request.FILES:
        return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)

    from django.core.files.storage import default_storage
    from django.conf import settings as django_settings

    # Re-encoded as JPEG: anything that isn't a real image is refused, and EXIF/GPS is stripped
    content, error = sanitize_image(request.FILES['image'])
    if error:
        return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)
    filename = f"design_refs/ref_{int(time.time())}_{secrets.token_hex(4)}.jpg"
    saved_path = default_storage.save(filename, content)

    media_url = getattr(django_settings, 'MEDIA_URL', '/media/').rstrip('/')
    url = request.build_absolute_uri(f'{media_url}/{saved_path}')
    return Response({'url': url}, status=status.HTTP_201_CREATED)


RESET_SENT = ('If an account uses that email, we sent it a link to choose a new password. '
              'The link works for a few days and only once.')


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def password_reset_request(request):
    """
    POST /api/password-reset/  {email}
    Emails each active account with that address a one-time link:
    FRONTEND_URL/reset-password?uid=…&token=…  (Django's signed, single-use reset token).
    Always answers the same, so it can't be used to find out who has an account.
    """
    from django.contrib.auth.tokens import default_token_generator
    from django.core.mail import send_mail
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    email = str(request.data.get('email') or '').strip()
    if not email:
        return Response({'error': 'Enter the email on your account.'}, status=status.HTTP_400_BAD_REQUEST)
    for user in User.objects.filter(email__iexact=email, is_active=True):
        if not user.has_usable_password() and user.is_staff:
            continue  # staff without a password are managed by the owner
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        link = f'{settings.FRONTEND_URL}/reset-password?uid={uid}&token={default_token_generator.make_token(user)}'
        brand = notifications.business_name()
        try:
            send_mail(f'{brand}: choose a new password',
                      f'Hi {user.first_name or user.username},\n\n'
                      f'Someone (hopefully you) asked to reset the password for your {brand} account '
                      f'"{user.username}". Choose a new one here:\n\n{link}\n\n'
                      'If it wasn\'t you, ignore this email: your password stays the same.\n',
                      settings.DEFAULT_FROM_EMAIL, [user.email])
        except Exception:  # noqa: BLE001 — never reveal mail problems (or who exists) to the caller
            logger.exception('Password reset email to user #%s failed', user.pk)
    return Response({'message': RESET_SENT})


password_reset_request.cls.throttle_scope = 'password_reset'


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def password_reset_confirm(request):
    """POST /api/password-reset/confirm/  {uid, token, new_password} → sets the password once."""
    from django.contrib.auth.password_validation import validate_password
    from django.contrib.auth.tokens import default_token_generator
    from django.core.exceptions import ValidationError
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode

    invalid = Response({'error': 'This reset link is invalid or has already been used. Ask for a new one.'},
                       status=status.HTTP_400_BAD_REQUEST)
    try:
        user = User.objects.get(pk=force_str(urlsafe_base64_decode(str(request.data.get('uid') or ''))), is_active=True)
    except (User.DoesNotExist, ValueError, TypeError, OverflowError):
        return invalid
    if not default_token_generator.check_token(user, str(request.data.get('token') or '')):
        return invalid
    new_password = str(request.data.get('new_password') or '')
    try:
        validate_password(new_password, user)
    except ValidationError as e:
        return Response({'error': ' '.join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(new_password)   # also makes the link stop working (the token covers the hash)
    user.save(update_fields=['password'])
    return Response({'message': 'Your password was changed. You can sign in now.', 'username': user.username})


password_reset_confirm.cls.throttle_scope = 'password_reset_confirm'


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = ManageUserSerializer
    
    def get_permissions(self):
        from rest_framework.permissions import BasePermission
        
        class IsSuperUser(BasePermission):
            def has_permission(self, request, view):
                return bool(request.user and request.user.is_superuser)
        
        self.permission_classes = [IsSuperUser]
        return super().get_permissions()

    def perform_create(self, serializer):
        user = serializer.save()
        activity.record(self.request.user, 'people', 'user_added',
                        f'Added {activity.name_of(user)} as {activity.role_of(user)}', link='/manage-users', user=user.id)

    def perform_update(self, serializer):
        before = User.objects.select_related('staff_profile').get(pk=serializer.instance.pk)
        old_role, was_active = activity.role_of(before), before.is_active
        user = User.objects.select_related('staff_profile').get(pk=serializer.save().pk)
        who = activity.name_of(user)
        if activity.role_of(user) != old_role:
            activity.record(self.request.user, 'people', 'role_changed',
                            f'Changed {who} from {old_role} to {activity.role_of(user)}', link='/manage-users', user=user.id)
        if user.is_active != was_active:
            activity.record(self.request.user, 'people', 'account_enabled' if user.is_active else 'account_disabled',
                            f'{"Re-enabled" if user.is_active else "Disabled"} the account of {who}',
                            link='/manage-users', user=user.id)
        if self.request.data.get('password'):
            activity.record(self.request.user, 'people', 'password_reset', f'Set a new password for {who}',
                            link='/manage-users', user=user.id)

    def perform_destroy(self, instance):
        summary = f'Deleted the account of {activity.name_of(instance)} ({activity.role_of(instance)})'
        instance.delete()
        activity.record(self.request.user, 'people', 'user_deleted', summary, link='/manage-users')

    @action(detail=True, methods=['get'])
    def activity_log(self, request, pk=None):
        """
        GET /api/users/:id/activity_log/
        Returns chronological log of recent customer/staff actions.
        """
        user = self.get_object()
        
        role = 'Customer'
        if user.is_superuser:
            role = 'Admin'
        elif user.is_staff:
            role = 'Staff'
            
        logs = []
        
        if role == 'Staff':
            attendances = Attendance.objects.filter(staff=user).order_by('-clock_in_time')[:10]
            for att in attendances:
                if att.clock_in_time:
                    logs.append({
                        'type': 'clock_in',
                        'timestamp': att.clock_in_time.isoformat(),
                        'details': f"Clocked in at {att.clock_in_address or 'Unknown location'}",
                        'booking_id': att.booking_id,
                        'booking_type': att.booking.service_type if att.booking else None
                    })
                if att.clock_out_time:
                    logs.append({
                        'type': 'clock_out',
                        'timestamp': att.clock_out_time.isoformat(),
                        'details': f"Clocked out at {att.clock_out_address or 'Unknown location'} (Total Hours: {att.total_hours})",
                        'booking_id': att.booking_id,
                        'booking_type': att.booking.service_type if att.booking else None
                    })
        else:
            # Customer bookings
            bookings = ServiceBooking.objects.filter(user=user).order_by('-created_at')[:10]
            for b in bookings:
                logs.append({
                    'type': 'booking',
                    'timestamp': b.created_at.isoformat() if b.created_at else None,
                    'details': f"Service Booking #{b.id} ({b.service_type}) - Status: {b.status} (Scheduled: {b.scheduled_date})",
                    'id': b.id
                })
            # Customer orders
            orders = Order.objects.filter(user=user).order_by('-created_at')[:10]
            for o in orders:
                logs.append({
                    'type': 'order',
                    'timestamp': o.created_at.isoformat() if o.created_at else None,
                    'details': f"Order #{o.id} - Total: ₱{o.total_price:,.2f} - Status: {o.status} - Payment: {o.payment_status}",
                    'id': o.id
                })
                
        # Sort logs by timestamp descending
        logs.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        
        return Response({
            'user_id': user.id,
            'username': user.username,
            'role': role,
            'activity_logs': logs
        })



class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    The signed-in user's notifications (newest first), 20 per page:
      GET  /notifications/?page=N        → {results, has_more, unread}
      GET  /notifications/unread_count/  → {unread, latest}  (cheap; the bell polls it)
      PATCH /notifications/<id>/mark_read/,  POST /notifications/mark_all_read/
    """
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    PAGE_SIZE = 20

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        start = (page - 1) * self.PAGE_SIZE
        rows = list(self.get_queryset()[start:start + self.PAGE_SIZE + 1])
        return Response({
            'results': self.get_serializer(rows[:self.PAGE_SIZE], many=True).data,
            'has_more': len(rows) > self.PAGE_SIZE,
            'unread': self.get_queryset().filter(is_read=False).count(),
        })

    @action(detail=False, methods=['get'])
    def unread_count(self, request):
        latest = self.get_queryset().values_list('timestamp', flat=True).first()
        return Response({'unread': self.get_queryset().filter(is_read=False).count(), 'latest': latest})

    @action(detail=True, methods=['patch'])
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.save(update_fields=['is_read'])
        return Response({'status': 'read'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'status': 'all read'})


class AttendanceViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        from rest_framework.permissions import BasePermission
        class IsStaffOrAdmin(BasePermission):
            def has_permission(self, request, view):
                return bool(request.user and request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser))
        self.permission_classes = [IsStaffOrAdmin]
        return super().get_permissions()

    def get_queryset(self):
        if self.request.user.is_superuser:
            return Attendance.objects.all().order_by('-clock_in_time')
        return Attendance.objects.filter(staff=self.request.user).order_by('-clock_in_time')

    @action(detail=False, methods=['get'])
    def current(self, request):
        """GET /api/attendance/current/ - Get the active clock-in session of the staff member"""
        active_session = Attendance.objects.filter(staff=request.user, clock_out_time__isnull=True).first()
        if active_session:
            return Response({
                'clocked_in': True,
                'attendance': AttendanceSerializer(active_session, context={'request': request}).data
            })
        return Response({
            'clocked_in': False,
            'attendance': None
        })

    @action(detail=False, methods=['post'])
    def clock_in(self, request):
        """POST /api/attendance/clock_in/"""
        from django.utils import timezone
        import base64
        from django.core.files.base import ContentFile

        user = request.user
        if Attendance.objects.filter(staff=user, clock_out_time__isnull=True).exists():
            return Response({'error': 'You are already clocked in. Please clock out first.'}, status=status.HTTP_400_BAD_REQUEST)

        booking_id = request.data.get('booking_id')
        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        address = request.data.get('address') or request.data.get('clock_in_address')
        photo_data = request.data.get('photo') or request.data.get('clock_in_photo_url')

        try:
            latitude = float(latitude) if latitude is not None else None
            longitude = float(longitude) if longitude is not None else None
        except (ValueError, TypeError):
            latitude = None
            longitude = None

        # Clocking in to a job: it must be open, assigned to this crew member,
        # and — when the site has a GPS pin — the crew member must be on site.
        if booking_id:
            from .models import ServiceBooking
            try:
                booking = ServiceBooking.objects.get(id=booking_id)
            except (ServiceBooking.DoesNotExist, ValueError, TypeError):
                return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)
            role = getattr(getattr(user, 'staff_profile', None), 'role', None)
            if role == 'FIELD_CREW' and not booking.assigned_crew.filter(pk=user.pk).exists():
                return Response({'error': f'You are not assigned to Booking #{booking.id}.'},
                                status=status.HTTP_403_FORBIDDEN)
            # Confirmed and due today (or already under way): see field_work.start_job_error
            not_now = field_work.start_job_error(booking)
            if not_now:
                return Response({'error': not_now}, status=status.HTTP_400_BAD_REQUEST)
            if booking.site_lat is not None and booking.site_lng is not None:
                if latitude is None or longitude is None:
                    return Response({'error': 'Location is required to clock in to this job. Enable GPS and try again.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                dist_m = _haversine(latitude, longitude, booking.site_lat, booking.site_lng)
                if dist_m > SITE_RADIUS_M:
                    return Response(
                        {'error': f'You are {dist_m:.0f} m from the job site. You must be within {SITE_RADIUS_M} m to clock in.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        try:
            with transaction.atomic():
                attendance = Attendance(
                    staff=user,
                    booking_id=booking_id,
                    clock_in_time=offline.action_time(request),   # when they tapped it, even if sent later
                    latitude=latitude,
                    longitude=longitude,
                    clock_in_address=address
                )

                if photo_data:
                    if isinstance(photo_data, str) and photo_data.startswith('data:image'):
                        try:
                            format, imgstr = photo_data.split(';base64,')
                            ext = format.split('/')[-1].split(';')[0]
                            filename = f"clock_in_{user.id}_{int(timezone.now().timestamp())}.{ext}"
                            attendance.clock_in_photo_url.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                        except Exception as e:
                            return Response({'error': f'Failed to decode image: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
                    elif hasattr(photo_data, 'read'):
                        attendance.clock_in_photo_url = photo_data

                attendance.save()

                # Auto-update status to Installing if not already there, since active work is happening
                if booking_id:
                    try:
                        from .models import ServiceBooking
                        booking = ServiceBooking.objects.get(id=booking_id)
                        if booking.status in ['Pending', 'Preparing']:
                            booking.status = 'Installing'
                            booking.save()
                    except Exception:
                        pass
        except Exception as e:
            return Response({'error': f'Transaction failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(AttendanceSerializer(attendance, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def clock_out(self, request):
        """POST /api/attendance/clock_out/"""
        from django.utils import timezone
        import base64
        from django.core.files.base import ContentFile

        user = request.user
        active_session = Attendance.objects.filter(staff=user, clock_out_time__isnull=True).first()
        if not active_session:
            return Response({'error': 'No active clock-in session found. Please clock in first.'}, status=status.HTTP_400_BAD_REQUEST)

        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        address = request.data.get('address') or request.data.get('clock_out_address')
        photo_data = request.data.get('photo') or request.data.get('clock_out_photo_url')

        try:
            with transaction.atomic():
                try:
                    if latitude is not None:
                        active_session.latitude = float(latitude)
                    if longitude is not None:
                        active_session.longitude = float(longitude)
                except (ValueError, TypeError):
                    pass

                # When they tapped it (sent later if the phone was offline), never before clocking in
                active_session.clock_out_time = max(offline.action_time(request), active_session.clock_in_time)
                active_session.clock_out_address = address

                if photo_data:
                    if isinstance(photo_data, str) and photo_data.startswith('data:image'):
                        try:
                            format, imgstr = photo_data.split(';base64,')
                            ext = format.split('/')[-1].split(';')[0]
                            filename = f"clock_out_{user.id}_{int(timezone.now().timestamp())}.{ext}"
                            active_session.clock_out_photo_url.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                        except Exception as e:
                            return Response({'error': f'Failed to decode image: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
                    elif hasattr(photo_data, 'read'):
                        active_session.clock_out_photo_url = photo_data

                active_session.save()

                # Booking status transitions are managed explicitly via the Staff Dashboard.
                # Clock-out saves the attendance record only — no automatic status side-effects.

        except Exception as e:
            return Response({'error': f'Transaction failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(AttendanceSerializer(active_session, context={'request': request}).data, status=status.HTTP_200_OK)


# ───────────────────────────────────────────────────────────────
# AI Garden Designer — moved to garden_ai.py (hybrid Gemini + smart layout engine).
# Re-exported here for existing imports.
# ───────────────────────────────────────────────────────────────
from .garden_ai import (  # noqa: E402,F401
    AILayoutThrottle, GardenDesignSchema, GenerateLayoutsView, PlantArrangement, PlantCostBreakdown,
)


class ServiceReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceReviewSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        if self.action == 'public':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ServiceReview.objects.none()
        if user.is_staff or user.is_superuser:
            return ServiceReview.objects.select_related('booking', 'user').all()
        return ServiceReview.objects.filter(user=user).select_related('booking')

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def public(self, request):
        reviews = (
            ServiceReview.objects
            .filter(is_public=True, is_hidden=False)
            .select_related('booking', 'user')
            .prefetch_related('booking__assigned_crew')
            .order_by('-created_at')[:12]
        )
        serializer = self.get_serializer(reviews, many=True)
        return Response(serializer.data)


class ProductReviewViewSet(viewsets.ModelViewSet):
    """
    GET  /api/product-reviews/                      Own reviews (staff/admin → all)
    POST /api/product-reviews/                      Review a delivered order item
    GET  /api/product-reviews/public/?product=<id>  Public reviews, optionally for one product
    """
    serializer_class = ProductReviewSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        if self.action == 'public':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ProductReview.objects.none()
        qs = ProductReview.objects.select_related('product', 'user', 'order_item')
        if user.is_staff or user.is_superuser:
            return qs.all()
        return qs.filter(user=user)

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def public(self, request):
        reviews = (
            ProductReview.objects
            .filter(is_public=True, is_hidden=False)
            .select_related('product', 'user', 'order_item')
        )
        product_id = request.query_params.get('product')
        if product_id:
            if not product_id.isdigit():
                return Response({'error': 'product must be a numeric id.'}, status=status.HTTP_400_BAD_REQUEST)
            reviews = reviews.filter(product_id=product_id)
        serializer = self.get_serializer(reviews.order_by('-created_at')[:20], many=True)
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker endpoints
# ─────────────────────────────────────────────────────────────────────────────

class ProjectTrackerViewSet(viewsets.ModelViewSet):
    """
    GET    /api/projects/                           List all trackers
                                                    (staff/admin → all;
                                                     customer → own bookings only)
    GET    /api/projects/<id>/                      Full detail with history & media
    POST   /api/projects/                           Create tracker  [admin/staff]
    PATCH  /api/projects/<id>/                      Edit fields     [admin/staff]
    PATCH  /api/projects/<id>/update-status/        Advance pipeline status,
                                                    auto-log & recompute %  [admin/staff]
    POST   /api/projects/<id>/upload-media/         Attach a progress photo  [staff]
    """
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return ProjectTrackerListSerializer
        return ProjectTrackerSerializer

    def get_queryset(self):
        user = self.request.user
        qs = ProjectTracker.objects.select_related(
            'booking', 'booking__user', 'supervisor',
        ).prefetch_related(
            'history_logs',
            'history_logs__trigger_user',
            'progress_media',
            'progress_media__uploader',
        )
        if work_queues.is_field_crew(user):
            # Crew see the projects of their own jobs, not every customer's prices and payments
            return qs.filter(booking__assigned_crew=user).distinct()
        if user.is_staff or user.is_superuser:
            return qs.all()
        # Customers see only the tracker linked to their own bookings
        return qs.filter(booking__user=user)

    def get_permissions(self):
        # Write operations (create / edit / status advance) require admin/staff
        if self.action in ('create', 'update', 'partial_update', 'destroy', 'update_status', 'record_payment',
                           'cancel_payment_request', 'email_payment_request'):
            return [IsAuthenticated(), IsOfficeAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        tracker = serializer.save()
        ProjectHistoryLog.objects.create(
            project=tracker,
            status_reached=tracker.status,
            trigger_user=self.request.user,
            remarks='Project tracker initialized.',
        )

    def perform_update(self, serializer):
        old_price = serializer.instance.total_price
        tracker = serializer.save()
        if tracker.total_price != old_price:
            ProjectHistoryLog.objects.create(
                project=tracker, status_reached=tracker.status, trigger_user=self.request.user,
                remarks=f"Contract price changed: ₱{old_price:,.2f} → ₱{tracker.total_price:,.2f}",
            )
            activity.record(self.request.user, 'money', 'contract_price_changed',
                            f'Changed the contract price of booking #{tracker.booking_id}: '
                            f'₱{old_price:,.2f} → ₱{tracker.total_price:,.2f}',
                            link=notifications.booking_office_path(tracker.booking_id), booking=tracker.booking_id)

    # ── GET /api/projects/supervisor-options/ ────────────────────────────────
    @action(detail=False, methods=['get'], url_path='supervisor-options')
    def supervisor_options(self, request):
        crew = User.objects.filter(
            staff_profile__role='FIELD_CREW',
            is_active=True,
        ).select_related('staff_profile').order_by('first_name', 'username')
        return Response([
            {
                'id': u.id,
                'name': f"{u.first_name} {u.last_name}".strip() or u.username,
            }
            for u in crew
        ])

    # ── PATCH /api/projects/<id>/update-status/ ──────────────────────────────
    @action(detail=True, methods=['patch'], url_path='update-status')
    def update_status(self, request, pk=None):
        tracker = self.get_object()

        new_status = (request.data.get('status') or '').strip()
        remarks = (request.data.get('remarks') or '').strip()

        valid_statuses = dict(ProjectTracker.STATUS_CHOICES)
        if not new_status or new_status not in valid_statuses:
            return Response(
                {
                    'error': 'Invalid or missing status.',
                    'valid_choices': list(valid_statuses.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous_status = tracker.status
        tracker.status = new_status
        tracker.save()  # progress_percentage auto-recomputed inside save()

        # Append an immutable audit log entry for this transition
        ProjectHistoryLog.objects.create(
            project=tracker,
            status_reached=new_status,
            trigger_user=request.user,
            remarks=remarks,
        )

        # Completing the project from the tracker is the same office-admin sign-off
        # as approving the booking, so keep the booking status in step.
        booking = tracker.booking
        if new_status in ('COMPLETED', 'CUSTOMER_FEEDBACK') and booking.status not in ('Finished', 'Cancelled'):
            booking.status = 'Finished'
            booking.save()
            _draft_portfolio(booking, request.user)

        logger.info(
            "ProjectTracker #%s: %s → %s by user #%s",
            tracker.pk, previous_status, new_status, request.user.pk,
        )

        serializer = ProjectTrackerSerializer(
            tracker, context={'request': request}
        )
        return Response(serializer.data)

    # ── POST /api/projects/<id>/record-payment/ ──────────────────────────────
    @action(detail=True, methods=['post'], url_path='record-payment')
    def record_payment(self, request, pk=None):
        """Reduce the remaining balance and keep an audit entry for the payment."""
        from decimal import Decimal, InvalidOperation
        tracker = self.get_object()
        try:
            amount = Decimal(str(request.data.get('amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return Response({'error': 'amount must be a number.'}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({'error': 'amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)
        method = request.data.get('method') or 'cash'
        if method not in dict(ProjectPayment.METHODS) or method == 'online':
            return Response({'error': 'method must be cash, bank, ewallet or other.'}, status=status.HTTP_400_BAD_REQUEST)
        note = (request.data.get('remarks') or '').strip()[:300]
        with transaction.atomic():
            # Locked so two people recording at once can't take the balance below zero
            tracker = ProjectTracker.objects.select_for_update().get(pk=tracker.pk)
            if amount > tracker.remaining_balance:
                return Response(
                    {'error': f'amount exceeds the remaining balance of ₱{tracker.remaining_balance:,.2f}.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tracker.remaining_balance -= amount
            tracker.save()
            ProjectPayment.objects.create(project=tracker, amount=amount, method=method, received_by=request.user,
                                          note=note, paid_at=timezone.now())
            ProjectHistoryLog.objects.create(
                project=tracker, status_reached=tracker.status, trigger_user=request.user,
                remarks=f"Payment received ({dict(ProjectPayment.METHODS)[method].lower()}): ₱{amount:,.2f} "
                        f"(balance ₱{tracker.remaining_balance:,.2f})" + (f" — {note}" if note else ''),
            )
        activity.record(request.user, 'money', 'payment_recorded',
                        f'Recorded a ₱{amount:,.2f} payment on booking #{tracker.booking_id} '
                        f'(balance now ₱{tracker.remaining_balance:,.2f})' + (f' — {note}' if note else ''),
                        link=notifications.booking_office_path(tracker.booking_id),
                        booking=tracker.booking_id, amount=str(amount))
        return Response(ProjectTrackerSerializer(tracker, context={'request': request}).data)

    # ── GET/POST /api/projects/<id>/payment-requests/ ────────────────────────
    @action(detail=True, methods=['get', 'post'], url_path='payment-requests')
    def payment_requests(self, request, pk=None):
        """List a project's payment requests, or (office admin) ask the customer for a payment."""
        from decimal import Decimal, InvalidOperation
        tracker = self.get_object()
        if request.method == 'GET':
            return Response([payment_request_data(r) for r in tracker.payment_requests.all()])

        if not IsOfficeAdmin().has_permission(request, self):
            return Response({'error': 'Office admins only.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            amount = Decimal(str(request.data.get('amount'))).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError, ValueError):
            return Response({'error': 'amount must be a number.'}, status=status.HTTP_400_BAD_REQUEST)
        if amount <= 0:
            return Response({'error': 'amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)
        # Open requests count against the balance, so the customer is never asked twice for the same money
        pending = sum(r.amount for r in tracker.payment_requests.filter(status='PENDING'))
        available = tracker.remaining_balance - pending
        if amount > available:
            return Response({'error': f'amount exceeds the ₱{available:,.2f} not yet paid or requested.'},
                            status=status.HTTP_400_BAD_REQUEST)
        label = (request.data.get('label') or 'Downpayment').strip()[:120]

        req = ProjectPaymentRequest.objects.create(
            project=tracker, label=label, amount=amount,
            token=secrets.token_urlsafe(32), created_by=request.user,
        )
        ProjectHistoryLog.objects.create(
            project=tracker, status_reached=tracker.status, trigger_user=request.user,
            remarks=f"Payment requested: ₱{amount:,.2f} — {label}",
        )
        activity.record(request.user, 'money', 'payment_requested',
                        f'Asked for ₱{amount:,.2f} on booking #{tracker.booking_id} — {label}',
                        link=notifications.booking_office_path(tracker.booking_id), booking=tracker.booking_id)
        notifications.notify([tracker.booking.user], 'payment_request',
                             f"Payment request for your project: {label} ₱{amount:,.2f}. "
                             "Pay online with QR Ph, GCash, Maya or card.", link=f'/pay/{req.token}')
        sent, email_message = send_payment_request_email(req)
        return Response({**payment_request_data(req), 'email_sent': sent, 'email_message': email_message},
                        status=status.HTTP_201_CREATED)

    # ── POST /api/projects/<id>/payment-requests/<req_id>/send-email/ ────────
    @action(detail=True, methods=['post'], url_path=r'payment-requests/(?P<req_id>\d+)/send-email')
    def email_payment_request(self, request, pk=None, req_id=None):
        tracker = self.get_object()
        req = get_object_or_404(tracker.payment_requests, pk=req_id)
        if req.status != 'PENDING':
            return Response({'error': f'This request is already {req.get_status_display().lower()}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        sent, email_message = send_payment_request_email(req)
        if not sent:
            return Response({'error': email_message}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({**payment_request_data(req), 'email_sent': True, 'email_message': email_message})

    # ── POST /api/projects/<id>/payment-requests/<req_id>/cancel/ ────────────
    @action(detail=True, methods=['post'], url_path=r'payment-requests/(?P<req_id>\d+)/cancel')
    def cancel_payment_request(self, request, pk=None, req_id=None):
        tracker = self.get_object()
        req = get_object_or_404(tracker.payment_requests, pk=req_id)
        req = paymongo.sync_project_request_payment(req)  # don't cancel something already paid
        if req.status != 'PENDING':
            return Response({'error': f'This request is already {req.get_status_display().lower()}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        req.status = 'CANCELLED'
        req.save(update_fields=['status'])
        if req.paymongo_checkout_id:
            paymongo.expire_checkout_session(req.paymongo_checkout_id)
        ProjectHistoryLog.objects.create(
            project=tracker, status_reached=tracker.status, trigger_user=request.user,
            remarks=f"Payment request cancelled: ₱{req.amount:,.2f} — {req.label}",
        )
        activity.record(request.user, 'money', 'payment_request_cancelled',
                        f'Cancelled the ₱{req.amount:,.2f} payment request on booking #{tracker.booking_id} — {req.label}',
                        link=notifications.booking_office_path(tracker.booking_id), booking=tracker.booking_id)
        return Response(payment_request_data(req))

    # ── POST /api/projects/<id>/upload-media/ ────────────────────────────────
    @action(detail=True, methods=['post'], url_path='upload-media')
    def upload_media(self, request, pk=None):
        # Field crew and admin can both upload photos; customers cannot
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {'error': 'Staff access required to upload progress media.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        tracker = self.get_object()
        role = getattr(getattr(request.user, 'staff_profile', None), 'role', None)
        if role == 'FIELD_CREW' and not tracker.booking.assigned_crew.filter(pk=request.user.pk).exists():
            return Response({'error': 'You are not assigned to this project.'}, status=status.HTTP_403_FORBIDDEN)

        construction_phase = (
            request.data.get('construction_phase') or ''
        ).strip().upper()
        description = (request.data.get('description') or '').strip()
        location = (request.data.get('location') or '').strip()
        file_obj = request.FILES.get('file')

        if construction_phase not in ('BEFORE', 'DURING', 'AFTER'):
            return Response(
                {'error': "construction_phase must be 'BEFORE', 'DURING', or 'AFTER'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not file_obj:
            return Response(
                {'error': 'A file upload is required (multipart field name: file).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        clean, error = sanitize_image(file_obj)  # strips EXIF/GPS metadata
        if error:
            return Response({'error': error}, status=status.HTTP_400_BAD_REQUEST)

        media = ProjectProgressMedia(
            project=tracker,
            construction_phase=construction_phase,
            uploader=request.user,
            description=description,
            location=location,
        )
        media.file_path.save(f'project_{tracker.id}_{construction_phase.lower()}.jpg', clean, save=False)
        media.save()

        serializer = ProjectProgressMediaSerializer(
            media, context={'request': request}
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)
