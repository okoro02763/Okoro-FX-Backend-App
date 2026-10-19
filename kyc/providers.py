"""Pluggable KYC review providers.

The default review path is manual (a staff member approves/rejects in the
Django admin). This interface exists so an automated provider -- Smile Identity
is the strongest option for Nigeria, but Onfido/Veriff also cover it -- can be
dropped in later without touching the views, models or admin.

To integrate a provider later:
  1. Subclass ``KYCProvider`` and implement ``submit_for_review``.
  2. Set ``KYC_PROVIDER`` in the environment to your provider's key.
  3. The provider calls back into ``kyc.services.approve_profile`` /
     ``kyc.services.reject_profile`` (or the admin does) -- those stay the
     single place the status transition happens.

No provider is integrated yet: ManualProvider simply records that a human
review is pending.
"""
from django.conf import settings


class KYCProvider:
    """Interface for KYC review backends."""

    name = 'base'

    def submit_for_review(self, profile) -> None:
        """Called after a submission is validated and stored.

        Implementations may kick off an asynchronous check. They must not
        change ``profile.status`` directly -- report the result through
        ``kyc.services.approve_profile`` / ``reject_profile`` (or leave the
        manual admin flow to decide).
        """
        raise NotImplementedError


class ManualProvider(KYCProvider):
    """Default: no automation, a staff member reviews in the Django admin."""

    name = 'manual'

    def submit_for_review(self, profile) -> None:
        # Nothing to do -- the profile stays 'pending' until an admin acts.
        return None


def get_provider() -> KYCProvider:
    """Return the configured provider. Unknown keys fall back to manual."""
    key = getattr(settings, 'KYC_PROVIDER', 'manual').strip().lower()
    registry = {
        'manual': ManualProvider,
    }
    provider_cls = registry.get(key, ManualProvider)
    return provider_cls()
