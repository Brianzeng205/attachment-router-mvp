"""Operator-controlled single-item Gmail draft worker. It never sends email."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

from .execution_queue_service import ExecutionQueueService
from .gmail_draft import GmailDraftClient
from .gmail_draft_executor import GmailDraftExecutor, preview_next
from .inbox_repository import SqliteInboxRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create at most one Gmail draft; never send email")
    parser.add_argument("--once", action="store_true", help="process one eligible execution")
    parser.add_argument("--dry-run", action="store_true", help="validate one item without claiming or calling Gmail")
    parser.add_argument("--reconcile", metavar="EXECUTION_ID",
                        help="record an operator-confirmed Gmail draft for an unknown outcome")
    parser.add_argument("--draft-id", help="confirmed Gmail draft ID for --reconcile")
    parser.add_argument("--thread-id", help="confirmed Gmail thread ID for --reconcile")
    parser.add_argument("--message-id", help="optional confirmed Gmail message ID")
    parser.add_argument("--worker-id", default="gmail-draft-cli")
    args = parser.parse_args(argv)
    if sum((args.once, args.dry_run, bool(args.reconcile))) != 1:
        parser.error("choose exactly one of --once, --dry-run, or --reconcile")
    load_dotenv()
    account_email = os.environ.get("GMAIL_COMPOSE_ACCOUNT_EMAIL", "").strip()
    if not args.reconcile and not account_email:
        print("Error: GMAIL_COMPOSE_ACCOUNT_EMAIL is required", file=sys.stderr)
        return 2
    try:
        max_mime_bytes = int(os.environ.get("MAX_GMAIL_DRAFT_BYTES", "1000000"))
        busy_timeout_ms = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "5000"))
    except ValueError:
        print("Error: Gmail draft size and SQLite timeout settings must be integers", file=sys.stderr)
        return 2
    if max_mime_bytes < 1_000 or max_mime_bytes > 25_000_000:
        print("Error: MAX_GMAIL_DRAFT_BYTES must be between 1000 and 25000000", file=sys.stderr)
        return 2
    repository = SqliteInboxRepository(Path(os.environ.get("STATE_DB_PATH", "data/state.sqlite3")),
                                       busy_timeout_ms)
    try:
        queue = ExecutionQueueService(repository)
        if args.reconcile:
            if not args.draft_id or not args.thread_id:
                parser.error("--reconcile requires --draft-id and --thread-id")
            intent = queue.reconcile_gmail_draft(
                args.reconcile, draft_id=args.draft_id, thread_id=args.thread_id, message_id=args.message_id,
            )
            print(f"execution_id={intent.execution_id} status=completed draft_id={args.draft_id}")
            return 0
        if args.dry_run:
            command = preview_next(queue, authenticated_account=account_email,
                                   max_mime_bytes=max_mime_bytes)
            print("No eligible Gmail draft execution." if command is None else
                  f"Draft preview valid: execution_id={command.execution_id} provider=gmail mime_bytes={command.mime_size}")
            return 0
        compose_settings = SimpleNamespace(
            gmail_compose_oauth_client_secrets_file=Path(os.environ.get(
                "GMAIL_COMPOSE_OAUTH_CLIENT_SECRETS_FILE",
                os.environ.get("GMAIL_OAUTH_CLIENT_SECRETS_FILE",
                               os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "secrets/google-oauth-client.json")))),
            gmail_compose_oauth_token_file=Path(os.environ.get(
                "GMAIL_COMPOSE_OAUTH_TOKEN_FILE", "secrets/google-gmail-compose-token.json")),
        )
        client = GmailDraftClient.from_settings(compose_settings)
        outcome = GmailDraftExecutor(
            queue, client, authenticated_account=account_email,
            max_mime_bytes=max_mime_bytes,
        ).execute_once(args.worker_id)
        if outcome.intent is None:
            print("No eligible Gmail draft execution.")
        else:
            result = repository.get_gmail_draft_result(outcome.intent.execution_id)
            suffix = f" draft_id={result.provider_draft_id}" if result else ""
            print(f"execution_id={outcome.intent.execution_id} status={outcome.intent.status}{suffix}")
        return 0
    except Exception as exc:
        print(f"Error: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
