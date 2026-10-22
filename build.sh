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

# ---------------------------------------------------------------------------
# Configuration preflight
#
# This runs before any manage.py command, because Django's settings module
# refuses to import without KYC_ENCRYPTION_KEY in production and the
# resulting traceback is a lot less helpful than the message below.
#
# Failing the *build* rather than the deploy is deliberate: the currently
# running release keeps serving traffic, so a missing secret can never take
# the site down.
# ---------------------------------------------------------------------------
if [ "${DEBUG:-False}" = "False" ] || [ "${DEBUG:-False}" = "false" ]; then
    if [ -z "${KYC_ENCRYPTION_KEY:-}" ]; then
        echo ""
        echo "ERROR: KYC_ENCRYPTION_KEY is not set."
        echo "       KYC document numbers (NIN, BVN, ID numbers) are encrypted with"
        echo "       this key. Without it the app refuses to start, because it would"
        echo "       otherwise have to fall back to a key derived from SECRET_KEY."
        echo ""
        echo "       Generate one with:"
        echo "         python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        echo ""
        echo "       Then add it to the service environment (Dashboard -> Environment)"
        echo "       and deploy again. Nothing has been changed by this failed build."
        exit 1
    fi
    echo "==> KYC_ENCRYPTION_KEY is set"
fi

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
