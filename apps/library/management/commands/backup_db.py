from django.core.management.base import BaseCommand, CommandError

from apps.library.backups import KEEP, BackupError, backup_database, list_backups


class Command(BaseCommand):
    help = 'Back up the SQLite database to backups/db-YYYY-MM-DD.sqlite3.gz (keeps the newest 7).'

    def add_arguments(self, parser):
        parser.add_argument('--keep', type=int, default=KEEP, help='How many daily backups to keep (default 7).')
        parser.add_argument('--dir', default=None, help='Where to put them (default BACKEND/backups or BACKUP_DIR).')

    def handle(self, *args, **options):
        try:
            path = backup_database(options['dir'], keep=max(1, options['keep']))
        except BackupError as e:
            raise CommandError(str(e))
        size_kb = path.stat().st_size // 1024
        self.stdout.write(self.style.SUCCESS(f'Backed up to {path} ({size_kb} KB).'))
        self.stdout.write(f'Backups kept: {", ".join(p.name for p in list_backups(options["dir"]))}')
