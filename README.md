# Attachment routing MVP

A small Python MVP that polls Gmail, classifies email attachments, and routes them to approved Google Drive folders. It makes no changes to Gmail messages.

## Setup

1. Create a virtual environment and install dependencies: `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env`, set Drive folder IDs, and configure the OAuth file paths.
3. Run one polling cycle: `python -m app.main`.
4. Run tests: `python -m unittest discover -s tests -v`.

## Design

- `EmailClient`, `DocumentClassifier`, and `DriveClient` are small provider boundaries in `app/interfaces.py`.
- `AttachmentProcessor` validates every classifier response, enforces configured Drive destinations, and routes low-confidence or invalid results to Needs Review.
- `GoogleDriveClient` uses the official Google Drive API, validates the configured folder is a real Drive folder, and independently enforces the allowlist.
- `SqliteStateManager` records each `(email_id, attachment_id)` only after Drive upload succeeds.
- `ClaudeDocumentClassifier` sends bounded email/attachment context to Anthropic and returns only validated structured classification data. It has no Drive API access.
- `GmailClient` polls a configured Gmail search query using read-only OAuth and supports multiple attachments per message.
- `python -m app.main` composes Gmail, Claude, Drive, and SQLite for one complete polling cycle. Schedule it externally with Task Scheduler or cron if required.

## Scheduled Windows deployment

Production polling remains a one-shot command owned by Windows Task Scheduler; the Python application contains no recurring loop. The recommended initial interval is every five minutes, with Task Scheduler configured to **Do not start a new instance** and the application advisory lock retained as defense in depth.

See [Scheduled Windows deployment](docs/windows-task-scheduler.md) for the exact program, arguments, working directory, exit semantics, 30-minute maximum-runtime recommendation, crash recovery, operational verification, retry ownership, and the known Google transport-timeout limitation.

## Configuration

`ALLOWED_DRIVE_FOLDERS` is a JSON map from a classifier `target_folder` name to a Google Drive folder ID. Claude can never select a folder outside that map. The Needs Review folder is separately configured. The Drive client accepts only IDs in these two configuration values; it never creates folders.

## Operational observability

Each polling invocation is recorded in the SQLite database configured by `STATE_DB_PATH`, including its lifecycle status, safe error class, and content-free processing counts. Inspect the 20 most recent invocations with `python -m app.runtime_status`. This status command needs only `STATE_DB_PATH` (default: `data/state.sqlite3`); it does not require Gmail, Drive, or Anthropic configuration or credentials.

Runtime and provider-retry logs use stable `key=value` fields such as `event`, `status`, `run_id`, `provider`, `operation`, and `error_class`. Operational events intentionally omit message bodies, sender addresses, filenames, OAuth tokens, API keys, and exception messages.

Logging is configured with these environment variables:

- `LOG_LEVEL`: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (default: `INFO`).
- `LOG_FILE`: optional rotating log path. Leave empty for console-only logging; `.env.example` uses `logs/agent.log`.
- `LOG_MAX_BYTES`: file size in bytes before rotation, from 1,024 through 1,073,741,824 (default: 5,242,880).
- `LOG_BACKUP_COUNT`: retained rotated files, from 0 through 100 (default: 3).

The application creates the log directory when file logging is enabled. Local logs and runtime SQLite artifacts under `data/` are ignored by Git; other project files placed in `data/` are not ignored wholesale.

## Human Review Console

Phase 8A provides a local, server-rendered operator console over review items already persisted in SQLite. Start it from the repository root with:

```bash
python -m app.review_console
```

Open `http://localhost:8000`. In GitHub Codespaces, use the forwarded port 8000 URL and keep its visibility private. The console reads `STATE_DB_PATH` (default `data/state.sqlite3`) and creates/additively upgrades the local schema when opened. It does not require Gmail, Google Drive, or Claude credentials and does not start polling, analysis, retrieval, or draft generation.

The queue defaults to pending items and can be filtered to approved or rejected history. A pending item shows persisted message/thread context, readable analyses, supporting retrieval context, the policy decision, and the original generated reply. An operator can edit the reply and approve it, or reject it with an optional note. Approval stores the final human-approved text separately from the immutable AI-generated draft. Completed items are read-only, and stale or repeated submissions cannot replace an existing decision.

**Approval means only “approved for a future downstream action.” It does not send email, create a Gmail draft, or modify Gmail in any way.** Phase 8A includes no send action or Gmail write scope.

This console has no authentication and is intended only for a trusted local/private operator environment. It must not be exposed to the public internet. Other current limitations are a single operator identity entered per decision, plain-text message/draft rendering, no decision reversal, and no downstream Gmail action.

## Approved action execution queue (Phase 8B)

Phase 8B adds a durable orchestration boundary after human approval. In the same SQLite transaction that changes a review from `pending` to `approved`, the repository creates one execution intent. Phase 8C1 evolves its action to the truthful `create_gmail_draft`; migrated `send_approved_reply` rows retain that old value in `legacy_action_type` for provenance and are never silently treated as completed sends. The intent stores the exact approved body, stable provider thread/message identifiers, a deterministic execution ID and idempotency key, and no credentials or unrelated message content. A database uniqueness constraint on the source review ID makes enqueue idempotent even across concurrent callers. The original AI draft, the review's approved snapshot, and the execution snapshot remain separate records; queue processing never regenerates or edits them.

