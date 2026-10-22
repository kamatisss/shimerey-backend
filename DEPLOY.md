# Deploying the backend (PythonAnywhere)

The code is ~2 MB; its packages install to ~280 MB (the free plan has 512 MB).
`venv/`, `.env`, `db.sqlite3`, `*.bak`, `media/` and `backups/` never go into git.

Paths below use the live account: user `shimereyslandscaping`, project folder `~/shimerey-backend`,
virtualenv `~/shimerey-backend/venv`. Replace them if yours differ.

---

## A. First deploy

### 1. Code and packages (Bash console)
```bash
git clone https://github.com/kamatisss/shimerey-backend.git
cd shimerey-backend
python3.13 -m venv venv
source venv/bin/activate
pip install --no-cache-dir -r requirements.txt
echo 'export PIP_NO_CACHE_DIR=1' >> ~/.bashrc
```
`--no-cache-dir` matters: pip's download cache alone can fill the 512 MB quota.

### 2. Settings: `~/shimerey-backend/.env`
Copy `.env.example` and fill in at least:
```
DJANGO_SECRET_KEY=<new random key: python -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=shimereyslandscaping.pythonanywhere.com
FRONTEND_URL=https://shimereyslandscaping.vercel.app
PAYMONGO_SECRET_KEY=...
PAYMONGO_WEBHOOK_SECRET=...
GEMINI_API_KEY=...
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=<Gmail app password>
VAPID_PUBLIC_KEY=...
VAPID_PRIVATE_KEY=...
VAPID_SUBJECT=mailto:...
```
- One setting per line, no spaces around `=`. Put a value in double quotes if it has spaces,
  e.g. `DEFAULT_FROM_EMAIL="Garden Studio <you@gmail.com>"`.
- `FRONTEND_URL` must be exactly the site customers use (https, no slash at the end): the API only
  answers that site (CORS), and password-reset and payment emails link to it.
- Without the email settings, password-reset links are only printed to the server log.

### 3. Database, static files, photos
```bash
python manage.py migrate --settings=config.settings.prod
python manage.py collectstatic --noinput --settings=config.settings.prod
```
Using an existing database: upload `db.sqlite3` and `media.zip` to `~/shimerey-backend/` (Files tab),
then `unzip -o media.zip && rm media.zip`. Otherwise: `python manage.py createsuperuser --settings=config.settings.prod`.

### 4. Web tab
**Code:** Python version **3.13** (must match the venv) · Source code and working directory
`/home/shimereyslandscaping/shimerey-backend` · Virtualenv `/home/shimereyslandscaping/shimerey-backend/venv`

**WSGI configuration file:** delete everything in it and paste exactly this (no ``` lines, nothing indented except the one line under `if`):

```python
import os
import sys

path = '/home/shimereyslandscaping/shimerey-backend'
if path not in sys.path:
    sys.path.insert(0, path)

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.prod'

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

**Static files:**

| URL | Directory |
|---|---|
| `/static/` | `/home/shimereyslandscaping/shimerey-backend/staticfiles` |
| `/media/` | `/home/shimereyslandscaping/shimerey-backend/media` |

**Security:** Force HTTPS on. Then **Reload**.

Check: `https://shimereyslandscaping.pythonanywhere.com/api/business/` shows JSON, and a photo URL
such as `/media/thumbnails/product.jpg` shows an image.

### 5. Daily task (Tasks tab)
Free accounts get one scheduled task. Add a daily one (any quiet hour, e.g. 18:00 UTC = 2 AM Manila):
```bash
cd ~/shimerey-backend && venv/bin/python manage.py daily_maintenance --settings=config.settings.prod
```
It backs up the database (`backups/db-YYYY-MM-DD.sqlite3.gz`, newest 7 kept, ~0.4 MB each), cancels
unpaid online orders that expired, and clears old crew retry keys. Its log is on the Tasks tab.

### 6. PayMongo webhook
Dashboard → Developers → Webhooks → `https://shimereyslandscaping.pythonanywhere.com/api/paymongo-webhook/`,
event `checkout_session.payment.paid`. Put its secret in `PAYMONGO_WEBHOOK_SECRET`, then Reload.

### 7. Frontend (Vercel)
Project → Settings: **Root Directory** `frontend`; Environment Variable
`VITE_API_BASE_URL = https://shimereyslandscaping.pythonanywhere.com/api` (Production). A build
without it stops on purpose. Pushing to GitHub deploys automatically; after changing a variable,
redeploy the **newest** deployment (redeploying an older row rebuilds old code).

---

## B. Deploying an update
On your computer: `git push` (backend and/or frontend). Then, for the backend, in a Bash console:
```bash
cd ~/shimerey-backend && source venv/bin/activate
git pull
pip install --no-cache-dir -r requirements.txt     # only if requirements.txt changed
python manage.py migrate --settings=config.settings.prod
python manage.py collectstatic --noinput --settings=config.settings.prod   # only if Django admin styles changed
```
Then **Web tab → Reload**. Deploy the backend first (or together) when a frontend change needs it.

---

## C. Backups

**Take one now** (before anything risky): `python manage.py backup_db --settings=config.settings.prod`

**Keep a copy off the server** now and then: Files tab → `shimerey-backend/backups/` → download the
newest `.gz`. A copy on the same account doesn't help if the account itself is lost.

**Restore a backup:**
```bash
cd ~/shimerey-backend
cp db.sqlite3 db.sqlite3.before-restore          # keep the current one, just in case
gunzip -c backups/db-2026-10-23.sqlite3.gz > db.sqlite3
```
Then **Reload**. (Uploaded photos live in `media/`; they are not inside the database backup.)

---

## D. Keep the free plan running
- **Every month:** Web tab → **"Run until 1 month from today"**. Otherwise the site switches off.
  Put a monthly reminder in your calendar.
- **Disk (512 MB):** Files tab shows usage. If it gets high: `rm -rf ~/.cache/pip`, and check
  `du -sh ~/shimerey-backend/media ~/shimerey-backend/backups`.
- **Outgoing connections** are limited to an allow-list on the free plan. PayMongo, Gemini and
  Android/Firefox push are on it; Apple's push service may not be, so iPhone crew alerts may need a
  paid plan.

## Check after deploying
`python manage.py check --deploy --settings=config.settings.prod` should only warn about HSTS
subdomains/preload.
