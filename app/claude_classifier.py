from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import Settings
from .errors import (
    ClassifierAPIError,
    ClassifierAuthenticationError,
    ClassifierError,
    ClassifierRateLimitError,
    ClassifierResponseError,
)
from .models import Attachment, Classification, EmailMessage
from .text_extraction import extract_attachment_text

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "classify_document.txt"
CLASSIFICATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_type": {"type": ["string", "null"]},
        "company_or_sender": {"type": ["string", "null"]},
        "document_date": {"type": ["string", "null"]},
        "reference_number": {"type": ["string", "null"]},
        "suggested_filename": {"type": "string"},
        "target_folder": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "document_type", "company_or_sender", "document_date", "reference_number",
        "suggested_filename", "target_folder", "confidence",
    ],
}


class ClaudeDocumentClassifier:
    """Anthropic adapter that returns validated logical routing data only."""

    def __init__(
        self, client: Any, model: str, allowed_folder_labels: set[str], max_extracted_text_chars: int = 12_000,
        extractor: Callable[[Attachment, int], str | None] = extract_attachment_text,
        prompt: str | None = None,
    ) -> None:
        if not allowed_folder_labels:
            raise ValueError("At least one logical destination label must be configured")
        self._client = client
        self._model = model
        self._allowed_folder_labels = frozenset(allowed_folder_labels)
        self._max_extracted_text_chars = max_extracted_text_chars
        self._extractor = extractor
        self._prompt = prompt or PROMPT_PATH.read_text(encoding="utf-8")
        self._schema = {
            **CLASSIFICATION_SCHEMA,
            "properties": {
                **CLASSIFICATION_SCHEMA["properties"],
                "target_folder": {"type": "string", "enum": sorted(self._allowed_folder_labels)},
            },
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClaudeDocumentClassifier":
        if not settings.anthropic_api_key:
            raise ClassifierAuthenticationError("ANTHROPIC_API_KEY is required")
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=settings.anthropic_api_key)
        except Exception as exc:
            raise ClassifierAuthenticationError(f"Unable to initialise Anthropic client: {exc}") from exc
        return cls(client, settings.anthropic_model, set(settings.allowed_drive_folders), settings.max_extracted_text_chars)

    def classify(self, message: EmailMessage, attachment: Attachment) -> Mapping[str, object]:
        extracted_text = self._safe_extract(attachment)
        context = {
            "sender": message.sender,
            "subject": message.subject,
            "body": message.body[:self._max_extracted_text_chars],
            "attachment_filename": attachment.filename,
            "attachment_mime_type": attachment.mime_type,
            "attachment_text": extracted_text,
            "available_target_folder_labels": sorted(self._allowed_folder_labels),
        }
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=self._prompt.format(allowed_folder_labels=", ".join(sorted(self._allowed_folder_labels))),
                messages=[{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                output_config={"format": {"type": "json_schema", "schema": self._schema}},
            )
        except Exception as exc:
            self._raise_api_error(exc)
            raise AssertionError("unreachable")
        return self._validate_response(response)

    def _safe_extract(self, attachment: Attachment) -> str | None:
        try:
            text = self._extractor(attachment, self._max_extracted_text_chars)
            return text[:self._max_extracted_text_chars] if text else None
        except Exception:
            return None

    def _validate_response(self, response: Any) -> Mapping[str, object]:
        content = getattr(response, "content", None) or []
        text = next((getattr(block, "text", None) for block in content if getattr(block, "text", None)), None)
        if not text:
            raise ClassifierResponseError("Claude returned an empty structured response")
        try:
            decoded = json.loads(text)
            if not isinstance(decoded, dict):
                raise ValueError("response is not an object")
            classification = Classification.from_mapping(decoded)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ClassifierResponseError(f"Claude returned malformed classification: {exc}") from exc
        if classification.target_folder not in self._allowed_folder_labels:
            raise ClassifierResponseError("Claude selected an unconfigured logical target folder")
        return asdict(classification)

    @staticmethod
    def _raise_api_error(exc: Exception) -> None:
        error_name = type(exc).__name__
        if error_name in {"AuthenticationError", "PermissionDeniedError"}:
            raise ClassifierAuthenticationError("Anthropic authentication failed") from exc
        if error_name == "RateLimitError":
            raise ClassifierRateLimitError("Anthropic rate limit reached") from exc
        raise ClassifierAPIError(f"Anthropic API request failed: {exc}") from exc
