#!/usr/bin/env python3
"""
Generate a pre-rendered XTCH news bundle and serve it via a simple OPDS feed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\\\|?*]+', "_", name)
    name = name.strip().strip(".")
    return name or "news"


def xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def get_local_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def looks_like_path(value: str) -> bool:
    if not value:
        return False
    lower = value.lower()
    if lower.startswith(("http://", "https://", "ftp://")):
        return False
    if value.startswith((".", "/", "\\")):
        return True
    if any(sep in value for sep in ("/", "\\", os.sep)):
        return True
    if lower.endswith((".txt", ".sqlite", ".json", ".db", ".cookie", ".cookies", ".pem", ".crt", ".key")):
        return True
    return False


def resolve_recipe_options(recipe_opts: list, base_dir: Path | None) -> list[str]:
    resolved: list[str] = []
    for opt in recipe_opts:
        if opt is None:
            continue
        opt = str(opt)
        if "=" in opt:
            key, val = opt.split("=", 1)
            sep = "="
        elif ":" in opt:
            key, val = opt.split(":", 1)
            sep = ":"
        else:
            resolved.append(opt)
            continue
        if base_dir and looks_like_path(val):
            path = Path(val)
            if not path.is_absolute():
                path = (base_dir / path).resolve()
            val = str(path)
        resolved.append(f"{key}{sep}{val}")
    return resolved


def run_epub_to_xtch(recipe: Path, out_xtch: Path, title: str, date_str: str, recipe_opts: list[str]) -> None:
    script = Path(__file__).resolve().parent / "epub_to_xtch.py"
    cmd = [sys.executable, str(script), "--recipe", str(recipe), "--out", str(out_xtch), "--title", title, "--date", date_str]
    for opt in recipe_opts:
        cmd += ["--recipe-option", opt]
    subprocess.run(cmd, check=True)


def write_feed_recipe(recipe_path: Path, title: str, feed_url: str | None, max_articles: int, feeds: list | None = None) -> None:
    if feeds:
        feed_items = []
        for item in feeds:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                feed_items.append((item[0], item[1]))
            elif isinstance(item, dict):
                feed_items.append((item.get("title", title), item.get("url")))
    else:
        feed_items = [(title, feed_url)]
    content = f"""#!/usr/bin/env python
from calibre.web.feeds.news import BasicNewsRecipe


class FeedNews(BasicNewsRecipe):
    title = {title!r}
    language = "en"
    no_stylesheets = True
    use_embedded_content = True
    max_articles_per_feed = {max_articles}
    feeds = {feed_items!r}
