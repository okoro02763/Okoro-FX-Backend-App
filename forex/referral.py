"""
Referral system helpers: code generation, referral creation and rewards.
"""

import secrets
import string
from decimal import Decimal
from .models import Profile, Referral

# Referral rewards (configurable via env later).
REFERRER_REWARD = Decimal('10.00')
REFEREE_REWARD = Decimal('5.00')


def generate_referral_code():
    """Return a unique, human-friendly referral code."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(secrets.choice(alphabet) for _ in range(8))
        if not Profile.objects.filter(referral_code=code).exists():
            return code


def ensure_referral_code(profile):
    """Assign a referral code to a profile if it doesn't have one."""
    if not profile.referral_code:
        profile.referral_code = generate_referral_code()
        profile.save()
    return profile.referral_code


def get_referrer_profile(code):
    """Return the Profile that owns ``code`` (case-insensitive), or None.

    Used both to validate an invite code before signup and to resolve the
    referrer when a referral is applied.
    """
    if not code:
        return None
    normalized = code.strip().upper()
    if not normalized:
        return None
    return (
        Profile.objects.filter(referral_code=normalized)
        .select_related('user')
        .first()
    )


def process_referral(new_user, code, reward_referrer=REFERRER_REWARD, reward_referee=REFEREE_REWARD):
    """
    Called at signup with a referral code. If the code is valid and doesn't
    belong to the user signing up, creates a Referral and credits both parties.

    Returns the created Referral or None.
    """
    referrer_profile = get_referrer_profile(code)
    if not referrer_profile:
        return None
    if referrer_profile.user_id == new_user.id:
        return None

    code = code.strip().upper()

    new_profile = _profile_for(new_user)

    referral = Referral.objects.create(
        referrer=referrer_profile.user,
        referee=new_user,
        code=code,
        referrer_reward=reward_referrer,
        referee_reward=reward_referee,
    )

    referrer_profile.reward_balance += reward_referrer
    referrer_profile.save()

    new_profile.reward_balance += reward_referee
    new_profile.reffered_by = referrer_profile.user
    if not new_profile.referral_code:
        new_profile.referral_code = generate_referral_code()
    new_profile.save()

    return referral


def _profile_for(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile
