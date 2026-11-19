"""
What an uploaded file really is: checked from its bytes, never from its name or the type the browser
claims (both are trivially faked by renaming a file). Every upload endpoint uses these, so a wrong file
gets the same plain answer everywhere:

    image_problem(upload)        None if it's a real JPG / PNG / WebP photo, else why not
    image_from_data_url(text)    attendance photos sent as "data:image/...;base64,..." → clean JPEG file
    document_problem(upload)     None if it's a real PDF or photo (business credentials), else why not

Photos that are kept are also re-encoded by portfolio.sanitize_image (EXIF/GPS stripped, size capped).
"""
import base64
import binascii
import io

from PIL import Image, UnidentifiedImageError

MAX_PHOTO_BYTES = 15 * 1024 * 1024
PHOTO_TYPES = 'JPG, PNG or WebP'


def _mb(n):
    return f'{n / (1024 * 1024):.0f} MB'


def _head(upload, n=32):
    upload.seek(0)
    head = upload.read(n)
    upload.seek(0)
    return head or b''


def sniff(head):
    """A short name for what the first bytes of a file say it is."""
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:5] == b'%PDF-':
        return 'pdf'
    if head[4:8] == b'ftyp' and head[8:12] in (b'heic', b'heix', b'mif1', b'msf1', b'hevc', b'heim', b'heis'):
        return 'heic'
    if head[:4] == b'glTF':
        return 'glb'
    if head[:3] == b'GIF':
        return 'gif'
    return 'other'


def _wrong_kind_message(kind):
    if kind == 'heic':
        return (f'This is an iPhone HEIC photo, which can’t be used here. Choose it from the Photos app '
                f'(it’s converted for you) or use a {PHOTO_TYPES} copy.')
    if kind == 'pdf':
        return f'This is a PDF, not a photo. Upload a {PHOTO_TYPES} image.'
    if kind == 'gif':
        return f'GIF images aren’t supported. Upload a {PHOTO_TYPES} image.'
    return f'This file isn’t a photo. Upload a {PHOTO_TYPES} image.'


def image_problem(upload, max_bytes=MAX_PHOTO_BYTES):
    """None if `upload` (a file-like object with .size) is a real, readable JPG/PNG/WebP photo."""
    if upload is None:
        return 'Choose a photo.'
    size = getattr(upload, 'size', None)
    if size is not None and size > max_bytes:
        return f'This photo is larger than {_mb(max_bytes)}. Use a smaller one.'
    kind = sniff(_head(upload))
    if kind not in ('jpeg', 'png', 'webp'):
        return _wrong_kind_message(kind)
    try:
        upload.seek(0)
        Image.open(upload).verify()   # reads the whole structure; a truncated or corrupt file fails here
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        return 'This photo is damaged or incomplete. Take or choose it again.'
    finally:
        upload.seek(0)
    return None


def image_from_data_url(data, max_bytes=MAX_PHOTO_BYTES):
    """
    ("data:image/...;base64,<...>") → (clean JPEG ContentFile, None) or (None, reason).
    The saved file is always a re-encoded .jpg: the type and extension in the text are never trusted
    (a "data:image/html" upload must not become an .html file on our domain).
    """
    from .portfolio import sanitize_image   # portfolio imports a lot; keep this module light
    if not isinstance(data, str) or not data.startswith('data:') or ';base64,' not in data:
        return None, 'The photo didn’t come through. Take it again.'
    encoded = data.split(';base64,', 1)[1]
    if len(encoded) * 3 // 4 > max_bytes:
        return None, f'This photo is larger than {_mb(max_bytes)}. Use a smaller one.'
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None, 'The photo didn’t come through. Take it again.'
    upload = io.BytesIO(raw)
    upload.size = len(raw)
    problem = image_problem(upload, max_bytes)
    if problem:
        return None, problem
    clean, error = sanitize_image(upload)
    return (clean, None) if clean else (None, error)


def document_problem(upload, max_bytes):
    """None if `upload` is a real PDF or JPG/PNG/WebP image (business credentials)."""
    if upload.size > max_bytes:
        return f'The file is larger than {_mb(max_bytes)}.'
    kind = sniff(_head(upload))
    named_pdf = (getattr(upload, 'name', '') or '').lower().endswith('.pdf')
    if kind == 'pdf':
        return None if named_pdf else 'This file is a PDF but its name says otherwise. Rename it to end in .pdf.'
    if kind in ('jpeg', 'png', 'webp'):
        # It is served by its name: a photo named .pdf would open as a broken PDF
        return 'This file is a photo but its name ends in .pdf. Rename it (e.g. .jpg).' if named_pdf             else image_problem(upload, max_bytes)
    return 'Upload a PDF or a photo (JPG, PNG or WebP) of the document.'
