"""Content-based validation for uploaded KYC files.

The extension is attacker-controlled, so the type is decided by the magic
bytes of the content itself. Only JPEG, PNG and PDF are accepted, and only up
to :data:`MAX_UPLOAD_BYTES`.
"""

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

# magic prefix -> (label, content type)
_MAGIC_PREFIXES = {
    b'\xff\xd8\xff': ('image/jpeg', 'image/jpeg'),
    b'\x89PNG\r\n\x1a\n': ('PNG image', 'image/png'),
    b'%PDF-': ('PDF document', 'application/pdf'),
}


class FileValidationError(Exception):
    """Raised when an upload fails content validation. Message is user-safe."""


def sniff_content_type(uploaded_file) -> str:
    """Return the detected content type, or raise FileValidationError.

    Reads only the first bytes; the file pointer is restored afterwards so the
    storage layer still writes the complete file.
    """
    if uploaded_file is None:
        raise FileValidationError('No file was provided.')

    size = getattr(uploaded_file, 'size', None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise FileValidationError('File is too large. The maximum size is 5 MB.')
    if size == 0:
        raise FileValidationError('The file is empty.')

    pos = uploaded_file.tell() if hasattr(uploaded_file, 'tell') else 0
    try:
        header = uploaded_file.read(16)
    finally:
        if hasattr(uploaded_file, 'seek'):
            uploaded_file.seek(pos)

    if not header:
        raise FileValidationError('The file is empty.')

    for prefix, (_label, content_type) in _MAGIC_PREFIXES.items():
        if header.startswith(prefix):
            return content_type

    raise FileValidationError(
        'Only JPG, PNG or PDF files are accepted.'
    )
