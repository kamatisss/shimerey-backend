# Deploying the backend

**Render** (Singapore) + **Neon** Postgres (Singapore) + **Cloudflare R2** for 3D models *and* photos.
The frontend stays on Vercel. The PythonAnywhere guide further down is for the old server, until the
move is finished.

Render's disk is wiped on every deploy, so nothing may live on it: the database is Neon, uploads are
R2, and `REQUIRE_CLOUD_STORAGE=true` makes the server refuse to start if R2 isn't configured.

## R. Render

### R1. Neon (database)
1. neon.tech → New project, Postgres 17, region **AWS Asia Pacific (Singapore)**.
2. Dashboard → **Connect** → turn **Connection pooling off** → copy the connection string
   (`postgresql://…@ep-….ap-southeast-1.aws.neon.tech/neondb?sslmode=require`). That is `DATABASE_URL`.
   Use the direct one: Django keeps its own connections open, and the pooled one breaks some queries.
3. Backups: Neon keeps a restore window (point-in-time restore, "Restore" / branches). The daily task
   no longer copies the database (it says "Backup: skipped (postgresql…)").

### R2. Cloudflare R2: a second bucket for photos
3D models keep their bucket (section "Cloudflare R2" below). Photos and documents get their own:
1. R2 → **Create bucket** `garden-studio-media` → Settings → Public access → **R2.dev subdomain → Allow**.
   Copy the `https://pub-….r2.dev` address (`R2_MEDIA_PUBLIC_URL`).
2. Settings → CORS policy:
   ```json
   [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"], "MaxAgeSeconds": 86400}]
   ```
   `*` on purpose: the 3D view loads depth maps through the API's `/media/…` redirect, and browsers send
   `Origin: null` after a cross-site redirect. The bucket is public and read-only to visitors anyway.
3. R2 → Manage API tokens → edit the existing token (or make a new one): **Object Read & Write** on
   **both** buckets. Its keys go in `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`.
4. Later, for production traffic: connect your own domain to each bucket (Cloudflare recommends that
   over r2.dev, which is rate-limited) and update the two `*_PUBLIC_URL` settings.

### R3. Render services
1. render.com → **New → Blueprint** → this repo. `render.yaml` creates:
   - `garden-studio-api`: web service, Starter, Singapore. Every deploy runs `build.sh`: install,
     `collectstatic`, `migrate` (a failing step stops the deploy); health check `/api/business/`.
   - `garden-studio-daily`: cron job, 18:00 UTC (2 AM Manila), `manage.py daily_maintenance`
     (expires unpaid orders, clears old retry keys).
2. Fill in the values it asks for (each service has the same list):
   - **Copy from the old server unchanged:** `DJANGO_SECRET_KEY` (keeps everyone signed in),
     `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT` (keeps crew phone alerts working),
     `PAYMONGO_SECRET_KEY`, `GEMINI_API_KEY`, the email settings, all `R2_*` model settings.
   - **New:** `DATABASE_URL` (R1), `R2_MEDIA_BUCKET` + `R2_MEDIA_PUBLIC_URL` (R2),
     `ALLOWED_HOSTS=garden-studio-api.onrender.com`, `FRONTEND_URL=https://shimereyslandscaping.vercel.app`.
   - `PAYMONGO_WEBHOOK_SECRET`: from the new webhook (step R4.6).
3. Check: `https://garden-studio-api.onrender.com/api/business/` shows JSON, `/admin/` has its styling.

### R4. Moving the data (switch-over day, ~30–60 min, a quiet hour)
Rehearse it first against an empty Neon branch: same steps, without 5–7.
1. Old server: `python manage.py backup_db --settings=config.settings.prod`. Download `db.sqlite3` and
   zip + download `media/`. From now on nobody should place orders or upload (quiet hour).
2. On your computer, export the data from that file:
   ```bash
   DATABASE_PATH=path/to/db.sqlite3 python -X utf8 manage.py dumpdata --natural-foreign --natural-primary \
       -e contenttypes -e auth.permission -e sessions -o dump.json --settings=config.settings.dev
   ```
