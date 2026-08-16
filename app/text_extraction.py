from __future__ import annotations

from io import BytesIO

from .models import Attachment

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
TEXT_MIME_TYPES = {"text/plain", "text/csv", "text/markdown"}
TEXT_SUFFIXES = (".txt", ".csv", ".md")


def extract_attachment_text(attachment: Attachment, max_chars: int) -> str | None:
    """Best-effort, bounded text extraction. Never raises for bad input files."""
    if not attachment.content or len(attachment.content) > MAX_ATTACHMENT_BYTES:
        return None
    try:
        name = attachment.filename.lower()
        mime_type = (attachment.mime_type or "").lower().split(";", 1)[0]
        if mime_type in TEXT_MIME_TYPES or name.endswith(TEXT_SUFFIXES):
            return attachment.content.decode("utf-8", errors="replace")[:max_chars]
        if mime_type == "application/pdf" or name.endswith(".pdf"):
            return _extract_pdf_text(attachment.content, max_chars)
    except Exception:
        return None
    return None


def _extract_pdf_text(content: bytes, max_chars: int) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        chunks: list[str] = []
        remaining = max_chars
        for page in reader.pages:
            if remaining <= 0:
                break
            text = page.extract_text() or ""
            chunks.append(text[:remaining])
            remaining -= len(text)
        extracted = "".join(chunks).strip()
        return extracted or None
    except Exception:
        return None
