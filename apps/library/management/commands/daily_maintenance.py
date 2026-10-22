"""
The one daily scheduled task (PythonAnywhere free accounts get a single scheduled task):

  1. back up the database          (backups/db-YYYY-MM-DD.sqlite3.gz, newest 7 kept)
  2. cancel expired unpaid online orders and return their stock
  3. forget crew-action retry keys older than 3 days

Each step runs even if an earlier one fails; the command exits with an error if any step failed,
so PythonAnywhere's task log shows it.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.library.backups import backup_database
from apps.library.offline import KEEP_DAYS
from apps.library.paymongo import expire_unpaid_online_orders


class Command(BaseCommand):
    help = 'Daily: back up the database, expire unpaid online orders, clean old retry keys.'

    def handle(self, *args, **options):
        from apps.library.models import IdempotentRequest

        steps = [
            ('Backup', lambda: f'{backup_database().name} written'),
            ('Unpaid orders', lambda: f'{expire_unpaid_online_orders()} expired'),
            ('Retry keys', lambda: f'{IdempotentRequest.objects.filter(created_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()[0]} removed'),
        ]
        failed = []
        for name, run in steps:
            try:
                self.stdout.write(self.style.SUCCESS(f'{name}: {run()}'))
            except Exception as e:  # noqa: BLE001 — keep going; report at the end
                failed.append(name)
                self.stderr.write(self.style.ERROR(f'{name} FAILED: {e}'))
        if failed:
            raise CommandError(f'Daily maintenance finished with errors in: {", ".join(failed)}')
