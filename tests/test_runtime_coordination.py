import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.main as runtime_main
from app.inbox_repository import SqliteInboxRepository
from app.process_lock import ProcessLock, poll_lock_path
from app.runtime_coordinator import RuntimeCoordinator


class RuntimeCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "state.sqlite3"
        self.lock_path = poll_lock_path(self.database_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def runs(self):
        repository = SqliteInboxRepository(self.database_path)
        try:
            return repository.list_runtime_runs()
        finally:
            repository.close()

    def test_first_coordinator_acquires_lock_records_lifecycle_and_calls_business_once(self):
        calls = []

        def business():
            calls.append("called")
            current = self.runs()
            self.assertEqual((len(current), current[0].status, current[0].lock_outcome), (1, "running", "acquired"))
            return "business-result"

        result = RuntimeCoordinator(self.database_path, business, instance_id="instance-1").execute_once()
        self.assertEqual(calls, ["called"])
        self.assertEqual((result.status, result.business_result), ("completed", "business-result"))
        run = self.runs()[0]
        self.assertEqual((run.status, run.instance_id, run.trigger_type), ("completed", "instance-1", "cli"))
        self.assertIsNotNone(run.completed_at)

    def test_real_os_lock_contention_skips_without_calling_or_queueing_business(self):
        owner = ProcessLock(self.lock_path)
        self.assertTrue(owner.acquire())
        try:
            business = Mock()
            coordinator = RuntimeCoordinator(self.database_path, business)
            first = coordinator.execute_once()
            second = coordinator.execute_once()
            self.assertEqual((first.status, second.status), ("skipped_locked", "skipped_locked"))
            business.assert_not_called()
            self.assertEqual(self.runs(), [])
        finally:
            owner.release()

    def test_lock_releases_after_success_exception_and_interruption(self):
        RuntimeCoordinator(self.database_path, lambda: None).execute_once()
        available = ProcessLock(self.lock_path)
        self.assertTrue(available.acquire())
        available.release()

        with self.assertRaises(RuntimeError):
            RuntimeCoordinator(self.database_path, lambda: (_ for _ in ()).throw(RuntimeError("private failure detail"))).execute_once()
        available = ProcessLock(self.lock_path)
        self.assertTrue(available.acquire())
        available.release()

        with self.assertRaises(KeyboardInterrupt):
            RuntimeCoordinator(self.database_path, lambda: (_ for _ in ()).throw(KeyboardInterrupt())).execute_once()
        available = ProcessLock(self.lock_path)
        self.assertTrue(available.acquire())
        available.release()
        self.assertEqual([run.status for run in self.runs()], ["completed", "failed", "interrupted"])

    def test_failure_persists_only_normalized_error_class(self):
        secret = "email body and OAuth token must not be stored"
        with self.assertRaises(ValueError):
            RuntimeCoordinator(self.database_path, lambda: (_ for _ in ()).throw(ValueError(secret))).execute_once()
        run = self.runs()[0]
        self.assertEqual((run.status, run.error_class), ("failed", "ValueError"))
        row_text = " ".join(str(value) for value in self._runtime_row())
        self.assertNotIn(secret, row_text)

    def test_persistent_unlocked_file_does_not_block_execution(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_bytes(b"\0")
        called = []
        result = RuntimeCoordinator(self.database_path, lambda: called.append(True)).execute_once()
        self.assertEqual(result.status, "completed")
        self.assertEqual(called, [True])
        self.assertTrue(self.lock_path.exists())

    def test_acquired_lock_marks_previous_running_history_abandoned(self):
        repository = SqliteInboxRepository(self.database_path)
        try:
            stale = repository.create_runtime_run(trigger_type="cli", instance_id="crashed", lock_outcome="acquired")
        finally:
            repository.close()
        completed = RuntimeCoordinator(self.database_path, lambda: None, instance_id="new-owner").execute_once()
        runs = self.runs()
        self.assertEqual((runs[0].id, runs[0].status), (stale.id, "abandoned"))
        self.assertIsNotNone(runs[0].completed_at)
        self.assertEqual((runs[1].id, runs[1].status), (completed.runtime_run_id, "completed"))

    def test_subprocess_lock_ownership_and_crash_release_are_real_os_semantics(self):
        holding_script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from app.process_lock import ProcessLock\n"
            "lock=ProcessLock(Path(sys.argv[1]))\n"
            "print('acquired' if lock.acquire() else 'failed', flush=True)\n"
            "sys.stdin.readline()\n"
            "lock.release()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", holding_script, str(self.lock_path)], cwd=Path.cwd(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "acquired")
            contender = ProcessLock(self.lock_path)
            self.assertFalse(contender.acquire())
            child.communicate("release\n", timeout=10)
            self.assertEqual(child.returncode, 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

        crash_script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from app.process_lock import ProcessLock\n"
            "lock=ProcessLock(Path(sys.argv[1]))\n"
            "raise SystemExit(2) if not lock.acquire() else os._exit(0)\n"
        )
        crashed = subprocess.run([sys.executable, "-c", crash_script, str(self.lock_path)], cwd=Path.cwd(), timeout=10)
        self.assertEqual(crashed.returncode, 0)
        recovered = ProcessLock(self.lock_path)
        self.assertTrue(recovered.acquire())
        recovered.release()

    def test_schema_initialization_is_idempotent(self):
        first = SqliteInboxRepository(self.database_path)
        first.close()
        second = SqliteInboxRepository(self.database_path)
        try:
            columns = [row[1] for row in second.connection.execute("PRAGMA table_info(runtime_runs)").fetchall()]
        finally:
            second.close()
        self.assertEqual(columns, ["id", "trigger_type", "instance_id", "status", "started_at", "completed_at",
                                   "error_class", "lock_outcome", "messages_polled", "inbox_errors",
                                   "attachments_uploaded", "attachments_skipped", "attachment_errors",
                                   "outcome_status", "created_at"])

    def test_named_main_invokes_coordinator_without_constructing_business_early(self):
        settings = SimpleNamespace(state_db_path=self.database_path, sqlite_busy_timeout_ms=1234)
        coordinator = Mock()
        coordinator.execute_once.return_value = SimpleNamespace(status="skipped_locked")
        with patch.object(runtime_main.Settings, "from_env", return_value=settings), \
             patch.object(runtime_main, "RuntimeCoordinator", return_value=coordinator) as coordinator_type, \
             patch.object(runtime_main, "run_once") as business:
            self.assertEqual(runtime_main.main(), 0)
            coordinator.execute_once.assert_called_once_with()
            business.assert_not_called()
            self.assertEqual(coordinator_type.call_args.kwargs["sqlite_busy_timeout_ms"], 1234)
            callback = coordinator_type.call_args.args[1]
            callback()
            business.assert_called_once_with(settings)

    def _runtime_row(self):
        repository = SqliteInboxRepository(self.database_path)
        try:
            return repository.connection.execute("SELECT * FROM runtime_runs").fetchone()
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
