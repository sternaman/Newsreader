import os
from datetime import datetime, date
from typing import List, Optional

from dateutil import tz
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import get_conn, init_db
from renderer import build_issue_epub, sanitize_html
from renderer.renderer import compute_content_hash, embed_images

app = FastAPI()

TEMPLATES = Jinja2Templates(directory="/app/app/templates")
API_TOKEN = os.environ.get("API_TOKEN", "changeme")
EPUB_DIR = "/data/epubs"

app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")


def _now_local() -> datetime:
    tzinfo = tz.gettz(os.environ.get("TZ", "UTC"))
    return datetime.now(tzinfo)


def _issue_date() -> str:
    return _now_local().date().isoformat()


def _require_token(x_api_token: Optional[str] = Header(None)) -> None:
    if not x_api_token or x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token")


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
            "INSERT INTO issues (book_id, issue_date, title, epub_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (book_id, issue_date, title, epub_path, now, now),
        )
        issue = conn.execute(
            "SELECT * FROM issues WHERE book_id = ? AND issue_date = ?",
            (book_id, issue_date),
        ).fetchone()
        return issue


def _build_issue(book_id: int):
    issue = _current_issue(book_id)
    start_day = _now_local().replace(hour=0, minute=0, second=0, microsecond=0)
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
            content = sanitize_html(row["content_html"])
            content = embed_images(content, fetch_remote=False)
            chapters.append(
                {
                    "title": row["title"],
                    "content_html": content,
                    "article_id": row["id"],
                }
            )

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

        now = _now_local().isoformat()
        conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (now, issue["id"]))

    return issue


@app.on_event("startup")
def startup():
    os.makedirs(EPUB_DIR, exist_ok=True)
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
    return TEMPLATES.TemplateResponse(
        "book.html",
        {
            "request": request,
            "book": book,
            "items": items,
            "articles": articles,
            "issue": issue,
        },
    )


@app.post("/books/{book_id}/build")
async def build_issue_ui(book_id: int):
    _build_issue(book_id)
    return RedirectResponse(f"/books/{book_id}", status_code=303)


@app.get("/issues", response_class=HTMLResponse)
async def issues_list(request: Request):
    with get_conn() as conn:
        issues = conn.execute(
            "SELECT issues.*, books.name AS book_name FROM issues JOIN books ON books.id = issues.book_id ORDER BY issue_date DESC"
        ).fetchall()
    return TEMPLATES.TemplateResponse("issues.html", {"request": request, "issues": issues})


@app.post("/api/books", dependencies=[Depends(_require_token)])
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


@app.get("/api/books", dependencies=[Depends(_require_token)])
async def list_books():
    with get_conn() as conn:
        books = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
    return {"books": [dict(row) for row in books]}


@app.post("/api/books/{book_id}/snapshot", dependencies=[Depends(_require_token)])
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


@app.get("/api/books/{book_id}/items", dependencies=[Depends(_require_token)])
async def list_items(book_id: int):
    _book_or_404(book_id)
    with get_conn() as conn:
        items = conn.execute(
            "SELECT * FROM book_items WHERE book_id = ? ORDER BY created_at DESC",
            (book_id,),
        ).fetchall()
    return {"items": [dict(row) for row in items]}


@app.post("/api/books/{book_id}/articles/ingest", dependencies=[Depends(_require_token)])
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
            "INSERT INTO articles (book_id, url, title, byline, excerpt, content_html, source_domain, published_at_raw, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id,
                url,
                title,
                payload.get("byline"),
                payload.get("excerpt"),
                content_html,
                payload.get("source_domain"),
                payload.get("published_at_raw"),
                content_hash,
                now,
            ),
        )
        article_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    return {"status": "ok", "article_id": article_id}


@app.post("/api/books/{book_id}/issue/build", dependencies=[Depends(_require_token)])
async def build_issue_api(book_id: int):
    issue = _build_issue(book_id)
    return {"issue_id": issue["id"], "title": issue["title"], "issue_date": issue["issue_date"]}


@app.get("/api/books/{book_id}/issue/current", dependencies=[Depends(_require_token)])
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


@app.get("/api/issues", dependencies=[Depends(_require_token)])
async def list_issues():
    with get_conn() as conn:
        issues = conn.execute(
            "SELECT issues.*, books.name AS book_name FROM issues JOIN books ON books.id = issues.book_id ORDER BY issue_date DESC"
        ).fetchall()
    return {"issues": [dict(row) for row in issues]}
