"""Django email backend that delivers mail through Termii.

Termii (https://developers.termii.com) does not expose an SMTP-style "send
arbitrary subject + body" endpoint. Every message is rendered from a template
and email configuration created on the Termii dashboard, and the caller only
supplies the destination, the subject and a ``variables`` object that fills the
template's placeholders.

To stay a drop-in Django backend, this class maps a standard ``EmailMessage``
onto that API:

* ``message.subject`` is forwarded as the Termii ``subject``.
* ``message.body`` is injected under ``TERMII_BODY_VARIABLE`` (default
  ``message``) inside ``variables`` -- so the dashboard template must contain
  that placeholder, e.g. ``{{message}}``.
* Any extra template variables can be attached per message as
  ``message.termii_variables = {...}``.

Configure via the environment (see ENVIRONMENT.md)::

    EMAIL_BACKEND=forex.email_backend.TermiiEmailBackend
    TERMII_API_KEY=...
    TERMII_EMAIL_CONFIG_ID=...
    TERMII_TEMPLATE_ID=...
"""
import logging

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


def _notify_email_failure(status) -> None:
    """Send one best-effort warning per hour when the email provider fails.

    No recipient address, message body, subject or provider response body is
    included -- only the provider name and a coarse status.
    """
    try:
        from monitoring.activity import send_activity

        send_activity(
            f'Email provider failure | provider: termii | status: {status}',
            level='warning',
            dedupe_key='email-provider-failed',
            cooldown_seconds=3600,
            fields={'provider': 'termii', 'status': status},
        )
    except Exception:  # noqa: BLE001 - alerting is best-effort
        pass


class TermiiEmailError(Exception):
    """Raised when the Termii API rejects or fails to deliver an email."""


class TermiiEmailBackend(BaseEmailBackend):
    """Send Django email messages through Termii's template email endpoint."""

    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = getattr(settings, 'TERMII_API_KEY', '') or ''
        self.base_url = (
            getattr(settings, 'TERMII_BASE_URL', 'https://v4.api.termii.com')
            or 'https://v4.api.termii.com'
        ).rstrip('/')
        self.email_configuration_id = (
            getattr(settings, 'TERMII_EMAIL_CONFIG_ID', '') or ''
        )
        self.template_id = getattr(settings, 'TERMII_TEMPLATE_ID', '') or ''
        self.body_variable = (
            getattr(settings, 'TERMII_BODY_VARIABLE', 'message') or 'message'
        )
        # A provider call that hangs is worse than no call at all: the email is
        # fire-and-forget, so a slow Termii must not hold the request open.
        self.timeout = float(
            getattr(settings, 'TERMII_TIMEOUT', None)
            or getattr(settings, 'EMAIL_TIMEOUT', 5)
            or 5
        )

    def send_messages(self, email_messages):
        """Send one or more EmailMessage objects; returns the number sent."""
        if not email_messages:
            return 0
        if not self.api_key:
            if self.fail_silently:
                return 0
            raise ImproperlyConfigured('TERMII_API_KEY must be set to send email.')

        sent = 0
        for message in email_messages:
            sent += self._send_message(message)
        return sent

    def _send_message(self, message):
        recipients = [addr for addr in (message.to or []) if addr]
        if not recipients:
            return 0

        # A message may target a different Termii dashboard template (e.g. when
        # the account has created dedicated templates per email type). Falls
        # back to the global TERMII_TEMPLATE_ID.
        template_id = (
            getattr(message, 'termii_template_id', None)
            or self.template_id
        )

        variables = {self.body_variable: message.body}
        extra = getattr(message, 'termii_variables', None)
        if isinstance(extra, dict):
            variables.update(extra)

        sent = 0
        for recipient in recipients:
            if self._post(recipient, message.subject, variables, template_id):
                sent += 1
        return sent

    def _post(self, recipient, subject, variables, template_id):
        payload = {
            'api_key': self.api_key,
            'email': recipient,
            'subject': subject,
            'email_configuration_id': self.email_configuration_id,
            'template_id': template_id,
            'variables': variables,
        }
        try:
            response = requests.post(
                f'{self.base_url}/api/templates/send-email',
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            # A dead, slow or out-of-credit provider silently stops all mail, so
            # it is worth one warning per hour. The recipient address and the
            # provider response body are never included.
            status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
            _notify_email_failure(status_code or 'unavailable')
            if self.fail_silently:
                # One warning per failed delivery attempt; never raise into the
                # (fire-and-forget) caller, and never log recipient secrets.
                logger.warning('Termii email to %s failed: %s', recipient, exc)
                return False
            raise TermiiEmailError(
                f'Termii request for {recipient} failed: {exc}'
            ) from exc

        if str(data.get('code', '')).lower() != 'ok':
            _notify_email_failure('rejected')
            detail = data.get('message') or data
            if self.fail_silently:
                logger.warning(
                    'Termii rejected email to %s: %s', recipient, detail
                )
                return False
            raise TermiiEmailError(
                f'Termii rejected email to {recipient}: {detail}'
            )
        return True
