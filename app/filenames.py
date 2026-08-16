from __future__ import annotations

import re

from .errors import InvalidFilenameError

MAX_FILENAME_LENGTH = 180
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(suggested_filename: str, original_filename: str) -> str:
    """Produce a safe Drive name while retaining the source file extension."""
    if not isinstance(suggested_filename, str) or not isinstance(original_filename, str):
        raise InvalidFilenameError("Filename values must be strings")

    requested = _clean(suggested_filename)
    original = _clean(original_filename)
    original_base, original_extension = _split_extension(original)
    # Only remove the extension we expect from the source attachment. A dot in
    # arbitrary classifier text is otherwise part of its sanitized base name.
    requested_base = requested
    if original_extension and requested.lower().endswith(original_extension.lower()):
        requested_base = requested[: -len(original_extension)]
    base = requested_base or original_base or "attachment"
    extension = original_extension
    allowed_base_length = MAX_FILENAME_LENGTH - len(extension)
    if allowed_base_length < 1:
        raise InvalidFilenameError("Original file extension is unreasonably long")
    result = base[:allowed_base_length].rstrip(". ") + extension
    if not result or result in {".", ".."}:
        raise InvalidFilenameError("Filename is empty after sanitization")
    return result


def validate_upload_filename(filename: str) -> None:
    """Defence in depth: Drive clients reject unsanitized names."""
    if not isinstance(filename, str) or not filename or len(filename) > MAX_FILENAME_LENGTH:
        raise InvalidFilenameError("Filename must be non-empty and at most 180 characters")
    if filename.strip(". ") == "" or _INVALID_CHARS.search(filename):
        raise InvalidFilenameError("Filename contains invalid path characters")


def _clean(name: str) -> str:
    return _INVALID_CHARS.sub("_", name).strip().strip(". ")


def _split_extension(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0 or dot == len(name) - 1:
        return name, ""
    return name[:dot], name[dot:]
