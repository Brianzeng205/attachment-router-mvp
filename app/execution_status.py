"""Credential-free execution queue inspection and legacy reconciliation CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .execution_queue_service import ExecutionQueueService
from .inbox_repository import SqliteInboxRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the approved-action execution queue")
    parser.add_argument("--reconcile", action="store_true",
                        help="idempotently create intents for valid legacy approvals")
    args = parser.parse_args(argv)
    load_dotenv()
    repository = SqliteInboxRepository(Path(os.environ.get("STATE_DB_PATH", "data/state.sqlite3")))
    try:
        service = ExecutionQueueService(repository)
        if args.reconcile:
            print(f"Reconciled {len(service.reconcile_approved_reviews())} execution intent(s).")
        counts = service.status_counts()
        print("Execution queue: " + " ".join(f"{key}={counts[key]}" for key in
              ("pending", "processing", "retry_wait", "completed", "failed")))
        return 0
    except Exception as exc:
        print(f"Error: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
