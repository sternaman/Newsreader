import html
import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, date, timedelta
from urllib.parse import urlparse

from dateutil import tz
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests

from app.db import get_conn, init_db
from renderer import audit_and_heal_content, build_issue_epub, derive_byline_from_text, sanitize_html
from renderer.renderer import compute_content_hash, embed_images

app = FastAPI()

TEMPLATES = Jinja2Templates(directory="/app/app/templates")
EPUB_DIR = "/data/epubs"
DEBUG_DIR = "/data/debug"
DEBUG_ARTICLES_DIR = os.path.join(DEBUG_DIR, "articles")
DEBUG_ISSUES_DIR = os.path.join(DEBUG_DIR, "issues")
COVERS_DIR = "/data/covers"

app.mount("/static", StaticFiles(directory="/app/app/static"), name="static")


def _now_local() -> datetime:
    tzinfo = tz.gettz(os.environ.get("TZ", "UTC"))
    return datetime.now(tzinfo)


def _issue_date() -> str:
    return _now_local().date().isoformat()


def _retention_days() -> int:
    raw = os.environ.get("RETENTION_DAYS", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(0, value)


def _remove_issue_files(issue) -> None:
    epub_path = issue.get("epub_path")
    audit_path = issue.get("audit_path")
    cover_paths = [
        _cover_file_path(issue["id"], "cover"),
        _cover_file_path(issue["id"], "thumb"),
    ]
    for path in (epub_path, audit_path, *cover_paths):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    debug_dir = os.path.join(DEBUG_ISSUES_DIR, f"issue_{issue['id']}_{issue['issue_date']}")
    if os.path.isdir(debug_dir):
        try:
            shutil.rmtree(debug_dir, ignore_errors=True)
        except OSError:
            pass


def _prune_old_issues(book_id: int | None = None) -> int:
    retention_days = _retention_days()
    if retention_days <= 0:
        return 0
    cutoff_date = _now_local().date() - timedelta(days=retention_days - 1)
    cutoff_str = cutoff_date.isoformat()
    rows = []
    with get_conn() as conn:
        if book_id is None:
            rows = conn.execute(
                "SELECT id, issue_date, epub_path, audit_path FROM issues WHERE issue_date < ?",
                (cutoff_str,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, issue_date, epub_path, audit_path FROM issues WHERE book_id = ? AND issue_date < ?",
                (book_id, cutoff_str),
            ).fetchall()
        rows = [dict(row) for row in rows]
        issue_ids = [row["id"] for row in rows]
        if not issue_ids:
            return 0
        placeholders = ",".join("?" for _ in issue_ids)
        conn.execute(
            f"DELETE FROM issue_articles WHERE issue_id IN ({placeholders})",
            issue_ids,
        )
        conn.execute(
            f"DELETE FROM issues WHERE id IN ({placeholders})",
            issue_ids,
        )
        cutoff_dt = _now_local().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=retention_days - 1
        )
        if book_id is None:
            conn.execute(
                "DELETE FROM articles WHERE created_at < ? AND id NOT IN (SELECT article_id FROM issue_articles)",
                (cutoff_dt.isoformat(),),
            )
        else:
            conn.execute(
                "DELETE FROM articles WHERE book_id = ? AND created_at < ? AND id NOT IN (SELECT article_id FROM issue_articles)",
                (book_id, cutoff_dt.isoformat()),
            )
    for row in rows:
        _remove_issue_files(row)
    return len(rows)


def _cover_file_path(issue_id: int, size: str) -> str:
    suffix = "thumb" if size == "thumb" else "cover"
    return os.path.join(COVERS_DIR, f"issue_{issue_id}_{suffix}.png")


def _cover_dimensions(size: str) -> tuple[int, int]:
    if size == "thumb":
        return (300, 400)
    return (600, 800)


def _load_cover_font(point_size: int):
    try:
        from PIL import ImageFont

        return ImageFont.truetype("DejaVuSans.ttf", point_size)
    except Exception:
        try:
            from PIL import ImageFont

            return ImageFont.load_default()
        except Exception:
            return None


def _wrap_cover_text(text: str, draw, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    try:
        measure = draw.textlength
    except AttributeError:
        measure = lambda value, font=None: len(value) * 6
    lines = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        if not current or measure(test, font=font) <= max_width:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _generate_cover_image(issue: dict, size: str) -> str | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    width, height = _cover_dimensions(size)
    image = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(image)
    title_font = _load_cover_font(28 if size == "thumb" else 36)
    meta_font = _load_cover_font(18 if size == "thumb" else 22)
    padding = int(width * 0.08)
    y = padding
    book_name = issue.get("book_name") or "Newsreader"
    title_lines = _wrap_cover_text(str(book_name), draw, title_font, width - padding * 2)
    line_height = int((title_font.size if title_font else 14) * 1.2)
    for line in title_lines:
        draw.text((padding, y), line, fill="black", font=title_font)
        y += line_height
    y += int(line_height * 0.6)
    issue_date = issue.get("issue_date") or _issue_date()
    draw.text((padding, y), f"Issue {issue_date}", fill="black", font=meta_font)
    y += int((meta_font.size if meta_font else 12) * 1.6)
    draw.text((padding, y), "Newsreader", fill="black", font=meta_font)
    cover_path = _cover_file_path(issue["id"], size)
    os.makedirs(COVERS_DIR, exist_ok=True)
    image.save(cover_path, format="PNG")
    return cover_path


def _ensure_cover(issue: dict, size: str) -> str | None:
    cover_path = _cover_file_path(issue["id"], size)
    if os.path.exists(cover_path):
        return cover_path
    return _generate_cover_image(issue, size)


def _issue_file_size(epub_path: str | None) -> int | None:
    if not epub_path:
        return None
    if not os.path.exists(epub_path):
        return None
    try:
        return os.path.getsize(epub_path)
    except OSError:
        return None


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


def _opds_timestamp(value: str | None) -> str:
    if not value:
        return _now_local().isoformat()
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return _now_local().isoformat()


def _render_opds_issue_entry(issue: dict) -> str:
    title = issue.get("title") or f"Issue {issue.get('id')}"
    book_name = issue.get("book_name") or ""
    updated_at = _opds_timestamp(issue.get("updated_at") or issue.get("created_at"))
    published_at = _opds_timestamp(issue.get("issue_date"))
    size_bytes = issue.get("file_size")
    summary_parts = []
    if issue.get("issue_date"):
        summary_parts.append(f"Issue date: {issue['issue_date']}")
    if size_bytes:
        summary_parts.append(f"Size: {int(size_bytes / 1024)} KB")
    summary_block = ""
    if summary_parts:
        summary_block = f"<summary type=\"text\">{' | '.join(summary_parts)}</summary>"
    author_block = ""
    if book_name:
        author_block = f"<author><name>{html.escape(book_name, quote=True)}</name></author>"
    size_attr = f" length=\"{int(size_bytes)}\"" if size_bytes else ""
    issue_id = issue.get("id")
    return "\n".join(
        [
            "<entry>",
            f"  <title>{html.escape(title, quote=True)}</title>",
            f"  <id>tag:newsreader:issue:{issue_id}</id>",
            f"  <updated>{updated_at}</updated>",
            f"  <published>{published_at}</published>",
            f"  {author_block}" if author_block else "",
            f"  {summary_block}" if summary_block else "",
            "  <link rel=\"http://opds-spec.org/acquisition\" type=\"application/epub+zip\" "
            f"href=\"/download/{issue_id}.epub\"{size_attr} />",
            "  <link rel=\"http://opds-spec.org/image\" type=\"image/png\" "
            f"href=\"/covers/{issue_id}.png\" />",
            "  <link rel=\"http://opds-spec.org/image/thumbnail\" type=\"image/png\" "
            f"href=\"/covers/{issue_id}.png?size=thumb\" />",
            "</entry>",
        ]
    )


def _render_opds_acquisition_feed(
    issues: list,
    *,
    title: str,
    feed_id: str,
    self_href: str,
) -> str:
    updated = _now_local().isoformat()
    entries_xml = "\n".join(_render_opds_issue_entry(issue) for issue in issues)
    return "\n".join(
        [
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>",
            "<feed xmlns=\"http://www.w3.org/2005/Atom\" xmlns:opds=\"http://opds-spec.org/2010/catalog\">",
            f"  <id>{html.escape(feed_id, quote=True)}</id>",
            f"  <title>{html.escape(title, quote=True)}</title>",
            f"  <updated>{updated}</updated>",
            f"  <link rel=\"self\" type=\"application/atom+xml\" href=\"{html.escape(self_href, quote=True)}\" />",
            "  <link rel=\"start\" type=\"application/atom+xml\" href=\"/opds\" />",
            entries_xml,
            "</feed>",
        ]
    )


def _render_opds_navigation_feed(
    *,
    title: str,
    feed_id: str,
    self_href: str,
    entries: list[dict],
) -> str:
    updated = _now_local().isoformat()
    entry_lines = []
    for entry in entries:
        summary_block = ""
        if entry.get("summary"):
            summary_block = f"<summary type=\"text\">{html.escape(entry['summary'], quote=True)}</summary>"
        entry_lines.append(
            "\n".join(
                [
                    "<entry>",
                    f"  <title>{html.escape(entry['title'], quote=True)}</title>",
                    f"  <id>{html.escape(entry['id'], quote=True)}</id>",
                    f"  <updated>{_opds_timestamp(entry.get('updated'))}</updated>",
                    f"  {summary_block}" if summary_block else "",
                    "  <link rel=\"subsection\" type=\"application/atom+xml\" "
                    f"href=\"{html.escape(entry['href'], quote=True)}\" />",
                    "</entry>",
                ]
            )
        )
    return "\n".join(
        [
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>",
            "<feed xmlns=\"http://www.w3.org/2005/Atom\" xmlns:opds=\"http://opds-spec.org/2010/catalog\">",
            f"  <id>{html.escape(feed_id, quote=True)}</id>",
            f"  <title>{html.escape(title, quote=True)}</title>",
            f"  <updated>{updated}</updated>",
            f"  <link rel=\"self\" type=\"application/atom+xml\" href=\"{html.escape(self_href, quote=True)}\" />",
            "  <link rel=\"start\" type=\"application/atom+xml\" href=\"/opds\" />",
            "\n".join(entry_lines),
            "</feed>",
        ]
    )


def _fetch_opds_issues(where_clause: str = "", params: tuple = ()) -> list:
    query = (
        "SELECT issues.*, books.name AS book_name FROM issues "
        "JOIN books ON books.id = issues.book_id "
        "WHERE issues.build_status = 'complete' "
    )
    if where_clause:
        query += f"AND {where_clause} "
    query += "ORDER BY issue_date DESC, issues.id DESC LIMIT 200"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    issues = []
    for row in rows:
        item = dict(row)
        epub_path = item.get("epub_path")
        size_bytes = _issue_file_size(epub_path)
        if not epub_path or size_bytes is None:
            continue
        item["file_size"] = size_bytes
        issues.append(item)
    return issues


def _fetch_opds_books() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT books.*, MAX(issues.updated_at) AS updated_at, COUNT(issues.id) AS issue_count
            FROM books
            LEFT JOIN issues ON issues.book_id = books.id AND issues.build_status = 'complete'
            GROUP BY books.id
            ORDER BY books.created_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _book_or_404(book_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Book not found")
    return row


def _ingest_article_payload(book_id: int, payload: dict) -> dict:
    url = payload.get("url")
    title = payload.get("title")
    content_html = payload.get("content_html")
    if not url or not title or not content_html:
        raise ValueError("Missing url/title/content_html")
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


_BLOOMBERG_API = "https://cdn-mobapi.bloomberg.com"
_BLOOMBERG_UA = "Mozilla/5.0 (Newsreader; Bloomberg recipe import)"


def _bloomberg_get_json(url: str) -> dict:
    resp = requests.get(
        url,
        headers={"User-Agent": _BLOOMBERG_UA, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _normalize_bloomberg_html(content_html: str) -> str:
    if not content_html:
        return ""
    html_out = re.sub(
        r"(<img[^>]*?)src=[\"']\s*[\"'][^>]*?data-native-src=[\"']([^\"']+)[\"']",
        r"\1src=\"\2\"",
        content_html,
        flags=re.IGNORECASE,
    )
    html_out = re.sub(
        r"(<img[^>]*?)\sdata-native-src=[\"']([^\"']+)[\"']",
        r"\1 src=\"\2\"",
        html_out,
        flags=re.IGNORECASE,
    )
    html_out = re.sub(r"\sdata-native-src=[\"'][^\"']+[\"']", "", html_out, flags=re.IGNORECASE)
    html_out = re.sub(
        r"(-1x-1)(\.(?:jpg|png))",
        r"750x-1\2",
        html_out,
        flags=re.IGNORECASE,
    )
    return html_out


def _bloomberg_text_content(content_html: str) -> str:
    if not content_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", content_html)
    return re.sub(r"\s+", " ", text).strip()


def _epoch_to_iso(value) -> str | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1_000_000_000_000:
        num = num / 1000.0
    try:
        dt = datetime.fromtimestamp(num, tz=_now_local().tzinfo)
    except (OSError, ValueError):
        return None
    return dt.isoformat()


def _bloomberg_story_detail(story_id: str) -> tuple[dict, str]:
    detail = _bloomberg_get_json(f"{_BLOOMBERG_API}/wssmobile/v1/stories/{story_id}")
    body_data = _bloomberg_get_json(f"{_BLOOMBERG_API}/wssmobile/v1/bw/news/stories/{story_id}")
    body_html = body_data.get("html") or ""
    return detail, body_html


def _bloomberg_collect_stories(*, days: float, max_articles: int | None, max_sections: int | None) -> list[dict]:
    nav = _bloomberg_get_json(f"{_BLOOMBERG_API}/wssmobile/v1/navigation/bloomberg_app/search-v2")
    sections = nav.get("searchNav") or []
    cutoff = _now_local().replace(tzinfo=None) - timedelta(days=days)
    stories: list[dict] = []
    seen: set[str] = set()
    section_count = 0
    for group in sections:
        for item in group.get("items") or []:
            if max_sections and section_count >= max_sections:
                return stories
            section_count += 1
            section_title = item.get("title") or "Bloomberg"
            href = (item.get("links") or {}).get("self", {}).get("href")
            if not href:
                continue
            sec_data = _bloomberg_get_json(f"{_BLOOMBERG_API}{href}")
            for module in sec_data.get("modules") or []:
                for story in module.get("stories") or []:
                    if story.get("type") not in {"article", "interactive"}:
                        continue
                    story_id = story.get("internalID") or story.get("id")
                    if not story_id or story_id in seen:
                        continue
                    published = story.get("published")
                    if published:
                        try:
                            published_val = float(published)
                            if published_val > 1_000_000_000_000:
                                published_val = published_val / 1000.0
                            published_dt = datetime.fromtimestamp(published_val)
                            if published_dt < cutoff:
                                continue
                        except (TypeError, ValueError, OSError):
                            pass
                    seen.add(str(story_id))
                    stories.append(
                        {
                            "id": str(story_id),
                            "title": story.get("title"),
                            "section": section_title,
                            "summary": story.get("autoGeneratedSummary"),
                        }
                    )
                    if max_articles and len(stories) >= max_articles:
                        return stories
    return stories


def _bloomberg_collect_businessweek(*, issue_id: str | None, max_articles: int | None) -> list[dict]:
    listing = _bloomberg_get_json(f"{_BLOOMBERG_API}/wssmobile/v1/bw/news/list?limit=1")
    magazines = listing.get("magazines") or []
    edition_id = issue_id or (magazines[0]["id"] if magazines else None)
    if not edition_id:
        return []
    week = _bloomberg_get_json(f"{_BLOOMBERG_API}/wssmobile/v1/bw/news/week/{edition_id}")
    stories: list[dict] = []
    for module in week.get("modules") or []:
        section_title = module.get("title") or "Businessweek"
        for article in module.get("articles") or []:
            story_id = article.get("id")
            if not story_id:
                continue
            stories.append(
                {
                    "id": str(story_id),
                    "title": article.get("title"),
                    "section": section_title,
                    "summary": None,
                }
            )
            if max_articles and len(stories) >= max_articles:
                return stories
    return stories


def _bloomberg_payload_for_story(story: dict) -> dict | None:
    detail, body_html = _bloomberg_story_detail(story["id"])
    long_url = detail.get("longURL") or detail.get("url") or detail.get("mobileURL")
    if not long_url:
        return None
    body_html = _normalize_bloomberg_html(body_html)
    if detail.get("type") == "interactive":
        body_html = "<p><em>This interactive article is best read in a browser.</em></p>" + body_html
    lede = detail.get("ledeImage") or {}
    lede_url = (lede.get("imageURLs") or {}).get("default")
    if lede_url and lede_url.split("?")[0] not in body_html:
        caption = lede.get("caption") or ""
        credit = lede.get("credit") or ""
        caption_html = ""
        if caption or credit:
            caption_text = " ".join(x for x in [caption, credit] if x)
            caption_html = f"<figcaption>{html.escape(caption_text, quote=True)}</figcaption>"
        body_html = f"<figure><img src=\"{lede_url}\"/>{caption_html}</figure>{body_html}"
    excerpt = None
    abstract = detail.get("abstract")
    if isinstance(abstract, list) and abstract:
        excerpt = " ".join(abstract)
    if not excerpt:
        excerpt = detail.get("summary") or story.get("summary")
    published_at_raw = _epoch_to_iso(detail.get("updatedAt") or detail.get("publishedAt") or detail.get("published"))
    section = detail.get("primaryCategory") or story.get("section")
    source_domain = urlparse(long_url).netloc or "bloomberg.com"
    text_content = _bloomberg_text_content(body_html)
    return {
        "url": long_url,
        "title": detail.get("title") or story.get("title") or long_url,
        "byline": detail.get("byline"),
        "excerpt": excerpt,
        "content_html": body_html,
        "source_domain": source_domain,
        "published_at_raw": published_at_raw,
        "text_content": text_content,
        "section": section,
    }


def _save_book_items(book_id: int, items: list[dict]) -> None:
    now = _now_local().isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM book_items WHERE book_id = ?", (book_id,))
        for item in items:
            conn.execute(
                "INSERT INTO book_items (book_id, title, url, ts, created_at) VALUES (?, ?, ?, ?, ?)",
                (book_id, item.get("title"), item.get("url"), item.get("ts"), now),
            )


def _import_bloomberg(
    *,
    book_id: int,
    mode: str,
    days: float,
    max_articles: int | None,
    max_sections: int | None,
    issue_id: str | None,
    update_snapshot: bool,
) -> dict:
    if mode not in {"bloomberg", "businessweek"}:
        raise HTTPException(status_code=400, detail="Invalid mode")
    if mode == "businessweek":
        stories = _bloomberg_collect_businessweek(issue_id=issue_id, max_articles=max_articles)
    else:
        stories = _bloomberg_collect_stories(days=days, max_articles=max_articles, max_sections=max_sections)
    snapshot_items = []
    results = {"ok": 0, "duplicate": 0, "error": 0, "errors": []}
    for item in stories:
        try:
            payload = _bloomberg_payload_for_story(item)
            if not payload:
                results["error"] += 1
                results["errors"].append({"id": item.get("id"), "error": "missing payload"})
                continue
            snapshot_items.append({"title": payload.get("title"), "url": payload.get("url")})
            inserted = _ingest_article_payload(book_id, payload)
            results[inserted["status"]] += 1
        except Exception as exc:
            results["error"] += 1
            results["errors"].append({"id": item.get("id"), "error": str(exc)[:200]})
    if update_snapshot:
        snapshot_items = [item for item in snapshot_items if item.get("title") and item.get("url")]
        _save_book_items(book_id, snapshot_items)
    return {
        "status": "ok",
        "mode": mode,
        "fetched": len(stories),
        "ingested": results["ok"],
        "duplicates": results["duplicate"],
        "errors": results["error"],
        "error_details": results["errors"][:10],
    }


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

        _prune_old_issues(book_id=book_id)
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
    os.makedirs(COVERS_DIR, exist_ok=True)
    init_db()
    _prune_old_issues()


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
    import_status = request.query_params.get("import")
    import_message = None
    import_error = None
    if import_status == "ok":
        import_message = "Bloomberg import complete."
    elif import_status == "error":
        import_error = "Bloomberg import failed. Check server logs for details."
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
            "import_message": import_message,
            "import_error": import_error,
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


@app.post("/books/{book_id}/import/bloomberg")
async def import_bloomberg_ui(request: Request, book_id: int):
    _book_or_404(book_id)
    form = await request.form()
    mode = (form.get("mode") or "bloomberg").strip().lower()
    issue_id = form.get("issue_id") or None
    try:
        days = float(form.get("days") or 1.2)
    except (TypeError, ValueError):
        days = 1.2
    try:
        max_articles = int(form.get("max_articles") or 40)
        if max_articles <= 0:
            max_articles = None
    except (TypeError, ValueError):
        max_articles = 40
    try:
        max_sections = int(form.get("max_sections") or 6)
        if max_sections <= 0:
            max_sections = None
    except (TypeError, ValueError):
        max_sections = 6
    try:
        _import_bloomberg(
            book_id=book_id,
            mode=mode,
            days=days,
            max_articles=max_articles,
            max_sections=max_sections,
            issue_id=issue_id,
            update_snapshot=True,
        )
        return RedirectResponse(f"/books/{book_id}?import=ok", status_code=303)
    except Exception:
        return RedirectResponse(f"/books/{book_id}?import=error", status_code=303)


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
    try:
        return _ingest_article_payload(book_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/books/{book_id}/import/bloomberg")
async def import_bloomberg_api(book_id: int, payload: dict):
    _book_or_404(book_id)
    mode = (payload.get("mode") or "bloomberg").strip().lower()
    issue_id = payload.get("issue_id") or None
    try:
        days = float(payload.get("days", 1.2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid days")
    max_articles = payload.get("max_articles")
    max_sections = payload.get("max_sections")
    try:
        if max_articles is not None:
            max_articles = int(max_articles)
            if max_articles <= 0:
                max_articles = None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid max_articles")
    try:
        if max_sections is not None:
            max_sections = int(max_sections)
            if max_sections <= 0:
                max_sections = None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid max_sections")
    update_snapshot = payload.get("update_snapshot", True)
    return _import_bloomberg(
        book_id=book_id,
        mode=mode,
        days=days,
        max_articles=max_articles,
        max_sections=max_sections,
        issue_id=issue_id,
        update_snapshot=bool(update_snapshot),
    )


@app.post("/api/books/{book_id}/issue/build")
async def build_issue_api(book_id: int):
    issue = _build_issue(book_id)
    return {"issue_id": issue["id"], "title": issue["title"], "issue_date": issue["issue_date"]}


@app.get("/api/books/{book_id}/issue/current")
async def current_issue_api(book_id: int):
    issue = _current_issue(book_id)
    return {"issue_id": issue["id"], "title": issue["title"], "issue_date": issue["issue_date"]}


@app.get("/covers/{issue_id}.png")
async def cover_image(issue_id: int, size: str = "cover"):
    if size not in {"cover", "thumb"}:
        raise HTTPException(status_code=400, detail="Invalid size")
    with get_conn() as conn:
        issue = conn.execute(
            "SELECT issues.*, books.name AS book_name FROM issues "
            "JOIN books ON books.id = issues.book_id WHERE issues.id = ?",
            (issue_id,),
        ).fetchone()
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    issue_data = dict(issue)
    cover_path = _ensure_cover(issue_data, size)
    if not cover_path or not os.path.exists(cover_path):
        raise HTTPException(status_code=404, detail="Cover not available")
    return FileResponse(cover_path, media_type="image/png", filename=os.path.basename(cover_path))


@app.get("/opds")
@app.get("/opds.xml")
async def opds_catalog(request: Request):
    base_url = str(request.base_url).rstrip("/")
    books = _fetch_opds_books()
    today_count = len(_fetch_opds_issues("issue_date = ?", (_issue_date(),)))
    total_count = len(_fetch_opds_issues())
    entries = [
        {
            "title": "Today",
            "id": "tag:newsreader:today",
            "href": "/opds/today",
            "updated": _now_local().isoformat(),
            "summary": f"{today_count} issues",
        },
        {
            "title": "All Issues",
            "id": "tag:newsreader:all",
            "href": "/opds/all",
            "updated": _now_local().isoformat(),
            "summary": f"{total_count} issues",
        },
    ]
    for book in books:
        issue_count = book.get("issue_count") or 0
        entries.append(
            {
                "title": book["name"],
                "id": f"tag:newsreader:book:{book['id']}",
                "href": f"/opds/books/{book['id']}",
                "updated": book.get("updated_at") or _now_local().isoformat(),
                "summary": f"{issue_count} issues",
            }
        )
    xml = _render_opds_navigation_feed(
        title="Newsreader Catalog",
        feed_id=f"{base_url}/opds",
        self_href="/opds",
        entries=entries,
    )
    return Response(content=xml, media_type="application/atom+xml;profile=opds-catalog;kind=navigation")


@app.get("/opds/all")
async def opds_all(request: Request):
    base_url = str(request.base_url).rstrip("/")
    issues = _fetch_opds_issues()
    xml = _render_opds_acquisition_feed(
        issues,
        title="All Issues",
        feed_id=f"{base_url}/opds/all",
        self_href="/opds/all",
    )
    return Response(content=xml, media_type="application/atom+xml;profile=opds-catalog;kind=acquisition")


@app.get("/opds/today")
async def opds_today(request: Request):
    base_url = str(request.base_url).rstrip("/")
    issues = _fetch_opds_issues("issue_date = ?", (_issue_date(),))
    xml = _render_opds_acquisition_feed(
        issues,
        title="Today",
        feed_id=f"{base_url}/opds/today",
        self_href="/opds/today",
    )
    return Response(content=xml, media_type="application/atom+xml;profile=opds-catalog;kind=acquisition")


@app.get("/opds/books/{book_id}")
async def opds_book(request: Request, book_id: int):
    book = _book_or_404(book_id)
    base_url = str(request.base_url).rstrip("/")
    issues = _fetch_opds_issues("issues.book_id = ?", (book_id,))
    xml = _render_opds_acquisition_feed(
        issues,
        title=f"{book['name']} Issues",
        feed_id=f"{base_url}/opds/books/{book_id}",
        self_href=f"/opds/books/{book_id}",
    )
    return Response(content=xml, media_type="application/atom+xml;profile=opds-catalog;kind=acquisition")


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
