import base64
import hashlib
import html
import math
import os
import re
from datetime import datetime
from typing import Iterable, List, Optional

import bleach
import requests
from ebooklib import epub
from dateutil import parser, tz

SCENE_BREAK_MARKER = "* * *"
MIN_CONTENT_TEXT_LEN = 800

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS.union(
    {
        "article",
        "section",
        "header",
        "footer",
        "figure",
        "figcaption",
        "img",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "pre",
        "code",
        "blockquote",
        "hr",
        "br",
        "span",
    }
)

ALLOWED_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "img": ["src", "alt"],
    "a": ["href", "title"],
    "span": ["class"],
    "p": ["class"],
}

EINK_CSS = """
body {
  font-family: "Georgia", "Times New Roman", serif;
  font-size: 1.1em;
  line-height: 1.5;
  margin: 0;
  padding: 0;
}
article { padding: 1em 1.4em; }
h1, h2, h3 { font-weight: bold; margin: 1em 0 0.5em; }
img {
  max-width: 100%;
  height: auto;
  max-height: 65vh;
  object-fit: contain;
  display: block;
  margin: 0.8em auto;
}
div.meta {
  margin: 0.2em 0 1em;
  padding-bottom: 0.6em;
  border-bottom: 1px solid #ccc;
  color: #444;
  font-size: 0.9em;
}
div.meta-line { margin: 0.2em 0; }
p.meta-excerpt { margin: 0.4em 0 0; font-style: italic; color: #555; }
p.scene-break { text-align: center; letter-spacing: 0.2em; margin: 1em 0; }
figure {
  margin: 0.8em 0;
  break-inside: avoid;
  page-break-inside: avoid;
}
figcaption {
  font-size: 0.85em;
  text-align: center;
  color: #555;
}
blockquote { border-left: 3px solid #999; padding-left: 0.8em; color: #333; }
"""


_JUNK_PHRASES = [
    r"Skip to Main Content",
    r"Skip to\.\.\.",
    r"This copy is for your personal, non-commercial use only",
    r"Subscriber Agreement",
    r"Dow Jones Reprints",
    r"www\.djreprints\.com",
    r"1-800-843-0008",
]


def sanitize_html(html: str) -> str:
    pre = re.sub(r"<(script|style|noscript)[^>]*>.*?</\\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    pre = re.sub(r"<!--.*?-->", "", pre, flags=re.DOTALL)
    for pattern in _JUNK_PHRASES:
        pre = re.sub(pattern, "", pre, flags=re.IGNORECASE)
    cleaned = bleach.clean(
        pre,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=["http", "https", "mailto", "data"],
        strip=True,
    )
    return bleach.linkify(cleaned)


def _data_url_from_bytes(content: bytes, content_type: str) -> str:
    encoded = base64.b64encode(content).decode("utf-8")
    return f"data:{content_type};base64,{encoded}"


def embed_images(html: str, fetch_remote: bool = False) -> str:
    def replace(match):
        src = match.group(1)
        if src.startswith("data:"):
            return match.group(0)
        if not fetch_remote:
            return match.group(0)
        try:
            response = requests.get(src, timeout=10)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "image/jpeg")
            data_url = _data_url_from_bytes(response.content, content_type)
            return match.group(0).replace(src, data_url)
        except Exception:
            return match.group(0)

    return re.sub(r'<img[^>]+src=["\']([^"\']+)["\']', replace, html, flags=re.IGNORECASE)


def compute_content_hash(url: str, content_html: str) -> str:
    sha = hashlib.sha256()
    sha.update(url.encode("utf-8"))
    sha.update(content_html.encode("utf-8"))
    return sha.hexdigest()


def _format_byline(byline: Optional[str]) -> Optional[str]:
    if not byline:
        return None
    stripped = byline.strip()
    if not stripped:
        return None
    if stripped.lower().startswith("by "):
        return stripped
    return f"By {stripped}"


