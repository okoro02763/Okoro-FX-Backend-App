"""Reusable OKORO FX transactional email service.

Every logical email lives in the codebase as two Django templates -- an HTML
file and a plain-text (.txt) file -- plus a subject line registered below, so a
new notification is added without touching the sending path.

How the message is delivered depends on the configured transport:

* **Termii** (https://developers.termii.com) is a *template* email API. It does
  not accept arbitrary HTML; every send is rendered from a template created on
  the Termii dashboard, and the caller only supplies ``subject`` and a
  ``variables`` object. The renderer therefore starts from the plain-text body
  and forwards only the variables the dashboard template declares
  (``name``, ``action_url``, ``action_label`` -- see
  ``templates/emails/termii_email_template.html``, the source-of-truth mirror).
  Per-type dashboard templates can be selected with
  ``TERMII_TEMPLATE_<NAME>_ID`` (e.g. ``TERMII_TEMPLATE_WELCOME_ID``); each one
  must accept at least those placeholders.

* **Any other backend** (SMTP, console, locmem) receives the full HTML +
  plain-text pair rendered from this codebase via ``EmailMultiAlternatives``.

No real email is ever sent during automated testing: tests either override the
backend to the locmem backend or mock ``_uses_termii``/``_transport_configured``.
"""
import logging
import os
import threading

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .email_backend import TermiiEmailBackend

logger = logging.getLogger('forex.email_service')


class EmailSpec:
    """A single logical email: subject plus codebase HTML/text templates."""

    __slots__ = ('subject', 'html', 'text')

    def __init__(self, subject, html, text):
        self.subject = subject
        self.html = f'emails/{html}'
        self.text = f'emails/{text}'


# Subject lines are static. Callers may override per send with `subject=`.
EMAILS = {
    'welcome': EmailSpec(
        'Welcome to OKORO FX', 'welcome.html', 'welcome.txt'),
    'verify_email': EmailSpec(
        'Confirm your email address', 'verify_email.html', 'verify_email.txt'),
    'password_reset': EmailSpec(
        'OKORO FX - Reset your password',
        'password_reset.html', 'password_reset.txt',
    ),
    'password_changed': EmailSpec(
        'Your OKORO FX password was changed',
        'password_changed.html', 'password_changed.txt',
    ),
    'login_alert': EmailSpec(
        'New sign-in to your OKORO FX account',
        'login_alert.html', 'login_alert.txt',
    ),
    'deposit_confirmation': EmailSpec(
        'OKORO FX - Deposit confirmed',
        'deposit_confirmation.html', 'deposit_confirmation.txt',
    ),
    'withdrawal_update': EmailSpec(
        'OKORO FX - Withdrawal update',
        'withdrawal_update.html', 'withdrawal_update.txt',
    ),
    'trade_notification': EmailSpec(
        'OKORO FX - Trade notification',
        'trade_notification.html', 'trade_notification.txt',
    ),
    'admin_invite': EmailSpec(
        'You are invited to OKORO FX',
        'admin_invite.html', 'admin_invite.txt',
    ),
    'kyc_received': EmailSpec(
        'We received your documents', 'kyc_received.html', 'kyc_received.txt',
    ),
    'kyc_approved': EmailSpec(
        'Your identity is verified', 'kyc_approved.html', 'kyc_approved.txt',
    ),
    'kyc_rejected': EmailSpec(
        'Action needed on your verification',
        'kyc_rejected.html', 'kyc_rejected.txt',
    ),
}


def _frontend_url():
    return (
        getattr(settings, 'FRONTEND_URL', '')
        or os.getenv('FRONTEND_URL', 'http://localhost:3000')
    )


def _support_email():
    return (
        getattr(settings, 'DEFAULT_SUPPORT_EMAIL', '')
        or os.getenv('DEFAULT_SUPPORT_EMAIL', 'support@okorofx.com')
    )


def default_context(overrides=None):
    """Base template context. Never include secrets in here."""
    context = {
        'name': 'there',
        'action_url': None,
        'action_label': 'Continue',
        'support_email': _support_email(),
        'frontend_url': _frontend_url(),
        'brand_name': 'OKORO FX',
        'brand_tagline': 'Trade Global. Trade Smarter.',
        'brand_logo_url': os.getenv('BRAND_LOGO_URL', ''),
        'current_year': timezone.now().year,
    }
    if overrides:
        context.update(overrides)
    return context


def _uses_termii():
    """True when the current EMAIL_BACKEND is the Termii backend."""
    try:
        return isinstance(get_connection(), TermiiEmailBackend)
    except Exception:  # noqa: BLE001 - never let transport detection crash a send
        return False


