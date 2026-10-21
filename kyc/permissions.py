"""Permission checks for live-account features."""
import logging

from django.db import DatabaseError
from rest_framework.permissions import BasePermission

from .models import KYCProfile

logger = logging.getLogger('kyc')


class IsKYCVerified(BasePermission):
    """Allow only users whose KYC status is 'verified'.

    Applied to live-account endpoints (live trades, deposits, withdrawals).
    Demo endpoints must never use this -- demo stays open to everyone.

    The check fails *closed* when the KYC tables cannot be read (for example
    when the kyc migrations have not been applied on the deployed database).
    A permission check must never raise: an unreachable table means "not
    verified", not a 500 on the funding and withdrawal endpoints. The
    underlying error is logged so it stays diagnosable.
    """

    message = 'Verify your identity to unlock live trading.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        try:
            profile = KYCProfile.objects.filter(user=user).first()
            if profile is not None and profile.status == 'verified':
                return True
            # Legacy single-document KYC (forex.IdentityVerification) still
            # counts so users approved before this app existed keep access.
            return user.identity_verifications.filter(status='approved').exists()
        except DatabaseError:
            logger.exception(
                'KYC permission check failed for user %s. '
                'Is the kyc migration applied on this database?',
                getattr(user, 'pk', None),
            )
            return False
