#!/usr/bin/env python3
"""
Build a Bloomberg-style HTML news index from EPUB bundles.

For each *.epub in --out-dir:
  - Parses section/article metadata from internal feed TOC pages
  - Extracts article HTML + images to articles/{Source}-{date}/
  - Injects reader.css link and back-link into article pages

Writes:
  out_dir/index.html      — landing page
  out_dir/reader.css      — article reader stylesheet
  out_dir/fonts/          — self-hosted woff2 font files (downloaded once)

Called from entrypoint.sh after each build_once() cycle.
"""

import argparse
import gzip
import html as html_mod
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from itertools import zip_longest
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # fallback below

# Ensure Unicode article titles print cleanly on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# Google Fonts CSS2 API — fetched once with a modern UA to get woff2 format
_FONT_CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Playfair+Display:ital,wght@0,400;0,700;1,400"
    "&family=Source+Serif+4:wght@400;700"
    "&display=swap"
)
_FONT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_FALLBACK_SERIF = "Georgia, 'Times New Roman', serif"
# Sources rendered first in the nav/index (others appended in filename order)
_SOURCE_PRIORITY = ["Bloomberg", "Bloomberg Weekends", "Businessweek"]

# Bloomberg mobile API section endpoints in descending editorial priority.
# Each page has featured modules that mirror the bloomberg.com homepage.
_BB_SECTION_ENDPOINTS = [
    "/wssmobile/v1/pages/business/phx-markets",
    "/wssmobile/v1/pages/business/phx-economics-v2",
    "/wssmobile/v1/pages/business/phx-technology",
    "/wssmobile/v1/pages/technology/phx-ai",
    "/wssmobile/v1/pages/business/phx-politics",
    "/wssmobile/v1/pages/business/phx-industries",
    "/wssmobile/v1/pages/business/phx-wealth",
    "/wssmobile/v1/pages/business/phx-green",
    "/wssmobile/v1/pages/business/phx-commodities",
    "/wssmobile/v1/pages/business/phx-etfs",
    "/wssmobile/v1/pages/business/phx-crypto",
    "/wssmobile/v1/pages/technology/phx-technology",
    "/wssmobile/v1/pages/technology/phx-screentime",
    "/wssmobile/v1/pages/pursuits/phx-pursuits",
]
# All module IDs that contain stories relevant to the homepage.
# topic_package_* and video_package are wildcard-matched at runtime.
_BB_FEATURED_MODULES = {
    "top_single_story", "top_stories", "top_stories_1",
    "top_story", "feature_story", "featured_story",
    "top_stories_grid", "archive_story_list", "archive_stories_list",
}
def _is_featured_module(mod_id: str) -> bool:
    """Check if a module ID should be included for featured titles."""
    if mod_id in _BB_FEATURED_MODULES:
        return True
    # Match topic_package_1, topic_package_2, etc.
    if mod_id.startswith("topic_package_"):
        return True
    # Match video_package
    if mod_id == "video_package":
        return True
    return False
_BB_API_BASE = "https://cdn-mobapi.bloomberg.com"


def _bloomberg_featured_titles() -> list[str]:
    """Return Bloomberg article titles in live homepage prominence order.

    First tries to scrape the bloomberg.com homepage HTML directly. If that
    fails (403, timeout, etc.), falls back to the mobile API section endpoints.

    Returns an empty list on any network or parse failure so callers can
    fall back to the existing feed order gracefully.
    """
    # Strategy 1: Scrape bloomberg.com homepage HTML
    titles = _bloomberg_homepage_titles()
    if titles:
        return titles

    # Strategy 2: Fall back to mobile API
    return _bloomberg_api_titles()


def _bloomberg_homepage_titles() -> list[str]:
    """Extract article titles from bloomberg.com homepage HTML."""
    try:
        import http.client
        import ssl

        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("www.bloomberg.com", context=ctx, timeout=10)
        conn.request(
            "GET", "/",
            headers={
                "User-Agent": _FONT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            }
        )
        resp = conn.getresponse()
        if resp.status != 200:
            conn.close()
            return []
        html = resp.read().decode("utf-8", errors="replace")
        conn.close()

        # Extract titles from multiple HTML patterns Bloomberg uses:
        # - <a class="...title..." ...>Title</a>
        # - <h2>...<a ...>Title</a></h2>
        # - <a class="...headline..." ...>Title</a>
        # - <h3>...<a ...>Title</a></h3>
        import re as _re

        seen: set[str] = set()
        result: list[str] = []

        # Pattern 1: <a href="/news/..." class="...">Title</a> — matches most article links
        for m in _re.finditer(
            r'<a[^>]*href="/news/[^"]*"[^>]*>([^<]*(?:(?!<a[^>]*>)<[^<]*)*[^<]*)</a>',
            html, _re.I | _re.S,
        ):
            t = _re.sub(r"<[^>]+>", "", m.group(1)).strip()
            t = _re.sub(r"\s+", " ", t)
            if len(t) > 10 and t not in seen:
                seen.add(t)
                result.append(t)

        # Pattern 2: <a href="/opinion/..." ...>Title</a>
        for m in _re.finditer(
            r'<a[^>]*href="/opinion/[^"]*"[^>]*>([^<]*(?:(?!<a[^>]*>)<[^<]*)*[^<]*)</a>',
            html, _re.I | _re.S,
        ):
            t = _re.sub(r"<[^>]+>", "", m.group(1)).strip()
            t = _re.sub(r"\s+", " ", t)
            if len(t) > 10 and t not in seen:
                seen.add(t)
                result.append(t)

        return result
    except Exception:
        return []


def _bloomberg_api_titles() -> list[str]:
    """Return titles from Bloomberg mobile API section endpoints."""
    featured_per_section: list[list[str]] = []
    for path in _BB_SECTION_ENDPOINTS:
        try:
            req = urllib.request.Request(
                _BB_API_BASE + path,
                headers={"Accept-Encoding": "gzip", "User-Agent": _FONT_UA},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read()
            data = json.loads(gzip.decompress(raw))
            titles: list[str] = []
            for mod in data.get("modules", []):
                if _is_featured_module(mod.get("id", "")):
                    for story in mod.get("stories") or []:
                        t = (story.get("title") or "").strip()
                        if t:
                            titles.append(t)
            featured_per_section.append(titles)
        except Exception:
            featured_per_section.append([])

    # Round-robin across sections: top story from each section first
    seen: set[str] = set()
    result: list[str] = []
    for group in zip_longest(*featured_per_section):
        for t in group or []:
            if t and t not in seen:
                result.append(t)
                seen.add(t)
    return result


def _title_key(title: str) -> str:
    """Normalize a title for fuzzy matching (lowercase, alphanumeric only)."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

# Global timezone for display (set by --timezone arg in main())
_DISPLAY_TZ = None


def _format_ts(ts: datetime) -> str:
    """Format a datetime as a concise publish time in the configured display timezone."""
    if _DISPLAY_TZ:
        ts = ts.astimezone(_DISPLAY_TZ)
    today = datetime.now(_DISPLAY_TZ).date()
    ts_date = ts.date()
    time_str = ts.strftime("%I:%M %p")
    time_str = time_str[1:] if time_str.startswith("0") else time_str
    delta = (today - ts_date).days
    if delta == 0:
        return time_str
    if delta == 1:
        return f"Yesterday {time_str}"
    return f"{ts.strftime('%b')} {ts_date.day}, {time_str}"


def _strip_tags(s: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _he(s: str) -> str:
    """HTML-escape."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _slug(source: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", source.lower()).strip("-")


