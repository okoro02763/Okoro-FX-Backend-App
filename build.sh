#!/usr/bin/env bash
#
# Build step for the OKORO FX backend (Render, native Python runtime).
#
# The build runs *before* the service is deployed and has no guaranteed
# database access, so migrations are NOT applied here -- they run in the start
# command (Render's pre-deploy command is ignored on the free plan).
#
# What this script does check is that every model change in the repository
# already has a committed migration. That check is the one that would have
# caught the outage this file exists to prevent: the `kyc` app was deployed
# without a migrations package, so `kyc_kycprofile` was never created and
# every request to /api/account/ returned 500 with
# `relation "kyc_kycprofile" does not exist`.
#
# The check runs through `manage.py checkmigrations`, which compares the
# models against the migration files on disk *without opening a database
# connection*. Django's own `makemigrations --check --dry-run` answers the
# same question but connects to the database first, and psycopg2 has no
# connect timeout -- with an unreachable database the build would hang
# instead of failing cleanly. `checkmigrations` cannot hang.
#
# A failure here fails the build, which is the point: a release whose schema
# is behind the code must not go out.
set -euo pipefail

echo "==> Installing dependencies"
pip install -r requirements.txt

echo "==> Verifying that every model change has a committed migration"
if ! python manage.py checkmigrations; then
    echo ""
    echo "ERROR: models have changes without a committed migration."
    echo "       Run 'python manage.py makemigrations <app>' and commit the result."
    exit 1
fi

# Collect ADMIN/REST_FRAMEWORK assets so the browsable API and the admin do not
# 404 on their own stylesheets in production. Harmless if storage is already
# populated; ``--noinput`` keeps it non-interactive.
echo "==> Collecting static files"
python manage.py collectstatic --noinput --clear

echo "==> Build OK"
