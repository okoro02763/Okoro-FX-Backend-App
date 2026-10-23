"""Redaction of personal and secret data before anything leaves the server.

This application handles identity documents (NIN, BVN, ID numbers), so an
alert is only useful if it can be sent without leaking what it is about. The
scrubber runs twice:

* on every string that goes into a Discord embed (titles, field names/values,
  description), and
* on the final payload structure, where any key that *looks* sensitive has its
  whole value replaced.

The rules are deliberately blunt -- over-redacting a stack trace is harmless;
leaking a document number is not.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = '[REDACTED]'
REDACTED_EMAIL = '[REDACTED_EMAIL]'
REDACTED_PHONE = '[REDACTED_PHONE]'
REDACTED_NUMBER = '[REDACTED_NUMBER]'
REDACTED_KEY = '[REDACTED_KEY]'
REDACTED_WEBHOOK = '[REDACTED_WEBHOOK]'

# A dict key matching this has its entire value removed. "number" intentionally
# matches document_number, account_number, card_number, phone_number, ...
_SENSITIVE_KEY = re.compile(
    r'(pass(?:word|wd|phrase)?|token|secret|authorization|cookie|'
    r'number|nin|bvn|document|email|phone|otp|pin|api[_-]?key)',
    re.IGNORECASE,
)

# Discord webhook URLs themselves must never appear inside an alert.
_WEBHOOK_URL = re.compile(
    r'https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\S+',
    re.IGNORECASE,
)

# "Authorization: Bearer abc123", "Bearer abc123".
_BEARER = re.compile(r'\bBearer\s+[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE)

# "NIN 12345678901", "password = hunter2", "token: abc", ...
_KEYED_SECRET = re.compile(
    r'\b('
    r'pass(?:word|wd|phrase)?|token|secret|authorization|cookie|'
    r'nin|bvn|phone(?:\s*number)?|email(?:\s*address)?|'
    r'document(?:\s*(?:number|no\.?|id))?'
    r')\b\s*[:=#]?\s*([^\s,;]+)',
    re.IGNORECASE,
)

_EMAIL = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)

# Nigerian mobile numbers in local (080...) or international (+234...) form.
_PHONE = re.compile(r'(?<!\d)(?:\+?234|0)[789]\d{9}(?!\d)')

# Standalone long digit runs (NIN is 11, BVN is 11, card numbers are 16).
_LONG_NUMBER = re.compile(r'(?<!\d)\d{11,}(?!\d)')

_API_KEY = re.compile(
    r'\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]+|'
    r'\bwhsec_[A-Za-z0-9]+|'
    r'\bAIza[0-9A-Za-z\-_]{20,}',
)

# Long opaque values: reset tokens, session ids, base64 blobs. Kept at 24+ so
# ordinary words and file names survive.
_LONG_TOKEN = re.compile(r'\b[A-Za-z0-9_\-]{24,}\b')


def scrub_text(value: Any) -> str:
    """Redact secrets, document numbers and contact details from a string."""
    if value is None:
        return ''
    text = str(value)

    text = _WEBHOOK_URL.sub(REDACTED_WEBHOOK, text)
    text = _BEARER.sub('Bearer ' + REDACTED, text)
    # Keyed values first, so "NIN 12345678901" becomes "NIN [REDACTED]"
    # rather than "NIN [REDACTED_NUMBER]" (either is safe; the first reads
    # better in an alert).
    text = _KEYED_SECRET.sub(lambda m: f'{m.group(1)} {REDACTED}', text)
    text = _EMAIL.sub(REDACTED_EMAIL, text)
    text = _PHONE.sub(REDACTED_PHONE, text)
    text = _LONG_NUMBER.sub(REDACTED_NUMBER, text)
    text = _API_KEY.sub(REDACTED_KEY, text)
    text = _LONG_TOKEN.sub(REDACTED_KEY, text)
    return text


def scrub_value(value: Any, depth: int = 0) -> Any:
    """Recursively scrub a JSON-like value.

    Sensitive keys lose their value entirely; everything else is passed
    through :func:`scrub_text`.
    """
    if depth > 8:
        return '[TRUNCATED]'
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = scrub_value(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub_value(item, depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_payload(payload: Any) -> Any:
    """Scrub a complete Discord payload before it is posted."""
    return scrub_value(payload)


def is_sensitive_key(key: str) -> bool:
    """Exposed for tests/documentation: whether a key is treated as secret."""
    return bool(_SENSITIVE_KEY.search(key or ''))
