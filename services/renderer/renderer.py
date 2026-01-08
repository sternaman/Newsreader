import base64
import hashlib
import html
import io
import math
import os
import re
from datetime import datetime
from typing import Iterable, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

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
  font-size: 1.05em;
  line-height: 1.55;
  margin: 0;
  padding: 0;
}
article { padding: 0.9em 1.2em; }
h1, h2, h3 {
  font-weight: bold;
  line-height: 1.2;
  margin: 0.9em 0 0.4em;
}
h1 { font-size: 1.6em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.15em; }
p { margin: 0 0 0.9em; }
img {
  max-width: 85%;
  width: auto;
  height: auto;
  max-height: 48vh;
  object-fit: contain;
  display: block;
  margin: 0.7em auto;
  page-break-inside: avoid;
}
div.meta {
  margin: 0.3em 0 0.9em;
  padding-bottom: 0.5em;
  border-bottom: 1px solid #ccc;
  color: #444;
  font-size: 0.85em;
}
div.meta-line { margin: 0.15em 0; }
p.meta-excerpt { margin: 0.35em 0 0; font-style: italic; color: #555; }
p.scene-break { text-align: center; letter-spacing: 0.2em; margin: 0.8em 0; }
figure {
  margin: 0.7em 0 0.9em;
  break-inside: avoid;
  page-break-inside: avoid;
}
figcaption {
  font-size: 0.8em;
  text-align: center;
  color: #555;
}
blockquote {
  border-left: 3px solid #999;
  padding-left: 0.8em;
  color: #333;
  margin: 0.6em 0 0.9em;
}
"""


_JUNK_PHRASES = [
    r"Skip to Main Content",
    r"Skip to\.\.\.",
    r"This copy is for your personal, non-commercial use only",
    r"Distribution and use of this material are governed by our Subscriber Agreement",
    r"For non-personal use or to order multiple copies",
    r"Subscriber Agreement",
    r"Dow Jones Reprints",
    r"Copyright ©\d{4} Dow Jones & Company, Inc\. All Rights Reserved\.",
    r"Copyright ©\d{4} Dow Jones & Company, Inc\.",
    r"All Rights Reserved\.",
    r"www\.djreprints\.com",
    r"1-800-843-0008",
    r"An artificial-intelligence tool created this summary",
    r"Read more about how we use artificial intelligence",
    r"Videos Most Popular News",
    r"Most Popular News",
    r"Further Reading",
    r"Show conversation",
    r"Advertisement",
    r"Coverage and analysis, selected by editors",
]
_JUNK_TEXT_LINES = [
    r"^share$",
    r"^resize$",
    r"^print$",
    r"^gift unlocked$",
    r"^gift unlocked article",
    r"^listen$",
    r"^listen to article$",
    r"^sponsored offers$",
    r"^utility bar$",
    r"^conversation$",
    r"^show conversation$",
    r"^advertisement$",
    r"^video$",
    r"^videos$",
    r"^write to\b",
    r"^what to read next$",
    r"^most popular$",
    r"^recommended videos$",
    r"^quick summary$",
    r"^view more$",
    r"^updated\b",
    r"^photography by\b",
    r"^\|+\s*photography by\b",
    r"^copyright ©\d{4} dow jones & company, inc\. all rights reserved\.",
    r"^copyright ©\d{4} dow jones & company, inc\.",
    r"^all rights reserved\.",
    r"^videos most popular news",
    r"^most popular news",
    r"^most popular$",
    r"^further reading$",
    r"^[0-9a-f]{16,}$",
]

_CSS_DUMP_PATTERNS = [
    r"/\*\s*theme vars",
    r"--colors-",
    r"--space-presets-",
    r"--typography-presets-",
    r":host\s*\{",
]

_DATA_IMAGE_RE = re.compile(r'<img[^>]+src=["\'](data:image/[^"\']+)["\']', flags=re.IGNORECASE)
_DATA_IMAGE_PREFIX = "data:image/"
_DATA_IMAGE_EXTS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

_WSJ_MARKET_TOKENS = {
    "Select",
    "DJIA",
    "S&P 500",
    "Nasdaq",
    "Russell 2000",
    "U.S. 10 Yr",
    "VIX",
    "Gold",
    "Bitcoin",
    "Crude Oil",
    "Dollar Index",
    "KBW Nasdaq Bank Index",
    "S&P GSCI Index Spot",
}
_WSJ_MENU_TOKENS = [
    "The Wall Street Journal",
    "English Edition",
    "Print Edition",
    "Latest Headlines",
    "Puzzles",
    "More",
]
_WSJ_NAV_TOKENS = [
    "World",
    "Business",
    "U.S.",
    "Politics",
    "Economy",
    "Tech",
    "Markets",
    "Opinion",
    "Free Expression",
    "Arts",
    "Lifestyle",
    "Real Estate",
    "Personal Finance",
    "Health",
    "Style",
    "Sports",
    "Autos",
]
_WSJ_RELATED_MARKERS = [
    "Videos",
    "Most Popular",
    "Most Popular News",
    "Further Reading",
    "Show conversation",
    "Advertisement",
    "Coverage and analysis, selected by editors",
    "Navigating the Markets",
]
_WSJ_CONTACT_RE = re.compile(r"^write to\b.*@wsj\.com", flags=re.IGNORECASE)
_URL_ONLY_RE = re.compile(r"^https?://\S+$", flags=re.IGNORECASE)


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


def _maybe_convert_webp(content: bytes, content_type: str) -> tuple:
    if content_type.lower() != "image/webp":
        return content, content_type
    try:
        from PIL import Image
    except ImportError:
        return content, content_type
    try:
        image = Image.open(io.BytesIO(content))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return content, content_type


def embed_images(
    html: str,
    fetch_remote: bool = False,
    base_url: Optional[str] = None,
    max_bytes: int = 5 * 1024 * 1024,
) -> str:
    def resolve_url(raw: str) -> Optional[str]:
        if not raw:
            return None
        src = raw.strip()
        if not src:
            return None
        if src.startswith("data:"):
            return src
        if src.startswith("//"):
            src = f"https:{src}"
        if base_url:
            try:
                return urljoin(base_url, src)
            except Exception:
                return src
        return src

    def normalize_wsj_image_url(src: str) -> str:
        try:
            parsed = urlparse(src)
        except Exception:
            return src
        if not parsed.netloc.endswith("wsj.net"):
            return src
        path = parsed.path
        if path.endswith("/OR"):
            path = path[: -len("/OR")]
        elif path.endswith("/OR/"):
            path = path[: -len("/OR/")]
        if path != parsed.path:
            parsed = parsed._replace(path=path)
            return urlunparse(parsed)
        return src

    def read_response_bytes(response: requests.Response) -> Optional[bytes]:
        total = 0
        chunks = []
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        if not chunks:
            return None
        return b"".join(chunks)

    def replace(match):
        src = match.group(1)
        resolved = resolve_url(src)
        if not resolved:
            return match.group(0)
        if resolved.startswith("data:"):
            return match.group(0).replace(src, resolved)
        if not fetch_remote:
            return match.group(0)
        response = None
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if base_url:
                headers["Referer"] = base_url
            headers["Accept"] = "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5"
            fetch_url = normalize_wsj_image_url(resolved)
            response = requests.get(fetch_url, timeout=10, stream=True, headers=headers)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
            if not content_type.lower().startswith("image/"):
                return match.group(0)
            size_header = response.headers.get("Content-Length")
            if size_header:
                try:
                    size = int(size_header)
                except ValueError:
                    size = None
                if size and size > max_bytes:
                    return match.group(0)
            raw = read_response_bytes(response)
            if not raw:
                return match.group(0)
            raw, content_type = _maybe_convert_webp(raw, content_type)
            data_url = _data_url_from_bytes(raw, content_type)
            return match.group(0).replace(src, data_url)
        except Exception:
            return match.group(0)
        finally:
            try:
                response.close()
            except Exception:
                pass

    return re.sub(r'<img[^>]+src=["\']([^"\']+)["\']', replace, html, flags=re.IGNORECASE)


def _extract_data_images(
    content_html: str,
    book: epub.EpubBook,
    image_cache: dict,
    chapter_idx: int,
) -> str:
    if not content_html:
        return content_html

    counter = 0

    def replace(match):
        nonlocal counter
        data_url = match.group(1)
        if not data_url or not data_url.startswith(_DATA_IMAGE_PREFIX):
            return match.group(0)
        if ";base64," not in data_url:
            return match.group(0)
        header, b64_data = data_url.split(",", 1)
        mime = header.split(";", 1)[0].replace("data:", "")
        ext = _DATA_IMAGE_EXTS.get(mime)
        if not ext:
            return match.group(0)
        try:
            raw = base64.b64decode(b64_data)
        except Exception:
            return match.group(0)
        raw, mime = _maybe_convert_webp(raw, mime)
        digest = hashlib.sha256(raw).hexdigest()
        ext = _DATA_IMAGE_EXTS.get(mime)
        if not ext:
            return match.group(0)
        filename = image_cache.get(digest)
        if not filename:
            filename = f"images/chapter_{chapter_idx}_{counter}.{ext}"
            counter += 1
            item = epub.EpubItem(uid=filename, file_name=filename, media_type=mime, content=raw)
            book.add_item(item)
            image_cache[digest] = filename
        return match.group(0).replace(data_url, filename)

    return _DATA_IMAGE_RE.sub(replace, content_html)


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


def _looks_like_css_dump(content_html: str) -> bool:
    for pattern in _CSS_DUMP_PATTERNS:
        if re.search(pattern, content_html, flags=re.IGNORECASE):
            return True
    return False


def _clean_text_content(text_content: Optional[str]) -> str:
    return _clean_text_content_with_context(text_content)


def _clean_text_content_with_context(
    text_content: Optional[str],
    source_domain: Optional[str] = None,
    byline: Optional[str] = None,
) -> str:
    if not text_content:
        return ""
    lines = []
    wsj = bool(source_domain and "wsj.com" in source_domain.lower())
    byline_norm = re.sub(r"\s+", " ", byline.strip()) if byline else ""
    skip_byline = False
    skip_summary = False
    summary_skipped = 0
    skip_related = False
    related_skipped = 0

    def looks_like_content(value: str) -> bool:
        if len(value) >= 60:
            return True
        if "." in value:
            return True
        return False

    def is_all_caps_section(value: str) -> bool:
        letters = re.sub(r"[^A-Za-z]", "", value)
        if len(letters) < 6:
            return False
        upper = sum(1 for ch in letters if ch.isupper())
        return upper / max(len(letters), 1) >= 0.85

    for raw_line in text_content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            lines.append("")
            skip_related = False
            continue
        lower = stripped.lower()

        if wsj:
            if skip_related:
                related_skipped += 1
                if related_skipped >= 25:
                    skip_related = False
                continue
            if lower.startswith(
                ("videos most popular", "most popular news", "most popular", "further reading", "show conversation")
            ):
                skip_related = True
                related_skipped = 0
                continue
            if lower.startswith("write to"):
                skip_related = True
                related_skipped = 0
                continue
            if "The Wall Street Journal" in stripped:
                continue
            if is_all_caps_section(stripped) and len(stripped) <= 40:
                continue
            if skip_summary:
                summary_skipped += 1
                if "view more" in lower:
                    skip_summary = False
                elif summary_skipped >= 8:
                    skip_summary = False
                continue
            if lower == "quick summary":
                skip_summary = True
                summary_skipped = 0
                continue
            if "artificial-intelligence tool created this summary" in lower:
                continue
            if "read more about how we use artificial intelligence" in lower:
                continue

            if skip_byline:
                if looks_like_content(stripped):
                    skip_byline = False
                else:
                    continue
            if lower == "by":
                skip_byline = True
                continue
            if byline_norm:
                if lower == byline_norm.lower() or lower == f"by {byline_norm.lower()}":
                    continue
            if lower in {",", "and"}:
                continue
            if stripped in _WSJ_MARKET_TOKENS:
                continue
            if _is_market_value(stripped):
                continue
            if re.match(r"^\d+(\.\d+)?$", stripped) and len(stripped) <= 6:
                continue
            if re.match(r"^\(?\d+\s*min\)?$", lower):
                continue

        if re.match(r"^https?://", stripped, flags=re.IGNORECASE):
            continue
        if any(re.search(pattern, stripped, flags=re.IGNORECASE) for pattern in _JUNK_TEXT_LINES):
            continue
        if any(re.search(pattern, stripped, flags=re.IGNORECASE) for pattern in _JUNK_PHRASES):
            continue
        lines.append(stripped)

    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _text_to_paragraphs(
    text_content: Optional[str],
    *,
    source_domain: Optional[str] = None,
    byline: Optional[str] = None,
) -> str:
    cleaned = _clean_text_content_with_context(text_content, source_domain, byline)
    if not cleaned:
        return ""
    chunks = [chunk.strip() for chunk in re.split(r"\n{2,}", cleaned) if chunk.strip()]
    if not chunks:
        return ""
    return "".join(f"<p>{html.escape(chunk, quote=True)}</p>" for chunk in chunks)


def _strip_tags(value: str) -> str:
    if not value:
        return ""
    stripped = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", stripped).strip())


def _looks_like_byline_name(text: str) -> bool:
    if not text:
        return False
    if len(text) > 60:
        return False
    if any(ch.isdigit() for ch in text):
        return False
    if not re.match(r"^[A-Za-z'\-\.\s,]+$", text):
        return False
    words = [word for word in re.split(r"\s+", text.replace(",", " ").strip()) if word]
    if not words or len(words) > 6:
        return False
    if not all(word[0].isupper() for word in words if word):
        return False
    return True


def derive_byline_from_text(text_content: Optional[str], source_domain: Optional[str]) -> Optional[str]:
    if not text_content:
        return None
    if not source_domain or "wsj.com" not in source_domain.lower():
        return None
    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return None
    window = lines[:12]
    for idx, line in enumerate(window):
        lower = line.lower()
        if lower.startswith("by "):
            candidate = line[3:].strip(" ,")
            if candidate:
                return candidate
        if lower == "by":
            tokens: List[str] = []
            for next_line in window[idx + 1 : idx + 8]:
                lower_next = next_line.lower()
                if lower_next in {"and", "&"}:
                    tokens.append("and")
                    continue
                if lower_next == ",":
                    tokens.append(",")
                    continue
                if _looks_like_byline_name(next_line):
                    tokens.append(next_line)
                    continue
                break
            if tokens:
                joined = " ".join(tokens)
                joined = re.sub(r"\s*,\s*", ", ", joined)
                joined = re.sub(r"\s+and\s+", " and ", joined, flags=re.IGNORECASE)
                return joined.strip(" ,")
    return None


def _is_market_value(text: str) -> bool:
    if not text:
        return False
    if re.match(r"^[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?$", text):
        return True
    if re.match(r"^\d+/\d+$", text):
        return True
    return False


def _has_wsj_menu(text: str) -> bool:
    if not text:
        return False
    hits = sum(1 for token in _WSJ_MENU_TOKENS if token in text)
    nav_hits = sum(1 for token in _WSJ_NAV_TOKENS if token in text)
    return hits >= 2 or nav_hits >= 6


def _strip_wsj_blocks(content_html: str) -> str:
    if not content_html:
        return content_html
    paragraphs = re.findall(r"<p[^>]*>.*?</p>", content_html, flags=re.IGNORECASE | re.DOTALL)
    numeric_hits = 0
    for para in paragraphs[:25]:
        text = _strip_tags(para)
        if _is_market_value(text) or re.match(r"^\d+(\.\d+)?$", text):
            numeric_hits += 1
    has_numeric_ticker = numeric_hits >= 4

    if (
        not has_numeric_ticker
        and not any(token in content_html for token in _WSJ_MARKET_TOKENS)
        and not any(token in content_html for token in _WSJ_MENU_TOKENS)
    ):
        return content_html
    parts = re.split(r"(<p[^>]*>.*?</p>)", content_html, flags=re.IGNORECASE | re.DOTALL)
    cleaned_parts = []
    in_ticker = False
    for part in parts:
        if not part.lower().startswith("<p"):
            cleaned_parts.append(part)
            continue
        text = _strip_tags(part)
        if not text:
            cleaned_parts.append(part)
            continue
        if "The Wall Street Journal" in text:
            cleaned_parts.append("")
            continue
        if text in _WSJ_MARKET_TOKENS:
            in_ticker = True
            continue
        if (in_ticker or has_numeric_ticker) and (_is_market_value(text) or text in _WSJ_MARKET_TOKENS):
            continue
        if has_numeric_ticker and re.match(r"^\d+(\.\d+)?$", text):
            continue
        if in_ticker and not (_is_market_value(text) or text in _WSJ_MARKET_TOKENS):
            in_ticker = False
        if _has_wsj_menu(text):
            continue
        cleaned_parts.append(part)
    return "".join(cleaned_parts)


def _strip_leading_byline_blocks(content_html: str) -> str:
    if not content_html:
        return content_html
    block_re = re.compile(r"<(p|div|section)[^>]*>.*?</\1>", flags=re.IGNORECASE | re.DOTALL)
    matches = list(block_re.finditer(content_html))
    if not matches:
        return content_html
    remove_idxs = set()
    found = False
    max_blocks = min(len(matches), 14)
    for idx in range(max_blocks):
        match = matches[idx]
        text = _strip_tags(match.group(0)).strip()
        if not text:
            continue
        lower = text.lower()
        if not found:
            if lower == "by" or lower.startswith("by "):
                found = True
                remove_idxs.add(idx)
            continue
        if lower in {"and", ",", "&"}:
            remove_idxs.add(idx)
            continue
        if text.replace(" ", "") == SCENE_BREAK_MARKER.replace(" ", ""):
            remove_idxs.add(idx)
            continue
        if _looks_like_byline_name(text):
            remove_idxs.add(idx)
            continue
        break
    if not remove_idxs:
        return content_html
    output = []
    last = 0
    for idx, match in enumerate(matches):
        if idx not in remove_idxs:
            continue
        output.append(content_html[last:match.start()])
        last = match.end()
    output.append(content_html[last:])
    return "".join(output)


def _strip_paragraphs_by_patterns(content_html: str) -> str:
    if not content_html:
        return content_html
    patterns = list(_JUNK_PHRASES) + list(_JUNK_TEXT_LINES)
    tags = ("p", "li")
    cleaned = content_html
    for tag in tags:
        regex = re.compile(rf"<{tag}[^>]*>.*?</{tag}>", flags=re.IGNORECASE | re.DOTALL)

        def replace(match):
            text = _strip_tags(match.group(0))
            if not text:
                return match.group(0)
            if _URL_ONLY_RE.match(text):
                return ""
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return ""
            return match.group(0)

        cleaned = regex.sub(replace, cleaned)
    return cleaned


def _strip_small_blocks_by_patterns(content_html: str, max_len: int = 180) -> str:
    if not content_html:
        return content_html
    patterns = list(_JUNK_PHRASES) + list(_JUNK_TEXT_LINES)
    tags = ("div", "section")
    cleaned = content_html
    for tag in tags:
        regex = re.compile(rf"<{tag}[^>]*>.*?</{tag}>", flags=re.IGNORECASE | re.DOTALL)

        def replace(match):
            text = _strip_tags(match.group(0))
            if not text:
                return match.group(0)
            if len(text) > max_len:
                return match.group(0)
            if _URL_ONLY_RE.match(text):
                return ""
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return ""
            return match.group(0)

        cleaned = regex.sub(replace, cleaned)
    return cleaned


def _truncate_after_heading(
    content_html: str,
    markers: Iterable[str],
    *,
    min_links: int = 4,
) -> str:
    if not content_html:
        return content_html
    marker_set = [marker.lower() for marker in markers]
    heading_re = re.compile(r"<h[1-6][^>]*>.*?</h[1-6]>", flags=re.IGNORECASE | re.DOTALL)
    for match in heading_re.finditer(content_html):
        heading_text = _strip_tags(match.group(0)).strip().lower()
        if not heading_text:
            continue
        if not any(marker in heading_text for marker in marker_set):
            continue
        tail = content_html[match.end() :]
        link_count = len(re.findall(r"<a\b", tail, flags=re.IGNORECASE))
        if link_count >= min_links:
            return content_html[: match.start()]
    return content_html


def _truncate_after_marker_block(
    content_html: str,
    markers: Iterable[str],
    *,
    min_links: int = 4,
) -> str:
    if not content_html:
        return content_html
    marker_set = [re.sub(r"\\s+", " ", marker.lower()).strip() for marker in markers]
    block_re = re.compile(r"<(p|div|section|h[1-6])[^>]*>.*?</\\1>", flags=re.IGNORECASE | re.DOTALL)
    for match in block_re.finditer(content_html):
        text = _strip_tags(match.group(0))
        if not text:
            continue
        normalized = re.sub(r"\\s+", " ", text).strip().lower()
        if len(normalized) > 40:
            continue
        cleaned = re.sub(r"[^a-z0-9 ]", "", normalized).strip()
        if not cleaned:
            continue
        if cleaned not in marker_set:
            continue
        tail = content_html[match.end() :]
        link_count = len(re.findall(r"<a\b", tail, flags=re.IGNORECASE))
        img_count = len(re.findall(r"<img\b", tail, flags=re.IGNORECASE))
        if link_count + img_count >= min_links:
            return content_html[: match.start()]
    return content_html


def _truncate_after_plain_marker(
    content_html: str,
    marker_re: str,
    *,
    required_text: Optional[str] = None,
    tag_names: Iterable[str] = ("p", "div", "section", "li", "h3", "h4"),
) -> str:
    if not content_html:
        return content_html
    match = re.search(marker_re, content_html, flags=re.IGNORECASE)
    if not match:
        return content_html
    if required_text:
        tail = content_html[match.start() : match.start() + 2000].lower()
        if required_text.lower() not in tail:
            return content_html
    start = -1
    for tag in tag_names:
        idx = content_html.rfind(f"<{tag}", 0, match.start())
        if idx > start:
            start = idx
    if start == -1:
        start = match.start()
    return content_html[:start]


def _truncate_after_contact_line(content_html: str) -> str:
    if not content_html:
        return content_html
    block_re = re.compile(r"<(p|div|section|li)[^>]*>.*?</\\1>", flags=re.IGNORECASE | re.DOTALL)
    for match in block_re.finditer(content_html):
        text = _strip_tags(match.group(0)).strip()
        if not text:
            continue
        if not re.search(r"\bwrite to\b", text, flags=re.IGNORECASE):
            continue
        if "@wsj.com" not in text.lower():
            continue
        # If the block is small, drop everything after it.
        if len(text) <= 260:
            return content_html[: match.start()]
    return content_html


def _truncate_after_contact_paragraph(content_html: str) -> str:
    if not content_html:
        return content_html
    para_re = re.compile(r"<p[^>]*>.*?</p>", flags=re.IGNORECASE | re.DOTALL)
    for match in para_re.finditer(content_html):
        text = _strip_tags(match.group(0)).strip()
        if not text:
            continue
        if not re.search(r"\bwrite to\b", text, flags=re.IGNORECASE):
            continue
        if "@wsj.com" not in text.lower():
            continue
        return content_html[: match.start()]
    return content_html


def _strip_link_heavy_blocks(
    content_html: str,
    markers: Iterable[str],
    *,
    min_links: int = 6,
) -> str:
    if not content_html:
        return content_html
    patterns = [re.compile(re.escape(marker), flags=re.IGNORECASE) for marker in markers]
    regex = re.compile(r"<(section|div|ul|ol)[^>]*>.*?</\\1>", flags=re.IGNORECASE | re.DOTALL)

    def replace(match):
        block = match.group(0)
        text = _strip_tags(block)
        if not text:
            return block
        if not any(pattern.search(text) for pattern in patterns):
            return block
        link_count = len(re.findall(r"<a\b", block, flags=re.IGNORECASE))
        if link_count >= min_links:
            return ""
        return block

    return regex.sub(replace, content_html)


def _wsj_ticker_present(text: str) -> bool:
    if not text:
        return False
    hits = [token for token in _WSJ_MARKET_TOKENS if token in text]
    if len(hits) >= 3:
        return True
    if "DJIA" in text and "S&P 500" in text:
        return True
    return False


def audit_content(content_html: str, source_domain: Optional[str] = None) -> dict:
    text = _strip_tags(content_html)
    issues: List[str] = []
    junk_hits = [pattern for pattern in _JUNK_PHRASES if re.search(pattern, text, flags=re.IGNORECASE)]
    if junk_hits:
        issues.append("junk_phrases")
    if _html_text_length(content_html) < MIN_CONTENT_TEXT_LEN:
        issues.append("short_content")
    if _looks_like_css_dump(content_html):
        issues.append("css_dump")

    wsj_ticker = False
    wsj_menu = False
    wsj_brand = False
    wsj_summary = False
    if source_domain and "wsj.com" in source_domain.lower():
        wsj_ticker = _wsj_ticker_present(text)
        wsj_menu = _has_wsj_menu(text)
        wsj_brand = "The Wall Street Journal" in text
        wsj_summary = "Quick Summary" in text
        if wsj_ticker:
            issues.append("wsj_ticker")
        if wsj_menu:
            issues.append("wsj_menu")
        if wsj_brand:
            issues.append("wsj_brand")
        if wsj_summary:
            issues.append("wsj_summary")

    return {
        "text_length": len(text),
        "issues": issues,
        "junk_hits": junk_hits,
        "wsj": {"ticker": wsj_ticker, "menu": wsj_menu, "brand": wsj_brand, "summary": wsj_summary},
        "needs_heal": bool(issues),
    }


def audit_and_heal_content(
    content_html: str,
    text_content: Optional[str],
    source_domain: Optional[str],
    byline: Optional[str] = None,
) -> tuple:
    audit_before = audit_content(content_html, source_domain)
    actions: List[str] = []
    cleaned = content_html
    if source_domain and "wsj.com" in source_domain.lower():
        stripped = _strip_wsj_blocks(cleaned)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("strip_wsj_blocks")
    stripped = _strip_paragraphs_by_patterns(cleaned)
    if stripped != cleaned:
        cleaned = stripped
        actions.append("strip_junk_paragraphs")
    stripped = _strip_small_blocks_by_patterns(cleaned)
    if stripped != cleaned:
        cleaned = stripped
        actions.append("strip_small_blocks")

    if source_domain and "wsj.com" in source_domain.lower():
        stripped = _strip_leading_byline_blocks(cleaned)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("strip_leading_byline")
        stripped = _truncate_after_contact_paragraph(cleaned)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("truncate_after_contact_paragraph")
        stripped = _truncate_after_plain_marker(cleaned, r"\bwrite to\b", required_text="@wsj.com")
        if stripped != cleaned:
            cleaned = stripped
            actions.append("truncate_after_plain_marker")
        stripped = _truncate_after_contact_line(cleaned)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("truncate_after_contact_line")
        stripped = _truncate_after_heading(cleaned, _WSJ_RELATED_MARKERS)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("truncate_after_heading")
        stripped = _truncate_after_marker_block(cleaned, _WSJ_RELATED_MARKERS)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("truncate_after_marker_block")
        stripped = _strip_link_heavy_blocks(cleaned, _WSJ_RELATED_MARKERS)
        if stripped != cleaned:
            cleaned = stripped
            actions.append("strip_wsj_related_blocks")

    audit_mid = audit_content(cleaned, source_domain)
    fallback_issues = {
        "short_content",
        "css_dump",
        "wsj_ticker",
        "wsj_menu",
        "wsj_brand",
        "wsj_summary",
    }
    should_fallback = any(issue in fallback_issues for issue in audit_mid.get("issues", []))
    if audit_mid["needs_heal"] and should_fallback and text_content:
        fallback_html = _text_to_paragraphs(
            text_content,
            source_domain=source_domain,
            byline=byline,
        )
        if fallback_html:
            cleaned = fallback_html
            actions.append("fallback_text_content")
            if source_domain and "wsj.com" in source_domain.lower():
                stripped = _strip_wsj_blocks(cleaned)
                if stripped != cleaned:
                    cleaned = stripped
                    actions.append("strip_wsj_blocks_after_fallback")
            stripped = _strip_paragraphs_by_patterns(cleaned)
            if stripped != cleaned:
                cleaned = stripped
                actions.append("strip_junk_paragraphs_after_fallback")
            stripped = _strip_small_blocks_by_patterns(cleaned)
            if stripped != cleaned:
                cleaned = stripped
                actions.append("strip_small_blocks_after_fallback")
            if source_domain and "wsj.com" in source_domain.lower():
                stripped = _strip_link_heavy_blocks(cleaned, _WSJ_RELATED_MARKERS)
                if stripped != cleaned:
                    cleaned = stripped
                    actions.append("strip_wsj_related_blocks_after_fallback")
    audit_after = audit_content(cleaned, source_domain)
    return cleaned, audit_before, audit_after, actions


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

    image_cache = {}

    for idx, chapter in enumerate(chapters, start=1):
        chapter_title = chapter["title"]
        if chapter_title.endswith(" - WSJ"):
            chapter_title = chapter_title[:-6]
        content_html = _normalize_scene_breaks(chapter["content_html"])
        source_domain = (chapter.get("source_domain") or "").lower()
        if "wsj.com" in source_domain:
            content_html = _strip_wsj_blocks(content_html)
        content_looks_bad = _html_text_length(content_html) < MIN_CONTENT_TEXT_LEN
        if not content_looks_bad:
            if "wsj.com" in source_domain and _looks_like_css_dump(content_html):
                content_looks_bad = True
        if content_looks_bad:
            fallback_html = _text_to_paragraphs(
                chapter.get("text_content"),
                source_domain=chapter.get("source_domain"),
                byline=chapter.get("byline"),
            )
            if fallback_html:
                content_html = fallback_html
        content_html = _extract_data_images(content_html, book, image_cache, idx)
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
