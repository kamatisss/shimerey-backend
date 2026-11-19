"""
Move to Render, step "links": point full links to the old server's photos at Cloudflare R2.

    python manage.py rewrite_old_media_urls --old https://shimereyslandscaping.pythonanywhere.com/media/
    python manage.py rewrite_old_media_urls --old https://shimereyslandscaping.pythonanywhere.com/media/ --apply

Some rows keep a whole address (e.g. a design's reference photo
"https://….pythonanywhere.com/media/design_refs/ref_….jpg"). Once the old server is gone those break,
so every text and JSON value that contains --old gets it replaced by R2_MEDIA_PUBLIC_URL/. Short links
("/media/…") need nothing: the new server redirects them to R2 (media_redirect.py).

Dry run by default (lists what would change). Rows are updated with .update(), so "last edited" dates
and save() side effects (notifications, logs) are left alone. Run copy_media_to_r2 first.
"""
import json

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction


class Command(BaseCommand):
    help = 'Replace full links to the old server\'s /media/ with the R2 media bucket\'s address.'

    def add_arguments(self, parser):
        parser.add_argument('--old', required=True, help='e.g. https://yourname.pythonanywhere.com/media/')
        parser.add_argument('--apply', action='store_true', help='Really change the rows (default: dry run)')

    def handle(self, *args, old, apply, **options):
        if not old.startswith(('http://', 'https://')) or not old.endswith('/media/'):
            raise CommandError('--old must be the old site\'s full media address, ending in /media/')
        if not settings.R2_MEDIA_PUBLIC_URL:
            raise CommandError('R2_MEDIA_PUBLIC_URL is not set.')
        new = settings.R2_MEDIA_PUBLIC_URL + '/'
        self.stdout.write(f'{"Rewriting" if apply else "Dry run (add --apply to change)"}: {old} → {new}')

        total = 0
        with transaction.atomic():
            for model in apps.get_models():
                fields = [f for f in model._meta.concrete_fields
                          if isinstance(f, (models.CharField, models.TextField, models.JSONField))]
                if not fields:
                    continue
                for row in model._default_manager.values('pk', *[f.attname for f in fields]).iterator():
                    changes = {}
                    for f in fields:
                        value = row[f.attname]
                        if isinstance(f, models.JSONField):
                            if value is None:
                                continue
                            text = json.dumps(value)
                            if old in text:
                                changes[f.attname] = json.loads(text.replace(old, new))
                        elif isinstance(value, str) and old in value:
                            changes[f.attname] = value.replace(old, new)
                    if changes:
                        total += 1
                        self.stdout.write(f'  {model.__name__} #{row["pk"]}: {", ".join(changes)}')
                        if apply:
                            model._default_manager.filter(pk=row['pk']).update(**changes)
        self.stdout.write(self.style.SUCCESS(f'{total} row(s) {"changed" if apply else "would change"}.'))
