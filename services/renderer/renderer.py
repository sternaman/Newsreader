import base64
import hashlib
import os
import re
from datetime import datetime
from typing import Iterable, List, Optional

import bleach
import requests
from ebooklib import epub

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS.union(
    {
        "article",
        "section",
        "header",
        "footer",
        "figure",
        "figcaption",
        "img",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
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
img { max-width: 100%; height: auto; }
blockquote { border-left: 3px solid #999; padding-left: 0.8em; color: #333; }
"""


def sanitize_html(html: str) -> str:
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
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
        content_html = chapter["content_html"]
        chapter_html = f"""
        <article>
          <h1>{chapter_title}</h1>
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
