"""
Production settings (the web server's WSGI file uses these: config/wsgi.py).

Everything comes from environment variables / BACKEND/.env — see .env.example and DEPLOY.md.
Required:  DJANGO_SECRET_KEY, ALLOWED_HOSTS, FRONTEND_URL
"""
import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403


def _list(name, default=''):
    return [v.strip() for v in os.environ.get(name, default).split(',') if v.strip()]


DEBUG = os.environ.get('DJANGO_DEBUG', '').lower() in ('1', 'true', 'yes')

if not os.environ.get('DJANGO_SECRET_KEY') or SECRET_KEY.startswith('django-insecure-'):
    raise ImproperlyConfigured('Set a long random DJANGO_SECRET_KEY (see DEPLOY.md).')

# e.g. ALLOWED_HOSTS=yourname.pythonanywhere.com
ALLOWED_HOSTS = _list('ALLOWED_HOSTS')
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured('Set ALLOWED_HOSTS to the backend\'s domain, e.g. yourname.pythonanywhere.com')

# Only the real frontend may call the API from a browser (FRONTEND_URL, plus any extra origins)
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = _list('CORS_ALLOWED_ORIGINS', FRONTEND_URL)
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS + [f'https://{h}' for h in ALLOWED_HOSTS]

# HTTPS: the host terminates TLS in front of Django
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# Prefer the host's own "Force HTTPS" switch; set SECURE_SSL_REDIRECT=true only where the proxy sends X-Forwarded-Proto
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'false').lower() in ('1', 'true', 'yes')
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '3600'))  # raise to 31536000 once all is well
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'

# Rate limits (login, sign-up, reset) count the visitor's IP as seen by the host's proxy
REST_FRAMEWORK['NUM_PROXIES'] = int(os.environ.get('NUM_PROXIES', '1'))

# Static files (Django admin) are collected here: `manage.py collectstatic`.
# Media (uploads) are served by the host from MEDIA_ROOT at /media/ — see DEPLOY.md.
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# SQLite by default (fine for one small server). DATABASE_PATH moves the file if needed.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DATABASE_PATH') or (BASE_DIR / 'db.sqlite3'),
    }
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
}
