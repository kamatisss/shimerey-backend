"""
Customer emails. Sending never raises: a mail server problem must not undo the
action that triggered it, so callers get (sent, reason) and can tell staff.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from .models import BusinessProfile

logger = logging.getLogger(__name__)


def send_payment_request_email(req):
    """Email the customer the /pay/<token> link for a ProjectPaymentRequest. Returns (sent, reason)."""
    booking = req.project.booking
    customer = booking.user
    if not customer.email:
        return False, 'The customer has no email address on their account.'

    context = {
        'first_name': customer.first_name or customer.username,
        'label': req.label,
        'amount': f'₱{req.amount:,.2f}',
        'service': booking.get_service_type_display(),
        'booking_id': booking.id,
        'project_total': f'₱{req.project.total_price:,.2f}',
        'project_balance': f'₱{req.project.remaining_balance:,.2f}',
        'pay_url': f'{settings.FRONTEND_URL}/pay/{req.token}',
        'business': BusinessProfile.current(),
    }
    message = EmailMultiAlternatives(
        subject=f'Payment request: {req.label} — ₱{req.amount:,.2f}',
        body=render_to_string('emails/payment_request.txt', context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[customer.email],
    )
    message.attach_alternative(render_to_string('emails/payment_request.html', context), 'text/html')
    try:
        message.send()
    except Exception as e:  # SMTP auth, network, bad address...
        logger.warning('Payment request #%s email to %s failed: %s', req.id, customer.email, e)
        return False, 'The email could not be sent. Copy the link and send it another way.'
    return True, f'Emailed to {customer.email}.'
