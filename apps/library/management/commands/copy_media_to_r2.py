"""
Move to Render, step "files": copy the old server's media/ folder into Cloudflare R2.

    python manage.py copy_media_to_r2 --source path/to/media            # dry run: lists what it would do
    python manage.py copy_media_to_r2 --source path/to/media --apply

  * Photos and documents (everything except media/models/) → the media bucket (R2_MEDIA_BUCKET),
    under the same path, so the names saved in the database keep pointing at them.
  * 3D models the office uploaded to the old server's disk (UploadedModel.storage = "server")
    → the models bucket (R2_BUCKET, models/<name>), then marked storage = "r2".

Safe to run again: files already in R2 are skipped, models already marked "r2" are left alone.
Run it on Render (Shell tab) after the database is loaded, with the media folder uploaded next to it,
or on your computer with the production settings and DATABASE_URL pointing at Neon.
"""
from pathlib import Path

import requests
from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

from apps.library import r2
from apps.library.models import UploadedModel

MODEL_DIR = 'models'


class Command(BaseCommand):
    help = 'Copy media/ (photos, documents, server-stored 3D models) into Cloudflare R2.'

    def add_arguments(self, parser):
        parser.add_argument('--source', required=True, help='The media folder from the old server')
        parser.add_argument('--apply', action='store_true', help='Really copy (default: dry run)')

    def handle(self, *args, source, apply, **options):
        src = Path(source)
        if not src.is_dir():
            raise CommandError(f'{src} is not a folder.')
        if not settings.MEDIA_ON_R2 or not r2.enabled():
            raise CommandError('Both R2 buckets must be configured (R2_* and R2_MEDIA_* settings): '
                               'run this with the production settings.')
        mode = 'Copying' if apply else 'Dry run (add --apply to copy)'
        self.stdout.write(f'{mode}: {src} → media bucket "{settings.R2_MEDIA_BUCKET}", '
                          f'models bucket "{settings.R2_BUCKET}"')

        copied = skipped = 0
        for path in sorted(p for p in src.rglob('*') if p.is_file()):
            key = path.relative_to(src).as_posix()
            if key.split('/', 1)[0] == MODEL_DIR:
                continue   # handled below, from the database's list
            if default_storage.exists(key):
                skipped += 1
                continue
            if apply:
                with path.open('rb') as fh:
                    saved = default_storage.save(key, File(fh))
                if saved != key:
                    raise CommandError(f'{key} was saved as {saved}: stop and check the bucket.')
            copied += 1
            self.stdout.write(f'  {"copied" if apply else "would copy"} {key}')
        self.stdout.write(self.style.SUCCESS(f'Photos & documents: {copied} {"copied" if apply else "to copy"}, '
                                             f'{skipped} already in R2.'))

        moved, missing = 0, []
        for model in UploadedModel.objects.filter(storage='server').order_by('id'):
            path = src / MODEL_DIR / model.file_name
            if not path.is_file():
                missing.append(model.file_name)
                continue
            if apply:
                res = requests.put(r2.presign('PUT', f'{MODEL_DIR}/{model.file_name}'), data=path.read_bytes(),
                                   headers={'Content-Type': 'model/gltf-binary'}, timeout=120)
                if res.status_code not in (200, 201):
                    raise CommandError(f'R2 refused {model.file_name}: {res.status_code} {res.text[:200]}')
                UploadedModel.objects.filter(pk=model.pk).update(storage='r2')
            moved += 1
            self.stdout.write(f'  {"moved" if apply else "would move"} 3D model {model.file_name}')
        self.stdout.write(self.style.SUCCESS(f'3D models: {moved} {"moved" if apply else "to move"} to R2.'))
        if missing:
            self.stderr.write(self.style.ERROR(
                f'{len(missing)} server-stored model(s) not found under {src / MODEL_DIR}: {", ".join(missing)}'))