The state machine is:

```text
pending → processing → completed
                 ↘ retry_wait → processing
                 ↘ failed
```

`completed` and `failed` are terminal. Claiming is an atomic SQLite transaction. Each claim has an unpredictable token, worker identifier, claim timestamp, and lease expiry; completion and failure updates require the current token. An expired claim is explicitly recovered into a retry state (or terminal failure at the configured maximum), so a process crash does not strand work and a stale worker cannot overwrite its successor. Retryable failures use deterministic bounded exponential backoff and are not claimable before `next_attempt_at`; permanent or exhausted failures become terminal. Persisted failure information is restricted to a safe error code and small allowlisted metadata.

Inspect the credential-free queue with:

```bash
python -m app.execution_status
```

For a database containing Phase 8A approvals created before this queue existed, run:

```bash
python -m app.execution_status --reconcile
```

Reconciliation considers only completed `approved` reviews with a valid persisted approved body and provider identity. It skips malformed, pending, and rejected records, and repeated runs do not duplicate intents. Neither inspection nor reconciliation needs Gmail, Drive, Claude, internet connectivity, or OAuth credentials, and neither processes an action.

Approval and queue inspection still perform no provider operation. The guarantee at this boundary is exactly one durable execution intent per eligible approved review, idempotent enqueue, atomic claim, durable transitions, and stale-claim protection—not mathematically strict exactly-once external execution.

## Phase 8C1 Gmail Draft Executor

**Phase 8C1 never sends email.** Its sole Gmail mutation is creating a draft for another human inspection inside Gmail. Although the required `https://www.googleapis.com/auth/gmail.compose` OAuth scope technically also authorizes sending, the production adapter exposes only `users.drafts.create`; it exposes no draft send, message send, modify, label, archive, trash, or delete operation.

Approval creates the immutable `create_gmail_draft` intent but does not call Gmail. Review Console startup, page loads, polling, and status inspection also do not execute it. An operator must deliberately run one item:

```bash
python -m app.gmail_draft_worker --once
```

A credential-free, non-mutating validation preview is available with `python -m app.gmail_draft_worker --dry-run`. It claims nothing, calls no provider, and prints only the execution ID and MIME byte size—not recipient, subject, or body. `python -m app.execution_status` shows safe execution/provider state and the Gmail draft ID after confirmed creation.

The executor atomically claims one item using the Phase 8B lease token, builds a plain-text MIME reply only from the intent's immutable approved body, records an external-attempt event immediately before dispatch, calls `drafts.create` once, then persists the returned Gmail draft/message/thread identities and marks completion in one SQLite transaction. A confirmed result is unique per execution and terminal, so later worker invocations cannot create it again. The worker never invokes Claude or regenerates text.

Threading is fail-closed. Ingestion persists normalized Gmail `Message-ID`, `References`, `Reply-To`, sender, subject, and thread ID. Execution requires a valid thread ID and RFC Message-ID, chooses one recipient from persisted `Reply-To` (otherwise sender), rejects header injection, malformed/multiple/self recipients, adds no CC/BCC, normalizes a single `Re:` subject, and supplies Gmail `threadId`, `In-Reply-To`, and `References`. Unicode is serialized with Python's standard email library and base64URL encoded into `message.raw`. Missing legacy metadata fails before any Gmail call rather than creating an unrelated draft.

### Compose authorization

Readonly ingestion remains on `GMAIL_OAUTH_TOKEN_FILE` with only `gmail.readonly`. Draft execution uses a distinct `GMAIL_COMPOSE_OAUTH_TOKEN_FILE` and requests exactly `gmail.compose`; the desktop client JSON may be shared. Set `GMAIL_COMPOSE_ACCOUNT_EMAIL` to the mailbox address so the executor can reject accidental self-replies. Only `--once` loads compose credentials; Review Console, status, polling, and dry-run do not.

Existing readonly tokens do not gain compose permission. Do not reuse the readonly token path. To authorize compose, choose a new private token path, run `python -m app.gmail_draft_worker --once`, and complete Google's browser consent. If an existing configured compose token lacks the scope, the worker stops with a compose-authorization error and does not delete or overwrite it. Move that file aside manually or choose another path only after verifying it is the token you intend to replace. To return to readonly-only operation, stop invoking the draft worker and remove the compose settings/token from the runtime environment; polling continues with its separate readonly token.

### Ambiguous outcomes and reconciliation

Gmail's create POST has no local SQLite idempotency key. If a timeout, disconnect, malformed success response, or other transport failure occurs after dispatch, Gmail may have created the draft even though its ID was not received. The executor persists `outcome_unknown`, clears the lease, and blocks normal claiming and all automatic retries. This state survives restart.

