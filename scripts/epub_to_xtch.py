#!/usr/bin/env python3
"""
Generate an XTCH file from an EPUB (or Calibre recipe).

Pipeline:
  1) Recipe -> EPUB (optional, via ebook-convert)
  2) EPUB -> PDF with footer template (ebook-convert)
  3) Render PDF pages to 480x800 grayscale (pypdfium2)
  4) Pack to XTCH (xtch_pack.py)
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from xtch_pack import pack_xtch_from_images


DEFAULT_SIZE = (480, 800)
CHAPTER_MARKER_RE = re.compile(r"X4CHAP::([^\r\n]+)")


def _find_ebook_convert() -> str:
    exe = shutil.which("ebook-convert")
    if exe:
        return exe
    default = Path(r"C:\Program Files\Calibre2\ebook-convert.exe")
    if default.exists():
        return str(default)
    raise FileNotFoundError("ebook-convert.exe not found in PATH or default install location")


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _render_pdf_to_images(pdf_path: Path, target_size: tuple[int, int]) -> list[Image.Image]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    images: list[Image.Image] = []
    for i in range(len(pdf)):
        page = pdf[i]
        bitmap = page.render(scale=1, rotation=0, grayscale=True)
        img = bitmap.to_pil()
        if img.size != target_size:
            img = img.resize(target_size, Image.LANCZOS)
        images.append(img)
    return images


def _split_marker(name: str) -> tuple[str, str]:
    if name == "Contents":
        return "Contents", ""
    if " | " in name:
        section, title = name.split(" | ", 1)
        return section.strip(), title.strip()
    return "", name.strip()

def _format_section_label(section: str) -> str:
    section = section.strip()
    if not section:
        return section
    return f"— {section} —"

def _extract_chapters_from_pdf(pdf_path: Path) -> list[tuple[str, int]]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    chapters: list[tuple[str, int]] = []
    seen_sections: set[str] = set()
    seen_articles: set[str] = set()
    seen_contents = False

    for i in range(len(pdf)):
        page = pdf[i]
        textpage = page.get_textpage()
        text = textpage.get_text_range() if textpage else ""
        if not text:
            continue
        for match in CHAPTER_MARKER_RE.finditer(text):
            raw = match.group(1).strip()
            if not raw:
                continue
            if raw == "Contents":
                if not seen_contents:
                    chapters.append(("Contents", i))
                    seen_contents = True
                continue

            section, title = _split_marker(raw)
            if section and section not in seen_sections:
                chapters.append((_format_section_label(section), i))
                seen_sections.add(section)

            article = title or raw
            if article and article not in seen_articles:
                chapters.append((article, i))
                seen_articles.add(article)

    return chapters


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert EPUB/recipe to XTCH for Crosspoint X4")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--epub", help="Input EPUB file")
    src.add_argument("--recipe", help="Input Calibre recipe (.recipe)")
    parser.add_argument("--out", required=True, help="Output .xtch path")
    parser.add_argument("--title", default="", help="Title for footer and metadata")
    parser.add_argument("--date", default="", help="Date string for footer (default: today)")
    parser.add_argument("--footer-font-size", type=int, default=8, help="Footer font size in pt")
    parser.add_argument("--margin", type=int, default=0, help="PDF page margin (pt) for top/left/right")
    parser.add_argument("--margin-bottom", type=int, default=8, help="PDF bottom margin (pt)")
    parser.add_argument("--no-cover", action="store_true", help="Disable cover page in PDF output")
    parser.add_argument("--keep-temp", action="store_true", help="Keep intermediate files")
    parser.add_argument("--dump-pages", action="store_true", help="Write rendered PNGs to temp folder")
    parser.add_argument("--recipe-option", action="append", default=[], help="Recipe options as key=value")
    args = parser.parse_args()

    today = dt.date.today().isoformat()
    title = args.title or "News"
    date_str = args.date or today

    footer_html = (
        f'<div style="font-size:{args.footer_font_size}pt; text-align:center;">'
        f'_TITLE_ | _AUTHOR_ | _PAGENUM_'
        f"</div>"
    )

    ebook_convert = _find_ebook_convert()

    temp_dir = Path(tempfile.mkdtemp(prefix="xtch_"))
    epub_path = temp_dir / "input.epub"
    pdf_path = temp_dir / "output.pdf"
    pages_dir = temp_dir / "pages"

    if args.recipe:
        recipe_path = Path(args.recipe)
        if not recipe_path.exists():
            raise SystemExit(f"Recipe not found: {recipe_path}")
        cmd = [ebook_convert, str(recipe_path), str(epub_path)]
        if title:
            cmd += ["--title", title]
        if date_str:
            cmd += ["--authors", date_str]
        for opt in args.recipe_option:
            if "=" in opt:
                key, val = opt.split("=", 1)
            elif ":" in opt:
                key, val = opt.split(":", 1)
            else:
                raise SystemExit(f"Invalid recipe option (expected key=value): {opt}")
            cmd += ["--recipe-specific-option", f"{key}:{val}"]
        _run(cmd)
    else:
        epub_path = Path(args.epub)
        if not epub_path.exists():
            raise SystemExit(f"EPUB not found: {epub_path}")

    cmd = [
        ebook_convert,
        str(epub_path),
        str(pdf_path),
        "--custom-size=480x800",
        "--unit=point",
        f"--pdf-page-margin-top={args.margin}",
        f"--pdf-page-margin-left={args.margin}",
        f"--pdf-page-margin-right={args.margin}",
        f"--pdf-page-margin-bottom={args.margin_bottom}",
        f"--pdf-footer-template={footer_html}",
    ]
    if title:
        cmd += ["--title", title]
    if date_str:
        cmd += ["--authors", date_str]
    if args.no_cover:
        cmd += ["--pdf-no-cover"]

    _run(cmd)

    chapters = _extract_chapters_from_pdf(pdf_path)
    images = _render_pdf_to_images(pdf_path, DEFAULT_SIZE)
    if args.dump_pages:
        pages_dir.mkdir(parents=True, exist_ok=True)
        for i, img in enumerate(images):
            img.save(pages_dir / f"page_{i + 1:04d}.png")

    out_path = Path(args.out)
    pack_xtch_from_images(images, out_path, title=title, dither=True, chapters=chapters)

    if not args.keep_temp:
        shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        print(f"Kept temp files at: {temp_dir}")

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