def _feed_num(path: str) -> int:
    m = re.search(r"feed_(\d+)", path)
    return int(m.group(1)) if m else 9999


# Matches "Updated on Apr 21, 2026 at 10:00 PM" or "Published on ..."
_AUTH_TS_RE = re.compile(
    r"((?:Updated|Published) on ([A-Za-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M))",
    re.I,
)


def _parse_article_ts(html_text: str, fallback: datetime) -> tuple[datetime, str | None]:
    """Extract publication datetime from the Calibre auth line, or return fallback.

    The container runs with TZ=America/Chicago, so the timestamps Calibre embeds
    are already in Chicago time. We attach the display timezone directly so
    _format_ts renders them as-is (no double-conversion).

    Returns (datetime, full_auth_match) where full_auth_match is the matched text
    (group 1) for in-place replacement, or None if no match.
    """
    m = _AUTH_TS_RE.search(html_text)
    if m:
        try:
            dt = datetime.strptime(m.group(2), "%b %d, %Y at %I:%M %p")
            tz = _DISPLAY_TZ if _DISPLAY_TZ else timezone.utc
            return dt.replace(tzinfo=tz), m.group(1)
        except ValueError:
            pass
    tz = _DISPLAY_TZ if _DISPLAY_TZ else timezone.utc
    fb = fallback.replace(tzinfo=tz) if fallback.tzinfo is None else fallback
    return fb, None