Phase 8C1 deliberately performs no speculative Gmail search and makes no guarantee that Gmail preserves or indexes the deterministic MIME `Message-ID`. An operator must inspect Gmail manually. If the draft is found, confirm its IDs without another create call:

```bash
python -m app.gmail_draft_worker --reconcile exec_... --draft-id gmail-draft-id --thread-id gmail-thread-id
```

The thread ID must match the intent. If no draft can be established, Phase 8C1 leaves the item unresolved; it does not offer a blind retry switch. The actual guarantee is one durable local intent, one active claim, no re-execution after a confirmed result, and at most one automatic create attempt per unresolved external outcome—not strict exactly-once draft creation.

Current limitations are plain-text single-recipient replies, manual reconciliation of unknown outcomes, no reply-all, no attachments, and no Gmail-side E2E test in the automated suite. A future Phase 8C2 may add a separately reviewed and separately implemented send boundary; no such boundary exists here.

## Google Drive setup

1. Create or choose a Google Cloud project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Google Drive API** under APIs & Services.
3. Configure the OAuth consent screen for the single Google account that owns or can edit the destination folders. Add that account as a test user while the app is in testing mode.
4. Create an OAuth 2.0 **Desktop app** client and download its JSON file. Store it outside the repository if possible, then set `GOOGLE_OAUTH_CLIENT_SECRETS_FILE` to its path.
5. Set `GOOGLE_OAUTH_TOKEN_FILE` to a private local path. On first run, the adapter opens a browser for the account to grant the requested Drive scope; it stores the resulting refresh token at this path. Later runs refresh it automatically.

The required scope is `https://www.googleapis.com/auth/drive`. This full Drive scope is used by this small MVP so it can validate configured folders, find prior application uploads after a crash, and upload into client-selected folders. The OAuth account should have the minimum practical access to those folders.

To find a folder ID, open that folder in Google Drive. In a URL such as `https://drive.google.com/drive/folders/1AbC...`, the portion after `/folders/` is the folder ID. Put only those IDs in `NEEDS_REVIEW_FOLDER_ID` and `ALLOWED_DRIVE_FOLDERS`.

Never commit the downloaded OAuth client JSON, OAuth token JSON, or `.env`. They are covered by `.gitignore`; keeping them outside the repository is preferred.

## Gmail setup and polling

In the same Google Cloud project, enable the **Gmail API**. The existing desktop OAuth client JSON can be reused for Gmail and Drive. Configure the consent screen and add the single processing account as a test user if the app is in testing mode. Gmail uses the restricted, read-only scope `https://www.googleapis.com/auth/gmail.readonly`; it uses a separate token file (`GMAIL_OAUTH_TOKEN_FILE`) because that scope differs from Drive.

On the first `python -m app.main` run, Gmail opens browser consent if a valid Gmail token is absent. The default query is `has:attachment`; set `GMAIL_SEARCH_QUERY` to narrow it. Gmail messages are never marked read, moved, deleted, labelled, or otherwise modified.

The adapter retrieves full candidate messages, prefers a `text/plain` MIME body and falls back to simple HTML stripping, then recursively finds normal file attachments. Inline images without an explicit attachment disposition are ignored. `MAX_ATTACHMENT_BYTES` limits in-memory downloads (10 MB by default); malformed, oversized, or inaccessible attachments are logged and skipped without stopping the rest of the polling cycle.

Each attachment is keyed by Gmail message ID plus Gmail attachment ID (or a stable MIME-part fallback), so two same-named attachments in one message remain distinct. SQLite skips attachment identities that have already completed a Drive upload.

## Claude classifier setup

Create an Anthropic API key in the [Anthropic Console](https://console.anthropic.com/), put it in `ANTHROPIC_API_KEY` in your local `.env`, and set `ANTHROPIC_MODEL` to a Claude model that supports structured outputs (the example uses `claude-haiku-4-5`). Never commit `.env` or expose the key in logs.

For each attachment, the classifier receives the sender, subject, bounded email body, attachment filename/MIME type, configured **logical** folder labels, and bounded extracted text when available. Plain-text files and text-based PDFs are supported. Scanned/image-only PDFs, malformed PDFs, binaries, and files over 10 MB contribute no extracted text; classification still uses email metadata and the filename.

Claude is constrained to JSON fields and logical labels such as `invoices`. It never receives Drive IDs. The application validates the response again, maps only configured labels to real folders, and controls the final filename extension. Low confidence, invalid output, an unknown label, or a Claude API error routes the attachment to Needs Review; it is marked processed only after that Drive upload succeeds.

## Retry and duplicate behavior

SQLite remains the primary processed-attachment record and is written only after Drive confirms an upload. If the process crashes after Drive succeeds but before that SQLite write, the next run uses a stable hash of the email and attachment IDs to search the configured Drive folder for the application's matching `appProperties` marker. It then reuses the existing Drive file ID instead of creating another file. This is a small best-effort mitigation for a single-process MVP, not a distributed transaction.

New uploads include a short stable key in their visible filename, preventing ambiguous same-name uploads from different attachments while retaining the classifier-provided title and the source attachment extension.
