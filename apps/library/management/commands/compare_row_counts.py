"""
Move to Render, step "check": compare every table's row count in the old SQLite file with this database.

    python manage.py compare_row_counts path/to/db.sqlite3

Run it right after `loaddata` into Postgres. It exits with an error if any table differs (or is
missing on one side), so the switch-over only goes ahead when everything arrived.
"""
import sqlite3
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

# Not copied on purpose (dumpdata -e …): Django rebuilds these itself, sessions are throwaway
SKIPPED = {'django_content_type', 'auth_permission', 'django_session', 'django_migrations'}


class Command(BaseCommand):
    help = 'Compare row counts per table: old SQLite file vs. the current database.'

    def add_arguments(self, parser):
        parser.add_argument('sqlite_file')

    def handle(self, *args, sqlite_file, **options):
        path = Path(sqlite_file)
        if not path.is_file():
            raise CommandError(f'{path} not found.')
        old = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
        old_tables = {r[0] for r in old.execute("select name from sqlite_master where type='table'")}

        tables = set()
        for model in apps.get_models(include_auto_created=True):
            tables.add(model._meta.db_table)
        rows, bad = [], 0
        for table in sorted(tables - SKIPPED):
            before = (old.execute(f'select count(*) from "{table}"').fetchone()[0]
                      if table in old_tables else None)
            with connection.cursor() as cur:
                cur.execute(f'select count(*) from {connection.ops.quote_name(table)}')
                after = cur.fetchone()[0]
            ok = before == after or (before is None and after == 0)   # a table newer than the old file
            bad += not ok
            if not ok or before:
                rows.append((table, before, after, ok))
        old.close()

        for table, before, after, ok in rows:
            line = f'{table:45} {"missing" if before is None else before:>8} -> {after:>8}'
            self.stdout.write(line if ok else self.style.ERROR(line + '   DIFFERENT'))
        if bad:
            raise CommandError(f'{bad} table(s) differ. Do not switch over yet.')
        self.stdout.write(self.style.SUCCESS(f'All {len(tables - SKIPPED)} tables match '
                                             f'({sum(r[2] for r in rows)} rows).'))
