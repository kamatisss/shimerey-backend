"""
3D models (.glb) uploaded by the office from the Inventory page.

    GET    /api/inventory-models/              office: the uploaded models, space used / left, and where
                                               new uploads go (storage: "r2" or "server")
    POST   /api/inventory-models/upload-url/   office, R2: label, category, size → a link to upload to
    POST   /api/inventory-models/confirm/      office, R2: the token from upload-url, once the file is up
    POST   /api/inventory-models/              office, no R2: upload through this server (file, label, category)
    DELETE /api/inventory-models/<id>/         office: delete one no inventory item uses
    GET    /api/model-files/<file_name>        anyone: the .glb (what the 3D Studio loads)

Two places a file can live (UploadedModel.storage):
  r2      Cloudflare R2 (r2.py), used when the R2_* settings are filled in. The browser PUTs the file
          straight to R2 with a 15-minute signed link, so it never passes through this server: no disk
          used here, and PythonAnywhere's single free worker isn't tied up by a big upload.
          /api/model-files/<name> redirects to the bucket's public address.
  server  this server's disk (MEDIA_ROOT/models/), the fallback when R2 isn't set up. Served by Django
          rather than PythonAnywhere's /media/ mapping, because the 3D viewer fetches it from the
          Vercel site and needs CORS headers the static mapping doesn't send.

Checks (a renamed .zip or a model the viewer can't draw is refused with a plain reason): real binary
glTF 2.0, one self-contained file (no external textures), at least one mesh, no Draco or KTX2/Basis
(the viewer decodes Meshopt only), within the size caps. For R2 uploads this server downloads the
file back to check it and deletes it from R2 if it fails. If it can't reach R2 at all (PythonAnywhere's
free plan only reaches whitelisted sites) the model is kept on the browser's checks alone; uploads are
office-only, and the browser already opened the model in the same 3D loader.
"""
import json
import logging
import re
import secrets
import struct

import requests
from django.conf import settings
from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.db.models import Sum
from django.http import FileResponse, Http404, HttpResponseRedirect
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import activity, r2
from .inventory import MODEL_NAME
from .models import InventoryItem, UploadedModel

log = logging.getLogger(__name__)

MODEL_DIR = 'models'          # folder on the server's disk, and key prefix in R2
UPLOAD_SALT = 'model-upload'
UPLOAD_LINK_SECONDS = 15 * 60
CATEGORIES = {c for c, _ in InventoryItem.CATEGORY_CHOICES}
# The 3D Studio's loader decodes Meshopt but not these (ThreeGardenCanvas.jsx, modelThumbnail.js)
UNSUPPORTED_EXTENSIONS = {
    'KHR_draco_mesh_compression': 'Draco compression',
    'KHR_texture_basisu': 'KTX2 / Basis textures',
}
GLB_MAGIC, JSON_CHUNK = b'glTF', 0x4E4F534A


def max_file_bytes():
    return settings.MAX_MODEL_UPLOAD_BYTES


def max_total_bytes():
    return settings.MAX_TOTAL_MODEL_BYTES


def _mb(n):
    return f'{n / (1024 * 1024):.1f} MB'


