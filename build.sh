#!/usr/bin/env bash
#
# Build step for the OKORO FX backend (Render, native Python runtime).
#
# The build runs *before* the service is deployed and has no database
# access, so migrations are NOT applied here -- they run in the start
# command (Render's pre-deploy command is ignored on the free plan).
#
# What this script does check is that every model change in the repository
# already has a committed migration. That check is the one that would have
# caught the outage this file exists to prevent: the `kyc` app was deployed
# without a migrations package, so `kyc_kycprofile` was never created and
# every request to /api/account/ returned 500 with
# `relation "kyc_kycprofile" does not exist`.
#
# A failure here fails the build, which is the point: a release whose schema
# is behind the code must not go out.
set -euo pipefail

echo "==> Installing dependencies"
pip install -r requirements.txt

echo "==> Verifying that every model change has a committed migration"
if ! python manage.py makemigrations --check --dry-run; then
    echo ""
    echo "ERROR: models have changes without a committed migration."
    echo "       Run 'python manage.py makemigrations <app>' and commit the result."
    exit 1
fi

echo "==> Build OK"
