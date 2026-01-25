#!/usr/bin/env python3
"""
Preview XTCH/XTC files by exporting pages to PNG.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from xtch_pack import decode_page_to_image, read_page_table, _read_header, XTCH_MAGIC, XTC_MAGIC


def main() -> int:
    parser = argparse.ArgumentParser(description="Export XTCH/XTC pages to PNG")
    parser.add_argument("input", help="Input .xtch or .xtc file")
    parser.add_argument("--out-dir", default="xtch_preview", help="Output directory for PNGs")
    parser.add_argument("--page", type=int, default=0, help="1-based page number to export (0 = all)")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"Input not found: {in_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with in_path.open("rb") as f:
        header = _read_header(f)
        if header.magic not in (XTCH_MAGIC, XTC_MAGIC):
            raise SystemExit("Not an XTC/XTCH file")
        entries = read_page_table(f, header)

        if args.page > 0:
            idx = args.page - 1
            if idx < 0 or idx >= len(entries):
                raise SystemExit("Page out of range")
            img = decode_page_to_image(f, header, entries[idx])
            out_path = out_dir / f"page_{idx + 1:04d}.png"
            img.save(out_path)
            print(out_path)
        else:
            for i, entry in enumerate(entries):
                img = decode_page_to_image(f, header, entry)
                out_path = out_dir / f"page_{i + 1:04d}.png"
                img.save(out_path)
            print(out_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