def check_glb(data):
    """Return None if `data` is a .glb the 3D Studio can show, else a plain reason why not."""
    if len(data) < 20 or data[:4] != GLB_MAGIC:
        return 'This is not a .glb 3D model. Export the model as binary glTF (.glb).'
    version, length = struct.unpack_from('<II', data, 4)
    if version != 2:
        return f'This is a glTF {version} file; only glTF 2.0 models are supported.'
    if length != len(data):
        return 'The file is incomplete or damaged (its size does not match). Export it again.'
    chunk_len, chunk_type = struct.unpack_from('<II', data, 12)
    if chunk_type != JSON_CHUNK or 20 + chunk_len > len(data):
        return 'The file is damaged (no model description found). Export it again.'
    try:
        gltf = json.loads(data[20:20 + chunk_len].decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return 'The file is damaged (unreadable model description). Export it again.'
    if not isinstance(gltf, dict) or not str((gltf.get('asset') or {}).get('version', '')).startswith('2'):
        return 'Only glTF 2.0 models are supported.'
    if not gltf.get('meshes'):
        return 'The file has no 3D shapes in it.'
    for kind in ('buffers', 'images'):
        for entry in gltf.get(kind) or []:
            uri = entry.get('uri') if isinstance(entry, dict) else None
            if uri and not uri.startswith('data:'):
                return ('The model points to separate files (e.g. textures) that are not inside it. '
                        'Export it as one self-contained .glb.')
    for ext in gltf.get('extensionsRequired') or []:
        if ext in UNSUPPORTED_EXTENSIONS:
            return (f'The model uses {UNSUPPORTED_EXTENSIONS[ext]}, which the 3D Studio can’t open. '
                    'Re-export without it (Meshopt compression and WebP textures work).')
    return None


def _file_name_for(label):
    stem = re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')[:60].strip('-') or 'model'
    name = f'{stem}-{secrets.token_hex(3)}.glb'
    return name if MODEL_NAME.match(name) else f'model-{secrets.token_hex(3)}.glb'


def _used_bytes():
    return UploadedModel.objects.aggregate(t=Sum('size_bytes'))['t'] or 0


def _check_details(label, category, size, file_name):
    """Errors (field → message) in what the office typed and the file's name and size."""
    errors = {}
    used = _used_bytes()
    if size is None:
        errors['file'] = 'Choose a .glb file.'
    elif not (file_name or '').lower().endswith('.glb'):
        errors['file'] = 'Choose a .glb file (binary glTF).'
    elif size > max_file_bytes():
        errors['file'] = (f'This model is {_mb(size)}; the limit is {_mb(max_file_bytes())}. '
                          'Compress it first (Meshopt + WebP textures).')
    elif used + size > max_total_bytes():
        errors['file'] = (f'Not enough space for 3D models: {_mb(max(0, max_total_bytes() - used))} left of '
                          f'{_mb(max_total_bytes())}. Delete models no item uses, then try again.')
    if not label:
        errors['label'] = 'Give the model a name.'
    elif len(label) > 80:
        errors['label'] = 'Keep the name under 80 characters.'
    if category not in CATEGORIES:
        errors['category'] = 'Choose plant, hardscape or furniture.'
    return errors


def _row(m, request):
    return {
        'id': m.id, 'label': m.label, 'category': m.category, 'file_name': m.file_name,
        'size_bytes': m.size_bytes, 'storage': m.storage, 'created_at': m.created_at,
        'uploaded_by': m.uploaded_by.get_username() if m.uploaded_by else None,
        'url': request.build_absolute_uri(f'/api/model-files/{m.file_name}'),
        'used_by': list(InventoryItem.objects.filter(model_file=m.file_name).values_list('name', flat=True)),
    }


def _is_office(request):
    from .views_api import IsOfficeAdmin   # views_api imports a lot; keep this module light
    return IsOfficeAdmin().has_permission(request, None)


FORBIDDEN = {'error': 'Only the office can manage 3D models.'}


def _created(request, model):
    activity.record(request.user, 'stock', 'model_uploaded',
                    f'Uploaded the 3D model "{model.label}" ({_mb(model.size_bytes)})', link='/inventory', model=model.id)
    return Response(_row(model, request), status=status.HTTP_201_CREATED)


@api_view(['GET', 'POST'])
@parser_classes([MultiPartParser, FormParser])
def inventory_models(request):
    if not _is_office(request):
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'GET':
        return Response({
            'models': [_row(m, request) for m in UploadedModel.objects.select_related('uploaded_by')],
            'used_bytes': _used_bytes(), 'limit_bytes': max_total_bytes(), 'max_file_bytes': max_file_bytes(),
            'storage': 'r2' if r2.enabled() else 'server',
        })

    # Upload through this server (only when R2 isn't set up)
    if r2.enabled():
        return Response({'error': 'Uploads go straight to cloud storage now. Reload the page and try again.'},
                        status=status.HTTP_400_BAD_REQUEST)
    upload = request.FILES.get('file')
    label = (request.data.get('label') or '').strip()
    category = request.data.get('category') or 'plant'
    errors = _check_details(label, category, upload.size if upload else None, upload.name if upload else '')
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    data = upload.read()
    problem = check_glb(data)
    if problem:
        return Response({'file': problem}, status=status.HTTP_400_BAD_REQUEST)
    saved = default_storage.save(f'{MODEL_DIR}/{_file_name_for(label)}', ContentFile(data))
    model = UploadedModel.objects.create(label=label, category=category, file_name=saved.rsplit('/', 1)[-1],
                                         size_bytes=len(data), storage='server', uploaded_by=request.user)
    return _created(request, model)


@api_view(['POST'])
@parser_classes([JSONParser])
def model_upload_url(request):
    """R2 step 1: check the details, hand back a 15-minute link the browser PUTs the file to."""
    if not _is_office(request):
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    if not r2.enabled():
        return Response({'error': 'Cloud storage is not set up; upload through the server instead.'},
                        status=status.HTTP_400_BAD_REQUEST)
    label = (request.data.get('label') or '').strip()
    category = request.data.get('category') or 'plant'
    try:
        size = int(request.data.get('size'))
    except (TypeError, ValueError):
        size = None
    errors = _check_details(label, category, size, request.data.get('file_name') or '')
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    file_name = _file_name_for(label)
    token = signing.dumps({'f': file_name, 'l': label, 'c': category, 's': size, 'u': request.user.pk},
                          salt=UPLOAD_SALT)
    return Response({'upload_url': r2.presign('PUT', f'{MODEL_DIR}/{file_name}', expires=UPLOAD_LINK_SECONDS),
                     'file_name': file_name, 'token': token})


def _r2_read(key):
    """(bytes, None) from R2; (None, reason) if it isn't there; (None, None) if R2 can't be reached."""
    try:
        res = requests.get(r2.presign('GET', key), timeout=60)
    except requests.RequestException as exc:
        log.warning('R2 not reachable to check %s: %s', key, exc)
        return None, None
    if res.status_code == 404:
        return None, 'The upload didn’t arrive. Try again.'
    if res.status_code != 200:
        log.error('R2 GET %s → %s %s', key, res.status_code, res.text[:300])
        return None, f'Cloud storage refused the check ({res.status_code}). Check the R2 keys in the server settings.'
    return res.content, None


def _r2_delete(key):
    try:
        res = requests.delete(r2.presign('DELETE', key), timeout=30)
        if res.status_code not in (200, 204, 404):
            log.error('R2 DELETE %s → %s %s', key, res.status_code, res.text[:300])
    except requests.RequestException as exc:
        log.warning('R2 not reachable to delete %s: %s', key, exc)


@api_view(['POST'])
@parser_classes([JSONParser])
def model_upload_confirm(request):
    """R2 step 2: the browser finished the PUT. Check the file in R2, then add it to the list."""
    if not _is_office(request):
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    try:
        info = signing.loads(request.data.get('token') or '', salt=UPLOAD_SALT, max_age=UPLOAD_LINK_SECONDS + 600)
    except signing.BadSignature:
        return Response({'error': 'This upload expired. Upload the model again.'}, status=status.HTTP_400_BAD_REQUEST)
    if info['u'] != request.user.pk:
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    existing = UploadedModel.objects.filter(file_name=info['f']).first()
    if existing:   # a retried confirm
        return Response(_row(existing, request))

    key = f'{MODEL_DIR}/{info["f"]}'
    data, problem = _r2_read(key)
    size = info['s']
    if data is not None:
        problem = ('The upload was cut short. Try again.' if len(data) != size else None) or check_glb(data)
        size = len(data)
        if not problem and _used_bytes() + size > max_total_bytes():
            problem = 'Not enough space for 3D models. Delete models no item uses, then try again.'
    if problem:
        if data is not None:
            _r2_delete(key)
        return Response({'file': problem}, status=status.HTTP_400_BAD_REQUEST)
    try:
        model = UploadedModel.objects.create(label=info['l'], category=info['c'], file_name=info['f'],
                                             size_bytes=size, storage='r2', uploaded_by=request.user)
    except IntegrityError:
        return Response(_row(UploadedModel.objects.get(file_name=info['f']), request))
    return _created(request, model)


@api_view(['DELETE'])
def inventory_model_detail(request, pk):
    if not _is_office(request):
        return Response(FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    model = UploadedModel.objects.filter(pk=pk).first()
    if model is None:
        return Response({'error': 'That model was already deleted.'}, status=status.HTTP_404_NOT_FOUND)
    users = list(InventoryItem.objects.filter(model_file=model.file_name).values_list('name', flat=True))
    if users:
        return Response({'error': f'{", ".join(users)} still use{"s" if len(users) == 1 else ""} this model. '
                                  'Pick another model for them first.'}, status=status.HTTP_409_CONFLICT)
    key = f'{MODEL_DIR}/{model.file_name}'
    if model.storage == 'r2':
        _r2_delete(key)   # if R2 can't be reached the file stays there unused; the list no longer shows it
    else:
        default_storage.delete(key)
    model.delete()
    activity.record(request.user, 'stock', 'model_deleted', f'Deleted the 3D model "{model.label}"', link='/inventory')
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes([AllowAny])
def model_file(request, file_name):
    """The .glb itself. Only names of uploaded models are served (no other path is reachable)."""
    model = UploadedModel.objects.filter(file_name=file_name).first() if MODEL_NAME.match(file_name) else None
    if model is None:
        raise Http404
    key = f'{MODEL_DIR}/{file_name}'
    if model.storage == 'r2':
        # The name never changes content, but keep the redirect itself short-lived in case storage moves
        response = HttpResponseRedirect(r2.public_url(key))
        response['Cache-Control'] = 'public, max-age=86400'
        return response
    if not default_storage.exists(key):
        raise Http404
    response = FileResponse(default_storage.open(key, 'rb'), content_type='model/gltf-binary')
    response['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response