def _format_published_at(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        parsed = parser.parse(stripped)
    except (ValueError, TypeError):
        return stripped
    local_tz = tz.gettz(os.environ.get("TZ", "UTC"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    localized = parsed.astimezone(local_tz) if local_tz else parsed
    return localized.strftime("%b %d, %Y %I:%M %p")


def _reading_wpm() -> int:
    raw = os.environ.get("READING_WPM", "230").strip()
    try:
        wpm = int(raw)
    except ValueError:
        return 230
    return max(120, min(400, wpm))


def _estimate_reading_time(text_content: Optional[str], *, wpm: Optional[int] = None) -> Optional[str]:
    if not text_content:
        return None
    words = len(re.findall(r"\b\w+\b", text_content))
    if not words:
        return None
    effective_wpm = wpm or _reading_wpm()
    minutes = max(1, int(math.ceil(words / effective_wpm)))
    return f"{minutes} min read"


def _normalize_scene_breaks(content_html: str) -> str:
    if not content_html:
        return content_html
    marker = f'<p class="scene-break">{SCENE_BREAK_MARKER}</p>'
    content_html = re.sub(r"<hr[^>]*>", marker, content_html, flags=re.IGNORECASE)
    content_html = re.sub(
        r"<p[^>]*>(?:\s|&nbsp;|&#160;|<br\s*/?>)*</p>",
        marker,
        content_html,
        flags=re.IGNORECASE,
    )
    content_html = re.sub(
        r"<p[^>]*>\s*(?:\* ?\* ?\*|-{3,}|- - -)\s*</p>",
        marker,
        content_html,
        flags=re.IGNORECASE,
    )
    content_html = re.sub(rf"(?:{re.escape(marker)}\s*){{2,}}", marker, content_html)
    return content_html


def _html_text_length(content_html: str) -> int:
    if not content_html:
        return 0
    stripped = re.sub(r"<[^>]+>", " ", content_html)
    return len(re.sub(r"\s+", " ", stripped).strip())


def _text_to_paragraphs(text_content: Optional[str]) -> str:
    if not text_content:
        return ""
    chunks = [chunk.strip() for chunk in re.split(r"\n{2,}", text_content) if chunk.strip()]
    if not chunks:
        return ""
    return "".join(f"<p>{html.escape(chunk, quote=True)}</p>" for chunk in chunks)


def _render_metadata(
    *,
    byline: Optional[str],
    excerpt: Optional[str],
    published_at_raw: Optional[str],
    source_domain: Optional[str],
    url: Optional[str],
    text_content: Optional[str],
    section: Optional[str],
) -> str:
    if excerpt:
        trimmed_excerpt = excerpt.strip()
        if trimmed_excerpt.startswith("{") or trimmed_excerpt.startswith("[") or "schema.org" in trimmed_excerpt:
            excerpt = None
        elif len(trimmed_excerpt) > 400:
            excerpt = trimmed_excerpt[:397] + "..."

    meta_primary: List[str] = []
    meta_secondary: List[str] = []

    formatted_byline = _format_byline(byline)
    if formatted_byline:
        meta_primary.append(html.escape(formatted_byline, quote=True))

    formatted_published = _format_published_at(published_at_raw)
    if formatted_published:
        meta_primary.append(f"Published {html.escape(formatted_published, quote=True)}")

    reading_time = _estimate_reading_time(text_content)
    if reading_time:
        meta_primary.append(html.escape(reading_time, quote=True))

    if url:
        label = source_domain.strip() if source_domain else url
        safe_label = html.escape(label, quote=True)
        safe_url = html.escape(url.strip(), quote=True)
        meta_secondary.append(f'Source <a href="{safe_url}">{safe_label}</a>')
    elif source_domain:
        meta_secondary.append(f"Source {html.escape(source_domain.strip(), quote=True)}")

    if section and section.strip():
        meta_secondary.insert(0, f"Section {html.escape(section.strip(), quote=True)}")

    if not meta_primary and not meta_secondary and not (excerpt and excerpt.strip()):
        return ""

    meta_lines = []
    if meta_primary:
        meta_lines.append(f"<div class=\"meta-line\">{' | '.join(meta_primary)}</div>")
    if meta_secondary:
        meta_lines.append(f"<div class=\"meta-line\">{' | '.join(meta_secondary)}</div>")

    meta_excerpt = ""
    if excerpt and excerpt.strip():
        meta_excerpt = f"<p class=\"meta-excerpt\">{html.escape(excerpt.strip(), quote=True)}</p>"

    return f"<div class=\"meta\">{''.join(meta_lines)}{meta_excerpt}</div>"


def build_issue_epub(
    *,
    title: str,
    issue_date: str,
    output_path: str,
    chapters: List[dict],
    book_name: str,
) -> str:
    book = epub.EpubBook()
    book.set_identifier(f"{book_name}-{issue_date}")
    book.set_title(title)
    book.set_language("en")

    book.add_author(book_name)

    style_item = epub.EpubItem(uid="style", file_name="styles/style.css", media_type="text/css", content=EINK_CSS)
    book.add_item(style_item)

    front_html = f"""
    <article>
      <h1>{title}</h1>
      <p>Issue date: {issue_date}</p>
      <p>Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
    </article>
    """
    front = epub.EpubHtml(title="Front Matter", file_name="front.xhtml", content=front_html)
    front.add_item(style_item)
    book.add_item(front)

    toc_items = [front]
    spine_items = ["nav", front]

    for idx, chapter in enumerate(chapters, start=1):
        chapter_title = chapter["title"]
        if chapter_title.endswith(" - WSJ"):
            chapter_title = chapter_title[:-6]
        content_html = _normalize_scene_breaks(chapter["content_html"])
        if _html_text_length(content_html) < MIN_CONTENT_TEXT_LEN:
            fallback_html = _text_to_paragraphs(chapter.get("text_content"))
            if fallback_html:
                content_html = fallback_html
        meta_html = _render_metadata(
            byline=chapter.get("byline"),
            excerpt=chapter.get("excerpt"),
            published_at_raw=chapter.get("published_at_raw"),
            source_domain=chapter.get("source_domain"),
            url=chapter.get("url"),
            text_content=chapter.get("text_content"),
            section=chapter.get("section"),
        )
        chapter_html = f"""
        <article>
          <h1>{chapter_title}</h1>
          {meta_html}
          {content_html}
        </article>
        """
        item = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter_{idx}.xhtml",
            content=chapter_html,
        )
        item.add_item(style_item)
        book.add_item(item)
        toc_items.append(item)
        spine_items.append(item)

    book.toc = toc_items
    book.spine = spine_items

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    epub.write_epub(output_path, book, {})
    return output_path
