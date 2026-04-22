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
import html as html_mod
import re
import shutil
import sys
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path

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
_SOURCE_PRIORITY = ["Bloomberg", "Businessweek", "WSJ", "NYTimes", "NPR Text"]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _time_ago(date_str: str) -> str:
    """Date-granular relative time: Today / Yesterday / N days ago."""
    try:
        d = date.fromisoformat(date_str)
        delta = (date.today() - d).days
        if delta == 0:
            return "Today"
        if delta == 1:
            return "Yesterday"
        return f"{delta} days ago"
    except ValueError:
        return date_str


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
    r"(?:Updated|Published) on ([A-Za-z]+ \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)",
    re.I,
)


def _parse_article_ts(html_text: str, fallback: datetime) -> datetime:
    """Extract publication datetime from the Calibre auth line, or return fallback."""
    m = _AUTH_TS_RE.search(html_text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b %d, %Y at %I:%M %p")
        except ValueError:
            pass
    return fallback


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
                    article_ts[rel] = _parse_article_ts(text, epub_dt)

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
/* CrossPoint News — article reader stylesheet */
{font_import}

:root {{ --red: #ed1c24; }}

*, *::before, *::after {{ box-sizing: border-box; }}

body {{
  font-family: 'Source Serif 4', {fallback};
  font-size: 18px;
  line-height: 1.7;
  color: #111;
  margin: 0;
  padding: 1rem;
  background: #fff;
}}

.back-nav {{
  max-width: 680px;
  margin: 0 auto 1.5rem;
  padding-bottom: 0.75rem;
  border-bottom: 1px solid #ddd;
}}

.back-nav a {{
  text-decoration: none;
  color: #555;
  font-size: 0.85rem;
  font-family: Arial, sans-serif;
}}

.back-nav a:hover {{ color: var(--red); }}

.article, .article1, article {{
  max-width: 680px;
  margin: 0 auto;
}}

h1 {{
  font-family: 'Playfair Display', {fallback};
  font-size: 2rem;
  font-weight: 700;
  line-height: 1.2;
  margin-bottom: 0.5rem;
}}

h2, h3, h4 {{
  font-family: 'Playfair Display', {fallback};
  margin: 1.5rem 0 0.5rem;
}}

p {{ margin-bottom: 1.25em; }}

.auth, p.auth {{
  color: #555;
  font-size: 0.85rem;
  margin-bottom: 1.5rem;
  font-family: Arial, sans-serif;
}}

.subhead, .standfirst {{
  font-style: italic;
  color: #333;
  margin-bottom: 1.5rem;
}}

img {{
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1.5rem auto;
}}

blockquote {{
  border-left: 3px solid var(--red);
  margin-left: 0;
  padding-left: 1.25rem;
  color: #444;
  font-style: italic;
}}

.img-cap, .figc {{
  font-size: 0.8rem;
  text-align: center;
  color: #777;
  margin-top: -1rem;
  margin-bottom: 1.5rem;
}}

/* Hide Calibre internal markers */
.x4-chap-marker {{ display: none !important; }}
.calibre_navbar {{ display: none !important; }}
.toc-page {{ display: none !important; }}
"""


def _write_reader_css(out_dir: Path, fonts_available: bool) -> None:
    font_import = "@import url('fonts/_faces.css');" if fonts_available else ""
    css = _READER_CSS_TEMPLATE.format(font_import=font_import, fallback=_FALLBACK_SERIF)
    (out_dir / "reader.css").write_text(css, encoding="utf-8")


def _build_index_html(
    all_articles: list[dict],
    font_css: str,
) -> str:
    """Produce the Bloomberg-style index.html as a string."""

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

    # ── Hero: most-recent article with thumb from top source; rail = next 4 ──
    hero_html = ""
    if all_articles and sources_ordered:
        hero_src = sources_ordered[0]
        src_arts = sorted(
            [a for a in all_articles if a["source"] == hero_src],
            key=lambda a: a["ts"], reverse=True,
        )
        h = next((a for a in src_arts if a.get("thumb")), src_arts[0] if src_arts else None)
        if h:
            rail = [a for a in src_arts if a["href"] != h["href"]][:4]

            hero_img = (
                f'<a href="{_he(h["href"])}" class="hero-img-link">'
                f'<img class="hero-img" src="{_he(h["thumb"])}" alt="" loading="eager">'
                f'</a>\n'
            ) if h.get("thumb") else ""

            rail_items = ""
            for ra in rail:
                ri_img = (
                    f'<a href="{_he(ra["href"])}" class="rail-img-link">'
                    f'<img class="rail-thumb" src="{_he(ra["thumb"])}" alt="" loading="lazy">'
                    f'</a>\n'
                ) if ra.get("thumb") else ""
                rail_items += (
                    f'<div class="rail-item">\n'
                    + ri_img
                    + f'<a class="rail-title" href="{_he(ra["href"])}">{_he(ra["title"])}</a>\n'
                    + f'<span class="rail-date">{_he(_time_ago(ra["date"]))}</span>\n'
                    + '</div>\n'
                )

            hero_html = (
                f'<section class="hero-section" data-source="{_he(_slug(hero_src))}">\n'
                '<div class="hero-grid">\n'
                '<div class="hero-main">\n'
                + hero_img
                + f'<div class="hero-eyebrow">{_he(hero_src)}</div>\n'
                + f'<h2><a href="{_he(h["href"])}">{_he(h["title"])}</a></h2>\n'
                + (f'<p class="hero-desc">{_he(h["desc"])}</p>\n' if h["desc"] else "")
                + f'<div class="hero-date">{_he(_time_ago(h["date"]))}</div>\n'
                + '</div>\n'
                + (f'<div class="hero-rail">\n{rail_items}</div>\n' if rail_items else "")
                + '</div>\n</section>\n'
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

        cat_blocks: list[str] = []
        for sec in section_order.get(src, []):
            arts = sorted(src_secs[sec], key=lambda a: a["ts"], reverse=True)
            items: list[str] = []
            for art in arts:
                thumb_html = (
                    f'<a href="{_he(art["href"])}" class="article-thumb-link">'
                    f'<img class="article-thumb" src="{_he(art["thumb"])}" alt="" loading="lazy">'
                    f'</a>\n'
                ) if art.get("thumb") else ""
                items.append(
                    '<li class="article-item">\n'
                    + thumb_html
                    + '<div class="article-body">\n'
                    + f'<a class="article-title" href="{_he(art["href"])}">{_he(art["title"])}</a>\n'
                    + f'<span class="article-date">{_he(_time_ago(art["date"]))}</span>\n'
                    + (f'<p class="article-desc">{_he(art["desc"][:140])}</p>\n' if art["desc"] else "")
                    + '</div>\n</li>'
                )
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
            f'<span class="source-date">{_he(date_display)}</span>'
            '</div>\n'
            + "\n".join(cat_blocks) + "\n"
            + "</section>"
        )

    sections_html = "\n<hr class=\"section-divider\">\n".join(sections_parts)
    today_d = date.today()
    today_display = f"{today_d.strftime('%B')} {today_d.day}, {today_d.year}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CrossPoint News</title>
<style>
{font_css}

:root {{
  --red: #ed1c24;
  --black: #000;
  --white: #fff;
  --gray: #767676;
  --serif: {serif};
  --body: {body_font};
  --max: 1100px;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: var(--body); background: var(--white); color: var(--black); font-size: 15px; line-height: 1.5; }}

/* Header */
header {{ background: var(--black); position: sticky; top: 0; z-index: 100; }}
.header-inner {{
  max-width: var(--max); margin: 0 auto; padding: 0.55rem 1.25rem;
  display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; flex-wrap: wrap;
}}
.site-name {{
  font-family: var(--serif); font-size: 1.25rem; font-weight: 900;
  color: var(--white); letter-spacing: -0.01em; white-space: nowrap; text-transform: uppercase;
}}
nav {{ display: flex; gap: 0.2rem; flex-wrap: wrap; }}
.filter {{
  background: none; border: 1px solid rgba(255,255,255,0.25); border-radius: 2px;
  padding: 0.18rem 0.5rem; cursor: pointer; font-size: 0.7rem;
  font-family: Arial, sans-serif; color: rgba(255,255,255,0.75);
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}}
.filter:hover, .filter.active {{ background: var(--white); color: var(--black); border-color: var(--white); }}

.container {{ max-width: var(--max); margin: 0 auto; padding: 1.5rem 1.25rem 3rem; }}
hr.divider {{ border: none; border-top: 3px solid var(--black); margin: 0 0 1.75rem; }}
hr.section-divider {{ border: none; border-top: 1px solid #ddd; margin: 0; }}

/* Hero */
.hero-section {{ padding: 1.5rem 0 2rem; }}
.hero-grid {{ display: grid; grid-template-columns: 5fr 2fr; gap: 2rem; align-items: start; }}
.hero-img-link {{ display: block; overflow: hidden; margin-bottom: 0.7rem; }}
.hero-img {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }}
.hero-img:hover {{ opacity: 0.9; }}
.hero-eyebrow {{
  text-transform: uppercase; font-size: 0.63rem; letter-spacing: 0.14em;
  color: var(--red); font-weight: 700; margin-bottom: 0.4rem; font-family: Arial, sans-serif;
}}
.hero-main h2 {{
  font-family: var(--serif); font-size: clamp(1.5rem, 3vw, 2.3rem);
  font-weight: 700; line-height: 1.15; margin-bottom: 0.5rem;
}}
.hero-main h2 a {{ color: inherit; text-decoration: none; }}
.hero-main h2 a:hover {{ color: var(--red); }}
.hero-desc {{ font-size: 0.95rem; line-height: 1.5; color: #444; margin-bottom: 0.4rem; }}
.hero-date {{ font-size: 0.67rem; color: var(--gray); font-family: Arial, sans-serif; }}
.hero-rail {{ border-left: 1px solid #e0e0e0; padding-left: 1.5rem; display: flex; flex-direction: column; }}
.rail-item {{ padding: 0.7rem 0; border-bottom: 1px solid #ebebeb; }}
.rail-item:first-child {{ padding-top: 0; }}
.rail-item:last-child {{ border-bottom: none; }}
.rail-img-link {{ display: block; overflow: hidden; margin-bottom: 0.3rem; }}
.rail-thumb {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }}
.rail-thumb:hover {{ opacity: 0.9; }}
.rail-title {{
  font-family: var(--serif); font-size: 0.87rem; font-weight: 700;
  line-height: 1.3; color: var(--black); text-decoration: none; display: block; margin-bottom: 0.15rem;
}}
.rail-title:hover {{ color: var(--red); }}
.rail-date {{ font-size: 0.63rem; color: var(--gray); font-family: Arial, sans-serif; }}

/* Source sections */
.source-section {{ padding: 1.5rem 0; }}
.source-label {{
  display: flex; align-items: baseline; gap: 0.75rem;
  border-bottom: 3px solid var(--black); padding-bottom: 0.5rem; margin-bottom: 1.25rem;
}}
.source-name {{
  font-family: Arial, Helvetica, sans-serif; font-size: 1rem; font-weight: 900;
  color: var(--black); text-transform: uppercase; letter-spacing: 0.03em;
}}
.source-date {{ font-size: 0.67rem; color: var(--gray); font-family: Arial, sans-serif; }}

/* Category blocks */
.category-block {{ margin-top: 1.5rem; }}
.category-block:first-of-type {{ margin-top: 0; }}
.category-label {{ display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.75rem; }}
.category-label span {{
  font-family: Arial, sans-serif; font-size: 0.68rem; font-weight: 700;
  color: var(--red); text-transform: uppercase; letter-spacing: 0.1em; white-space: nowrap;
}}
.category-label::after {{ content: ''; flex: 1; height: 1px; background: #ddd; }}

/* Article grid */
.article-list {{ list-style: none; display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 2rem; }}
.article-item {{ padding: 0.85rem 0; border-bottom: 1px solid #ebebeb; display: flex; flex-direction: column; }}
.article-item:last-child {{ border-bottom: none; }}
.article-thumb-link {{ display: block; overflow: hidden; margin-bottom: 0.4rem; }}
.article-thumb {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }}
.article-thumb:hover {{ opacity: 0.9; }}
.article-body {{ display: flex; flex-direction: column; gap: 0.2rem; flex: 1; }}
.article-title {{
  font-family: var(--serif); font-size: 0.9rem; font-weight: 700;
  color: var(--black); text-decoration: none; line-height: 1.3;
}}
.article-title:hover {{ color: var(--red); }}
.article-date {{ font-size: 0.63rem; color: var(--gray); font-family: Arial, sans-serif; }}
.article-desc {{ font-size: 0.78rem; color: #555; line-height: 1.4; }}

footer {{
  border-top: 3px solid var(--black); padding: 0.85rem 1.25rem;
  text-align: center; font-size: 0.67rem; color: var(--gray);
  font-family: Arial, sans-serif; max-width: var(--max); margin: 2rem auto 0;
}}

@media (max-width: 900px) {{
  .hero-grid {{ grid-template-columns: 1fr; }}
  .hero-rail {{
    border-left: none; padding-left: 0;
    border-top: 1px solid #e0e0e0; padding-top: 1rem; margin-top: 1rem;
    flex-direction: row; flex-wrap: wrap; gap: 0 1.5rem;
  }}
  .rail-item {{ flex: 1 1 45%; }}
  .article-list {{ grid-template-columns: repeat(2, 1fr); }}
}}
@media (max-width: 560px) {{
  .hero-rail {{ flex-direction: column; }}
  .rail-item {{ flex: none; }}
  .article-list {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div class="site-name">CrossPoint News</div>
    <nav id="source-filters">
      {nav_html}
    </nav>
  </div>
</header>
<main>
  <div class="container">
    {hero_html}
    {sections_html}
  </div>
</main>
<footer>CrossPoint News &mdash; {today_display}</footer>
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
<title>CrossPoint News</title>
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
<header><div class="site-name">CrossPoint News</div></header>
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
    args = parser.parse_args()
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

    # 6. Build index.html
    index_html = _build_index_html(all_articles, font_css)
    (out_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"Wrote index.html — {len(all_articles)} articles from {len(epubs)} sources")


if __name__ == "__main__":
    main()
