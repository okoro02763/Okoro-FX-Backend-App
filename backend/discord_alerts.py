"""Compatibility facade for the requested prompt.

The repo uses monitoring/discord + monitoring/activity. This module exposes
send_activity(message, level) and references DiscordHandler (aliased).
"""

from __future__ import annotations

from monitoring.activity import send_activity, send_activity_on_commit, format_activity, _hash_ip  # noqa: F401
from monitoring.handlers import DiscordLogHandler as DiscordHandler  # noqa: F401

__all__ = ['send_activity', 'send_activity_on_commit', 'format_activity', 'DiscordHandler', '_hash_ip']
