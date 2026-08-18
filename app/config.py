from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    confidence_threshold: float
    needs_review_folder_id: str
    allowed_drive_folders: dict[str, str]
    state_db_path: Path
    google_oauth_client_secrets_file: Path = Path("secrets/google-oauth-client.json")
    google_oauth_token_file: Path = Path("secrets/google-drive-token.json")
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5"
    max_extracted_text_chars: int = 12_000
    gmail_oauth_client_secrets_file: Path = Path("secrets/google-oauth-client.json")
    gmail_oauth_token_file: Path = Path("secrets/google-gmail-token.json")
    gmail_search_query: str = "has:attachment"
    max_attachment_bytes: int = 10 * 1024 * 1024
    inbox_analyzer_model: str = "claude-haiku-4-5"
    inbox_analyzer_prompt_version: str = "v1"
    max_inbox_message_chars: int = 12_000
    inbox_analyzer_version: str = "v1"
    conversation_analyzer_model: str = "claude-haiku-4-5"
    conversation_analyzer_version: str = "v1"
    conversation_analyzer_prompt_version: str = "v1"
    max_thread_messages: int = 10
    max_thread_context_chars: int = 24_000
    thread_context_builder_version: str = "v1"
    knowledge_dir: Path = Path("knowledge")
    knowledge_chunk_max_chars: int = 1200
    knowledge_chunk_overlap_chars: int = 150
    knowledge_retrieval_limit: int = 5
    knowledge_retriever_version: str = "v1"
    knowledge_index_version: str = "v1"
    reply_draft_generator_model: str = "claude-haiku-4-5"
    reply_draft_generator_version: str = "v1"
    reply_draft_prompt_version: str = "v1"
    provider_retry_max_attempts: int = 3
    provider_retry_initial_delay_seconds: float = 0.5
    provider_retry_max_delay_seconds: float = 5.0
    provider_retry_multiplier: float = 2.0
    provider_retry_jitter_ratio: float = 0.1
    provider_request_timeout_seconds: float = 30.0
    sqlite_busy_timeout_ms: int = 5_000
    log_level: str = "INFO"
    log_file: Path | None = None
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3
    gmail_compose_oauth_client_secrets_file: Path = Path("secrets/google-oauth-client.json")
    gmail_compose_oauth_token_file: Path = Path("secrets/google-gmail-compose-token.json")
    gmail_compose_account_email: str | None = None
    max_gmail_draft_bytes: int = 1_000_000

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        try:
            threshold = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.85"))
        except ValueError as exc:
            raise ValueError("CONFIDENCE_THRESHOLD must be numeric") from exc
        if not 0 <= threshold <= 1:
            raise ValueError("CONFIDENCE_THRESHOLD must be between 0 and 1")
        review_folder = os.environ.get("NEEDS_REVIEW_FOLDER_ID", "").strip()
        if not review_folder:
            raise ValueError("NEEDS_REVIEW_FOLDER_ID is required")
        try:
            folders = json.loads(os.environ.get("ALLOWED_DRIVE_FOLDERS", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("ALLOWED_DRIVE_FOLDERS must be JSON") from exc
        if not isinstance(folders, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in folders.items()):
            raise ValueError("ALLOWED_DRIVE_FOLDERS must be a JSON string-to-folder-ID map")
        try:
            max_extracted_text_chars = int(os.environ.get("MAX_EXTRACTED_TEXT_CHARS", "12000"))
        except ValueError as exc:
            raise ValueError("MAX_EXTRACTED_TEXT_CHARS must be an integer") from exc
        if max_extracted_text_chars < 100 or max_extracted_text_chars > 100_000:
            raise ValueError("MAX_EXTRACTED_TEXT_CHARS must be between 100 and 100000")
        try:
            max_attachment_bytes = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
        except ValueError as exc:
            raise ValueError("MAX_ATTACHMENT_BYTES must be an integer") from exc
        if max_attachment_bytes < 1 or max_attachment_bytes > 100 * 1024 * 1024:
            raise ValueError("MAX_ATTACHMENT_BYTES must be between 1 and 104857600")
        try:
            max_inbox_message_chars = int(os.environ.get("MAX_INBOX_MESSAGE_CHARS", "12000"))
        except ValueError as exc:
            raise ValueError("MAX_INBOX_MESSAGE_CHARS must be an integer") from exc
        if max_inbox_message_chars < 100 or max_inbox_message_chars > 100_000:
            raise ValueError("MAX_INBOX_MESSAGE_CHARS must be between 100 and 100000")
        try:
            max_thread_messages = int(os.environ.get("MAX_THREAD_MESSAGES", "10"))
            max_thread_context_chars = int(os.environ.get("MAX_THREAD_CONTEXT_CHARS", "24000"))
        except ValueError as exc:
            raise ValueError("Thread context limits must be integers") from exc
        if max_thread_messages < 1 or max_thread_messages > 100:
            raise ValueError("MAX_THREAD_MESSAGES must be between 1 and 100")
        if max_thread_context_chars < 100 or max_thread_context_chars > 200_000:
            raise ValueError("MAX_THREAD_CONTEXT_CHARS must be between 100 and 200000")
        try:
            retry_max_attempts = int(os.environ.get("PROVIDER_RETRY_MAX_ATTEMPTS", "3"))
            retry_initial_delay = float(os.environ.get("PROVIDER_RETRY_INITIAL_DELAY_SECONDS", "0.5"))
            retry_max_delay = float(os.environ.get("PROVIDER_RETRY_MAX_DELAY_SECONDS", "5"))
            retry_multiplier = float(os.environ.get("PROVIDER_RETRY_MULTIPLIER", "2"))
            retry_jitter_ratio = float(os.environ.get("PROVIDER_RETRY_JITTER_RATIO", "0.1"))
            request_timeout = float(os.environ.get("PROVIDER_REQUEST_TIMEOUT_SECONDS", "30"))
            sqlite_busy_timeout_ms = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "5000"))
        except ValueError as exc:
            raise ValueError("Retry and timeout settings must be numeric") from exc
        if retry_max_attempts < 1 or retry_max_attempts > 10:
            raise ValueError("PROVIDER_RETRY_MAX_ATTEMPTS must be between 1 and 10")
        if not all(math.isfinite(value) for value in (
            retry_initial_delay, retry_max_delay, retry_multiplier, retry_jitter_ratio, request_timeout,
        )):
            raise ValueError("Retry and timeout settings must be finite")
        if retry_initial_delay < 0 or retry_max_delay < retry_initial_delay or retry_max_delay > 60:
            raise ValueError("Provider retry delays are invalid")
        if retry_multiplier < 1 or retry_multiplier > 10:
            raise ValueError("PROVIDER_RETRY_MULTIPLIER must be between 1 and 10")
        if not 0 <= retry_jitter_ratio <= 1:
            raise ValueError("PROVIDER_RETRY_JITTER_RATIO must be between 0 and 1")
        if request_timeout <= 0 or request_timeout > 300:
            raise ValueError("PROVIDER_REQUEST_TIMEOUT_SECONDS must be between 0 and 300")
        if sqlite_busy_timeout_ms < 0 or sqlite_busy_timeout_ms > 60_000:
            raise ValueError("SQLITE_BUSY_TIMEOUT_MS must be between 0 and 60000")
        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        log_file_str = os.environ.get("LOG_FILE", "").strip()
        log_file = Path(log_file_str) if log_file_str else None
        try:
            log_max_bytes = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
            log_backup_count = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
        except ValueError as exc:
            raise ValueError("LOG_MAX_BYTES and LOG_BACKUP_COUNT must be integers") from exc
        if log_max_bytes < 1024 or log_max_bytes > 1024 * 1024 * 1024:
            raise ValueError("LOG_MAX_BYTES must be between 1024 and 1073741824")
        if log_backup_count < 0 or log_backup_count > 100:
            raise ValueError("LOG_BACKUP_COUNT must be between 0 and 100")
        return cls(
            threshold,
            review_folder,
            folders,
            Path(os.environ.get("STATE_DB_PATH", "data/state.sqlite3")),
            Path(os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "secrets/google-oauth-client.json")),
            Path(os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "secrets/google-drive-token.json")),
            os.environ.get("ANTHROPIC_API_KEY") or None,
            os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
            max_extracted_text_chars,
            Path(os.environ.get("GMAIL_OAUTH_CLIENT_SECRETS_FILE", os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "secrets/google-oauth-client.json"))),
            Path(os.environ.get("GMAIL_OAUTH_TOKEN_FILE", "secrets/google-gmail-token.json")),
            os.environ.get("GMAIL_SEARCH_QUERY", "has:attachment"),
            max_attachment_bytes,
            os.environ.get("INBOX_ANALYZER_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")),
            os.environ.get("INBOX_ANALYZER_PROMPT_VERSION", "v1"),
            max_inbox_message_chars,
            os.environ.get("INBOX_ANALYZER_VERSION", "v1"),
            os.environ.get("CONVERSATION_ANALYZER_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")),
            os.environ.get("CONVERSATION_ANALYZER_VERSION", "v1"),
            os.environ.get("CONVERSATION_ANALYZER_PROMPT_VERSION", "v1"),
            max_thread_messages,
            max_thread_context_chars,
            os.environ.get("THREAD_CONTEXT_BUILDER_VERSION", "v1"),
            Path(os.environ.get("KNOWLEDGE_DIR", "knowledge")),
            int(os.environ.get("KNOWLEDGE_CHUNK_MAX_CHARS", "1200")), int(os.environ.get("KNOWLEDGE_CHUNK_OVERLAP_CHARS", "150")),
            int(os.environ.get("KNOWLEDGE_RETRIEVAL_LIMIT", "5")), os.environ.get("KNOWLEDGE_RETRIEVER_VERSION", "v1"), os.environ.get("KNOWLEDGE_INDEX_VERSION", "v1"),
            os.environ.get("REPLY_DRAFT_GENERATOR_MODEL", os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")),
            os.environ.get("REPLY_DRAFT_GENERATOR_VERSION", "v1"), os.environ.get("REPLY_DRAFT_PROMPT_VERSION", "v1"),
            retry_max_attempts, retry_initial_delay, retry_max_delay, retry_multiplier, retry_jitter_ratio,
            request_timeout, sqlite_busy_timeout_ms,
            log_level, log_file, log_max_bytes, log_backup_count,
            Path(os.environ.get("GMAIL_COMPOSE_OAUTH_CLIENT_SECRETS_FILE",
                                os.environ.get("GMAIL_OAUTH_CLIENT_SECRETS_FILE",
                                               os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "secrets/google-oauth-client.json")))),
            Path(os.environ.get("GMAIL_COMPOSE_OAUTH_TOKEN_FILE", "secrets/google-gmail-compose-token.json")),
            os.environ.get("GMAIL_COMPOSE_ACCOUNT_EMAIL") or None,
            int(os.environ.get("MAX_GMAIL_DRAFT_BYTES", "1000000")),
        )

    @property
    def allowed_drive_folder_ids(self) -> set[str]:
        """Configured destinations only; classifier text is never a folder ID."""
        return set(self.allowed_drive_folders.values()) | {self.needs_review_folder_id}
