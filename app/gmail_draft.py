"""The narrow Phase 8C1 Gmail draft-only mutation boundary."""

from __future__ import annotations

import base64
import re
import socket
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from .config import Settings
from .execution_models import ACTION_CREATE_GMAIL_DRAFT, ExecutionIntent

GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_MESSAGE_ID = re.compile(r"<[^<>\r\n\s]+@[^<>\r\n\s]+>\Z")


class GmailDraftError(RuntimeError):
    code = "gmail_draft_error"


class GmailDraftValidationError(GmailDraftError):
    code = "invalid_draft_input"


class GmailComposeAuthorizationError(GmailDraftError):
    code = "gmail_compose_authorization_required"


class GmailDraftDefinitiveError(GmailDraftError):
    def __init__(self, code: str, *, retryable: bool = False, provider_status: int | None = None) -> None:
        super().__init__(code)
        self.code, self.retryable, self.provider_status = code, retryable, provider_status


class GmailDraftOutcomeUnknown(GmailDraftError):
    code = "gmail_create_outcome_unknown"


@dataclass(frozen=True)
class GmailDraftCommand:
    execution_id: str
    thread_id: str
    raw: str
    recipient: str
    subject: str
    message_id_header: str
    mime_size: int


@dataclass(frozen=True)
class CreatedGmailDraft:
    draft_id: str
    message_id: str | None
    thread_id: str


def build_gmail_reply_command(intent: ExecutionIntent, *, authenticated_account: str,
                              max_mime_bytes: int = 1_000_000) -> GmailDraftCommand:
    if intent.action_type != ACTION_CREATE_GMAIL_DRAFT:
        raise GmailDraftValidationError("Unsupported execution action")
    if not intent.provider_thread_id or _has_newline(intent.provider_thread_id):
        raise GmailDraftValidationError("A valid Gmail thread ID is required")
    recipient = _single_address(intent.recipient, "recipient")
    account = _single_address(authenticated_account, "authenticated account")
    if recipient.casefold() == account.casefold():
        raise GmailDraftValidationError("Reply recipient is the authenticated account")
    in_reply_to = _valid_message_id(intent.in_reply_to_header, "In-Reply-To")
    references = tuple(_valid_message_id(value, "References") for value in intent.references)
    references = tuple(dict.fromkeys((*references, in_reply_to)))
    subject = _reply_subject(intent.subject)
    if not isinstance(intent.approved_body, str) or not intent.approved_body.strip():
        raise GmailDraftValidationError("Approved snapshot is empty")

    message = EmailMessage(policy=SMTP)
    message["To"] = recipient
    message["Subject"] = subject
    message["In-Reply-To"] = in_reply_to
    message["References"] = " ".join(references)
    # A deterministic, standards-compliant identity helps an operator inspect a raw draft,
    # but Phase 8C1 does not assume Gmail preserves/searches it for automatic reconciliation.
    message_id = f"<{intent.execution_id}@gmail-draft.local>"
    message["Message-ID"] = message_id
    message.set_content(intent.approved_body, subtype="plain", charset="utf-8")
    serialized = message.as_bytes(policy=SMTP)
    if len(serialized) > max_mime_bytes:
        raise GmailDraftValidationError("Approved MIME payload exceeds the configured limit")
    raw = base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")
    return GmailDraftCommand(intent.execution_id, intent.provider_thread_id, raw, recipient,
                             subject, message_id, len(serialized))


class GmailDraftClient:
    """Exposes exactly one provider operation: users.drafts.create."""

    def __init__(self, service: Any) -> None:
        self.__service = service

    @classmethod
    def from_settings(cls, settings: Settings) -> "GmailDraftClient":
        return cls(_build_compose_service(settings.gmail_compose_oauth_client_secrets_file,
                                          settings.gmail_compose_oauth_token_file))

    def create_reply_draft(self, command: GmailDraftCommand) -> CreatedGmailDraft:
        try:
            result = self.__service.users().drafts().create(
                userId="me", body={"message": {"raw": command.raw, "threadId": command.thread_id}},
            ).execute()
        except Exception as exc:
            raise _classify_create_error(exc) from None
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            # The POST returned but no durable provider identity was received. Retrying could duplicate.
            raise GmailDraftOutcomeUnknown("Gmail create returned no usable draft identity")
        message = result.get("message") if isinstance(result.get("message"), dict) else {}
        thread_id = message.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            raise GmailDraftOutcomeUnknown("Gmail create returned no usable thread identity")
        provider_message_id = message.get("id")
        return CreatedGmailDraft(result["id"], provider_message_id if isinstance(provider_message_id, str) else None,
                                 thread_id)


def _build_compose_service(client_secrets_file: Path, token_file: Path) -> Any:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = None
        if token_file.exists():
            credentials = Credentials.from_authorized_user_file(token_file, [GMAIL_COMPOSE_SCOPE])
            if not credentials.has_scopes([GMAIL_COMPOSE_SCOPE]):
                raise GmailComposeAuthorizationError(
                    "Configured compose token lacks gmail.compose; re-authorize it explicitly without deleting it automatically"
                )
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials:
            if not client_secrets_file.is_file():
                raise GmailComposeAuthorizationError("Gmail compose OAuth client secrets file was not found")
            credentials = InstalledAppFlow.from_client_secrets_file(
                client_secrets_file, [GMAIL_COMPOSE_SCOPE],
            ).run_local_server(port=0)
            if not credentials.has_scopes([GMAIL_COMPOSE_SCOPE]):
                raise GmailComposeAuthorizationError("Authorization did not grant gmail.compose")
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        if not credentials.valid:
            raise GmailComposeAuthorizationError("Gmail compose credentials are invalid or revoked")
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except GmailComposeAuthorizationError:
        raise
    except Exception as exc:
        raise GmailComposeAuthorizationError(f"Gmail compose authorization failed ({type(exc).__name__})") from None


def _classify_create_error(exc: Exception) -> GmailDraftError:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(status, int):
        if status in {401, 403}:
            return GmailDraftDefinitiveError("gmail_authorization_failed", provider_status=status)
        if status == 429:
            return GmailDraftDefinitiveError("gmail_transient_http_error", retryable=True, provider_status=status)
        if status in {408, 500, 502, 503, 504}:
            # Conservative: a gateway/server failure after dispatch may hide a successful create.
            return GmailDraftOutcomeUnknown("Gmail create HTTP outcome is unknown")
        return GmailDraftDefinitiveError("gmail_http_error", provider_status=status)
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return GmailDraftOutcomeUnknown("Gmail create transport outcome is unknown")
    return GmailDraftOutcomeUnknown("Gmail create outcome is unknown")


def _has_newline(value: str) -> bool:
    return "\r" in value or "\n" in value


def _single_address(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or _has_newline(value):
        raise GmailDraftValidationError(f"A valid {field} is required")
    parsed = getaddresses([value])
    if len(parsed) != 1 or not parsed[0][1] or "@" not in parsed[0][1]:
        raise GmailDraftValidationError(f"A single valid {field} is required")
    address = parsed[0][1]
    local, domain = address.rsplit("@", 1)
    if not local or not domain or any(char.isspace() for char in address):
        raise GmailDraftValidationError(f"A single valid {field} is required")
    return address


def _valid_message_id(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not _MESSAGE_ID.fullmatch(value.strip()):
        raise GmailDraftValidationError(f"A valid persisted {field} message ID is required")
    return value.strip()


def _reply_subject(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip() or _has_newline(value):
        raise GmailDraftValidationError("A valid persisted subject is required")
    subject = value.strip()
    return subject if re.match(r"(?i)^re\s*:", subject) else f"Re: {subject}"
