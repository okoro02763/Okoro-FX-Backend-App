"""Environment variables required in production.

When DEBUG is off, backend/settings.py refuses to start unless SECRET_KEY,
DJANGO_ALLOWED_HOSTS, DATABASE_URL and a live PAYSTACK_SECRET_KEY are all
present. This is the same list, kept in one place to make deployment setup
unambiguous.

Required:
    SECRET_KEY           python -c "import secrets; print(secrets.token_urlsafe(64))"
    DJANGO_ALLOWED_HOSTS extra hostnames to accept, e.g. api.example.com
                          (okoro-fx-backend-app.onrender.com is always included,
                          and RENDER_EXTERNAL_HOSTNAME is added automatically
                          when Render sets it)
    DATABASE_URL         postgres://user:pass@host:5432/dbname
    PAYSTACK_SECRET_KEY  live secret (sk_live_...); test keys are rejected

Strongly recommended:
    CORS_ALLOWED_ORIGINS comma-separated frontend origins.
                          Default: https://okorofx.netlify.app,
                          https://forex-trader-frontend.vercel.app,
                          http://localhost:3000, http://localhost:5173
    CSRF_TRUSTED_ORIGINS same origins, needed for session-authenticated calls.
                          Defaults to the CORS list above.
    FRONTEND_URL         used to build password-reset and invite links
    EMAIL_HOST / EMAIL_USER / EMAIL_PASSWORD / EMAIL_GENERAL
    TERMII_API_KEY       enables the Termii email backend (takes priority
                         over SMTP). Requires TERMII_EMAIL_CONFIG_ID and
                         TERMII_TEMPLATE_ID from the Termii dashboard; the
                         template must include the {{message}} placeholder
                         (override the name with TERMII_BODY_VARIABLE).
                         Optional: TERMII_BASE_URL (default
                         https://v4.api.termii.com), TERMII_TIMEOUT.
    DERIV_API_TOKEN / DERIV_APP_ID
    THROTTLE_ANON        e.g. 60/hour
    THROTTLE_USER        e.g. 600/hour

Optional:
    SECURE_SSL_REDIRECT  set to false only behind a TLS-terminating proxy
    SECURE_HSTS_SECONDS  defaults to 31536000

Never set DEBUG=True in production.

CORS notes:
    CorsMiddleware must stay first in MIDDLEWARE. Below CommonMiddleware the
    preflight short-circuits before any CORS header is attached and the
    browser reports "No 'Access-Control-Allow-Origin' header".
    CORS_ALLOW_ALL_ORIGINS is intentionally never enabled; list origins
    explicitly instead.
    API routes accept the path with or without a trailing slash and
    APPEND_SLASH is disabled, so the frontend's slash-less calls are not
    answered with a 301, which a browser rejects on a preflight request.
"""
