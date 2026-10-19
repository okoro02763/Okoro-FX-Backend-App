from rest_framework.permissions import BasePermission

from .models import KYCProfile


class IsKYCVerified(BasePermission):
    """Allow only users whose KYC status is 'verified'.

    Applied to live-account endpoints (live trades, deposits, withdrawals).
    Demo endpoints must never use this -- demo stays open to everyone.
    """

    message = 'Verify your identity to unlock live trading.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        profile = KYCProfile.objects.filter(user=user).first()
        if profile is not None and profile.status == 'verified':
            return True
        # Legacy single-document KYC (forex.IdentityVerification) still counts
        # so users approved before this app existed keep their access.
        return user.identity_verifications.filter(status='approved').exists()
