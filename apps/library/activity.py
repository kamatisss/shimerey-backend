"""
Activity log — who changed what, for the decisions an owner wants to check later.

Recorded (area / action):
  money     cash_received, order_refunded, order_cancelled, payment_recorded, payment_requested,
            payment_request_cancelled, contract_price_changed
  stock     price_changed, item_added, item_archived, item_restored, stock_adjusted
  people    user_added, role_changed, account_disabled, account_enabled, user_deleted, password_changed, email_changed,
            account_closed, signed_out_everywhere
  work      booking_cancelled, job_signed_off
  settings  business_profile_changed, pricing_changed
Only the owner reads it (GET /api/admin/activity/). Rows are never edited or deleted from the app.
"""
import logging

from .models import ActivityLog

log = logging.getLogger(__name__)


def record(actor, area, action, summary, link='', **details):
    """Write one entry. Never lets a logging problem break the change it describes."""
    try:
        ActivityLog.objects.create(actor=actor if getattr(actor, 'pk', None) else None, area=area, action=action,
                                   summary=summary[:300], link=link[:200], details=details)
    except Exception:  # noqa: BLE001
        log.exception('Could not write activity log entry: %s', summary)


def role_of(user):
    """The role names the User accounts page uses."""
    if user.is_superuser:
        return 'Owner (super admin)'
    if user.is_staff:
        role = getattr(getattr(user, 'staff_profile', None), 'role', None)
        return 'Field crew' if role == 'FIELD_CREW' else 'Office admin'
    return 'Customer'


def name_of(user):
    return (user.get_full_name() or user.username) if user else 'someone'