def _reading_time(html_text: str) -> int:
    """Estimate reading time in minutes (~250wpm). Returns 0 for empty content."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"\s+", " ", text).strip()
    words = len(text.split())
    return max(1, round(words / 250)) if words > 20 else 0


# ---------------------------------------------------------------------------
# Font download
# ---------------------------------------------------------------------------

def _ensure_fonts(out_dir: Path) -> str:
    """
    Download woff2 files once to out_dir/fonts/.
    Writes:
      fonts/_faces.css  — @font-face with url(filename.woff2), for @import from reader.css
      fonts/_index.css  — @font-face with url(fonts/filename.woff2), embedded in index.html
    Returns the content of _index.css (to embed in index.html <style>).
    On failure returns "" — pages fall back to system serif stack.
    """
    fonts_dir = out_dir / "fonts"
    marker = fonts_dir / ".downloaded"
    faces_for_articles = fonts_dir / "_faces.css"
    faces_for_index = fonts_dir / "_index.css"

    if marker.exists() and faces_for_articles.exists() and faces_for_index.exists():
        return faces_for_index.read_text(encoding="utf-8")

    fonts_dir.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(_FONT_CSS_URL, headers={"User-Agent": _FONT_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            css_text = resp.read().decode("utf-8")

        face_re = re.compile(r"(@font-face\s*\{[^}]+\})", re.DOTALL)
        url_re = re.compile(r"url\((https://fonts\.gstatic\.com[^)]+\.woff2)\)")

        article_faces: list[str] = []
        index_faces: list[str] = []
        # Map remote URL → local filename so variable fonts shared across weights
        # reference the same file without re-downloading.
        url_to_local: dict[str, str] = {}

        for face_m in face_re.finditer(css_text):
            block = face_m.group(1)
            url_m = url_re.search(block)
            if not url_m:
                continue

            # Skip non-latin subsets (keep only blocks containing U+0000-00FF)
            if "unicode-range" in block and "U+0000" not in block:
                continue

            woff2_url = url_m.group(1)

            if woff2_url in url_to_local:
                # Variable font: reuse already-downloaded file for this weight/style
                local_name = url_to_local[woff2_url]
            else:
                family_m = re.search(r"font-family:\s*'?([^;'\n]+)'?;", block)
                weight_m = re.search(r"font-weight:\s*(\d+)", block)
                style_m = re.search(r"font-style:\s*(\w+)", block)
                family = re.sub(r"\s+", "-", (family_m.group(1).strip() if family_m else "font")).lower()
                weight = weight_m.group(1) if weight_m else "400"
                style = style_m.group(1) if style_m else "normal"
                local_name = f"{family}-{weight}-{style}.woff2"
                local_path = fonts_dir / local_name
                if not local_path.exists():
                    req2 = urllib.request.Request(woff2_url, headers={"User-Agent": _FONT_UA})
                    with urllib.request.urlopen(req2, timeout=30) as r:
                        local_path.write_bytes(r.read())
                url_to_local[woff2_url] = local_name

            # articles CSS: paths relative to fonts/ dir → just filename
            article_faces.append(url_re.sub(f"url({local_name})", block))
            # index CSS: paths relative to out_dir/ → fonts/filename
            index_faces.append(url_re.sub(f"url(fonts/{local_name})", block))

        faces_for_articles.write_text("\n\n".join(article_faces), encoding="utf-8")
        faces_for_index.write_text("\n\n".join(index_faces), encoding="utf-8")
        marker.touch()
        print(f"Downloaded {len(article_faces)} font faces to {fonts_dir}")
        return "\n\n".join(index_faces)

    except Exception as exc:
        print(f"Warning: font download failed ({exc}); falling back to system serif")
        return ""


# ---------------------------------------------------------------------------
# EPUB parsing
# ---------------------------------------------------------------------------

def _parse_epub_meta(epub_path: Path) -> tuple[str, str]:
    """Return (source_title, pub_date) from OPF metadata."""
    with zipfile.ZipFile(epub_path) as z:
        # Find OPF via container.xml
        try:
            container = z.read("META-INF/container.xml").decode("utf-8", errors="replace")
            opf_path_m = re.search(r'full-path="([^"]+\.opf)"', container)
            opf_path = opf_path_m.group(1) if opf_path_m else "content.opf"
            opf = z.read(opf_path).decode("utf-8", errors="replace")
        except KeyError:
            opf = ""

    title_m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.I | re.S)
    # Calibre puts the date in dc:creator with role="aut"
    date_m = re.search(r'<dc:creator[^>]*>(.*?)</dc:creator>', opf, re.I | re.S)

    source = _strip_tags(title_m.group(1)).strip() if title_m else ""
    pub_date = _strip_tags(date_m.group(1)).strip() if date_m else ""

    # Validate ISO date; fall back to filename
    if not re.match(r"\d{4}-\d{2}-\d{2}$", pub_date):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", epub_path.stem)
        pub_date = m.group(1) if m else ""
    if not source:
        source = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", epub_path.stem).strip("-")

    return source, pub_date


def _parse_feed_toc(data: bytes) -> tuple[str, list[dict]]:
    """
    Parse a Calibre feed index page (feed_N/index_*.html).
    Returns (section_name, [{"title", "href", "desc"}]).
    href is relative to the feed_N/ directory.
    """
    text = data.decode("utf-8", errors="replace")

    # Section name from h2.calibre_feed_title or <title>
    section = ""
    m = re.search(r'class="calibre_feed_title"[^>]*>(.*?)</h\d>', text, re.I | re.S)
    if m:
        section = _strip_tags(m.group(1)).strip()
    if not section:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            section = _strip_tags(m.group(1)).strip()

    articles: list[dict] = []
    for li_m in re.finditer(r"<li[^>]*>(.*?)</li>", text, re.I | re.S):
        li = li_m.group(1)
        a_m = re.search(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.I | re.S)
        if not a_m:
            continue
        href = a_m.group(1).strip()
        title = _strip_tags(a_m.group(2)).strip()
        desc_m = re.search(r'class="article_description"[^>]*>(.*?)</div>', li, re.I | re.S)
        desc = _strip_tags(desc_m.group(1)).strip()[:180] if desc_m else ""
        if title and href:
            articles.append({"title": title, "href": href, "desc": desc})

    return section, articles


def _is_toc_item(path: str) -> bool:
    """True if this EPUB-relative path is a section TOC page (feed_N/index_*.html)."""
    parts = path.replace("\\", "/").split("/")
    return (
        len(parts) == 2
        and parts[0].startswith("feed_")
        and "index" in parts[1].lower()
        and parts[1].endswith(".html")
    )


def _is_article_html(path: str) -> bool:
    """True if this is an article HTML page (feed_N/article_M/index_*.html)."""
    parts = path.replace("\\", "/").split("/")
    return (
        len(parts) == 3
        and parts[0].startswith("feed_")
        and parts[1].startswith("article_")
        and parts[2].endswith((".html", ".xhtml"))
    )


def extract_epub(epub_path: Path, out_dir: Path, source: str, pub_date: str) -> list[dict]:
    """
    Extract article HTML + images from an EPUB into out_dir/articles/{source}-{pub_date}/.
    Strips Calibre CSS links, injects reader.css, adds back-link on article pages.
    Returns list of article metadata dicts (with per-article timestamps) for the index.
    """
    extract_dir = out_dir / "articles" / f"{source}-{pub_date}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    # extract_dir is 2 levels below out_dir (articles/{source}-{pub_date}/)
    # Articles are 2 more levels deep (feed_N/article_M/index.html) → total 4 levels
    EXTRACT_DEPTH = 2

    # EPUB build datetime used as fallback when no per-article timestamp exists
    epub_dt = datetime(
        *map(int, pub_date.split("-"))
    ) if re.match(r"\d{4}-\d{2}-\d{2}$", pub_date) else datetime.now()

    articles_by_feed: dict[int, dict] = {}
    # Map epub-relative article path → extracted datetime
    article_ts: dict[str, datetime] = {}
    # Map epub-relative article path → reading time in minutes
    article_rt: dict[str, int] = {}
    # Map "feed_N/article_M" → epub-relative path of first usable thumbnail image
    first_images: dict[str, str] = {}

    with zipfile.ZipFile(epub_path) as z:
        names = z.namelist()

        # Pass 1: parse feed TOC pages for section/article metadata
        for name in names:
            if _is_toc_item(name):
                feed_num = _feed_num(name)
                feed_key = name.split("/")[0]
                section, articles = _parse_feed_toc(z.read(name))
                if section and articles:
                    articles_by_feed[feed_num] = {
                        "feed_key": feed_key,
                        "section": section,
                        "articles": articles,
                    }

        # Pass 2: extract feed_N/ content (HTML + images + any other assets)
        for name in names:
            if name.endswith("/"):
                continue
            rel = name.replace("\\", "/")
            parts = rel.split("/")
            if not parts[0].startswith("feed_"):
                continue

            target = extract_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            data = z.read(name)

            if rel.endswith((".html", ".xhtml")):
                text = data.decode("utf-8", errors="replace")
                depth_within = len(parts) - 1  # dirs deep within extract_dir
                total_up = EXTRACT_DEPTH + depth_within
                up = "../" * total_up

                # Remove existing Calibre stylesheet links
                text = re.sub(
                    r'<link[^>]+href="[^"]*(?:stylesheet|page_styles)\.css"[^>]*/?>',
                    "",
                    text,
                    flags=re.I,
                )
                # Insert reader.css before </head>
                style_link = f'<link rel="stylesheet" href="{up}reader.css">\n'
                text = re.sub(r"(</head>)", style_link + r"\1", text, count=1, flags=re.I)

                if _is_article_html(rel):
                    # Extract per-article timestamp from auth line
                    ts, auth_match = _parse_article_ts(text, epub_dt)
                    article_ts[rel] = ts

                    # Convert UTC timestamp in article to display timezone
                    if auth_match and _DISPLAY_TZ:
                        display_ts = ts.astimezone(_DISPLAY_TZ)
                        time_str = display_ts.strftime("%I:%M %p").lstrip("0") or "12:00 AM"
                        new_ts = f"Updated on {display_ts.strftime('%b %d, %Y at ')}{time_str}"
                        text = text.replace(auth_match, new_ts)

                    # Calculate reading time from article body
                    article_rt[rel] = _reading_time(text)

                    # Add back-link nav
                    back = (
                        f'<div class="back-nav">'
                        f'<a href="{up}index.html">← Index</a>'
                        f'</div>\n'
                    )
                    text = re.sub(r"(<body[^>]*>)", r"\1\n" + back, text, count=1, flags=re.I)

                target.write_bytes(text.encode("utf-8"))
            else:
                target.write_bytes(data)
                # Track first image per article for thumbnail display on index page
                if "/images/" in rel and len(parts) == 4:
                    ext = parts[3].rsplit(".", 1)[-1].lower() if "." in parts[3] else ""
                    if ext in ("jpg", "jpeg", "png", "webp"):
                        art_key = f"{parts[0]}/{parts[1]}"
                        if art_key not in first_images:
                            first_images[art_key] = rel

    # Build article list in feed order, attaching timestamps
    result: list[dict] = []
    for feed_num in sorted(articles_by_feed):
        entry = articles_by_feed[feed_num]
        feed_key = entry["feed_key"]
        section = entry["section"]
        for art in entry["articles"]:
            # art["href"] is relative to feed_key/ (e.g. "article_0/index_u90.html")
            epub_rel = f"{feed_key}/{art['href']}"          # path within EPUB
            full_href = f"articles/{source}-{pub_date}/{epub_rel}"
            ts = article_ts.get(epub_rel, epub_dt)
            thumb_art_key = f"{feed_key}/{art['href'].split('/')[0]}"
            thumb_rel = first_images.get(thumb_art_key, "")
            result.append({
                "title": art["title"],
                "desc": art["desc"],
                "section": section,
                "source": source,
                "date": pub_date,
                "href": full_href,
                "ts": ts,
                "rt": article_rt.get(epub_rel, 0),
                "thumb": f"articles/{source}-{pub_date}/{thumb_rel}" if thumb_rel else "",
            })
    return result


# ---------------------------------------------------------------------------
# Stale cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale(out_dir: Path, active_keys: set[str]) -> None:
    """Remove articles/ subdirs whose corresponding EPUB no longer exists."""
    articles_dir = out_dir / "articles"
    if not articles_dir.is_dir():
        return
    for d in articles_dir.iterdir():
        if d.is_dir() and d.name not in active_keys:
            print(f"Removing stale articles dir: {d.name}")
            shutil.rmtree(d)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_READER_CSS_TEMPLATE = """\
