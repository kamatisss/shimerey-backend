from django.urls import path, include
from rest_framework import routers

from .views import generate_depth
from .analytics import income_analytics
from .portfolio import BusinessCredentialViewSet, PortfolioViewSet
from .funnel import funnel_summary, record_funnel_event
from .garden_ai import ai_designer_health, scan_zones
from . import push
from .overview import business_overview
from .reports import balances, crew_output, money_in
from .views_office import activity_list, business_profile, cash_overview, cash_receive
from .pricing import budget_suggestion, pricing_estimate, pricing_settings, quick_estimate
from .reviews import moderate_review, review_highlights, review_moderation_list
from .views_api import (
    InventoryItemViewSet, GardenDesignViewSet, RegisterView, BlackoutDateViewSet,
    ServiceBookingViewSet, checkout, OrderViewSet, create_checkout_session,
    paymongo_webhook, order_receipt, project_payment_detail, project_payment_checkout,
    password_reset_request, password_reset_confirm, UserViewSet, AttendanceViewSet,
    NotificationViewSet, GenerateLayoutsView, upload_design_image, ServiceReviewViewSet,
    ProjectTrackerViewSet, ProductReviewViewSet,
)

router = routers.DefaultRouter()
router.register(r'inventory', InventoryItemViewSet)
router.register(r'designs', GardenDesignViewSet)
router.register(r'blackout-dates', BlackoutDateViewSet)
router.register(r'bookings', ServiceBookingViewSet)
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'users', UserViewSet, basename='user')
router.register(r'attendance', AttendanceViewSet, basename='attendance')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'reviews', ServiceReviewViewSet, basename='review')
router.register(r'product-reviews', ProductReviewViewSet, basename='product-review')
router.register(r'projects', ProjectTrackerViewSet, basename='project')
router.register(r'portfolio', PortfolioViewSet, basename='portfolio')
router.register(r'business-credentials', BusinessCredentialViewSet, basename='business-credential')

urlpatterns = [
    path('', include(router.urls)),
    path('generate-depth/', generate_depth, name='generate-depth'),
    path('generate-layouts/', GenerateLayoutsView.as_view(), name='generate-layouts'),
    path('scan-zones/', scan_zones, name='scan-zones'),
    path('pricing/', pricing_settings, name='pricing'),
    path('pricing/estimate/', pricing_estimate, name='pricing-estimate'),
    path('pricing/suggest/', budget_suggestion, name='pricing-suggest'),
    path('pricing/quick/', quick_estimate, name='pricing-quick'),
    path('admin/overview/', business_overview, name='business-overview'),
    path('admin/activity/', activity_list, name='activity-log'),
    path('reports/money/', money_in, name='report-money'),
    path('reports/balances/', balances, name='report-balances'),
    path('reports/crew/', crew_output, name='report-crew'),
    path('cash/', cash_overview, name='cash-overview'),
    path('cash/receive/', cash_receive, name='cash-receive'),
    path('business/', business_profile, name='business-profile'),
    path('push/config/', push.push_config, name='push-config'),
    path('push/subscribe/', push.push_subscribe, name='push-subscribe'),
    path('push/unsubscribe/', push.push_unsubscribe, name='push-unsubscribe'),
    path('push/test/', push.push_test, name='push-test'),
    path('ai-designer/health/', ai_designer_health, name='ai-designer-health'),
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('checkout/', checkout, name='checkout'),
    path('create-checkout-session/', create_checkout_session, name='create_checkout_session'),
    path('paymongo-webhook/', paymongo_webhook, name='paymongo_webhook'),
    path('order-receipt/<int:pk>/', order_receipt, name='order_receipt'),
    path('project-payments/<str:token>/', project_payment_detail, name='project_payment_detail'),
    path('project-payments/<str:token>/checkout/', project_payment_checkout, name='project_payment_checkout'),
    path('password-reset/', password_reset_request, name='password-reset'),
    path('password-reset/confirm/', password_reset_confirm, name='password-reset-confirm'),
    path('upload-design-image/', upload_design_image, name='upload_design_image'),
    path('analytics/income/', income_analytics, name='income-analytics'),
    path('funnel-events/', record_funnel_event, name='funnel-event'),
    path('funnel-events/summary/', funnel_summary, name='funnel-summary'),
    path('review-highlights/', review_highlights, name='review-highlights'),
    path('review-moderation/', review_moderation_list, name='review-moderation'),
    path('review-moderation/<str:source>/<int:pk>/', moderate_review, name='review-moderate'),
]
