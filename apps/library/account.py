"""
My account: every signed-in person reads and corrects their own details (RA 10173 right to correction).

  GET   /api/me/            → {id, username, email, first_name, last_name, phone, role, date_joined}
  PATCH /api/me/            → change first_name, last_name, phone (PH mobile, stored as 09XXXXXXXXX)
  POST  /api/me/password/   → {current_password, new_password}
  POST  /api/me/email/      → {email, current_password}

Changes return fresh login tokens ({tokens: {access, refresh}}): the app reads the name and email from the
token, so without new ones the header would keep the old name until the next sign-in.
Email and password need the current password (a borrowed, unlocked phone can't take over the account),
and an email may belong to one account only ("Forgot password" finds accounts by email).
The username is the sign-in name and stays as it is.
"""
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from . import activity
from .models import AccountProfile

NAME_MAX = 150   # Django's User.first_name / last_name


class AccountSecurityThrottle(ScopedRateThrottle):
    """Password and email changes: slows down guessing the current password from a signed-in session."""
    scope = 'account_security'


def _role(user):
    if user.is_superuser:
        return 'owner'
    role = getattr(getattr(user, 'staff_profile', None), 'role', None)
    if role == 'FIELD_CREW':
        return 'crew'
    if user.is_staff:
        return 'office'
    return 'customer'


def profile_of(user):
    return AccountProfile.objects.get_or_create(user=user)[0]


def me_data(user):
    return {
        'id': user.id, 'username': user.username, 'email': user.email,
        'first_name': user.first_name, 'last_name': user.last_name,
        'phone': profile_of(user).phone, 'role': _role(user), 'date_joined': user.date_joined,
        'signed_out_everywhere_at': profile_of(user).signed_out_everywhere_at,
    }


def email_taken(email, user=None):
    """Another account already uses this email (case doesn't matter)."""
    qs = User.objects.filter(email__iexact=email.strip())
    if user is not None:
        qs = qs.exclude(pk=user.pk)
    return qs.exists()


def _tokens(user):
    from .serializers import CustomTokenObtainPairSerializer   # here: serializers → views_api would loop
    token = CustomTokenObtainPairSerializer.get_token(user)
    return {'access': str(token.access_token), 'refresh': str(token)}


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def me(request):
    user = request.user
    if request.method == 'GET':
        return Response(me_data(user))

    from .views_api import normalize_ph_mobile
    data, errors, changes = request.data, {}, {}
    for field in ('first_name', 'last_name'):
        if field in data:
            value = ' '.join(str(data.get(field) or '').split())
            if len(value) > NAME_MAX:
                errors[field] = f'Keep it under {NAME_MAX} characters.'
            else:
                changes[field] = value
    if 'first_name' in changes and not changes['first_name']:
        errors['first_name'] = 'Enter your first name.'
    phone = None
    if 'phone' in data:
        raw = str(data.get('phone') or '').strip()
        phone = '' if not raw else normalize_ph_mobile(raw)
        if phone is None:
            errors['phone'] = 'Enter a PH mobile number, e.g. 09171234567.'
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)

    if changes:
        for field, value in changes.items():
            setattr(user, field, value)
        user.save(update_fields=list(changes))
    if phone is not None:
        profile = profile_of(user)
        profile.phone = phone
        profile.save(update_fields=['phone', 'updated_at'])
    return Response({**me_data(user), 'tokens': _tokens(user)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AccountSecurityThrottle])
def change_password(request):
    user = request.user
    current = request.data.get('current_password') or ''
    new = request.data.get('new_password') or ''
    if not user.check_password(current):
        return Response({'current_password': 'That isn’t your current password.'}, status=status.HTTP_400_BAD_REQUEST)
    if new == current:
        return Response({'new_password': 'Choose a password different from the current one.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(new, user)
    except DjangoValidationError as e:
        return Response({'new_password': ' '.join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(new)
    user.save(update_fields=['password'])
    # Anyone who knew the old password is signed out everywhere; this device gets fresh tokens below
    from .jwt_session import end_all_sessions
    end_all_sessions(user)
    activity.record(user, 'people', 'password_changed', f'{user.username} changed their password (other devices signed out)')
    return Response({'tokens': _tokens(user)})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AccountSecurityThrottle])
def change_email(request):
    user = request.user
    email = str(request.data.get('email') or '').strip()
    if not user.check_password(request.data.get('current_password') or ''):
        return Response({'current_password': 'That isn’t your current password.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_email(email)
    except DjangoValidationError:
        return Response({'email': 'Enter a valid email address.'}, status=status.HTTP_400_BAD_REQUEST)
    if email.lower() == (user.email or '').lower():
        return Response({'email': 'That is already your email address.'}, status=status.HTTP_400_BAD_REQUEST)
    if email_taken(email, user):
        return Response({'email': 'Another account already uses this email.'}, status=status.HTTP_400_BAD_REQUEST)
    old = user.email
    user.email = email
    user.save(update_fields=['email'])
    activity.record(user, 'people', 'email_changed', f'{user.username} changed their email', old=old, new=email)
    return Response({**me_data(user), 'tokens': _tokens(user)})
