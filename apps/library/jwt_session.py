"""
Session-checked JWT (see signout.py for the why): every login token carries the account's session version
(claim "sv"); a request or refresh with an older version is refused. Kept apart from signout.py on purpose:
DRF imports this class from settings while loading its views, so it must not import views itself.
"""
from django.db.models import F
from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

CLAIM = 'sv'
ENDED = 'You were signed out of this device. Please sign in again.'


def current_version(user_id):
    from .models import AccountProfile
    return AccountProfile.objects.filter(user_id=user_id).values_list('session_version', flat=True).first() or 0


def _token_version(token):
    try:
        return int(token.get(CLAIM, 0) or 0)
    except (TypeError, ValueError):
        return -1


def end_all_sessions(user):
    """Every token issued so far stops working. Returns the new version."""
    from .models import AccountProfile
    profile, _ = AccountProfile.objects.get_or_create(user=user)
    AccountProfile.objects.filter(pk=profile.pk).update(session_version=F('session_version') + 1,
                                                        signed_out_everywhere_at=timezone.now())
    return current_version(user.id)


class SessionJWTAuthentication(JWTAuthentication):
    """The usual JWT check, plus: the token's session version is still the account's."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if _token_version(validated_token) != current_version(user.id):
            raise AuthenticationFailed(ENDED, code='session_ended')
        return user


class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        refresh = RefreshToken(attrs['refresh'])   # expired / malformed → TokenError → 401
        user_id = refresh.get('user_id')
        if user_id is None or _token_version(refresh) != current_version(user_id):
            raise InvalidToken(ENDED)
        return super().validate(attrs)
