"""
Database backups for the single-file SQLite database (PythonAnywhere free plan: one scheduled task,
512 MB disk). Used by `manage.py backup_db` and `manage.py daily_maintenance`.

Each backup is a consistent copy taken through SQLite's online-backup API (safe while the site is
serving requests), checked with `PRAGMA integrity_check`, gzip-compressed, and named by date:
    backups/db-2026-10-23.sqlite3.gz
Only the newest KEEP copies are kept. backups/ is git-ignored and not served on the web.
Restore: see DEPLOY.md ("Restore a backup").
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

KEEP = 7
PATTERN = 'db-*.sqlite3.gz'


class BackupError(Exception):
    pass


def backup_dir():
    return Path(os.environ.get('BACKUP_DIR') or (Path(settings.BASE_DIR) / 'backups'))


def _source_connection():
    """
    A separate connection to the database file (the normal case: never tangled in the web app's
    transactions), or Django's own connection for an in-memory database (tests). Returns (conn, own).
    """
    name = str(connection.settings_dict['NAME'])
    if name and ':memory:' not in name and 'mode=memory' not in name and Path(name).exists():
        return sqlite3.connect(name, timeout=30), True
    connection.ensure_connection()
    return connection.connection, False


def backup_database(dest=None, keep=KEEP):
    """Write today's backup (replacing an earlier one from today) and prune old ones. Returns the path."""
    if connection.vendor != 'sqlite':
        raise BackupError('backup_db only handles SQLite; use your database host\'s backups.')
    dest = Path(dest) if dest else backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f'db-{timezone.localdate():%Y-%m-%d}.sqlite3.gz'

    fd, tmp_name = tempfile.mkstemp(suffix='.sqlite3', dir=dest)
    os.close(fd)
    try:
        copy = sqlite3.connect(tmp_name)
        source, own = _source_connection()
        try:
            source.backup(copy)                          # consistent snapshot of the live database
            result = copy.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            copy.close()
            if own:
                source.close()
        if result != 'ok':
            raise BackupError(f'The copy failed its integrity check: {result}')
        partial = target.with_suffix('.gz.part')
        with open(tmp_name, 'rb') as src, gzip.open(partial, 'wb', compresslevel=9) as out:
            shutil.copyfileobj(src, out)
        os.replace(partial, target)                     # never leave a half-written backup behind
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)

    for old in sorted(dest.glob(PATTERN), reverse=True)[keep:]:
        old.unlink()
    return target


def list_backups(dest=None):
    dest = Path(dest) if dest else backup_dir()
    return sorted(dest.glob(PATTERN), reverse=True) if dest.exists() else []
