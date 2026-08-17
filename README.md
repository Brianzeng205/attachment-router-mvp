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