/* Bloomberg — article reader stylesheet */
{font_import}

:root {{
  --red: #ed1c24;
  --text: #e0e0e0;
  --bg: #111;
  --muted: #888;
  --border: #2c2c2c;
}}

*, *::before, *::after {{ box-sizing: border-box; }}

html {{
  -webkit-text-size-adjust: 100%;
  scroll-padding-top: 0;
}}

body {{
  font-family: 'Source Serif 4', {fallback};
  font-size: 18px;
  line-height: 1.7;
  color: var(--text);
  margin: 0;
  padding: 1rem;
  background: var(--bg);
  overflow-x: hidden;
}}

.back-nav {{
  max-width: 680px;
  margin: 0 auto 1.5rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid var(--border);
}}

.back-nav a {{
  text-decoration: none;
  color: var(--muted);
  font-size: 0.85rem;
  font-family: Arial, sans-serif;
  padding: 0.25rem 0;
  display: inline-block;
}}

.back-nav a:hover {{ color: var(--red); }}

.article, .article1, article {{
  max-width: 680px;
  margin: 0 auto;
}}

h1 {{
  font-family: 'Playfair Display', {fallback};
  font-size: clamp(1.6rem, 5vw, 2rem);
  font-weight: 700;
  line-height: 1.2;
  margin-bottom: 0.5rem;
}}

h2 {{
  font-family: 'Playfair Display', {fallback};
  font-size: clamp(1.3rem, 4vw, 1.5rem);
  margin: 1.5rem 0 0.5rem;
}}

h3 {{
  font-family: 'Playfair Display', {fallback};
  font-size: clamp(1.1rem, 3.5vw, 1.25rem);
  margin: 1.25rem 0 0.4rem;
}}

h4 {{
  font-family: 'Playfair Display', {fallback};
  font-size: 1.05rem;
  margin: 1rem 0 0.3rem;
}}

p {{
  margin-bottom: 1.25em;
  text-align: left;
  hyphens: auto;
  -webkit-hyphens: auto;
}}

.auth, p.auth {{
  color: var(--muted);
  font-size: 0.85rem;
  margin-bottom: 1.5rem;
  font-family: Arial, sans-serif;
}}

.subhead, .standfirst {{
  font-style: italic;
  color: #bbb;
  margin-bottom: 1.5rem;
  font-size: 1.05em;
}}

img {{
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1.5rem auto;
  border-radius: 2px;
}}

blockquote {{
  border-left: 3px solid var(--red);
  margin: 1.5rem 0;
  padding: 0.5rem 0 0.5rem 1.25rem;
  color: #aaa;
  font-style: italic;
}}

.img-cap, .figc {{
  font-size: 0.8rem;
  text-align: center;
  color: var(--muted);
  margin-top: -1rem;
  margin-bottom: 1.5rem;
}}

/* Tables in articles */
table {{
  width: 100%;
  max-width: 100%;
  border-collapse: collapse;
  margin: 1.5rem 0;
  font-size: 0.9em;
  overflow-x: auto;
  display: block;
}}

th, td {{
  border: 1px solid var(--border);
  padding: 0.5rem 0.75rem;
  text-align: left;
}}

th {{
  background: #1a1a1a;
  font-weight: 700;
}}

/* Lists */
ul, ol {{
  margin: 1rem 0 1.25rem 1.5rem;
}}

li {{
  margin-bottom: 0.5rem;
}}

/* Links */
a {{
  color: var(--text);
  text-decoration: none;
}}

a:hover {{
  color: var(--red);
  text-decoration: underline;
}}

/* Hide Calibre internal markers */
.x4-chap-marker {{ display: none !important; }}
.calibre_navbar {{ display: none !important; }}
.toc-page {{ display: none !important; }}

/* Mobile adjustments */
@media (max-width: 600px) {{
  body {{
    font-size: 17px;
    line-height: 1.65;
    padding: 0.75rem;
  }}
  .back-nav {{
    margin-bottom: 1rem;
  }}
  .back-nav a {{
    font-size: 0.9rem;
    padding: 0.5rem 0;
  }}
  h1 {{
    font-size: clamp(1.5rem, 7vw, 1.8rem);
  }}
  img {{
    margin: 1.25rem 0;
  }}
  blockquote {{
    margin: 1.25rem 0;
    padding-left: 1rem;
  }}
  table {{
    font-size: 0.8em;
  }}
  th, td {{
    padding: 0.4rem 0.5rem;
  }}
}}

