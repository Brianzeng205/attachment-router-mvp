from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .errors import (
    GmailAPIError,
    GmailAttachmentError,
    GmailAuthenticationError,
    GmailMessageError,
    GmailPayloadError,
    GmailRateLimitError,
)
from .models import Attachment, EmailMessage
from .retry import RetryPolicy, is_transient_provider_error, policy_from_settings

logger = logging.getLogger(__name__)
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


class GmailClient:
    """Read-only Gmail polling adapter; it never changes mailbox state."""

    def __init__(
        self, service: Any, search_query: str = "has:attachment",
        max_attachment_bytes: int = 10 * 1024 * 1024, retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._service = service
        self._search_query = search_query
        self._max_attachment_bytes = max_attachment_bytes
        self._retry_policy = retry_policy or RetryPolicy()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GmailClient":
        service = _build_service(settings.gmail_oauth_client_secrets_file, settings.gmail_oauth_token_file)
        return cls(
            service, settings.gmail_search_query, settings.max_attachment_bytes,
            policy_from_settings(settings),
        )

    def list_messages(self) -> Iterable[EmailMessage]:
        try:
            for summary in self._list_candidate_summaries():
                message_id = summary.get("id")
                if not isinstance(message_id, str) or not message_id:
                    logger.warning("Skipping malformed Gmail message summary without an ID")
                    continue
                try:
                    raw = self._provider_call(
                        lambda: self._service.users().messages().get(
                            userId="me", id=message_id, format="full",
                        ).execute(),
                        "get_message",
                    )
                    yield self._to_email_message(raw)
                except Exception as exc:
                    error = self._map_error(exc, f"Could not retrieve Gmail message {message_id}", GmailMessageError)
                    if isinstance(error, (GmailAuthenticationError, GmailRateLimitError)):
                        raise error
                    logger.error(
                        "event=gmail_message_failed message_id=%s error_class=%s",
                        message_id, type(error).__name__,
                    )
        except (GmailAuthenticationError, GmailRateLimitError, GmailAPIError):
            raise

    def _list_candidate_summaries(self) -> Iterable[dict[str, Any]]:
        page_token: str | None = None
        while True:
            try:
                response = self._provider_call(
                    lambda: self._service.users().messages().list(
                        userId="me", q=self._search_query, pageToken=page_token,
                    ).execute(),
                    "list_messages",
                )
            except Exception as exc:
                raise self._map_error(exc, "Could not list Gmail messages", GmailAPIError) from exc
            messages = response.get("messages", [])
            if not isinstance(messages, list):
                raise GmailPayloadError("Gmail list response contained an invalid messages field")
            yield from (item for item in messages if isinstance(item, dict))
            page_token = response.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                return

    def _to_email_message(self, raw: Any) -> EmailMessage:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not isinstance(raw.get("payload"), dict):
            raise GmailPayloadError("Gmail message payload is malformed")
        headers = _headers(raw["payload"].get("headers"))
        attachments = tuple(self._attachments(raw["id"], raw["payload"], "0"))
        received_at = _received_at(raw.get("internalDate"))
        return EmailMessage(
            id=raw["id"],
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            body=_body_text(raw["payload"]),
            received_at=received_at,
            attachments=attachments,
            thread_id=raw.get("threadId") if isinstance(raw.get("threadId"), str) else None,
            recipients=_recipients(headers),
        )

    def _attachments(self, message_id: str, part: dict[str, Any], part_path: str) -> Iterable[Attachment]:
        filename = part.get("filename")
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        mime_type = part.get("mimeType") if isinstance(part.get("mimeType"), str) else None
        if isinstance(filename, str) and filename.strip() and _is_real_attachment(part, mime_type):
            remote_attachment_id = body.get("attachmentId")
            part_id = part.get("partId")
            stable_part_id = part_id if isinstance(part_id, str) and part_id else part_path
            # Gmail's opaque attachmentId is only a retrieval handle. MIME part
            # IDs are stable within an immutable Gmail message and therefore
            # safe to use as our cross-polling application identity.
            stable_id = f"part:{stable_part_id}"
            try:
                content = self._attachment_content(message_id, body, remote_attachment_id)
                if content is not None:
                    yield Attachment(stable_id, filename.strip(), content, mime_type)
            except GmailAttachmentError as exc:
                logger.error(
                    "event=gmail_attachment_failed message_id=%s attachment_id=%s error_class=%s",
                    message_id, stable_id, type(exc).__name__,
                )
        parts = part.get("parts", [])
        if isinstance(parts, list):
            for index, child in enumerate(parts):
                if isinstance(child, dict):
                    yield from self._attachments(message_id, child, f"{part_path}.{index}")

    def _attachment_content(self, message_id: str, body: dict[str, Any], remote_attachment_id: Any) -> bytes | None:
        attachment_id = remote_attachment_id if isinstance(remote_attachment_id, str) and remote_attachment_id else "inline-part"
        declared_size = body.get("size")
        if isinstance(declared_size, int) and declared_size > self._max_attachment_bytes:
            logger.warning("Skipping oversized attachment message_id=%s attachment_id=%s size=%s limit=%s", message_id, attachment_id, declared_size, self._max_attachment_bytes)
            return None
        encoded = body.get("data")
        if not isinstance(encoded, str) and isinstance(remote_attachment_id, str) and remote_attachment_id:
            try:
                result = self._provider_call(
                    lambda: self._service.users().messages().attachments().get(
                        userId="me", messageId=message_id, id=remote_attachment_id,
                    ).execute(),
                    "get_attachment",
                )
                encoded = result.get("data") if isinstance(result, dict) else None
            except Exception as exc:
                raise self._map_error(exc, "Could not retrieve Gmail attachment", GmailAttachmentError) from exc
        if not isinstance(encoded, str):
            raise GmailAttachmentError("Gmail attachment has no retrievable content")
        try:
            content = _decode_data(encoded)
        except ValueError as exc:
            raise GmailAttachmentError("Gmail attachment data is malformed") from exc
        if len(content) > self._max_attachment_bytes:
            logger.warning("Skipping oversized attachment message_id=%s attachment_id=%s size=%s limit=%s", message_id, attachment_id, len(content), self._max_attachment_bytes)
            return None
        return content

    def _provider_call(self, operation, operation_name: str):
        return self._retry_policy.execute(
            operation, retry_if=is_transient_provider_error,
            provider="gmail", operation_name=operation_name,
        )

    @staticmethod
    def _map_error(exc: Exception, context: str, default: type[Exception]) -> Exception:
        status = getattr(getattr(exc, "resp", None), "status", None)
        detail = str(exc).lower()
        if status == 429 or "ratelimit" in detail or "quota" in detail:
            return GmailRateLimitError(f"{context}: Gmail quota or rate limit reached")
        if status in {401, 403}:
            return GmailAuthenticationError(f"{context}: authentication or read permission failed")
        if isinstance(exc, (GmailPayloadError, GmailMessageError, GmailAttachmentError)):
            return exc
        return default(f"{context}: {exc}")


def _headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(headers, list):
        return result
    for header in headers:
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            result[header["name"].lower()] = header["value"]
    return result


def _recipients(headers: dict[str, str]) -> tuple[str, ...]:
    """Keep parsed delivery facts without changing Gmail MIME processing."""
    values = (headers.get("to", ""), headers.get("cc", ""))
    return tuple(address.strip() for _, address in getaddresses(values) if address.strip())


def _body_text(part: dict[str, Any]) -> str:
    plain, html = _find_body(part)
    if plain:
        return plain
    if html:
        return _strip_html(html)
    return ""


def _find_body(part: dict[str, Any]) -> tuple[str, str]:
    plain = html = ""
    mime_type = part.get("mimeType")
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = body.get("data")
    if isinstance(data, str):
        try:
            text = _decode_data(data).decode("utf-8", errors="replace")
            if mime_type == "text/plain":
                plain = text
            elif mime_type == "text/html":
                html = text
        except ValueError:
            pass
    for child in part.get("parts", []) if isinstance(part.get("parts"), list) else []:
        if isinstance(child, dict):
            child_plain, child_html = _find_body(child)
            plain = plain or child_plain
            html = html or child_html
    return plain, html


def _is_real_attachment(part: dict[str, Any], mime_type: str | None) -> bool:
    disposition = _headers(part.get("headers")).get("content-disposition", "").lower()
    filename = str(part.get("filename", ""))
    if mime_type and mime_type.startswith("image/") and "attachment" not in disposition:
        return False
    return "inline" not in disposition or "attachment" in disposition or bool(filename)


def _decode_data(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _strip_html(value: str) -> str:
    import re

    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _received_at(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _build_service(client_secrets_file: Path, token_file: Path) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = None
        if token_file.exists():
            credentials = Credentials.from_authorized_user_file(token_file, [GMAIL_READONLY_SCOPE])
        if credentials and not credentials.has_scopes([GMAIL_READONLY_SCOPE]):
            credentials = None
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not client_secrets_file.is_file():
                raise GmailAuthenticationError(f"OAuth client secrets file not found: {client_secrets_file}")
            credentials = InstalledAppFlow.from_client_secrets_file(client_secrets_file, [GMAIL_READONLY_SCOPE]).run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except GmailAuthenticationError:
        raise
    except Exception as exc:
        raise GmailAuthenticationError(f"Gmail authentication failed: {exc}") from exc
