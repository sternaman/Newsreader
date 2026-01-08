import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime, date

from dateutil import tz
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import get_conn, init_db
from renderer import audit_and_heal_content, build_issue_epub, derive_byline_from_text, sanitize_html
from renderer.renderer import compute_content_hash, embed_images

app = FastAPI()

TEMPLATES = Jinja2Templates(directory="/app/app/templates")
EPUB_DIR = "/data/epubs"
DEBUG_DIR = "/data/debug"
DEBUG_ARTICLES_DIR = os.path.join(DEBUG_DIR, "articles")
DEBUG_ISSUES_DIR = os.path.join(DEBUG_DIR, "issues")

app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")


def _now_local() -> datetime:
    tzinfo = tz.gettz(os.environ.get("TZ", "UTC"))
    return datetime.now(tzinfo)


def _issue_date() -> str:
    return _now_local().date().isoformat()


def _kindle_send_enabled() -> bool:
    raw = os.environ.get("KINDLE_SEND_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _kindle_send_bin() -> str:
    return os.environ.get("KINDLE_SEND_BIN", "kindle-send").strip() or "kindle-send"


def _kindle_send_config() -> str:
    return os.environ.get("KINDLE_SEND_CONFIG", "/data/KindleConfig.json").strip()


def _kindle_send_state() -> dict:
    bin_path = _kindle_send_bin()
    config_path = _kindle_send_config()
    if not _kindle_send_enabled():
        return {"available": False, "reason": "disabled", "bin": bin_path, "config_path": config_path}
    if not shutil.which(bin_path):
        return {"available": False, "reason": "missing_binary", "bin": bin_path, "config_path": config_path}
    if not os.path.exists(config_path):
        return {"available": False, "reason": "missing_config", "bin": bin_path, "config_path": config_path}
    return {"available": True, "reason": None, "bin": bin_path, "config_path": config_path}


class KindleSendError(RuntimeError):
    pass


def _send_epub_to_kindle(epub_path: str) -> str:
    if not epub_path or not os.path.exists(epub_path):
        raise KindleSendError("EPUB file not found")
    state = _kindle_send_state()
    if not state["available"]:
        reason = state.get("reason") or "kindle-send unavailable"
        raise KindleSendError(reason)
    extra_args = os.environ.get("KINDLE_SEND_ARGS", "").strip()
    cmd = [state["bin"], "--config", state["config_path"]]
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    cmd.extend(["send", epub_path])
    try:
        timeout = int(os.environ.get("KINDLE_SEND_TIMEOUT", "120"))
    except ValueError:
        timeout = 120
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise KindleSendError("kindle-send timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "kindle-send failed").strip()
        raise KindleSendError(detail[:400]) from exc
    return (result.stdout or "").strip()


def _parse_summary(raw: str):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _summarize_audit(audit_entries: list) -> dict:
    summary = {
        "total_articles": len(audit_entries),
        "flagged_articles": 0,
        "healed_articles": 0,
        "fallback_used": 0,
        "issues": {},
    }
    for entry in audit_entries:
        audit_after = entry.get("audit_after") or {}
        issues = audit_after.get("issues") or []
        if audit_after.get("needs_heal"):
            summary["flagged_articles"] += 1
        actions = entry.get("actions") or []
        if actions:
            summary["healed_articles"] += 1
        if "fallback_text_content" in actions:
            summary["fallback_used"] += 1
        for issue in issues:
            summary["issues"][issue] = summary["issues"].get(issue, 0) + 1
    return summary


def _book_or_404(book_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    return row


def _current_issue(book_id: int):
    issue_date = _issue_date()
    with get_conn() as conn:
        issue = conn.execute(
            "SELECT * FROM issues WHERE book_id = ? AND issue_date = ?",
            (book_id, issue_date),
        ).fetchone()
        if issue:
            return issue
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")
        title = f"{book['name']} — {issue_date}"
        epub_path = os.path.join(EPUB_DIR, f"issue_{book_id}_{issue_date}.epub")
        now = _now_local().isoformat()
        conn.execute(
            """
            INSERT INTO issues (
                book_id,
                issue_date,
                title,
                epub_path,
                created_at,
                updated_at,
                build_status,
                build_started_at,
                build_finished_at,
                build_error,
                audit_path,
                audit_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (book_id, issue_date, title, epub_path, now, now, "new", None, None, None, None, None),
        )
        issue = conn.execute(
            "SELECT * FROM issues WHERE book_id = ? AND issue_date = ?",
            (book_id, issue_date),
        ).fetchone()
        return issue


def _build_issue(book_id: int):
    issue = _current_issue(book_id)
    start_day = _now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    issue_debug_dir = os.path.join(DEBUG_ISSUES_DIR, f"issue_{issue['id']}_{issue['issue_date']}")
    os.makedirs(issue_debug_dir, exist_ok=True)
    audit_entries = []
    build_started = _now_local().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE issues SET build_status = ?, build_started_at = ?, build_finished_at = NULL, build_error = NULL, updated_at = ? WHERE id = ?",
            ("building", build_started, build_started, issue["id"]),
        )

    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM articles WHERE book_id = ? AND created_at >= ? ORDER BY created_at ASC",
                (book_id, start_day.isoformat()),
            ).fetchall()

            by_url = {}
            for row in rows:
                existing = by_url.get(row["url"])
                if not existing or row["created_at"] > existing["created_at"]:
                    by_url[row["url"]] = row

            chapters = []
            for row in by_url.values():
                byline = row["byline"] or derive_byline_from_text(row["text_content"], row["source_domain"])
                content = sanitize_html(row["content_html"])
                content = embed_images(content, fetch_remote=True, base_url=row["url"])
                healed_content, audit_before, audit_after, actions = audit_and_heal_content(
                    content,
                    row["text_content"],
                    row["source_domain"],
                    byline,
                )
                chapters.append(
                    {
                        "title": row["title"],
                        "content_html": healed_content,
                        "article_id": row["id"],
                        "byline": byline,
                        "excerpt": row["excerpt"],
                        "published_at_raw": row["published_at_raw"],
                        "source_domain": row["source_domain"],
                        "url": row["url"],
                        "text_content": row["text_content"],
                        "section": row["section"],
                    }
                )
                audit_entries.append(
                    {
                        "article_id": row["id"],
                        "url": row["url"],
                        "title": row["title"],
                        "audit_before": audit_before,
                        "audit_after": audit_after,
                        "actions": actions,
                        "final_html_path": os.path.join(issue_debug_dir, f"article_{row['id']}.html"),
                    }
                )
                try:
                    with open(
                        os.path.join(issue_debug_dir, f"article_{row['id']}.html"), "w", encoding="utf-8"
                    ) as handle:
                        handle.write(healed_content)
                except OSError:
                    pass

            epub_path = issue["epub_path"]
            build_issue_epub(
                title=issue["title"],
                issue_date=issue["issue_date"],
                output_path=epub_path,
                chapters=chapters,
                book_name=_book_or_404(book_id)["name"],
            )

            conn.execute("DELETE FROM issue_articles WHERE issue_id = ?", (issue["id"],))
            for chapter in chapters:
                conn.execute(
                    "INSERT INTO issue_articles (issue_id, article_id) VALUES (?, ?)",
                    (issue["id"], chapter["article_id"]),
                )

            audit_summary = _summarize_audit(audit_entries)
            audit_path = os.path.join(issue_debug_dir, "audit.json")
            now = _now_local().isoformat()
            conn.execute(
                """
                UPDATE issues
                SET build_status = ?, build_finished_at = ?, audit_path = ?, audit_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                ("complete", now, audit_path, json.dumps(audit_summary, ensure_ascii=True), now, issue["id"]),
            )

        audit_report = {
            "issue_id": issue["id"],
            "issue_date": issue["issue_date"],
            "book_id": book_id,
            "generated_at": _now_local().isoformat(),
            "summary": audit_summary,
            "articles": audit_entries,
        }
        try:
            with open(os.path.join(issue_debug_dir, "audit.json"), "w", encoding="utf-8") as handle:
                json.dump(audit_report, handle, ensure_ascii=True, indent=2)
        except OSError:
            pass

        return issue
    except Exception as exc:
        now = _now_local().isoformat()
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE issues
                SET build_status = ?, build_finished_at = ?, build_error = ?, updated_at = ?
                WHERE id = ?
                """,
                ("failed", now, str(exc)[:500], now, issue["id"]),
            )
        raise


@app.on_event("startup")
def startup():
    os.makedirs(EPUB_DIR, exist_ok=True)
    os.makedirs(DEBUG_ARTICLES_DIR, exist_ok=True)
    os.makedirs(DEBUG_ISSUES_DIR, exist_ok=True)
    init_db()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with get_conn() as conn:
        books = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
    return TEMPLATES.TemplateResponse("index.html", {"request": request, "books": books})


@app.post("/books/create")
async def create_book_ui(request: Request):
    form = await request.form()
    name = form.get("name")
    source_url = form.get("source_url")
    if not name:
        raise HTTPException(status_code=400, detail="Missing name")
    now = _now_local().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO books (name, source_url, created_at) VALUES (?, ?, ?)",
            (name, source_url, now),
        )
    return RedirectResponse("/", status_code=303)


@app.get("/books/{book_id}", response_class=HTMLResponse)
async def book_detail(request: Request, book_id: int):
    book = _book_or_404(book_id)
    with get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM book_items WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
        articles = conn.execute(
            "SELECT * FROM articles WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
        issue = conn.execute(
            "SELECT * FROM issues WHERE book_id = ? ORDER BY issue_date DESC LIMIT 1",
            (book_id,),
        ).fetchone()
    issue_data = dict(issue) if issue else None
    if issue_data and issue_data.get("audit_summary"):
        issue_data["audit_summary"] = _parse_summary(issue_data["audit_summary"])
    send_status = request.query_params.get("send")
    send_message = None
    send_error = None
    if send_status == "ok":
        send_message = "Sent to Kindle."
    elif send_status == "error":
        send_error = "Send to Kindle failed. Check server logs for details."
    return TEMPLATES.TemplateResponse(
        "book.html",
        {
            "request": request,
            "book": book,
            "items": items,
            "articles": articles,
            "issue": issue_data,
            "kindle_send": _kindle_send_state(),
            "send_message": send_message,
            "send_error": send_error,
        },
    )


@app.post("/books/{book_id}/build")
async def build_issue_ui(book_id: int):
    _build_issue(book_id)
    return RedirectResponse(f"/books/{book_id}", status_code=303)


@app.post("/issues/{issue_id}/send")
async def send_issue_ui(issue_id: int):
    with get_conn() as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    try:
        _send_epub_to_kindle(issue["epub_path"])
        return RedirectResponse(f"/books/{issue['book_id']}?send=ok", status_code=303)
    except KindleSendError:
        return RedirectResponse(f"/books/{issue['book_id']}?send=error", status_code=303)


@app.post("/books/{book_id}/articles/clear")
async def clear_articles_ui(book_id: int):
    _book_or_404(book_id)
    with get_conn() as conn:
        issues = conn.execute(
            "SELECT id, epub_path FROM issues WHERE book_id = ?",
            (book_id,),
        ).fetchall()
        conn.execute(
            "DELETE FROM issue_articles WHERE issue_id IN (SELECT id FROM issues WHERE book_id = ?)",
            (book_id,),
        )
        conn.execute("DELETE FROM issues WHERE book_id = ?", (book_id,))
        conn.execute("DELETE FROM articles WHERE book_id = ?", (book_id,))
    for issue in issues:
        epub_path = issue["epub_path"]
        if epub_path and os.path.exists(epub_path):
            try:
                os.remove(epub_path)
            except OSError:
                pass
    return RedirectResponse(f"/books/{book_id}", status_code=303)


@app.get("/issues", response_class=HTMLResponse)
async def issues_list(request: Request):
    with get_conn() as conn:
        issues = conn.execute(
            "SELECT issues.*, books.name AS book_name FROM issues JOIN books ON books.id = issues.book_id ORDER BY issue_date DESC"
        ).fetchall()
    issue_rows = []
    for issue in issues:
        item = dict(issue)
        if item.get("audit_summary"):
            item["audit_summary"] = _parse_summary(item["audit_summary"])
        issue_rows.append(item)
    return TEMPLATES.TemplateResponse("issues.html", {"request": request, "issues": issue_rows})


@app.post("/api/books")
async def create_book(payload: dict):
    name = payload.get("name")
    source_url = payload.get("source_url")
    if not name:
        raise HTTPException(status_code=400, detail="Missing name")
    now = _now_local().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO books (name, source_url, created_at) VALUES (?, ?, ?)",
            (name, source_url, now),
        )
        book_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    return {"id": book_id, "name": name, "source_url": source_url}


@app.get("/api/books")
async def list_books():
    with get_conn() as conn:
        books = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
    return {"books": [dict(row) for row in books]}


@app.post("/api/books/{book_id}/snapshot")
async def snapshot(book_id: int, payload: dict):
    _book_or_404(book_id)
    items = payload.get("items")
    if items is None:
        raise HTTPException(status_code=400, detail="Missing items")
    now = _now_local().isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM book_items WHERE book_id = ?", (book_id,))
        for item in items:
            conn.execute(
                "INSERT INTO book_items (book_id, title, url, ts, created_at) VALUES (?, ?, ?, ?, ?)",
                (book_id, item.get("title"), item.get("url"), item.get("ts"), now),
            )
    return {"status": "ok", "count": len(items)}


@app.get("/api/books/{book_id}/items")
async def list_items(book_id: int):
    _book_or_404(book_id)
    with get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM book_items WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
    return {"items": [dict(row) for row in items]}


@app.post("/api/books/{book_id}/articles/ingest")
async def ingest_article(book_id: int, payload: dict):
    _book_or_404(book_id)
    url = payload.get("url")
    title = payload.get("title")
    content_html = payload.get("content_html")
    if not url or not title or not content_html:
        raise HTTPException(status_code=400, detail="Missing url/title/content_html")
    content_hash = compute_content_hash(url, content_html)
    now = _now_local().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM articles WHERE book_id = ? AND url = ? AND content_hash = ?",
            (book_id, url, content_hash),
        ).fetchone()
        if existing:
            return {"status": "duplicate", "article_id": existing["id"]}
        conn.execute(
            "INSERT INTO articles (book_id, url, title, byline, excerpt, content_html, source_domain, published_at_raw, text_content, section, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id,
                url,
                title,
                payload.get("byline"),
                payload.get("excerpt"),
                content_html,
                payload.get("source_domain"),
                payload.get("published_at_raw"),
                payload.get("text_content"),
                payload.get("section"),
                content_hash,
                now,
            ),
        )
        article_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    debug_payload = dict(payload)
    debug_payload.update(
        {
            "book_id": book_id,
            "article_id": article_id,
            "received_at": now,
        }
    )
    try:
        with open(os.path.join(DEBUG_ARTICLES_DIR, f"article_{article_id}.json"), "w", encoding="utf-8") as handle:
            json.dump(debug_payload, handle, ensure_ascii=True, indent=2)
    except OSError:
        pass
    return {"status": "ok", "article_id": article_id}


@app.post("/api/books/{book_id}/issue/build")
async def build_issue_api(book_id: int):
    issue = _build_issue(book_id)
    return {"issue_id": issue["id"], "title": issue["title"], "issue_date": issue["issue_date"]}


@app.get("/api/books/{book_id}/issue/current")
async def current_issue_api(book_id: int):
    issue = _current_issue(book_id)
    return {"issue_id": issue["id"], "title": issue["title"], "issue_date": issue["issue_date"]}


@app.get("/download/{issue_id}.epub")
async def download_issue(issue_id: int):
    with get_conn() as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return FileResponse(issue["epub_path"], media_type="application/epub+zip", filename=os.path.basename(issue["epub_path"]))


@app.get("/issues/{issue_id}/audit")
async def download_issue_audit(issue_id: int):
    with get_conn() as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue or not issue["audit_path"]:
        raise HTTPException(status_code=404, detail="Audit not found")
    audit_path = issue["audit_path"]
    if not os.path.exists(audit_path):
        raise HTTPException(status_code=404, detail="Audit file missing")
    return FileResponse(audit_path, media_type="application/json", filename=os.path.basename(audit_path))


@app.get("/api/issues")
async def list_issues():
    with get_conn() as conn:
        issues = conn.execute(
            "SELECT issues.*, books.name AS book_name FROM issues JOIN books ON books.id = issues.book_id ORDER BY issue_date DESC"
        ).fetchall()
    return {"issues": [dict(row) for row in issues]}


@app.post("/api/issues/{issue_id}/send")
async def send_issue_api(issue_id: int):
    with get_conn() as conn:
        issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    try:
        output = _send_epub_to_kindle(issue["epub_path"])
    except KindleSendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "message": output or "sent"}
