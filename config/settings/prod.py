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
# Trailing slashes are dropped (clean_origin, base.py): 'https://site.app/' would stop the server (corsheaders.E014)
CORS_ALLOWED_ORIGINS = [clean_origin(o) for o in _list('CORS_ALLOWED_ORIGINS', FRONTEND_URL)]
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

# Static files (Django admin) are collected here by `manage.py collectstatic` and served by WhiteNoise.
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MIDDLEWARE.insert(MIDDLEWARE.index('django.middleware.security.SecurityMiddleware') + 1,
                  'whitenoise.middleware.WhiteNoiseMiddleware')

# Database: DATABASE_URL (Postgres on Neon, see DEPLOY.md). Without it, the SQLite file next to the code.
if os.environ.get('DATABASE_URL'):
    import dj_database_url
    DATABASES = {'default': dj_database_url.config(conn_max_age=60, conn_health_checks=True, ssl_require=True)}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.environ.get('DATABASE_PATH') or (BASE_DIR / 'db.sqlite3'),
        }
    }

# Uploads (photos, documents): Cloudflare R2's media bucket when R2_MEDIA_BUCKET is set, else this
# server's disk (MEDIA_ROOT). Render's disk is wiped on every deploy, so set REQUIRE_CLOUD_STORAGE=true
# there: the server then refuses to start without R2 instead of quietly losing uploads.
STATIC_STORAGE = {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'}
if R2_MEDIA_BUCKET:
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_MEDIA_PUBLIC_URL):
        raise ImproperlyConfigured('R2_MEDIA_BUCKET needs R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY '
                                   'and R2_MEDIA_PUBLIC_URL too (see DEPLOY.md).')
    from urllib.parse import urlparse
    STORAGES = {
        'default': {
            'BACKEND': 'storages.backends.s3.S3Storage',
            'OPTIONS': {
                'bucket_name': R2_MEDIA_BUCKET,
                'endpoint_url': f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
                'access_key': R2_ACCESS_KEY_ID,
                'secret_key': R2_SECRET_ACCESS_KEY,
                'region_name': 'auto',
                'signature_version': 's3v4',
                # Public bucket: plain links on its public address, no signed query strings
                'custom_domain': urlparse(R2_MEDIA_PUBLIC_URL).netloc,
                'url_protocol': 'https:',
                'querystring_auth': False,
                'default_acl': None,
                'file_overwrite': False,   # same name → a new name, never replace someone's photo
                'object_parameters': {'CacheControl': 'public, max-age=31536000'},
            },
        },
        'staticfiles': STATIC_STORAGE,
    }
    MEDIA_ON_R2 = True
else:
    if os.environ.get('REQUIRE_CLOUD_STORAGE', '').lower() in ('1', 'true', 'yes'):
        raise ImproperlyConfigured('REQUIRE_CLOUD_STORAGE is on but R2_MEDIA_BUCKET is not set: uploads '
                                   'would be lost on the next deploy (see DEPLOY.md → Cloudflare R2).')
    STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': STATIC_STORAGE,
    }
    MEDIA_ON_R2 = False
if os.environ.get('REQUIRE_CLOUD_STORAGE', '').lower() in ('1', 'true', 'yes') and not R2_ENABLED:
    raise ImproperlyConfigured('REQUIRE_CLOUD_STORAGE is on but the R2_* model settings are incomplete: '
                               '3D models would be saved on a disk that is wiped on every deploy.')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'WARNING'},
}
