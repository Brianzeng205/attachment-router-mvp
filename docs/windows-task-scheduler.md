# Scheduled Windows deployment

Windows Task Scheduler owns recurrence. The application remains a one-shot process:

```text
Windows Task Scheduler
        -> python -m app.main
        -> RuntimeCoordinator
        -> OS advisory lock
        -> one polling cycle
        -> local Human Review Queue
        -> process exit
```

No Python scheduling loop or service is required.

## Prerequisites

1. Install the project dependencies in a dedicated virtual environment.
2. Complete one interactive Gmail and Drive OAuth run under the same Windows account that will own the task. A scheduled process must not depend on opening an OAuth browser.
3. Keep the existing local `.env`, OAuth token files, and credentials available to that account with appropriately restricted filesystem permissions. Never put their contents in task arguments or commit them.
4. Confirm that the task account can read the project and configuration paths and write the configured SQLite database, OAuth token locations, and application output destinations.

The examples below use placeholders. Replace `C:\path\to\project` with the project directory; do not put credentials or tokens in the task definition.

## Smoke-test one cycle first

Open PowerShell in the project directory and run the same interpreter and command that the task will use:

```powershell
Set-Location 'C:\path\to\project'
& '.\.venv\Scripts\python.exe' -m app.main
$LASTEXITCODE
```

Confirm that:

- the process finishes;
- `runtime_runs` records the invocation;
- Gmail was not mutated;
- expected attachment routing and local review records were produced, if applicable;
- no Gmail draft or email was created or sent.

If `STATE_DB_PATH` uses its default, this content-free operational query shows recent runs without exposing email or draft data:

```powershell
& '.\.venv\Scripts\python.exe' -c "import sqlite3; c=sqlite3.connect(r'data/state.sqlite3'); print(c.execute('SELECT id,status,started_at,completed_at,messages_polled,inbox_errors,attachments_uploaded,attachments_skipped,attachment_errors FROM runtime_runs ORDER BY id DESC LIMIT 10').fetchall())"
```

Substitute the configured database path when it differs from the default.

## Create the scheduled task

Open **Task Scheduler**, choose **Create Task**, and configure the following.

### General

- **Name:** a descriptive local name such as `Inbox Agent Poll`.
- Run the task as the same Windows account used for the successful manual OAuth smoke test.
- Choose whether it may run while the user is logged off according to local security policy. The task account must still have access to the project and private configuration files.
- Do not grant elevated privileges unless the project paths genuinely require them.

### Trigger

- Begin the task **On a schedule**.
- Use a daily trigger and enable **Repeat task every: 5 minutes**.
- Set **for a duration of: Indefinitely**.
- Keep the trigger enabled.

Five minutes is the recommended initial MVP interval. Operators may choose 10 or 15 minutes later without changing Python code; the interval is not hardcoded in the application.

### Action

Use **Start a program** with these three distinct fields:

| Field | Value |
| --- | --- |
| Program/script | `C:\path\to\project\.venv\Scripts\python.exe` |
| Add arguments | `-m app.main` |
| Start in | `C:\path\to\project` |

Use the actual Python executable that has the project dependencies installed. The **Start in** value is important: it keeps relative `.env`, knowledge, SQLite, prompt, and token paths consistent with manual execution.

Do not place API keys, OAuth tokens, passwords, or `.env` values in any of these fields. No launcher script is required for the normal deployment.

### Conditions

Practical starting choices are:

- enable **Start only if the following network connection is available** when appropriate for the machine;
- optionally enable **Wake the computer to run this task** for an always-on workstation;
- decide whether AC-power restrictions are appropriate for the device.

These are operational choices, not application requirements.

### Settings

- Select **If the task is already running: Do not start a new instance**.
- Enable **Stop the task if it runs longer than: 30 minutes** as a conservative initial operational limit.
- Do not configure rapid automatic restarts or immediate whole-cycle retry loops. Let the next normal scheduled trigger handle unfinished work through existing idempotency.
- It is reasonable to allow the task to run as soon as possible after a missed scheduled start.

