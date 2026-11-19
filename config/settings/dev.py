import os

from .base import *


DEBUG = True

ALLOWED_HOSTS = []

# SQLite by default (DATABASE_PATH picks another file, e.g. one downloaded from the old server).
# Set DATABASE_URL (e.g. postgres://postgres@127.0.0.1:54329/garden) to develop or run the tests against
# Postgres, the database production uses.
if os.environ.get('DATABASE_URL'):
    import dj_database_url
    DATABASES = {'default': dj_database_url.config()}
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.environ.get('DATABASE_PATH') or (BASE_DIR / 'db.sqlite3'),
        }
    }
