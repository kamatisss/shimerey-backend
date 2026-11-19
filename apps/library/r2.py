"""
Cloudflare R2 (S3-compatible storage) for uploaded 3D models, without boto3.

boto3 + botocore would take ~80 MB of PythonAnywhere's 512 MB, and all this app needs is a few
pre-signed URLs (AWS Signature Version 4, query-string form). Signing happens locally: making an
upload link needs no network at all, which matters on PythonAnywhere's free plan, where outbound
requests only reach whitelisted sites.

    presign('PUT', key)     → URL the browser uploads the file to (expires in 15 minutes)
    presign('GET', key)     → URL this server reads the file back with (to check it)
    presign('DELETE', key)  → URL this server deletes it with
    public_url(key)         → where the 3D Studio loads it (R2_PUBLIC_URL, a public bucket)
"""
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

from django.conf import settings

REGION, SERVICE = 'auto', 's3'


def enabled():
    return bool(getattr(settings, 'R2_ENABLED', False))


def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def presign_url(method, host, path, access_key, secret_key, region=REGION, expires=900, now=None):
    """AWS SigV4 pre-signed URL (only the Host header is signed; the body is UNSIGNED-PAYLOAD)."""
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    scope = f'{day}/{region}/{SERVICE}/aws4_request'
    params = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': f'{access_key}/{scope}',
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': str(int(expires)),
        'X-Amz-SignedHeaders': 'host',
    }
    query = '&'.join(f'{quote(k, safe="-_.~")}={quote(v, safe="-_.~")}' for k, v in sorted(params.items()))
    canonical_path = quote(path, safe='/-_.~')
    canonical = '\n'.join([method, canonical_path, query, f'host:{host}', '', 'host', 'UNSIGNED-PAYLOAD'])
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _sign(_sign(_sign(_sign(f'AWS4{secret_key}'.encode(), day), region), SERVICE), 'aws4_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f'https://{host}{canonical_path}?{query}&X-Amz-Signature={signature}'


def presign(method, object_key, expires=900):
    host = f'{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com'
    return presign_url(method, host, f'/{settings.R2_BUCKET}/{object_key}',
                       settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY, expires=expires)


def public_url(object_key):
    return f'{settings.R2_PUBLIC_URL}/{quote(object_key, safe="/-_.~")}'
