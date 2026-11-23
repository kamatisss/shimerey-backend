"""
Run slow side work (notification emails, phone pushes) once the database change is saved.

Where the web server lets apps start threads (gunicorn on Render, runserver), the work runs in a
background thread so the customer's request returns at once. PythonAnywhere's web workers (uWSGI)
do not run threads an app starts: a thread there is created and silently never runs, so emails and
pushes would just never go out. There the work runs right after the commit, inside the request.
That makes the request a few seconds slower when mail is sent, but the mail actually arrives.

settings.NOTIFY_EMAIL_ASYNC = False forces the in-request way everywhere (the tests use it).
"""
import logging
import threading

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


def threads_run_here():
    """False under uWSGI without threads enabled (PythonAnywhere); True elsewhere."""
    try:
        import uwsgi  # noqa: F401 — only importable inside a uWSGI worker
    except ImportError:
        return True   # gunicorn, runserver, management commands, tests
    opt = getattr(uwsgi, 'opt', {}) or {}

    def on(key):
        value = opt.get(key, opt.get(key.encode()))
        return value not in (None, False, b'0', '0', b'false', 'false')
    return on('enable-threads') or on('threads')


_THREADS = None


def _background_ok():
    global _THREADS
    if not getattr(settings, 'NOTIFY_EMAIL_ASYNC', True):
        return False
    if _THREADS is None:
        _THREADS = threads_run_here()
        if not _THREADS:
            logger.info('Web server does not run app threads: notifications are sent inside the request.')
    return _THREADS


def run_after_commit(fn):
    """Call fn() after the current transaction commits: in a thread where threads run, else directly."""
    if _background_ok():
        transaction.on_commit(lambda: threading.Thread(target=fn, daemon=True).start())
    else:
        transaction.on_commit(fn)
