"""Local/private, server-rendered human review console. No provider calls live here."""

from __future__ import annotations

import logging
import os
import hmac
import secrets
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template_string, request, session, url_for

from .inbox_repository import SqliteInboxRepository
from .logging_config import configure_logging
from .review_models import ReviewConflictError, ReviewNotFoundError, ReviewValidationError
from .review_queue_service import ReviewQueueService


logger = logging.getLogger(__name__)

BASE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>{{ title }} · Review Console</title><style>
body{font:16px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#20242a;background:#f6f7f8}
a{color:#174ea6}.card,table{background:white;border:1px solid #d9dde3;border-radius:8px;padding:1rem;margin:1rem 0}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.7rem;border-bottom:1px solid #e6e8eb;vertical-align:top}
.status{font-weight:700;text-transform:capitalize}.pending{color:#875d00}.approved{color:#16713a}.rejected{color:#a32626}
.tabs a{margin-right:1rem}.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.message{white-space:pre-wrap;background:#f7f8fa;padding:.8rem;border-radius:5px}
dt{font-weight:700;margin-top:.6rem}dd{margin-left:0}textarea{width:100%;min-height:16rem;box-sizing:border-box}input{padding:.45rem}
button{padding:.6rem 1rem;margin:.5rem .5rem 0 0}.danger{background:#a32626;color:white;border:0}.approve{background:#16713a;color:white;border:0}
.notice{padding:.8rem;background:#fff4ce;border:1px solid #e1c45b}.error{background:#fde7e9;border-color:#d83b47}.muted{color:#606974}
@media(max-width:750px){.grid{grid-template-columns:1fr}table{display:block;overflow:auto}}
</style></head><body><header><h1>Human Review Console</h1><p class=muted>Approval records a local decision only. It does not send or create email.</p></header>
{{ content|safe }}</body></html>"""

QUEUE = """<nav class=tabs>{% for value in ('pending','approved','rejected') %}<a href='{{ url_for("queue", status=value) }}' {% if status==value %}aria-current=page{% endif %}>{{ value|capitalize }}</a>{% endfor %}</nav>
{% if items %}<table><thead><tr><th>ID</th><th>Status / time</th><th>Message</th><th>Reason</th><th>Confidence</th><th>Draft</th></tr></thead><tbody>
{% for item in items %}<tr><td><a href='{{ url_for("detail", review_item_id=item.id) }}'>#{{ item.id }}</a></td><td><span class='status {{ item.status }}'>{{ item.status }}</span><br><small>{{ item.updated_at }}</small></td>
<td>{% if item.sender %}{{ item.sender }}<br>{% endif %}<strong>{{ item.subject or 'No subject' }}</strong>{% if item.summary %}<br><small>{{ item.summary }}</small>{% endif %}</td>
<td>{{ item.review_reason }}</td><td>{{ '%.0f%%'|format(item.confidence*100) if item.confidence is not none else '—' }}</td><td>{{ 'Yes' if item.has_draft else 'No' }}</td></tr>{% endfor %}
</tbody></table>{% else %}<div class=card>No {{ status }} review items.</div>{% endif %}"""

DETAIL = """<p><a href='{{ url_for("queue", status=detail.item.status if detail.item.status in ("pending","approved","rejected") else "pending") }}'>← Queue</a></p>
{% if error %}<div class='notice error'>{{ error }}</div>{% endif %}
<div class=card><h2>Review #{{ detail.item.id }}</h2><p class='status {{ detail.item.status }}'>{{ detail.item.status }}</p><dl><dt>Review type</dt><dd>{{ detail.item.review_type }}</dd><dt>Created</dt><dd>{{ detail.item.created_at }}</dd><dt>Updated</dt><dd>{{ detail.item.updated_at }}</dd>{% if detail.item.reviewer_id %}<dt>Reviewer</dt><dd>{{ detail.item.reviewer_id }}</dd>{% endif %}</dl></div>
<section class=card><h2>Message / thread context</h2>{% if detail.messages %}{% for message in detail.messages %}<article><h3>{{ message.subject or 'No subject' }}</h3><p><strong>From:</strong> {{ message.sender }} · {{ message.received_at }}</p><div class=message>{{ message.body_text }}</div></article>{% endfor %}{% else %}<p>Related messages are unavailable.</p>{% endif %}</section>
<div class=grid><section class=card><h2>Message analysis</h2>{{ analysis(detail.message_analysis) }}</section><section class=card><h2>Thread analysis</h2>{{ analysis(detail.thread_analysis) }}</section></div>
<section class=card><h2>Supporting context</h2>{% if detail.retrieval_context %}{% for context in detail.retrieval_context %}<article><h3>#{{ context.rank }} {{ context.title or context.source_filename }}</h3><div class=message>{{ context.chunk_text }}</div></article>{% endfor %}{% else %}<p>No retrieval context is available.</p>{% endif %}</section>
<section class=card><h2>Policy decision</h2><dl><dt>Decision</dt><dd>{{ detail.policy.decision or 'Unavailable' }}</dd><dt>Why human review</dt><dd>{{ detail.policy.primary_reason or detail.item.review_type }}</dd><dt>Reason flags</dt><dd>{{ detail.policy.reason_codes|join(', ') or 'None recorded' }}</dd><dt>Policy version</dt><dd>{{ detail.policy.version or 'Unavailable' }}</dd></dl></section>
<section class=card><h2>Reply draft</h2>{% if detail.item.status == 'pending' %}{% if detail.original_draft_body is not none %}<form method=post action='{{ url_for("decide", review_item_id=detail.item.id) }}'><input type=hidden name=csrf_token value='{{ csrf_token() }}'><input type=hidden name=expected_updated_at value='{{ detail.item.updated_at }}'><label>Reviewer <input name=reviewer_id value='operator' maxlength=128 required></label><br><label>Optional note <input name=note maxlength=1000></label><p><label>Draft<textarea name=draft_body maxlength=50000 required>{{ detail.original_draft_body }}</textarea></label></p><button class=approve name=action value=approve>Approve current draft</button><button class=danger name=action value=reject formnovalidate>Reject</button></form>{% else %}<div class=notice>No reply draft is available. Approval is disabled.</div><form method=post action='{{ url_for("decide", review_item_id=detail.item.id) }}'><input type=hidden name=csrf_token value='{{ csrf_token() }}'><input type=hidden name=expected_updated_at value='{{ detail.item.updated_at }}'><label>Reviewer <input name=reviewer_id value='operator' maxlength=128 required></label><br><label>Optional note <input name=note maxlength=1000></label><br><button class=danger name=action value=reject>Reject</button></form>{% endif %}
{% else %}<div class=notice>This completed review is read-only.</div><h3>Original AI draft</h3><div class=message>{{ detail.original_draft_body or 'Unavailable' }}</div>{% if detail.item.status == 'approved' %}<h3>Human-approved draft</h3><div class=message>{{ detail.item.approved_draft_body or 'Unavailable' }}</div>{% endif %}{% endif %}</section>
<section class=card><h2>History</h2><ul>{% for event in detail.history %}<li>{{ event.created_at }} — {{ event.event_type }}{% if event.reviewer_id %} by {{ event.reviewer_id }}{% endif %}{% if event.note %}: {{ event.note }}{% endif %}</li>{% endfor %}</ul></section>"""


def create_app(database_path: Path | str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    path = Path(database_path or os.environ.get("STATE_DB_PATH", "data/state.sqlite3"))
    # Open once now so startup fails clearly on an unusable database.
    SqliteInboxRepository(path).close()
    app.config["STATE_DB_PATH"] = path

    def repository():
        if "review_repository" not in g:
            g.review_repository = SqliteInboxRepository(app.config["STATE_DB_PATH"])
        return g.review_repository

    def service():
        return ReviewQueueService(repository())

    @app.teardown_appcontext
    def close_repository(_error=None):
        value = g.pop("review_repository", None)
        if value is not None:
            value.close()

    @app.template_global()
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    @app.template_global()
    def analysis(value):
        if not value:
            return "Not available."
        from markupsafe import Markup, escape
        fields = []
        for key, item in value.items():
            if key in {"id", "conversation_analysis_run_id", "conversation_id", "latest_message_id", "created_at"}:
                continue
            label = key.replace("_", " ").capitalize()
            display = ", ".join(map(str, item)) if isinstance(item, (list, tuple)) else ("Yes" if item is True else "No" if item is False else item)
            if display not in (None, "", (), []):
                fields.append(f"<dt>{escape(label)}</dt><dd>{escape(display)}</dd>")
        return Markup("<dl>" + "".join(fields) + "</dl>")

    def page(title, template, **context):
        content = render_template_string(template, **context)
        return render_template_string(BASE, title=title, content=content)

    @app.get("/")
    def queue():
        status = request.args.get("status", "pending")
        try:
            items = service().list_items(status)
        except ReviewValidationError:
            abort(400)
        return page("Queue", QUEUE, items=items, status=status)

    @app.get("/reviews/<int:review_item_id>")
    def detail(review_item_id):
        try:
            value = service().detail(review_item_id)
        except ReviewNotFoundError:
            abort(404)
        logger.info("event=review_viewed review_item_id=%s status=%s", review_item_id, value.item.status)
        return page(f"Review #{review_item_id}", DETAIL, detail=value, error=None)

    @app.post("/reviews/<int:review_item_id>/decision")
    def decide(review_item_id):
        submitted_token = request.form.get("csrf_token", "")
        expected_token = session.get("csrf_token", "")
        if not expected_token or not hmac.compare_digest(submitted_token, expected_token):
            abort(400)
        action = request.form.get("action")
        reviewer_id = request.form.get("reviewer_id", "")
        note = request.form.get("note") or None
        expected = request.form.get("expected_updated_at")
        try:
            if action == "approve":
                service().approve(review_item_id, reviewer_id, note,
                                  approved_draft_body=request.form.get("draft_body"), expected_updated_at=expected)
            elif action == "reject":
                service().reject(review_item_id, reviewer_id, note, expected_updated_at=expected)
            else:
                raise ReviewValidationError("Unknown review action")
        except ReviewNotFoundError:
            abort(404)
        except (ReviewValidationError, ReviewConflictError, ValueError) as exc:
            try:
                value = service().detail(review_item_id)
            except ReviewNotFoundError:
                abort(404)
            status = 409 if isinstance(exc, ReviewConflictError) else 400
            return page(f"Review #{review_item_id}", DETAIL, detail=value, error=str(exc)), status
        return redirect(url_for("detail", review_item_id=review_item_id), code=303)

    return app


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        app = create_app()
    except Exception as exc:
        logger.error("event=review_console_start_failed error_class=%s", type(exc).__name__)
        return 1
    app.run(host="0.0.0.0", port=8000, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
