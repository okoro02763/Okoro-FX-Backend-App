"""KYC models: one profile per user plus their uploaded documents.

Security posture (NDPA):
* Document numbers are encrypted at rest (see ``kyc.crypto``) and only ever
  returned masked.
* Files live in private storage (see ``kyc.storage``) and are served through
  short-lived signed URLs minted for staff only.
* Nothing here logs or returns a plaintext document number.
"""
from django.conf import settings
from django.db import models

from .storage import PrivateKYCStorage, kyc_upload_path


class KYCProfile(models.Model):
    """Identity-verification state for one user. Created lazily."""

    STATUS_CHOICES = [
        ('none', 'Not started'),
        ('pending', 'Under review'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='kyc_profile',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='none')

    # Step 1: personal details (a draft until submitted).
    full_name = models.CharField(max_length=150, blank=True, default='')
    date_of_birth = models.DateField(null=True, blank=True)
    country = models.CharField(max_length=80, blank=True, default='')
    phone = models.CharField(max_length=40, blank=True, default='')
    address_line = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    postal_code = models.CharField(max_length=20, blank=True, default='')

    # Step 2: identity document metadata (the number is encrypted at rest).
    doc_type = models.CharField(max_length=30, blank=True, default='')
    doc_number_encrypted = models.TextField(blank=True, default='')

    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='kyc_reviews_made',
    )
    rejection_reason = models.TextField(blank=True, default='')

    # Resubmission limiter: (window_start ISO, count) pairs are pruned on use.
    resubmit_count = models.IntegerField(default=0)
    resubmit_window_start = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'KYC profile'
        verbose_name_plural = 'KYC profiles'

    def __str__(self):
        return f'{self.user} - {self.status}'


class KYCDocument(models.Model):
    """One uploaded file (ID front/back or selfie) belonging to a profile."""

    KIND_CHOICES = [
        ('id_front', 'ID front'),
        ('id_back', 'ID back'),
        ('selfie', 'Selfie'),
    ]

    profile = models.ForeignKey(
        KYCProfile,
        on_delete=models.CASCADE,
        related_name='documents',
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    doc_type = models.CharField(max_length=30, blank=True, default='')
    file = models.FileField(
        storage=PrivateKYCStorage(),
        upload_to=kyc_upload_path,
    )
    content_type = models.CharField(max_length=60, blank=True, default='')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['kind', '-uploaded_at']
        constraints = [
            # At most one document per slot; re-uploading replaces it.
            models.UniqueConstraint(
                fields=['profile', 'kind'],
                name='unique_kyc_document_per_slot',
            ),
        ]

    def __str__(self):
        return f'{self.profile.user} - {self.get_kind_display()}'
