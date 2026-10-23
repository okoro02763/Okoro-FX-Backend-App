"""Django signals that feed the activity channel.

Only the Django admin login is monitored here: API logins use bearer tokens
and never send ``user_logged_in``. User id only -- never a username, email or
IP address.
"""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver


@receiver(user_logged_in, dispatch_uid='monitoring.admin_login')
def _on_admin_login(sender, request=None, user=None, **kwargs):  # noqa: ANN001
    try:
        path = (getattr(request, 'path', '') or '')
        if not path.startswith('/admin/'):
            return
        from .activity import send_activity

        send_activity(
            f'Django admin login | user_id: {getattr(user, "pk", None)}',
            level='warning',
            user_id=getattr(user, 'pk', None),
            dedupe_key=f'admin-login:{getattr(user, "pk", None)}',
            cooldown_seconds=60,
        )
    except Exception:  # noqa: BLE001 - a signal handler must never break login
        pass
