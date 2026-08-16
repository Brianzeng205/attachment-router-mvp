from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    content: bytes
    mime_type: str | None = None


@dataclass(frozen=True)
class EmailMessage:
    id: str
    sender: str
    subject: str
    body: str
    received_at: str
    attachments: tuple[Attachment, ...]
    thread_id: str | None = None


@dataclass(frozen=True)
class Classification:
    document_type: str | None
    company_or_sender: str | None
    document_date: str | None
    reference_number: str | None
    suggested_filename: str
    target_folder: str
    confidence: float

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Classification":
        required = (
            "document_type", "company_or_sender", "document_date",
            "suggested_filename", "target_folder", "confidence",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"Missing classification fields: {', '.join(missing)}")

        for key in ("suggested_filename", "target_folder"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError(f"{key} must be a non-empty string")
        optional_text_fields = ("document_type", "company_or_sender", "document_date")
        for key in optional_text_fields:
            if value[key] is not None and (not isinstance(value[key], str) or not value[key].strip()):
                raise ValueError(f"{key} must be a non-empty string or null")
        try:
            if value["document_date"] is not None:
                date.fromisoformat(str(value["document_date"]))
            if isinstance(value["confidence"], bool):
                raise ValueError
            confidence = float(value["confidence"])
        except (TypeError, ValueError) as exc:
            raise ValueError("document_date must be ISO YYYY-MM-DD and confidence numeric") from exc
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        reference = value.get("reference_number")
        if reference is not None and not isinstance(reference, str):
            raise ValueError("reference_number must be a string or null")
        return cls(
            document_type=value["document_type"].strip() if isinstance(value["document_type"], str) else None,
            company_or_sender=value["company_or_sender"].strip() if isinstance(value["company_or_sender"], str) else None,
            document_date=value["document_date"].strip() if isinstance(value["document_date"], str) else None,
            reference_number=reference.strip() if isinstance(reference, str) else None,
            suggested_filename=str(value["suggested_filename"]).strip(),
            target_folder=str(value["target_folder"]).strip(),
            confidence=confidence,
        )
