from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings
from .errors import (
    DriveAuthenticationError,
    DriveFolderError,
    DrivePermissionError,
    DriveUploadError,
)
from .filenames import validate_upload_filename
from .retry import RetryPolicy, is_transient_provider_error, policy_from_settings

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class GoogleDriveClient:
    """Single-user Google Drive adapter with a configured folder allowlist."""

    def __init__(
        self, service: Any, allowed_folder_ids: set[str], media_factory: Any | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._service = service
        self._allowed_folder_ids = frozenset(allowed_folder_ids)
        self._media_factory = media_factory or _media_upload
        self._retry_policy = retry_policy or RetryPolicy()

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleDriveClient":
        return cls(
            _build_service(settings.google_oauth_client_secrets_file, settings.google_oauth_token_file),
            settings.allowed_drive_folder_ids, retry_policy=policy_from_settings(settings),
        )

    def upload(
        self, *, folder_id: str, filename: str, content: bytes,
        mime_type: str | None, idempotency_key: str,
    ) -> str:
        if folder_id not in self._allowed_folder_ids:
            raise DriveFolderError("Destination folder is not in the configured allowlist")
        validate_upload_filename(filename)
        self._assert_folder(folder_id)

        try:
            return self._retry_policy.execute(
                lambda: self._upload_or_recover_once(
                    folder_id, filename, content, mime_type, idempotency_key,
                ),
                retry_if=is_transient_provider_error,
                provider="drive", operation_name="upload_or_recover",
            )
        except Exception as exc:
            self._raise_drive_error(exc, "Upload failed")
            raise AssertionError("unreachable")

    def _assert_folder(self, folder_id: str) -> None:
        try:
            metadata = self._retry_policy.execute(
                lambda: self._service.files().get(
                    fileId=folder_id, fields="id,name,mimeType,trashed", supportsAllDrives=True,
                ).execute(),
                retry_if=is_transient_provider_error,
                provider="drive", operation_name="get_folder",
            )
        except Exception as exc:
            self._raise_drive_error(exc, "Unable to access configured folder")
            return
        if metadata.get("trashed"):
            raise DriveFolderError("Configured destination folder is in trash")
        if metadata.get("mimeType") != FOLDER_MIME_TYPE:
            raise DriveFolderError("Configured destination is not a Google Drive folder")

    def _upload_or_recover_once(
        self, folder_id: str, filename: str, content: bytes,
        mime_type: str | None, idempotency_key: str,
    ) -> str:
        existing = self._find_existing_once(folder_id, idempotency_key)
        if existing:
            return existing
        media = self._media_factory(content, mime_type or "application/octet-stream")
        created = self._service.files().create(
            body={
                "name": self._deduplicated_filename(filename, idempotency_key),
                "parents": [folder_id],
                "appProperties": {"attachment_router_key": idempotency_key},
            },
            media_body=media,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    def _find_existing_once(self, folder_id: str, idempotency_key: str) -> str | None:
        safe_key = idempotency_key.replace("'", "\\'")
        query = (
            f"'{folder_id}' in parents and trashed = false and "
            f"appProperties has {{ key='attachment_router_key' and value='{safe_key}' }}"
        )
        response = self._service.files().list(
            q=query, spaces="drive", fields="files(id,name)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    @staticmethod
    def _raise_drive_error(exc: Exception, context: str) -> None:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in {401}:
            raise DriveAuthenticationError(f"{context}: authentication failed") from exc
        if status in {403}:
            raise DrivePermissionError(f"{context}: insufficient Google Drive permissions") from exc
        if status in {404}:
            raise DriveFolderError(f"{context}: configured folder does not exist") from exc
        raise DriveUploadError(f"{context}: {exc}") from exc

    @staticmethod
    def _deduplicated_filename(filename: str, idempotency_key: str) -> str:
        """Avoid ambiguous same-name files while retaining a recognizable title."""
        suffix = f"__{idempotency_key[:12]}"
        dot = filename.rfind(".")
        base, extension = (filename[:dot], filename[dot:]) if dot > 0 else (filename, "")
        return base[: 180 - len(extension) - len(suffix)].rstrip(". ") + suffix + extension


def _build_service(client_secrets_file: Path, token_file: Path) -> Any:
    """Load/refresh a local OAuth token, starting browser consent when needed."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        credentials = None
        if token_file.exists():
            credentials = Credentials.from_authorized_user_file(token_file, [DRIVE_SCOPE])
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials or not credentials.valid:
            if not client_secrets_file.is_file():
                raise DriveAuthenticationError(f"OAuth client secrets file not found: {client_secrets_file}")
            credentials = InstalledAppFlow.from_client_secrets_file(client_secrets_file, [DRIVE_SCOPE]).run_local_server(port=0)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except DriveAuthenticationError:
        raise
    except Exception as exc:
        raise DriveAuthenticationError(f"Google Drive authentication failed: {exc}") from exc


def _media_upload(content: bytes, mime_type: str) -> Any:
    from googleapiclient.http import MediaInMemoryUpload

    return MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