"""
    recipe_path.write_text(content, encoding="utf-8")


def write_opds(feed_path: Path, entries: list[dict]) -> None:
    updated = dt.datetime.now(dt.timezone.utc).isoformat()
    entry_xml = []
    for entry in entries:
        title = entry.get("title", "News")
        author = entry.get("author", "")
        href = entry.get("href", "")
        title_esc = xml_escape(title)
        author_esc = xml_escape(author)
        href_esc = xml_escape(href)
        entry_xml.append(
            "  <entry>\n"
            f"    <id>urn:news:{sanitize_filename(title)}:{author}</id>\n"
            f"    <title>{title_esc}</title>\n"
            f"    <author><name>{author_esc}</name></author>\n"
            f"    <updated>{updated}</updated>\n"
            f"    <link rel=\"http://opds-spec.org/acquisition\" href=\"{href_esc}\" type=\"application/octet-stream\"/>\n"
            "  </entry>"
        )
    feed = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<feed xmlns=\"http://www.w3.org/2005/Atom\">\n"
        "  <id>urn:news:local</id>\n"
        "  <title>News</title>\n"
        f"  <updated>{updated}</updated>\n"
        + "\n".join(entry_xml)
        + "\n</feed>\n"
    )
    feed_path.write_text(feed, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate XTCH news bundle + serve OPDS feed")
    parser.add_argument("--config", help="Path to JSON config for multiple sources")
    src = parser.add_mutually_exclusive_group(required=False)
    src.add_argument("--recipe", help="Path to Calibre recipe (.recipe)")
    src.add_argument("--feed-url", help="RSS/Atom feed URL (e.g., nytrss output)")
    parser.add_argument("--out-dir", default="news_out", help="Output directory for feed + files")
    parser.add_argument("--title", default="Bloomberg", help="News title")
    parser.add_argument("--date", default="", help="Date string for footer/author (default: today)")
    parser.add_argument("--serve", action="store_true", help="Serve output folder via HTTP")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address for HTTP server")
    parser.add_argument("--port", type=int, default=8080, help="Bind port for HTTP server")
    parser.add_argument("--recipe-option", action="append", default=[], help="Recipe options as key=value")
    parser.add_argument("--max-articles", type=int, default=50, help="Max articles per feed (feed-url mode)")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue building other sources if one fails")
    args = parser.parse_args()

    date_str = args.date or dt.date.today().isoformat()
    out_dir = Path(args.out_dir)
    serve = args.serve
    host = args.host
    port = args.port

    sources: list[dict] = []
    continue_on_error = args.continue_on_error
    config_base = Path.cwd()
    config_dir = None
    if args.config:
        config_path = Path(args.config)
        config_base = config_path.parent
        config_dir = config_base
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        out_dir = Path(config.get("out_dir", out_dir))
        serve = bool(config.get("serve", serve))
        host = config.get("host", host)
        port = int(config.get("port", port))
        continue_on_error = bool(config.get("continue_on_error", continue_on_error))
        sources = config.get("sources", [])
        if not sources:
            raise SystemExit("Config has no sources")
    else:
        if not args.recipe and not args.feed_url:
            raise SystemExit("Specify --recipe or --feed-url (or use --config)")
        sources = [{
            "type": "feed" if args.feed_url else "recipe",
            "title": args.title,
            "recipe": args.recipe,
            "feed_url": args.feed_url,
            "max_articles": args.max_articles,
            "recipe_options": args.recipe_option,
            "date": args.date,
        }]

    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    errors: list[dict] = []
    for src in sources:
        src_type = src.get("type") or ("feed" if src.get("feed_url") else "recipe")
        title = src.get("title", args.title)
        src_date = src.get("date") or date_str
        recipe_opts = src.get("recipe_options") or src.get("recipe-option") or []
        recipe_opts = resolve_recipe_options(recipe_opts, config_dir or config_base)
        max_articles = int(src.get("max_articles", args.max_articles))
        filename = src.get("filename")
        if filename:
            xtch_name = filename
        else:
            xtch_name = f"{sanitize_filename(title)}-{src_date}.xtch"
        xtch_path = out_dir / xtch_name

        temp_dir = None
        try:
            try:
                if src_type == "feed":
                    feed_url = src.get("feed_url")
                    feeds = src.get("feeds")
                    if not feed_url and not feeds:
                        raise SystemExit(f"Source '{title}' missing feed_url/feeds")
                    temp_dir = Path(tempfile.mkdtemp(prefix="feed_recipe_"))
                    temp_recipe = temp_dir / "feed.recipe"
                    write_feed_recipe(temp_recipe, title, feed_url, max_articles, feeds=feeds)
                    run_epub_to_xtch(temp_recipe, xtch_path, title, src_date, recipe_opts)
                else:
                    recipe_path = src.get("recipe") or args.recipe
                    if not recipe_path:
                        raise SystemExit(f"Source '{title}' missing recipe path")
                    recipe_path = Path(recipe_path)
                    if not recipe_path.is_absolute():
                        if config_dir:
                            recipe_path = (config_dir / recipe_path).resolve()
                        else:
                            recipe_path = (config_base / recipe_path).resolve()
                    run_epub_to_xtch(recipe_path, xtch_path, title, src_date, recipe_opts)
            except Exception as exc:
                errors.append({"title": title, "error": str(exc)})
                print(f"ERROR: {title} failed: {exc}")
                if not continue_on_error:
                    raise
        finally:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

        entries.append({"title": title, "author": src_date, "href": xtch_name})
        print(f"Wrote: {xtch_path}")

    feed_path = out_dir / "news.xml"
    write_opds(feed_path, entries)
    print(f"Wrote: {feed_path}")

    if errors:
        print("Some sources failed:")
        for err in errors:
            print(f"- {err['title']}: {err['error']}")

    if not serve:
        return 0

    os.chdir(out_dir)
    server = ThreadingHTTPServer((host, port), SimpleHTTPRequestHandler)

    ip = get_local_ip() if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"Serving: http://{ip}:{port}/news.xml")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
