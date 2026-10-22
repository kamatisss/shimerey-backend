# Deploying the backend (PythonAnywhere)

The backend is ~2 MB of code; its packages install to ~260 MB (the free plan has 512 MB).
Never upload `venv/`, `.env`, `db.sqlite3`, `*.bak` or `media/` through git — they are git-ignored.

## 0. Before the first deploy (security)
1. Make the GitHub repository **private** (Settings → General → Danger zone → Change visibility).
   Its history contains an old database copy and an old secret key.
2. In the Stripe dashboard, **roll/delete** the old `sk_test_…` key (Stripe is no longer used).
3. Use a **new** `DJANGO_SECRET_KEY` on the server (never one that was ever committed):
   `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`
4. Ask real users (especially the owner account) to change their passwords.

## 1. Code and packages (Bash console)
```bash
git clone https://github.com/<you>/BACKEND.git
cd BACKEND
mkvirtualenv --python=python3.13 garden     # or the newest Python your account offers
pip install --no-cache-dir -r requirements.txt
```
`--no-cache-dir` keeps pip's download cache from eating the disk quota.

## 2. Settings: create `BACKEND/.env` (Files tab or `nano .env`)
Copy `.env.example` and fill in at least:
```
DJANGO_SECRET_KEY=<new random key>
ALLOWED_HOSTS=<yourname>.pythonanywhere.com
FRONTEND_URL=https://<your-frontend>.vercel.app
PAYMONGO_SECRET_KEY=...        PAYMONGO_WEBHOOK_SECRET=...
GEMINI_API_KEY=...
EMAIL_HOST_USER=...            EMAIL_HOST_PASSWORD=<Gmail app password>
VAPID_PUBLIC_KEY=...           VAPID_PRIVATE_KEY=...      VAPID_SUBJECT=mailto:...
```
Password-reset emails **need** the email settings; without them the links are only printed to the server log.

## 3. Database and static files
```bash
python manage.py migrate --settings=config.settings.prod
python manage.py collectstatic --noinput --settings=config.settings.prod
python manage.py createsuperuser --settings=config.settings.prod   # first deploy only
```
(Or upload your existing `db.sqlite3` to `BACKEND/` instead of creating a new one.)

## 4. Web tab
- **Source code / Working directory:** `/home/<you>/BACKEND`
- **Virtualenv:** `/home/<you>/.virtualenvs/garden`
- **WSGI file** (click to edit) — replace its contents with:
  ```python
  import os, sys
  path = '/home/<you>/BACKEND'
  if path not in sys.path:
      sys.path.insert(0, path)
  os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.prod'
  from django.core.wsgi import get_wsgi_application
  application = get_wsgi_application()
  ```
  (`.env` is read by the settings themselves.)
- **Static files** mappings:
  | URL | Directory |
  |---|---|
  | `/static/` | `/home/<you>/BACKEND/staticfiles` |
  | `/media/`  | `/home/<you>/BACKEND/media` |
- **Force HTTPS:** on.
- Reload.

## 5. Frontend (Vercel) environment variables
```
VITE_API_BASE_URL=https://<yourname>.pythonanywhere.com/api
VITE_MEDIA_BASE_URL=https://<yourname>.pythonanywhere.com
```

## 6. PayMongo webhook
Dashboard → Developers → Webhooks → `https://<yourname>.pythonanywhere.com/api/paymongo-webhook/`,
event `checkout_session.payment.paid`; put its secret in `PAYMONGO_WEBHOOK_SECRET` and reload.

## Free-plan limits to know
- Outgoing internet is limited to an allow-list of sites. Check that `api.paymongo.com`,
  `generativelanguage.googleapis.com` and the push services (`fcm.googleapis.com`,
  `updates.push.services.mozilla.com`, `web.push.apple.com`) are reachable from a console
  (`curl -I https://api.paymongo.com`). If not, payments/AI/push need a paid plan or another host.
- Scheduled cleanup of unpaid online orders: add a daily task
  `cd ~/BACKEND && python manage.py expire_unpaid_orders --settings=config.settings.prod`.

## Checks after deploying
`python manage.py check --deploy --settings=config.settings.prod` should only warn about HSTS subdomains/preload.