/* Larger desktop */
@media (min-width: 900px) {{
  body {{
    font-size: 19px;
  }}
}}
"""


def _write_reader_css(out_dir: Path, fonts_available: bool) -> None:
    font_import = "@import url('fonts/_faces.css');" if fonts_available else ""
    css = _READER_CSS_TEMPLATE.format(font_import=font_import, fallback=_FALLBACK_SERIF)
    (out_dir / "reader.css").write_text(css, encoding="utf-8")


def _build_index_html(
    all_articles: list[dict],
    font_css: str,
    bb_featured: list[str] | None = None,
) -> str:
    """Produce the Bloomberg-style index.html as a string."""

    # Normalize featured titles to keys for fast lookup
    _bb_featured: dict[str, int] = (
        {_title_key(t): i for i, t in enumerate(bb_featured)}
        if bb_featured else {}
    )

    # Source order: priority list first, then others in appearance order
    sources_ordered: list[str] = []
    for s in _SOURCE_PRIORITY:
        if any(a["source"] == s for a in all_articles):
            sources_ordered.append(s)
    for a in all_articles:
        if a["source"] not in sources_ordered:
            sources_ordered.append(a["source"])

    serif = f"'Playfair Display', {_FALLBACK_SERIF}"
    body_font = f"'Source Serif 4', {_FALLBACK_SERIF}"

    # ── Hero: full-width featured article from top source ───────────────────
    hero_html = ""
    hero_excluded: set[str] = set()
    if all_articles and sources_ordered:
        hero_src = sources_ordered[0]
        src_arts = sorted(
            [a for a in all_articles if a["source"] == hero_src],
            key=lambda a: a["ts"], reverse=True,
        )
        h = next((a for a in src_arts if a.get("thumb")), src_arts[0] if src_arts else None)
        if h:
            hero_excluded = {h["href"]}

            hero_img = (
                f'<a href="{_he(h["href"])}" class="hero-img-link">'
                f'<img class="hero-img" src="{_he(h["thumb"])}" alt="" loading="eager">'
                f'</a>\n'
            ) if h.get("thumb") else ""

            hero_html = (
                f'<section class="hero-section" data-source="{_he(_slug(hero_src))}">\n'
                + hero_img
                + f'<div class="hero-eyebrow">{_he(hero_src)}</div>\n'
                + f'<h2><a href="{_he(h["href"])}">{_he(h["title"])}</a></h2>\n'
                + (f'<p class="hero-desc">{_he(h["desc"])}</p>\n' if h["desc"] else "")
                + f'<div class="hero-date">{_he(_format_ts(h["ts"]))}</div>\n'
                + '</section>\n'
                + '<hr class="divider">'
            )

    # ── Nav filter buttons ───────────────────────────────────────────────────
    nav_buttons = ['<button class="filter active" data-filter="all">All</button>']
    for src in sources_ordered:
        nav_buttons.append(
            f'<button class="filter" data-filter="{_he(_slug(src))}">{_he(src)}</button>'
        )
    nav_html = "\n      ".join(nav_buttons)

    # ── Group: source → section → articles (feed order; ts-sorted within) ───
    by_source: dict[str, dict[str, list[dict]]] = {}
    section_order: dict[str, list[str]] = {}
    for a in all_articles:
        src, sec = a["source"], a["section"]
        if src not in by_source:
            by_source[src] = {}
            section_order[src] = []
        if sec not in by_source[src]:
            by_source[src][sec] = []
            section_order[src].append(sec)
        by_source[src][sec].append(a)

    def _reading_time_html(art: dict) -> str:
        rt = art.get("rt", 0)
        return f'<span class="article-reading-time">{rt} min read</span>' if rt else ""

    def _article_card(art: dict, show_category: bool = False) -> str:
        thumb_html = (
            f'<a href="{_he(art["href"])}" class="article-thumb-link">'
            f'<img class="article-thumb" src="{_he(art["thumb"])}" alt="" loading="lazy">'
            f'</a>\n'
        ) if art.get("thumb") else ""
        cat_html = (
            f'<span class="article-category">{_he(art["section"])}</span>\n'
        ) if show_category else ""
        rt_html = _reading_time_html(art)
        return (
            '<li class="article-item">\n'
            + thumb_html
            + '<div class="article-body">\n'
            + cat_html
            + f'<a class="article-title" href="{_he(art["href"])}">{_he(art["title"])}</a>\n'
            + f'<span class="article-date">{_he(_format_ts(art["ts"]))}</span>\n'
            + (rt_html + "\n" if rt_html else "")
            + (f'<p class="article-desc">{_he(art["desc"][:140])}</p>\n' if art["desc"] else "")
            + '</div>\n</li>'
        )

    sections_parts: list[str] = []
    for src in sources_ordered:
        src_secs = by_source.get(src, {})
        if not src_secs:
            continue
        slug = _slug(src)
        first_art = next(iter(src_secs.values()))[0]
        try:
            d = date.fromisoformat(first_art["date"])
            date_display = f"{d.strftime('%b')} {d.day}, {d.year}"
        except ValueError:
            date_display = first_art["date"]

        if src == "Bloomberg":
            # Round-robin interleave across sections as baseline order,
            # then re-sort by live Bloomberg homepage prominence if available.
            section_lists = [src_secs[sec] for sec in section_order.get(src, [])]
            flat_arts = [
                art for group in zip_longest(*section_lists)
                for art in group
                if art is not None and art["href"] not in hero_excluded
            ]
            if _bb_featured:
                fallback = len(_bb_featured)
                flat_arts.sort(key=lambda a: _bb_featured.get(_title_key(a["title"]), fallback))

            # Split: top-6 featured articles → prominent 2-col block;
            # remainder → regular 3-col grid.
            def _is_featured(art: dict) -> bool:
                return _bb_featured.get(_title_key(art["title"])) is not None

            top_arts = [a for a in flat_arts if _is_featured(a)][:6]
            top_hrefs = {a["href"] for a in top_arts}
            rest_arts = [a for a in flat_arts if a["href"] not in top_hrefs]

            def _top_story_card(art: dict) -> str:
                img_html = (
                    f'<a href="{_he(art["href"])}" class="article-thumb-link">'
                    f'<img class="ts-img" src="{_he(art["thumb"])}" alt="" loading="lazy">'
                    f'</a>\n'
                ) if art.get("thumb") else ""
                rt_html = _reading_time_html(art)
                return (
                    '<li class="ts-item">\n'
                    + img_html
                    + f'<span class="article-category">{_he(art["section"])}</span>\n'
                    + f'<a class="ts-title" href="{_he(art["href"])}">{_he(art["title"])}</a>\n'
                    + f'<span class="article-date">{_he(_format_ts(art["ts"]))}</span>\n'
                    + (rt_html + "\n" if rt_html else "")
                    + (f'<p class="article-desc">{_he(art["desc"][:160])}</p>\n' if art["desc"] else "")
                    + '</li>'
                )

            top_block = ""
            if top_arts:
                top_items = "\n".join(_top_story_card(a) for a in top_arts)
                top_block = (
                    '<div class="ts-label"><span>Top Stories</span></div>\n'
                    '<ul class="ts-grid">\n' + top_items + "\n</ul>\n"
                    '<hr class="divider" style="margin:1.5rem 0 0">\n'
                )

            rest_items = [_article_card(art, show_category=True) for art in rest_arts]
            rest_block = (
                '<ul class="article-list">\n' + "\n".join(rest_items) + "\n</ul>"
            ) if rest_items else ""

            # Bloomberg: no source header — Top Stories label is sufficient
            sections_parts.append(
                f'<section class="source-section" data-source="{_he(slug)}">\n'
                + top_block
                + rest_block + "\n"
                + "</section>"
            )
        else:
            cat_blocks: list[str] = []
            for sec in section_order.get(src, []):
                arts = sorted(src_secs[sec], key=lambda a: a["ts"], reverse=True)
                items = [_article_card(art) for art in arts]
                cat_blocks.append(
                    '<div class="category-block">\n'
                    f'<div class="category-label"><span>{_he(sec)}</span></div>\n'
                    '<ul class="article-list">\n'
                    + "\n".join(items) + "\n"
                    + '</ul>\n</div>'
                )
            sections_parts.append(
                f'<section class="source-section" data-source="{_he(slug)}">\n'
                '<div class="source-label">'
                f'<span class="source-name">{_he(src)}</span>'
                '</div>\n'
                + "\n".join(cat_blocks) + "\n"
                + "</section>"
            )

    sections_html = "\n<hr class=\"section-divider\">\n".join(sections_parts)

    # ── Latest rail: 10 most-recent articles across all sources ─────────────
    latest_arts = sorted(all_articles, key=lambda a: a["ts"], reverse=True)[:15]
    latest_items = ""
    for la in latest_arts:
        latest_items += (
            '<li class="latest-item">\n'
            f'<span class="latest-source">{_he(la["source"])}</span>\n'
            f'<a class="latest-title" href="{_he(la["href"])}">{_he(la["title"])}</a>\n'
            f'<span class="latest-time">{_he(_format_ts(la["ts"]))}</span>\n'
            '</li>\n'
        )
    latest_html = (
        '<aside id="latest-rail" class="latest-rail">\n'
        '<div class="latest-header">Latest</div>\n'
        f'<ol class="latest-list">\n{latest_items}</ol>\n'
        '</aside>'
    )

    today_d = date.today()
    today_display = f"{today_d.strftime('%B')} {today_d.day}, {today_d.year}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bloomberg</title>
<meta name="theme-color" content="#111">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="mobile-web-app-capable" content="yes">
<style>
{font_css}

:root {{
  --red: #ed1c24;
  --text: #e0e0e0;
  --bg: #111;
  --muted: #888;
  --border: #2c2c2c;
  --serif: {serif};
  --body: {body_font};
  --max: 1280px;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: var(--body); background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.5; }}
a {{ color: inherit; text-decoration: none; }}

/* Header — black bar like bloomberg.com */
header {{
  background: #000; position: sticky; top: 0; z-index: 100;
  border-bottom: 2px solid var(--red);
}}
.header-inner {{
  max-width: var(--max); margin: 0 auto; padding: 0.5rem 1.25rem;
  display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap;
}}
.site-name {{
  font-family: Arial, sans-serif; font-size: 1.1rem; font-weight: 900;
  color: #fff; letter-spacing: 0.02em; white-space: nowrap; text-transform: uppercase;
}}
nav {{ display: flex; gap: 0.2rem; flex-wrap: wrap; }}
.filter {{
  background: none; border: none;
  padding: 0.25rem 0.6rem; cursor: pointer; font-size: 0.72rem;
  font-family: Arial, sans-serif; color: rgba(255,255,255,0.6);
  transition: color 0.15s; border-bottom: 2px solid transparent;
}}
.filter:hover {{ color: #fff; }}
.filter.active {{ color: #fff; border-bottom-color: var(--red); }}

/* Page layout */
.page-grid {{
  max-width: var(--max); margin: 0 auto; padding: 1.25rem 1.25rem 3rem;
  display: grid; grid-template-columns: 1fr 280px; gap: 0 2rem; align-items: start;
}}
.main-content {{ min-width: 0; }}
hr.divider {{ border: none; border-top: 1px solid var(--border); margin: 0 0 1.5rem; }}
hr.section-divider {{ border: none; border-top: 1px solid var(--border); margin: 1.5rem 0; }}

/* Latest rail — right sidebar */
.latest-rail {{
  position: sticky; top: 4rem;
  border-left: 1px solid var(--border); padding-left: 1rem;
}}
.latest-header {{
  font-family: Arial, sans-serif; font-size: 0.65rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--red); margin-bottom: 0.75rem;
}}
.latest-list {{ list-style: none; }}
.latest-item {{
  padding: 0.6rem 0; border-bottom: 1px solid var(--border);
}}
.latest-item:first-child {{ padding-top: 0; }}
.latest-item:last-child {{ border-bottom: none; }}
.latest-source {{
  display: block; font-family: Arial, sans-serif;
  font-size: 0.6rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--red); margin-bottom: 0.15rem;
}}
.latest-title {{
  display: block; font-family: var(--serif); font-size: 0.85rem;
  font-weight: 400; line-height: 1.35; color: var(--text);
  text-decoration: none; margin-bottom: 0.15rem;
}}
.latest-title:hover {{ color: var(--red); text-decoration: underline; }}
.latest-time {{
  font-family: Arial, sans-serif; font-size: 0.62rem; color: var(--muted);
}}

/* Hero */
.hero-section {{ padding: 1rem 0 1.5rem; }}
.hero-img-link {{ display: block; overflow: hidden; margin-bottom: 1rem; }}
.hero-img {{ width: 100%; max-height: 420px; object-fit: cover; display: block; }}
.hero-img:hover {{ opacity: 0.92; }}
.hero-eyebrow {{
  text-transform: uppercase; font-size: 0.65rem; letter-spacing: 0.12em;
  color: var(--red); font-weight: 700; margin-bottom: 0.35rem; font-family: Arial, sans-serif;
}}
.hero-section h2 {{
  font-family: var(--serif); font-size: clamp(1.8rem, 3.5vw, 2.8rem);
  font-weight: 700; line-height: 1.1; margin-bottom: 0.5rem; max-width: 800px;
}}
.hero-section h2 a {{ color: var(--text); }}
.hero-section h2 a:hover {{ color: var(--red); }}
.hero-desc {{ font-size: 0.95rem; line-height: 1.5; color: var(--muted); margin-bottom: 0.4rem; max-width: 680px; }}
.hero-date {{ font-size: 0.65rem; color: var(--muted); font-family: Arial, sans-serif; }}

/* Source sections */
.source-section {{ padding: 1rem 0; }}
.source-label {{
  display: flex; align-items: baseline; gap: 0.75rem;
  border-bottom: 2px solid #000; padding-bottom: 0.4rem; margin-bottom: 1rem;
}}
.source-name {{
  font-family: Arial, Helvetica, sans-serif; font-size: 0.9rem; font-weight: 900;
  color: var(--text); text-transform: uppercase; letter-spacing: 0.03em;
}}
.source-date {{ font-size: 0.65rem; color: var(--muted); font-family: Arial, sans-serif; }}

/* Category blocks */
.category-block {{ margin-top: 1.25rem; }}
.category-block:first-of-type {{ margin-top: 0; }}
.category-label {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem; }}
.category-label span {{
  font-family: Arial, sans-serif; font-size: 0.65rem; font-weight: 700;
  color: var(--red); text-transform: uppercase; letter-spacing: 0.08em; white-space: nowrap;
}}
.category-label::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}

/* Article grid — 3 columns */
.article-list {{ list-style: none; display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 1.5rem; }}
.article-item {{ padding: 0.75rem 0; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; }}
.article-item:last-child {{ border-bottom: none; }}
.article-thumb-link {{ display: block; overflow: hidden; margin-bottom: 0.35rem; }}
.article-thumb {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }}
.article-thumb:hover {{ opacity: 0.85; }}
.article-body {{ display: flex; flex-direction: column; gap: 0.15rem; flex: 1; }}
.article-title {{
  font-family: var(--serif); font-size: 0.9rem; font-weight: 700;
  color: var(--text); text-decoration: none; line-height: 1.3;
}}
.article-title:hover {{ color: var(--red); text-decoration: underline; }}
.article-date {{ font-size: 0.62rem; color: var(--muted); font-family: Arial, sans-serif; }}
.article-reading-time {{ font-size: 0.62rem; color: var(--muted); font-family: Arial, sans-serif; }}
.article-category {{
  font-size: 0.58rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--red); font-family: Arial, sans-serif;
}}
.article-desc {{ font-size: 0.78rem; color: var(--muted); line-height: 1.4; }}

/* Top Stories — 2-col prominent grid */
.ts-label {{
  display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.85rem;
}}
.ts-label span {{
  font-family: Arial, sans-serif; font-size: 0.65rem; font-weight: 700;
  color: var(--red); text-transform: uppercase; letter-spacing: 0.1em; white-space: nowrap;
}}
.ts-label::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}
.ts-grid {{
  list-style: none;
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.25rem 2rem;
}}
.ts-item {{ display: flex; flex-direction: column; gap: 0.2rem; }}
.ts-img {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; margin-bottom: 0.3rem; }}
.ts-img:hover {{ opacity: 0.85; }}
.ts-title {{
  font-family: var(--serif); font-size: 1rem; font-weight: 700;
  line-height: 1.2; color: var(--text); text-decoration: none;
}}
.ts-title:hover {{ color: var(--red); text-decoration: underline; }}

/* Footer */
footer {{
  border-top: 2px solid var(--border); padding: 0.75rem 1.25rem;
  text-align: center; font-size: 0.65rem; color: var(--muted);
  font-family: Arial, sans-serif; max-width: var(--max); margin: 1.5rem auto 0;
}}

/* Responsive */
@media (max-width: 1060px) {{
  .page-grid {{
    grid-template-columns: 1fr;
  }}
  .latest-rail {{
    position: static;
    border-left: none;
    padding-left: 0;
    border-top: 1px solid var(--border);
    padding-top: 1rem;
  }}
  .latest-header {{
    font-family: Arial, sans-serif;
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--red);
    margin-bottom: 0.75rem;
  }}
  .latest-list {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0 1rem;
  }}
  .latest-item {{
    padding: 0.5rem 0;
    border-bottom: none;
  }}
}}

@media (max-width: 900px) {{
  .ts-grid {{
    grid-template-columns: repeat(2, 1fr);
  }}
  .article-list {{
    grid-template-columns: repeat(2, 1fr);
  }}
  .hero-img {{
    max-height: 300px;
  }}
  .header-inner {{
    padding: 0.4rem 1rem;
  }}
}}

@media (max-width: 600px) {{
  .ts-grid {{
    grid-template-columns: 1fr;
  }}
  .article-list {{
    grid-template-columns: 1fr;
  }}
  .latest-list {{
    grid-template-columns: 1fr;
  }}
  .hero-img {{
    max-height: 220px;
  }}
  .hero-section h2 {{
    font-size: 1.5rem;
  }}
  .hero-desc {{
    font-size: 0.85rem;
  }}
  .header-inner {{
    padding: 0.35rem 0.75rem;
  }}
  .site-name {{
    font-size: 0.95rem;
  }}
  nav {{
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
    flex-wrap: nowrap;
    white-space: nowrap;
    padding-bottom: 0.25rem;
  }}
  nav::-webkit-scrollbar {{
    display: none;
  }}
  .filter {{
    font-size: 0.7rem;
    padding: 0.3rem 0.5rem;
    white-space: nowrap;
    flex-shrink: 0;
  }}
  .page-grid {{
    padding: 0.75rem 0.75rem 2rem;
  }}
  .article-item {{
    padding: 0.6rem 0;
  }}
  .article-title {{
    font-size: 0.95rem;
  }}
  .article-desc {{
    font-size: 0.8rem;
  }}
  .ts-title {{
    font-size: 1.05rem;
  }}
  .latest-title {{
    font-size: 0.9rem;
  }}
  .latest-item {{
    padding: 0.65rem 0;
  }}
  .source-section {{
    padding: 0.75rem 0;
  }}
  .category-block {{
    margin-top: 1rem;
  }}
  .hero-section {{
    padding: 0.5rem 0 1rem;
  }}
  .hero-eyebrow {{
    font-size: 0.6rem;
  }}
  #refresh-banner {{
    bottom: 1rem;
    font-size: 0.72rem;
    padding: 0.5rem 1rem;
  }}
  footer {{
    padding: 0.5rem 0.75rem;
    font-size: 0.6rem;
  }}
}}

@media (max-width: 380px) {{
  .hero-img {{
    max-height: 180px;
  }}
  .hero-section h2 {{
    font-size: 1.3rem;
  }}
  .filter {{
    font-size: 0.65rem;
    padding: 0.25rem 0.4rem;
  }}
}}

/* Refresh banner */
#refresh-banner {{
  position: fixed; bottom: 1.5rem; left: 50%;
  transform: translateX(-50%) translateY(120px);
  background: var(--red); color: #fff;
  padding: 0.55rem 1.25rem; border-radius: 2rem;
  font-family: Arial, sans-serif; font-size: 0.78rem; font-weight: 700;
  cursor: pointer; z-index: 200; white-space: nowrap;
  box-shadow: 0 4px 18px rgba(0,0,0,0.35);
  transition: transform 0.3s cubic-bezier(.34,1.56,.64,1);
  border: none;
}}
#refresh-banner.visible {{ transform: translateX(-50%) translateY(0); }}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="site-name">Bloomberg</div>
    <nav id="source-filters">
      {nav_html}
    </nav>
  </div>
</header>
<main>
  <div class="page-grid">
    <div class="main-content">
      {hero_html}
      {sections_html}
    </div>
    {latest_html}
  </div>
</main>
<footer>Bloomberg &mdash; {today_display}</footer>
<button id="refresh-banner" aria-live="polite" style="display:none"></button>
<script>
(function () {{
  var btns = document.querySelectorAll('.filter');
  btns.forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var f = btn.dataset.filter;
      btns.forEach(function (b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      document.querySelectorAll('[data-source]').forEach(function (el) {{
        el.style.display = (f === 'all' || el.dataset.source === f) ? '' : 'none';
      }});
    }});
  }});

  var known = new Set();
  document.querySelectorAll('a[href*="articles/"]').forEach(function (a) {{
    known.add(a.pathname);
  }});

  var banner = document.getElementById('refresh-banner');
  banner.addEventListener('click', function () {{ location.reload(); }});

  var rail = document.getElementById('latest-rail');

  function checkForNew() {{
    fetch('index.html?_=' + Date.now())
      .then(function (r) {{ return r.text(); }})
      .then(function (html) {{
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var count = 0;
        doc.querySelectorAll('a[href*="articles/"]').forEach(function (a) {{
          if (!known.has(a.pathname)) count++;
        }});
        var newRail = doc.getElementById('latest-rail');
        if (newRail && rail) {{ rail.innerHTML = newRail.innerHTML; }}
        if (count > 0) {{
          banner.textContent = count + ' new article' + (count === 1 ? '' : 's') + ' — Refresh';
          banner.style.display = '';
          requestAnimationFrame(function () {{
            requestAnimationFrame(function () {{ banner.classList.add('visible'); }});
          }});
        }}
      }})
      .catch(function () {{}});
  }}
  setInterval(checkForNew, 20 * 60 * 1000);
}})();
</script>
</body>
</html>"""


