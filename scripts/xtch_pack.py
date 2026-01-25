#!/usr/bin/env python3
"""
XTCH packer + preview utilities for Crosspoint Reader.

XTCH (2-bit) pages are column-major, right-to-left, with two bit planes.
See lib/Xtc/Xtc/XtcTypes.h for format details.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple, Union, Optional

from PIL import Image

XTC_MAGIC = 0x00435458
XTCH_MAGIC = 0x48435458
XTG_MAGIC = 0x00475458
XTH_MAGIC = 0x00485458


@dataclass
class XtcHeader:
    magic: int
    version_major: int
    version_minor: int
    page_count: int
    flags: int
    header_size: int
    reserved1: int
    toc_offset: int
    page_table_offset: int
    data_offset: int
    reserved2: int
    title_offset: int
    padding: int


@dataclass
class PageEntry:
    data_offset: int
    data_size: int
    width: int
    height: int


def _align(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment

def _encode_chapter_name(name: str, max_bytes: int = 80) -> bytes:
    if not name:
        return b""
    data = name.encode("utf-8")
    if len(data) <= max_bytes:
        return data
    # Trim without splitting multibyte UTF-8 sequences.
    trimmed = data[:max_bytes]
    while trimmed and (trimmed[-1] & 0xC0) == 0x80:
        trimmed = trimmed[:-1]
    return trimmed

def _normalize_chapters(
    chapters: Sequence[Union[Tuple[str, int], Tuple[str, int, int]]],
    page_count: int,
) -> List[Tuple[str, int, int]]:
    if not chapters:
        return []
    normalized: List[Tuple[str, int, int]] = []
    for item in chapters:
        if len(item) == 2:
            name, start = item
            end = -1
        else:
            name, start, end = item
        start = max(0, min(int(start), page_count - 1))
        end = int(end) if end is not None else -1
        normalized.append((str(name), start, end))
    normalized.sort(key=lambda x: x[1])
    # Fill end pages if missing
    for idx, (name, start, end) in enumerate(normalized):
        if end < 0:
            next_start = normalized[idx + 1][1] if idx + 1 < len(normalized) else page_count
            end = max(start, next_start - 1)
            normalized[idx] = (name, start, end)
    return normalized

def _write_chapters(
    f,
    chapters: Sequence[Tuple[str, int, int]],
) -> int:
    if not chapters:
        return 0
    chapter_offset = _align(f.tell(), 8)
    if chapter_offset != f.tell():
        f.write(b"\x00" * (chapter_offset - f.tell()))
    for name, start, end in chapters:
        name_bytes = _encode_chapter_name(name, 80)
        name_buf = name_bytes + b"\x00" * (80 - len(name_bytes))
        buf = bytearray(96)
        buf[0:80] = name_buf
        buf[0x50:0x52] = struct.pack("<H", int(start))
        buf[0x52:0x54] = struct.pack("<H", int(end))
        f.write(buf)
    return chapter_offset

def _make_palette() -> Image.Image:
    # Palette order matches XTCH pixel values:
    # 0=white, 1=dark gray, 2=light gray, 3=black
    palette = [
        255, 255, 255,  # white
        85, 85, 85,     # dark gray
        170, 170, 170,  # light gray
        0, 0, 0,        # black
    ] + [0] * (256 * 3 - 12)
    pal = Image.new("P", (1, 1))
    pal.putpalette(palette)
    return pal


def _quantize_2bit(img: Image.Image, dither: bool) -> List[int]:
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    pal = _make_palette()
    img_q = img.convert("L").quantize(palette=pal, dither=dither_mode)
    return list(img_q.getdata())


def _encode_xtch_planes(values: Sequence[int], width: int, height: int) -> Tuple[bytes, bytes]:
    plane_size = ((width * height + 7) // 8)
    plane1 = bytearray(plane_size)
    plane2 = bytearray(plane_size)
    col_bytes = (height + 7) // 8

    for y in range(height):
        row_offset = y * width
        byte_in_col = y // 8
        bit_mask = 1 << (7 - (y % 8))
        for x in range(width):
            pv = values[row_offset + x] & 0x3
            if pv == 0:
                continue
            col_index = width - 1 - x
            byte_offset = col_index * col_bytes + byte_in_col
            if pv & 0b10:
                plane1[byte_offset] |= bit_mask
            if pv & 0b01:
                plane2[byte_offset] |= bit_mask
    return bytes(plane1), bytes(plane2)


def _encode_xtc_bitmap(values: Sequence[int], width: int, height: int) -> bytes:
    row_bytes = (width + 7) // 8
    data = bytearray(row_bytes * height)
    for y in range(height):
        row_start = y * row_bytes
        for x in range(width):
            # 0=white, 1=black
            bit = 1 if values[y * width + x] else 0
            byte_index = row_start + (x // 8)
            bit_index = 7 - (x % 8)
            if bit == 0:
                continue
            data[byte_index] |= 1 << bit_index
    return bytes(data)


def _write_header(f, header: XtcHeader) -> None:
    data = struct.pack(
        "<I B B H I I I I Q Q Q I I",
        header.magic,
        header.version_major,
        header.version_minor,
        header.page_count,
        header.flags,
        header.header_size,
        header.reserved1,
        header.toc_offset,
        header.page_table_offset,
        header.data_offset,
        header.reserved2,
        header.title_offset,
        header.padding,
    )
    f.seek(0)
    f.write(data)


def _read_header(f) -> XtcHeader:
    data = f.read(56)
    if len(data) != 56:
        raise ValueError("File too small for XTC header")
    unpacked = struct.unpack("<I B B H I I I I Q Q Q I I", data)
    return XtcHeader(
        magic=unpacked[0],
        version_major=unpacked[1],
        version_minor=unpacked[2],
        page_count=unpacked[3],
        flags=unpacked[4],
        header_size=unpacked[5],
        reserved1=unpacked[6],
        toc_offset=unpacked[7],
        page_table_offset=unpacked[8],
        data_offset=unpacked[9],
        reserved2=unpacked[10],
        title_offset=unpacked[11],
        padding=unpacked[12],
    )


def read_page_table(f, header: XtcHeader) -> List[PageEntry]:
    f.seek(header.page_table_offset)
    entries: List[PageEntry] = []
    for _ in range(header.page_count):
        data = f.read(16)
        if len(data) != 16:
            raise ValueError("Truncated page table")
        data_offset, data_size, width, height = struct.unpack("<Q I H H", data)
        entries.append(PageEntry(data_offset, data_size, width, height))
    return entries


def pack_xtch_from_images(
    images: Sequence[Union[str, Path, Image.Image]],
    out_path: Union[str, Path],
    title: str,
    dither: bool = True,
    chapters: Optional[Sequence[Union[Tuple[str, int], Tuple[str, int, int]]]] = None,
) -> None:
    if not images:
        raise ValueError("No images provided")

    pil_images: List[Image.Image] = []
    for img in images:
        if isinstance(img, Image.Image):
            pil_images.append(img)
        else:
            pil_images.append(Image.open(img))

    width, height = pil_images[0].size
    for i, img in enumerate(pil_images):
        if img.size != (width, height):
            pil_images[i] = img.resize((width, height), Image.LANCZOS)

    page_count = len(pil_images)

    title_offset = 0x38
    title_block_size = 128
    page_table_offset = _align(title_offset + title_block_size, 8)
    data_offset = page_table_offset + page_count * 16

    header = XtcHeader(
        magic=XTCH_MAGIC,
        version_major=1,
        version_minor=0,
        page_count=page_count,
        flags=0,
        header_size=page_table_offset,
        reserved1=0,
        toc_offset=0,
        page_table_offset=page_table_offset,
        data_offset=data_offset,
        reserved2=0,
        title_offset=title_offset,
        padding=0,
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    title_bytes = title.encode("utf-8")[:127]
    title_buf = title_bytes + b"\x00" * (title_block_size - len(title_bytes))

    entries: List[PageEntry] = []
    chapter_entries = _normalize_chapters(chapters or [], page_count)

    with out_path.open("wb") as f:
        _write_header(f, header)
        f.seek(title_offset)
        f.write(title_buf)

        f.seek(page_table_offset)
        f.write(b"\x00" * (page_count * 16))

        f.seek(data_offset)
        for img in pil_images:
            values = _quantize_2bit(img, dither=dither)
            plane1, plane2 = _encode_xtch_planes(values, width, height)
            bitmap = plane1 + plane2

            page_offset = f.tell()
            page_header = struct.pack(
                "<I H H B B I Q",
                XTH_MAGIC,
                width,
                height,
                0,  # colorMode
                0,  # compression
                len(bitmap),
                0,  # md5 (unused)
            )
            f.write(page_header)
            f.write(bitmap)
            entries.append(PageEntry(page_offset, len(page_header) + len(bitmap), width, height))

        data_end = f.tell()

        f.seek(page_table_offset)
        for entry in entries:
            f.write(struct.pack("<Q I H H", entry.data_offset, entry.data_size, entry.width, entry.height))

        if chapter_entries:
            f.seek(data_end)
            chapter_offset = _write_chapters(f, chapter_entries)
            header.flags = 0x01000000  # hasChaptersFlag is the high byte at 0x0B
            header.toc_offset = int(chapter_offset)
            _write_header(f, header)


def decode_page_to_image(
    f,
    header: XtcHeader,
    entry: PageEntry,
) -> Image.Image:
    bit_depth = 2 if header.magic == XTCH_MAGIC else 1

    f.seek(entry.data_offset)
    page_header = f.read(22)
    if len(page_header) != 22:
        raise ValueError("Truncated page header")
    magic, width, height, _color_mode, _compression, data_size, _md5 = struct.unpack("<I H H B B I Q", page_header)

    if bit_depth == 2 and magic != XTH_MAGIC:
        raise ValueError("Unexpected page magic for XTCH")
    if bit_depth == 1 and magic != XTG_MAGIC:
        raise ValueError("Unexpected page magic for XTC")

    bitmap = f.read(data_size)
    if len(bitmap) != data_size:
        raise ValueError("Truncated bitmap data")

    if bit_depth == 1:
        row_bytes = (width + 7) // 8
        img = Image.new("L", (width, height), 255)
        px = img.load()
        for y in range(height):
            row_start = y * row_bytes
            for x in range(width):
                byte = bitmap[row_start + (x // 8)]
                bit = 7 - (x % 8)
                is_white = (byte >> bit) & 1
                px[x, y] = 255 if is_white else 0
        return img

    plane_size = ((width * height + 7) // 8)
    plane1 = bitmap[:plane_size]
    plane2 = bitmap[plane_size:]
    col_bytes = (height + 7) // 8
    img = Image.new("L", (width, height), 255)
    px = img.load()

    for y in range(height):
        byte_in_col = y // 8
        bit_mask = 1 << (7 - (y % 8))
        for x in range(width):
            col_index = width - 1 - x
            byte_offset = col_index * col_bytes + byte_in_col
            bit1 = 1 if (plane1[byte_offset] & bit_mask) else 0
            bit2 = 1 if (plane2[byte_offset] & bit_mask) else 0
            pv = (bit1 << 1) | bit2
            if pv == 0:
                val = 255
            elif pv == 1:
                val = 85
            elif pv == 2:
                val = 170
            else:
                val = 0
            px[x, y] = val

    return img
