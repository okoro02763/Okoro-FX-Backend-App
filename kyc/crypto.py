"""Encryption-at-rest for KYC document numbers (NIN, BVN, ID numbers).

Nigeria's data-protection law (NDPA) requires that identifiers collected for
KYC are stored securely. Every document number is encrypted with Fernet
(AES-128-CBC + HMAC) before it touches the database, and the plaintext only
ever exists inside this module and the request that submitted it.

The key comes from the ``KYC_ENCRYPTION_KEY`` environment variable. In
development, a deterministic key is derived from SECRET_KEY so the flow works
with zero setup -- production refuses to run without an explicit key.

Nothing in this module ever logs a plaintext value or a key.
"""
import base64
import hashlib
import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger('kyc.crypto')

# Memoised Fernet instance; the key never changes within a process.
_lock = threading.Lock()
_fernet: Fernet | None = None


class KycCryptoError(Exception):
    """Raised when a stored value cannot be decrypted (key rotated/lost)."""


def _build_fernet() -> Fernet:
    from django.conf import settings

    key = os.getenv('KYC_ENCRYPTION_KEY', '').strip()
    if key:
        return Fernet(key.encode('utf-8'))

    # Development fallback: derive a stable key from SECRET_KEY. This is still
    # real encryption at rest (the DB alone cannot be decrypted), but the key
    # lives next to the app secret, so production must set KYC_ENCRYPTION_KEY.
    if not settings.DEBUG:
        raise KycCryptoError(
            'KYC_ENCRYPTION_KEY must be set in production. '
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    digest = hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def get_fernet() -> Fernet:
    """Return the memoised Fernet instance, building it on first use."""
    global _fernet
    with _lock:
        if _fernet is None:
            _fernet = _build_fernet()
        return _fernet


def encrypt_value(plaintext: str) -> str:
    """Encrypt a document number for storage. Never logs the value."""
    if not plaintext:
        return ''
    return get_fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_value(token: str) -> str:
    """Reverse of :func:`encrypt_value`. Used only by review tooling."""
    if not token:
        return ''
    try:
        return get_fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        # Do not include the ciphertext in the message.
        raise KycCryptoError('Stored value could not be decrypted.') from exc


def mask_value(plaintext: str) -> str:
    """Return a masked preview: last 4 characters only (e.g. '••••6789')."""
    if not plaintext:
        return ''
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return f'••••{tail}'
