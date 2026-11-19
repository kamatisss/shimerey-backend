"""
GET /media/<path>  →  302 to the same file in Cloudflare R2's media bucket.

Only wired up when uploads live in R2 (settings.MEDIA_ON_R2, config/urls.py). Links saved before the
move ("/media/thumbnails/x.jpg" in designs, depth maps the frontend joins onto the API's site) keep
working without rewriting data. Nothing is read from R2 here: the address is built locally.
"""
from django.core.files.storage import default_storage
from django.http import Http404, HttpResponseRedirect


def media_redirect(request, path):
    parts = path.split('/')
    if path.startswith('/') or '\\' in path or any(p in ('', '.', '..') for p in parts):
        raise Http404
    response = HttpResponseRedirect(default_storage.url(path))
    # The file at a name never changes (uploads get new names), but keep the redirect itself short-lived
    response['Cache-Control'] = 'public, max-age=86400'
    return response