def _empty_index() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bloomberg</title>
<style>
body { font-family: Georgia, serif; background: #fff; color: #000; margin: 0; }
header { border-bottom: 4px solid #000; padding: 0.75rem 1.25rem; }
.site-name { font-size: 1.5rem; font-weight: 900; }
.empty { max-width: 600px; margin: 6rem auto; text-align: center; padding: 0 1.5rem; }
.empty h2 { font-size: 1.5rem; margin-bottom: 1rem; }
.empty p { color: #767676; line-height: 1.6; }
</style>
</head>
<body>
<header><div class="site-name">Bloomberg</div></header>
<main>
<div class="empty">
  <h2>No articles yet</h2>
  <p>The news build is in progress or has not run yet.<br>Check back after the next build cycle.</p>
</div>
</main>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build Bloomberg-style news index from EPUBs")
    default_out = str(
        Path(__file__).resolve().parent / "news_sync_docker" / "data" / "news_out"
    )
    parser.add_argument("--out-dir", default=default_out, help="Directory containing .epub files")
    parser.add_argument(
        "--timezone",
        default="America/Chicago",
        help="Display timezone for article timestamps (default: America/Chicago)",
    )
    args = parser.parse_args()

    # Set global display timezone
    global _DISPLAY_TZ
    if ZoneInfo:
        _DISPLAY_TZ = ZoneInfo(args.timezone)
    else:
        # Fallback: assume UTC offset from env (not ideal but works)
        _DISPLAY_TZ = None
        print(f"Warning: zoneinfo not available, timestamps in UTC")

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    epubs = sorted(out_dir.glob("*.epub"))

    # Determine active extract dirs (stem of each EPUB = "{Source}-{date}")
    active_keys: set[str] = {e.stem for e in epubs}

    # 1. Cleanup stale article dirs
    _cleanup_stale(out_dir, active_keys)

    # 2. Empty-state index if no EPUBs
    if not epubs:
        (out_dir / "index.html").write_text(_empty_index(), encoding="utf-8")
        print("No EPUBs found; wrote empty index.html")
        return

    # 3. Fonts (downloaded once, cached in fonts/)
    font_css = _ensure_fonts(out_dir)
    fonts_available = bool(font_css)

    # 4. reader.css for article pages
    _write_reader_css(out_dir, fonts_available)

    # 5. Extract EPUBs → articles/, collect metadata
    all_articles: list[dict] = []
    for epub in epubs:
        try:
            source, pub_date = _parse_epub_meta(epub)
            print(f"Extracting {epub.name} → articles/{source}-{pub_date}/")
            articles = extract_epub(epub, out_dir, source, pub_date)
            all_articles.extend(articles)
            sections = len({a["section"] for a in articles})
            print(f"  {len(articles)} articles across {sections} sections")
        except Exception as exc:
            print(f"ERROR processing {epub.name}: {exc}")

    # 6. Fetch live Bloomberg homepage articles (best-effort)
    bb_featured: list[str] = []
    if any(a["source"] == "Bloomberg" for a in all_articles):
        print("Fetching Bloomberg homepage articles…")
        try:
            bb_featured = _bloomberg_featured_titles()
            print(f"  {len(bb_featured)} featured titles retrieved")
        except Exception as exc:
            print(f"  WARNING: could not fetch Bloomberg prominence: {exc}")

    # 7. Filter Bloomberg articles to only those on the live homepage
    if bb_featured:
        featured_keys = {_title_key(t) for t in bb_featured}
        before = len(all_articles)
        all_articles = [
            a for a in all_articles
            if a["source"] != "Bloomberg" or _title_key(a["title"]) in featured_keys
        ]
        if before != len(all_articles):
            print(f"  Filtered Bloomberg: {before} → {len(all_articles)} articles")

    # 8. Build index.html
    index_html = _build_index_html(all_articles, font_css, bb_featured=bb_featured)
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"Wrote index.html — {len(all_articles)} articles from {len(epubs)} sources")


if __name__ == "__main__":
    main()