3. Load it into Neon (the empty tables were made by Render's `migrate`), then compare every table:
   ```bash
   DATABASE_URL=<neon> python manage.py loaddata dump.json --settings=config.settings.dev
   DATABASE_URL=<neon> python manage.py compare_row_counts path/to/db.sqlite3 --settings=config.settings.dev
   ```
   Stop here if it reports any difference.
4. Files to R2, then old full links → R2. These need the production settings: in that terminal, set
   the same values as Render's (`DJANGO_SECRET_KEY`, `ALLOWED_HOSTS`, `FRONTEND_URL`, `DATABASE_URL`, all
   `R2_*`) as environment variables. They win over your `.env`, so leave that file as it is. Both
   dry-run first; add `--apply` once the list looks right:
   ```bash
   python manage.py copy_media_to_r2 --source path/to/media --settings=config.settings.prod
   python manage.py rewrite_old_media_urls --old https://shimereyslandscaping.pythonanywhere.com/media/ --settings=config.settings.prod
   ```
   Short `/media/…` links need nothing: the new server redirects them to R2.
5. PayMongo → Webhooks → add `https://garden-studio-api.onrender.com/api/paymongo-webhook/`, event
   `checkout_session.payment.paid`; put its secret in Render's `PAYMONGO_WEBHOOK_SECRET`. Disable the old one.
6. Vercel → `VITE_API_BASE_URL = https://garden-studio-api.onrender.com/api` → redeploy the **newest**
   deployment.
7. Smoke test: sign in; a test payment (receipt page + webhook marks it paid); upload a photo and a 3D
   model; a crew alert reaches a phone; a password-reset email arrives; Render → cron → **Trigger Run**.
8. **Check the login limit:** type a wrong password 11 times on Wi-Fi until it says to wait, then try
   once on mobile data. If mobile data is blocked too, every visitor shares one limit: change
   `NUM_PROXIES` (try 2), redeploy, repeat.
9. Keep PythonAnywhere for 48 hours. Rolling back = set Vercel's `VITE_API_BASE_URL` back. Anything
   written on Render in between would have to be copied back by hand, so decide quickly.

### R5. Updates
`git push` → Render builds, runs `migrate`, then switches over with no downtime. Nothing else to do.

---

# Old server: PythonAnywhere (until the move is finished)

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

3D models the office uploads on the Inventory page go to Cloudflare R2 when the `R2_*` settings are
filled in (see "Cloudflare R2" below). Without them they're saved in `media/models/` (5 MB each, 150 MB
in all) and served by Django at `/api/model-files/<name>`, not by the `/media/` mapping: the 3D viewer
fetches them from the Vercel site, which needs CORS headers the static mapping doesn't send. Back up
`media/` with the database.

### Cloudflare R2 (uploaded 3D models)
The browser uploads each .glb straight to R2 (15 MB each, 8 GB in all; R2's free plan holds 10 GB),
so uploads use no PythonAnywhere disk and don't tie up its single web worker.
1. dash.cloudflare.com → **R2 Object Storage** (activating R2 asks for a card; the free allowance isn't charged).
2. **Create bucket**, e.g. `garden-studio-models`, location Automatic.
3. Bucket → **Settings → Public access → R2.dev subdomain → Allow**. Copy the `https://pub-….r2.dev` address.
4. Bucket → **Settings → CORS policy → Add** and paste (your real site address in place of the Vercel one):
   ```json
   [{"AllowedOrigins": ["https://shimereyslandscaping.vercel.app", "http://localhost:5173"],
     "AllowedMethods": ["GET", "HEAD", "PUT"], "AllowedHeaders": ["content-type"], "MaxAgeSeconds": 3600}]
   ```
5. R2 overview → **Manage API tokens → Create API token**: permission **Object Read & Write**, applied to
   that bucket only. Copy the **Access Key ID** and **Secret Access Key** (the secret is shown once).
   The **Account ID** is on the R2 overview page.
6. Add to `.env`, then Reload the web app:
   ```
   R2_ACCOUNT_ID=...
   R2_ACCESS_KEY_ID=...
   R2_SECRET_ACCESS_KEY=...
   R2_BUCKET=garden-studio-models
   R2_PUBLIC_URL=https://pub-....r2.dev
   ```
7. Check: Inventory → Add item → 3D model → Choose. The footer reads "… of 8.0 GB used".

After the upload, the server downloads the file back from R2 to check it. PythonAnywhere's free plan
only reaches whitelisted sites; if R2 isn't one, the error log shows "R2 not reachable" and uploads are
kept on the browser's checks alone, and deleting a model leaves its file in R2 (unused, harmless).
A paid PythonAnywhere plan removes that limit. Models uploaded before R2 was set up stay on this server.

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