def termii_template_id_for(name):
    """Resolve the Termii dashboard template for an email type.

    Precedence: TERMII_TEMPLATE_<NAME>_ID env var, then settings
    TERMII_TEMPLATE_ID. Only relevant when a Termii dashboard template is used.
    """
    env_key = 'TERMII_TEMPLATE_{}_ID'.format(name.upper().replace('-', '_'))
    return (
        os.getenv(env_key)
        or getattr(settings, 'TERMII_TEMPLATE_ID', '')
        or ''
    )


def _termii_variables(context):
    """Variables forwarded to the Termii dashboard template.

    Termii validates template placeholders on send, so this is intentionally
    restricted to the placeholders the source-of-truth dashboard template
    declares ({{name}}, {{action_url}}, {{action_label}}). Everything else --
    amounts, references, trade details -- lives inside the {{message}} text.
    """
    variables = {'name': context.get('name') or 'there'}
    url = context.get('action_url') or context.get('frontend_url')
    if url:
        variables['action_url'] = url
        variables['action_label'] = context.get('action_label') or 'Continue'
    return variables


def build_template_message(
    name,
    to_email,
    context=None,
    *,
    from_email=None,
    cc=None,
    bcc=None,
    subject=None,
    template_id=None,
):
    """Render one logical email into an ``EmailMultiAlternatives``.

    The HTML alternative is always rendered from the codebase. On the Termii
    transport the message is additionally seeded with ``termii_variables`` and
    ``termii_template_id`` so the backend can forward it to the dashboard.
    """
    if name not in EMAILS:
        raise KeyError(f'Unknown email template: {name!r}')

    spec = EMAILS[name]
    recipients = [email for email in (to_email or []) if email]
    if isinstance(to_email, str):
        recipients = [to_email] if to_email else []
    if not recipients:
        return None

    ctx = default_context(context)
    html = render_to_string(spec.html, ctx)
    body = render_to_string(spec.text, ctx).strip()

    msg = EmailMultiAlternatives(
        subject or spec.subject,
        body,
        from_email or getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        recipients,
        cc=cc,
        bcc=bcc,
    )
    msg.attach_alternative(html, 'text/html')
    msg.email_name = name

    if _uses_termii():
        msg.termii_variables = _termii_variables(ctx)
        msg.termii_template_id = template_id or termii_template_id_for(name)
    else:
        msg.termii_variables = None
        msg.termii_template_id = None

    return msg


def _transport_configured():
    """True when the environment is wired with *some* email transport."""
    return bool(
        os.getenv('EMAIL_BACKEND')
        or os.getenv('TERMII_API_KEY')
        or os.getenv('EMAIL_PASSWORD')
    )


def send_template_email(
    name,
    to_email,
    context=None,
    *,
    from_email=None,
    cc=None,
    bcc=None,
    subject=None,
    template_id=None,
    defer=False,
    background=False,
):
    """Render and send one email. Never raises on delivery failure.

    ``defer=True`` wraps the send in ``transaction.on_commit`` so a message is
    only dispatched once the surrounding database change is committed, and
    never on a rolled-back webhook retry.

    ``background=True`` dispatches the send on a daemon thread, so a slow or
    failing provider can never block or break the HTTP request that triggered
    it. The login alert (and any other low-priority notification) uses this.
    """
    if not to_email:
        return None

    def _send():
        if not _transport_configured():
            logger.info(
                'email would be sent [%s] to %s (no transport configured)',
                name,
                to_email,
            )
            return None
        msg = build_template_message(
            name,
            to_email,
            context,
            from_email=from_email,
            cc=cc,
            bcc=bcc,
            subject=subject,
            template_id=template_id,
        )
        if msg is None:
            return None
        try:
            msg.send(fail_silently=True)
        except Exception as exc:  # noqa: BLE001
            # fail_silently=True already swallows backend errors; this guard
            # covers anything raised before the backend (rendering etc.).
            logger.warning(
                'Failed to send %s email to %s: %s',
                name,
                to_email,
                exc,
            )
            return None
        return msg

    def _dispatch():
        try:
            _send()
        except Exception as exc:  # noqa: BLE001 - never propagate from a thread
            logger.warning('Background email send failed [%s]: %s', name, exc)

    if defer:
        transaction.on_commit(_send)
        return None

    if background:
        # Fire-and-forget delivery: the caller (usually an HTTP view) never
        # waits on the provider and never sees a delivery failure.
        worker = threading.Thread(
            target=_dispatch,
            name=f'forex-email-{name}',
            daemon=True,
        )
        worker.start()
        return None

    return _send()