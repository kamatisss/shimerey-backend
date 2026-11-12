"""
Sign out everywhere: end every login on every device at once.

Login tokens are JWTs (no server session), and access tokens live a whole day, so blacklisting refresh
tokens alone would leave a stolen access token working until it expires. Instead every token carries the
account's session version (claim "sv", AccountProfile.session_version):
  • each API request checks it (SessionJWTAuthentication) and so does each token refresh;
  • sign_out_everywhere() raises the number: every token issued before stops working at once.
The device that asked gets fresh tokens and stays signed in. A password change does the same (someone who
knew the old password is signed out), and closing an account ends its sessions too.
Tokens from before this existed have no "sv": they count as version 0, the default.
The checks live in jwt_session.py (loaded by DRF settings); this module has the endpoint and refresh view.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenRefreshView

from .jwt_session import CLAIM, current_version, end_all_sessions, SessionTokenRefreshSerializer  # noqa: F401 (re-exported)
from .models import AccountProfile


class SessionTokenRefreshView(TokenRefreshView):
    serializer_class = SessionTokenRefreshSerializer


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def sign_out_everywhere(request):
    """POST /api/me/sign-out-everywhere/ → every other device signed out; this one gets fresh tokens."""
    from . import activity
    from .account import _tokens
    end_all_sessions(request.user)
    activity.record(request.user, 'people', 'signed_out_everywhere', f'{request.user.username} signed out on every device')
    profile = AccountProfile.objects.get(user=request.user)
    return Response({'tokens': _tokens(request.user), 'signed_out_everywhere_at': profile.signed_out_everywhere_at},
                    status=status.HTTP_200_OK)
