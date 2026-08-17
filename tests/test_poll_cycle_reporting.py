import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.inbox_repository import SqliteInboxRepository
from app.main import process_poll_cycle
from app.orchestrator import ProcessingSummary
from app.runtime_coordinator import RuntimeCoordinator
from app.runtime_models import PollCycleReport


class _Stage:
    def __init__(self, errors=0):
        self.errors = errors

    def ingest_all(self, _items):
        return SimpleNamespace(errors=self.errors)

    def analyze_all(self, _items):
        return SimpleNamespace(errors=self.errors)


class _Repository:
    def __init__(self, conversations=(), analysis=None):
        self.conversations = tuple(conversations)
        self.analysis = analysis

    def list_conversations(self):
        return list(self.conversations)

    def latest_successful_conversation_analysis(self, _conversation_id):
        return self.analysis


class _Attachments:
    def __init__(self, summary):
        self.summary = summary
        self.calls = 0

    def process_all(self):
        self.calls += 1
        return self.summary


def _cycle(*, messages=(), repository=None, ingestion=None, inbox=None, conversation=None,
           retrieval=None, attachments=None, **optional):
    return process_poll_cycle(
        messages=messages,
        repository=repository or _Repository(),
        message_ingestion_service=ingestion or _Stage(),
        inbox_analysis_service=inbox or _Stage(),
        conversation_analysis_service=conversation or _Stage(),
        knowledge_retrieval_service=retrieval or Mock(),
        attachment_processor=attachments or _Attachments(ProcessingSummary()),
        **optional,
    )


class PollCycleReportTests(unittest.TestCase):
    def test_zero_message_cycle_returns_clean_valid_report(self):
        report = _cycle()
        self.assertEqual(report, PollCycleReport())
        self.assertFalse(report.has_partial_failures)

    def test_message_and_attachment_counts_use_observed_cycle_summaries(self):
        attachments = _Attachments(ProcessingSummary(uploaded=2, skipped=3, errors=0))
        report = _cycle(messages=(object(), object()), attachments=attachments)
        self.assertEqual(report, PollCycleReport(2, 0, 2, 3, 0))
        self.assertFalse(report.has_partial_failures)
        self.assertEqual(attachments.calls, 1)

    def test_upstream_conversation_error_is_counted_once_without_fabricated_downstream_errors(self):
        conversation = SimpleNamespace(id=1)
        retrieval = Mock()
        attachments = _Attachments(ProcessingSummary(uploaded=1))
        report = _cycle(
            messages=(object(),), repository=_Repository((conversation,), analysis=None),
            conversation=_Stage(errors=1), retrieval=retrieval, attachments=attachments,
        )
        self.assertEqual(report.inbox_errors, 1)
        self.assertTrue(report.has_partial_failures)
        retrieval.retrieve.assert_not_called()
        self.assertEqual(attachments.calls, 1)

    def test_safe_reply_draft_failure_outcome_counts_as_one_inbox_error(self):
        conversation = SimpleNamespace(id=1)

        class DraftRepository(_Repository):
            def latest_successful_retrieval(self, _conversation_id, _analysis_id):
                return 9, []

            def list_messages_for_conversation(self, _conversation_id):
                return []

        repository = DraftRepository((conversation,), analysis=(7, object()))
        retrieval = Mock()
        drafting = Mock()
        drafting.create_draft.return_value = SimpleNamespace(failed=True, draft=None)
        builder = Mock()
        with patch("app.main.ReplyDraftInput.from_context", return_value=object()):
            report = _cycle(
                messages=(object(),), repository=repository, retrieval=retrieval,
                reply_draft_service=drafting, thread_context_builder=builder,
            )
        self.assertEqual(report.inbox_errors, 1)
        drafting.create_draft.assert_called_once()

    def test_attachment_error_is_partial_while_idempotent_skip_is_not(self):
        failed = _cycle(attachments=_Attachments(ProcessingSummary(errors=1)))
        skipped = _cycle(attachments=_Attachments(ProcessingSummary(skipped=4)))
        self.assertEqual((failed.attachment_errors, failed.has_partial_failures), (1, True))
        self.assertEqual((skipped.attachments_skipped, skipped.has_partial_failures), (4, False))

    def test_report_rejects_negative_non_integer_and_boolean_counts(self):
        for value in (-1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PollCycleReport(messages_polled=value)


class RuntimeReportPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "state.sqlite3"

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, report):
        result = RuntimeCoordinator(self.database_path, lambda: report).execute_once()
        repository = SqliteInboxRepository(self.database_path)
        try:
            persisted = repository.get_runtime_run(result.runtime_run_id)
        finally:
            repository.close()
        return result, persisted

    def test_coordinator_persists_clean_report_as_completed(self):
        report = PollCycleReport(2, 0, 1, 1, 0)
        result, persisted = self._run(report)
        self.assertEqual((result.status, persisted.status), ("completed", "completed"))
        self.assertEqual(
            (persisted.messages_polled, persisted.inbox_errors, persisted.attachments_uploaded,
             persisted.attachments_skipped, persisted.attachment_errors),
            (2, 0, 1, 1, 0),
        )

    def test_coordinator_persists_recoverable_errors_as_partial_without_error_class(self):
        report = PollCycleReport(3, 1, 2, 0, 1)
        result, persisted = self._run(report)
        self.assertEqual((result.status, persisted.status), ("partial", "partial"))
        self.assertEqual((persisted.inbox_errors, persisted.attachment_errors), (1, 1))
        self.assertIsNone(persisted.error_class)

    def test_phase_7a_schema_upgrades_additively_and_supports_effective_partial_status(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("""CREATE TABLE runtime_runs (
            id INTEGER PRIMARY KEY, trigger_type TEXT NOT NULL, instance_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted','abandoned')),
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, error_class TEXT,
            lock_outcome TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        connection.commit()
        connection.close()

        report = PollCycleReport(messages_polled=1, inbox_errors=1)
        result, persisted = self._run(report)
        self.assertEqual((result.status, persisted.status), ("partial", "partial"))
        repository = SqliteInboxRepository(self.database_path)
        try:
            raw = repository.connection.execute(
                "SELECT status, outcome_status, messages_polled, inbox_errors FROM runtime_runs"
            ).fetchone()
        finally:
            repository.close()
        self.assertEqual(tuple(raw), ("completed", "partial", 1, 1))


if __name__ == "__main__":
    unittest.main()