The 30-minute limit is not a lease timeout and never authorizes lock stealing. If Task Scheduler terminates the process, Windows releases the effective OS advisory lock. The next process that acquires the lock marks any older `running` history row `abandoned` before starting a new run.

## Exit results and run status

The command has these scheduler-facing outcomes:

| Application outcome | Process exit | Meaning |
| --- | ---: | --- |
| `completed` | `0` | The cycle returned normally with no known recoverable errors. |
| `partial` | `0` | The cycle returned normally but isolated recoverable errors were recorded. Inspect `runtime_runs`; this is operationally handled, not a top-level crash. |
| `skipped_locked` | `0` | Another process owns the advisory lock. No business/provider work starts. |
| `failed` | `1` | An unhandled top-level failure prevented normal completion. |
| `interrupted` | `130` | The process received the supported interactive interruption path. |

Task Scheduler's **Last Run Result** therefore distinguishes top-level failures, while `runtime_runs` distinguishes clean and partial zero-exit cycles. A forced external termination may use a Windows-specific result code and leave a `running` row for abandoned-run recovery.

## Defense in depth and crash recovery

Task Scheduler's **Do not start a new instance** setting is the first overlap guard. The application OS advisory lock remains authoritative defense in depth for manual concurrent runs, duplicate task definitions, and scheduler misconfiguration.

Lock ownership is based on the operating system, not on the existence of the `.poll.lock` file. A persistent unlocked file is harmless. There is no time-based lock stealing.

Crash recovery is:

```text
process exits or crashes
        -> OS releases effective lock
        -> an old runtime_runs row may remain running
        -> next process acquires the OS lock
        -> old running row becomes abandoned
        -> new running row is created
```

## Retry behavior

Gmail, Claude, and Drive transient operations retry only within one logical provider call. The default policy permits three total attempts with bounded exponential delay. Anthropic SDK retries are disabled so retry layers do not multiply. `RuntimeCoordinator` never retries the entire polling cycle.

Do not configure an aggressive Task Scheduler restart storm. A later normal trigger safely revisits unfinished work through message, analysis, retrieval, draft, review, attachment, and Drive-upload idempotency.

## Verify the scheduled deployment

1. Save the task while leaving recurrence disabled if a controlled first run is preferred.
2. Choose **Run** once in Task Scheduler.
3. Check **Last Run Result** and the task history.
4. Inspect only the operational columns in `runtime_runs` using the query above.
5. If possible in a controlled test window, start the task and then invoke the command manually while it is still active. The second invocation should safely report/record no business run because the OS lock is held.
6. After the active cycle completes, run it once more and confirm a subsequent normal invocation works.
7. Confirm the Gmail OAuth scope remains `https://www.googleapis.com/auth/gmail.readonly` and no mailbox mutation, provider draft, send, automatic review transition, or action execution occurred.
8. Enable the recurring trigger.

Task Scheduler does not retain normal console output as an application log. The application uses stdout/stderr logging and `runtime_runs` operational history. If site policy later requires file capture, use an operator-managed wrapper that only sets the working directory, invokes `python -m app.main`, redirects output, and preserves the exit code. It must not contain secrets, loops, sleeps, or retries.

## Known Google transport limitation

Anthropic requests have an explicit 30-second timeout by default. Gmail and Drive operations have bounded application attempts, but the Google API request objects at the current adapter seam do not expose a safe straightforward per-call timeout without replacing the authenticated transport. Phase 7 intentionally does not replace that transport. Treat this as an explicit single-machine MVP operational limitation when choosing the 30-minute Task Scheduler maximum runtime.

## Disable or remove the task

To pause polling, right-click the task and choose **Disable**. To remove deployment scheduling, choose **Delete** after confirming no instance is running. These actions do not delete SQLite history, OAuth files, the persistent lock file, or locally pending review items.

Scheduled execution always stops at local attachment state and the local Human Review Queue. It never approves, rejects, requests changes, creates a Gmail draft, sends email, or executes an external business action.
