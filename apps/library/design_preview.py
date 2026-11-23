"""
Design card pictures: POST /api/designs/<id>/preview/ (multipart `image`, optional `revision`).

The customer's browser draws the garden after a save (frontend data/designPreview.js, the same 3D overview
the PDF proposal uses) and sends it here. We check the bytes, shrink it, re-encode a clean JPEG (no
metadata) and keep it on the design. It's only a picture of the design: it never counts as a change, so
it doesn't bump updated_at ("Edited …", list order) or the revision, and a booked (locked) design can
still get one.
"""
import io

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from .file_checks import image_problem

MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MAX_SIZE = (800, 500)   # the cards show it at about 300 × 190; room for sharp phones


def clean_preview(upload):
    """(ContentFile JPEG ≤ 800 × 500, None) or (None, reason)."""
    problem = image_problem(upload, MAX_UPLOAD_BYTES)
    if problem:
        return None, problem
    try:
        img = Image.open(upload)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        return None, 'This picture is damaged. Save the design again.'
    img = ImageOps.exif_transpose(img)
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    img.thumbnail(MAX_SIZE)
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=82, optimize=True)
    return ContentFile(out.getvalue(), name='preview.jpg'), None


def can_set(user, design):
    """The design's owner or the office. Crew can open a job's design but don't change its picture."""
    if user.is_superuser or user.is_staff and not _is_crew(user):
        return True
    return design.user_id == user.id


def _is_crew(user):
    from .work_queues import is_field_crew
    return is_field_crew(user)


def save_preview(design, content, revision=None):
    """Store the picture, drop the old file, and point the design at it without touching updated_at."""
    from .models import GardenDesign
    field = GardenDesign._meta.get_field('preview')
    name = field.storage.save(field.generate_filename(design, content.name), content)
    old = design.preview.name if design.preview else ''
    try:
        rev = int(revision)
    except (TypeError, ValueError):
        rev = design.revision
    rev = max(1, min(rev, design.revision))   # a picture can't claim a version that doesn't exist yet
    GardenDesign.objects.filter(pk=design.pk).update(preview=name, preview_revision=rev)
    if old and old != name:
        delete_file(old)
    design.preview.name, design.preview_revision = name, rev
    return design


def delete_file(name):
    from .models import GardenDesign
    if not name:
        return
    try:
        GardenDesign._meta.get_field('preview').storage.delete(name)
    except Exception:   # a missing file or a storage hiccup must never fail the request
        pass


def preview_url(design, request=None):
    if not design.preview:
        return None
    url = design.preview.url
    return request.build_absolute_uri(url) if request and url.startswith('/') else url
