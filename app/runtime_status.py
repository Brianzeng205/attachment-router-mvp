"""Read-only operational status inspection for recent polling runs."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

from .inbox_repository import SqliteInboxRepository


def format_runtime_run(run) -> str:
    """Format a single runtime run for display."""
    status_str = run.status
    if run.error_class:
        status_str = f"{status_str} ({run.error_class})"

    duration = ""
    if run.started_at and run.completed_at:
        try:
            started = datetime.fromisoformat(run.started_at)
            completed = datetime.fromisoformat(run.completed_at)
            elapsed = (completed - started).total_seconds()
            duration = f" ({elapsed:.1f}s)"
        except (ValueError, TypeError):
            pass

    metrics = []
    if run.messages_polled is not None:
        metrics.append(f"messages={run.messages_polled}")
    if run.attachments_uploaded is not None:
        metrics.append(f"uploaded={run.attachments_uploaded}")
    if run.attachments_skipped is not None:
        metrics.append(f"skipped={run.attachments_skipped}")
    if run.inbox_errors is not None or run.attachment_errors is not None:
        inbox_errs = run.inbox_errors or 0
        attach_errs = run.attachment_errors or 0
        metrics.append(f"errors={inbox_errs + attach_errs}")

    metrics_str = " [" + " ".join(metrics) + "]" if metrics else ""

    return (
        f"  Run #{run.id}: {run.started_at} → {status_str}{duration}{metrics_str} "
        f"lock={run.lock_outcome} trigger={run.trigger_type} instance={run.instance_id[:8]}"
    )


def list_recent_runs(
    state_db_path: Path,
    limit: int = 20,
) -> None:
    """
    Query and display recent runtime history.

    Args:
        state_db_path: Path to SQLite state database
        limit: Maximum number of recent runs to display
    """
    repository = SqliteInboxRepository(state_db_path)
    try:
        runs = repository.list_runtime_runs()
        if not runs:
            print("No recent runtime history found.")
            return

        # Display most recent runs first
        runs_to_display = list(reversed(runs[-limit:]))
        print(f"Recent {len(runs_to_display)} runtime invocation(s):")
        for run in runs_to_display:
            print(format_runtime_run(run))
    finally:
        repository.close()


def main() -> int:
    """CLI entry point for runtime status inspection."""
    try:
        load_dotenv()
        state_db_path = Path(os.environ.get("STATE_DB_PATH", "data/state.sqlite3"))
        list_recent_runs(state_db_path, limit=20)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
