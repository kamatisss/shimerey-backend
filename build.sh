#!/usr/bin/env bash
# Render build (render.yaml → buildCommand: bash build.sh). Runs on every deploy, with the service's
# environment variables (DATABASE_URL, R2_*, …) available. Stops at the first failing step, so a
# broken migration never goes live.
set -o errexit
set -o pipefail
set -o nounset

# --no-cache-dir: Render's build cache already keeps the downloaded wheels; pip's own cache only adds size
pip install --no-cache-dir -r requirements.txt

# Django admin styles → staticfiles/, served by WhiteNoise (config/settings/prod.py)
python manage.py collectstatic --noinput

# Bring the Postgres schema (Neon, DATABASE_URL) up to date before the new code takes traffic
python manage.py migrate --noinput
