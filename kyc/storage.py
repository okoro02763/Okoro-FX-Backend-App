"""Private storage for KYC documents.

Uploaded IDs and selfies must never be reachable from public MEDIA. Files go
to ``KYC_PRIVATE_ROOT`` (outside MEDIA_ROOT, never mounted by the static/media
handlers) and are only served through the signed-URL view in ``kyc.views``.
"""
import os
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def kyc_upload_path(instance, filename: str) -> str:
    """Unique, non-guessable path per document; the original name is dropped
    so a filename can never leak a user's identity on disk."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.pdf'}:
        ext = '.bin'
    return f'{instance.profile.user_id}/{uuid.uuid4().hex}{ext}'


class PrivateKYCStorage(FileSystemStorage):
    """FileSystemStorage rooted outside MEDIA_ROOT."""

    def __init__(self, location=None, base_url=None):
        super().__init__(
            location=location or settings.KYC_PRIVATE_ROOT,
            # No base_url: these files have no public URL at all.
            base_url=base_url,
        )
