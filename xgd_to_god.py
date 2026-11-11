#!/usr/bin/env python3
"""Convert Xbox 360 XGD2/XGD3 (or trimmed XISO) backups to GOD/SVOD split packages.

The output layout is:

    <output>/<TITLEID>/00007000/<MEDIAID>
    <output>/<TITLEID>/00007000/<MEDIAID>.data/Data0000 ...

This is intended for backups you are authorized to use on a compatible
development, modified, or emulated Xbox 360 environment. A retail console will
not accept the unsigned LIVE header produced by community ISO-to-GOD tools.

SVOD layout and the compressed blank LIVE header are derived from the
MIT-licensed iso2god-rs project by Ilia Pozdnyakov:
https://github.com/iliazeus/iso2god-rs
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import math
import os
from pathlib import Path
import shutil
import struct
import sys
import zlib
from dataclasses import dataclass, field
from typing import BinaryIO, Iterator


SECTOR_SIZE = 0x800
BLOCK_SIZE = 0x1000
BLOCKS_PER_SUBPART = 0xCC
SUBPARTS_PER_PART = 0xCB
BLOCKS_PER_PART = BLOCKS_PER_SUBPART * SUBPARTS_PER_PART  # 0xA1C4
SUBPART_DATA_SIZE = BLOCK_SIZE * BLOCKS_PER_SUBPART
FULL_PART_SIZE = BLOCK_SIZE + SUBPARTS_PER_PART * (BLOCK_SIZE + SUBPART_DATA_SIZE)
CONTENT_TYPE_GOD = 0x00007000
XEX_EXECUTION_ID = 0x00040006
MEDIA_MAGIC = b"MICROSOFT*XBOX*MEDIA"

# zlib-compressed 0xB000-byte blank LIVE/GOD header from iso2god-rs. Keeping it
# embedded makes this a dependency-free, single-file converter.
EMPTY_LIVE_ZLIB_B64 = (
    "eNrt0i9LQ1EUAPA7YSAGwW+wYBJRk6jF/6AiS9NiGjo2QeeYFpNJxCIWs2ARBOO6"
    "wa+gX8JkMQnzTMcGWiwK4u93OZdz3rn3PB681eX1xQTf0Gz7r99/Vxp7PHnqnR5b"
    "u315Pppfab4uXaR0259SrdXu+XS8rx0hk/kybDCbHbgZv5x6GLq+Ors/njxvnG6UG"
    "qPHM4W5lJ/tHFvId69k/IIAAD+gkCppO+2nXHsvpup7Xo3qIKqdWKW0Fc/KUe1GP"
    "hJ5Ie3FXoteMR2m4c6NUqrHrVxEJfJcnKpHp/zebc3qTsnFzNYbN2MeAAC/aWL2I"
    "wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAv+kNncY55A=="
)


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class IsoFormat:
    name: str
    root_offset: int


XGD2 = IsoFormat("XGD2", 0x0FD90000)
XGD3 = IsoFormat("XGD3", 0x02080000)
# Original Xbox redump images, and trimmed/rebuilt XISO images whose game
# partition starts at the very beginning of the file (what extract-xiso -c and
# most "XISO" downloads produce). Checked after the Xbox 360 layouts.
XGD1 = IsoFormat("XGD1", 0x18300000)
XISO = IsoFormat("XISO", 0x00000000)
ISO_FORMATS = (XGD2, XGD3, XGD1, XISO)


@dataclass
class DirectoryEntry:
    name: str
    sector: int
    size: int
    attributes: int
    children: list["DirectoryEntry"] = field(default_factory=list)

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & 0x10)


@dataclass(frozen=True)
class ExecutionInfo:
    media_id: int
    version: int
    base_version: int
    title_id: int
    platform: int
    executable_type: int
    disc_number: int
    disc_count: int


@dataclass
class ImageInfo:
    iso_format: IsoFormat
    source_size: int
    root_directory_sector: int
    root_directory_size: int
    entries: list[DirectoryEntry]
    execution: ExecutionInfo | None  # None for discs without default.xex (original Xbox)

    @property
    def root_offset(self) -> int:
        return self.iso_format.root_offset

    def trimmed_volume_size(self) -> int:
        # The volume descriptor occupies sectors 0x20..0x20 inclusive.
        maximum = 0x21 * SECTOR_SIZE
        maximum = max(
            maximum,
            self.root_directory_sector * SECTOR_SIZE + self.root_directory_size,
        )

        def visit(entries: list[DirectoryEntry]) -> None:
            nonlocal maximum
            for entry in entries:
                maximum = max(maximum, entry.sector * SECTOR_SIZE + entry.size)
                visit(entry.children)

        visit(self.entries)
        return maximum


def read_exact_at(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise ConversionError(
            f"unexpected end of file at 0x{offset:X}: wanted {size} bytes, got {len(data)}"
        )
    return data


def detect_iso_format(handle: BinaryIO) -> IsoFormat:
    file_size = handle.seek(0, os.SEEK_END)
    for candidate in ISO_FORMATS:
        descriptor_offset = candidate.root_offset + 0x20 * SECTOR_SIZE
        if descriptor_offset + len(MEDIA_MAGIC) > file_size:
            continue
        handle.seek(descriptor_offset)
        if handle.read(len(MEDIA_MAGIC)) == MEDIA_MAGIC:
            return candidate
    raise ConversionError(
        "not a supported Xbox image (XGD1/XGD2/XGD3/XISO): "
        "the Xbox media descriptor was not found"
    )


def parse_directory_table(
    handle: BinaryIO,
    root_offset: int,
    sector: int,
    size: int,
    active_tables: set[tuple[int, int]],
) -> list[DirectoryEntry]:
    """Read one GDF/XDVDFS directory table by walking its binary tree.

    Every entry holds the dword offsets of its left and right subtrees, starting
    from the entry at offset 0. Walking the tree (like extract-xiso does) instead
    of scanning the sectors linearly means zero-byte files, empty folders and
    stale bytes left in the padding are all handled correctly. The in-order walk
    returns the entries sorted by name.
    """
    if size == 0:
        return []  # an empty folder has no directory table at all
    key = (sector, size)
    if key in active_tables:
        raise ConversionError("cyclic GDF directory table")
    if size < 0 or size > 0x10000000:
        raise ConversionError(f"invalid GDF directory table size: 0x{size:X}")

    table_offset = root_offset + sector * SECTOR_SIZE
    table = read_exact_at(handle, table_offset, size)

    active_tables.add(key)
    entries: list[DirectoryEntry] = []
    try:
        if len(table) < 14 or table[:2] == b"\xff\xff":
            return []  # empty table, padding only

        visited: set[int] = set()
        stack: list[tuple[int, bool]] = [(0, False)]
        while stack:
            offset, expanded = stack.pop()
            position = offset * 4
            raw = table[position : position + 14]
            if len(raw) < 14:
                raise ConversionError(
                    f"GDF directory entry outside its table at ISO offset "
                    f"0x{table_offset + position:X}"
                )
            subtree_left, subtree_right, item_sector, item_size = struct.unpack_from(
                "<HHII", raw
            )
            if not expanded:
                if offset in visited:
                    raise ConversionError("cyclic GDF directory tree")
                visited.add(offset)
                # In-order: left subtree, this entry, then the right subtree.
                if subtree_right not in (0, 0xFFFF):
                    stack.append((subtree_right, False))
                stack.append((offset, True))
                if subtree_left not in (0, 0xFFFF):
                    stack.append((subtree_left, False))
                continue

            attributes = raw[12]
            name_length = raw[13]
            name_start = position + 14
            if not name_length or name_start + name_length > len(table):
                raise ConversionError(
                    f"invalid GDF directory entry at ISO offset "
                    f"0x{table_offset + position:X}"
                )
            if position // SECTOR_SIZE != (name_start + name_length - 1) // SECTOR_SIZE:
                raise ConversionError(
                    f"GDF directory entry crosses a sector boundary at ISO offset "
                    f"0x{table_offset + position:X}"
                )
            name = table[name_start : name_start + name_length].decode(
                "ascii", errors="replace"
            )
            entry = DirectoryEntry(name, item_sector, item_size, attributes)
            if entry.is_directory:
                entry.children = parse_directory_table(
                    handle,
                    root_offset,
                    item_sector,
                    item_size,
                    active_tables,
                )
            entries.append(entry)
    finally:
        active_tables.remove(key)
    return entries


def find_entry(entries: list[DirectoryEntry], path: str) -> DirectoryEntry | None:
    current = entries
    entry: DirectoryEntry | None = None
    for component in [p for p in path.replace("/", "\\").split("\\") if p]:
        entry = next((e for e in current if e.name.casefold() == component.casefold()), None)
        if entry is None:
            return None
        current = entry.children
    return entry


def parse_xex_execution_info(
    handle: BinaryIO, xex_offset: int, xex_size: int
) -> ExecutionInfo:
    if read_exact_at(handle, xex_offset, 4) != b"XEX2":
        raise ConversionError("default.xex does not begin with XEX2")

    fixed = read_exact_at(handle, xex_offset, 24)
    field_count = struct.unpack_from(">I", fixed, 20)[0]
    if field_count > 0x1000 or 24 + field_count * 8 > xex_size:
        raise ConversionError("invalid XEX optional-header table")

    fields = read_exact_at(handle, xex_offset + 24, field_count * 8)
    execution_offset: int | None = None
    for index in range(field_count):
        key, value = struct.unpack_from(">II", fields, index * 8)
        if key == XEX_EXECUTION_ID:
            execution_offset = value
            break
    if execution_offset is None or execution_offset + 0x18 > xex_size:
        raise ConversionError("default.xex has no valid execution-ID header")

    raw = read_exact_at(handle, xex_offset + execution_offset, 0x18)
    media_id, version, base_version, title_id = struct.unpack_from(">IIII", raw, 0)
    return ExecutionInfo(
        media_id=media_id,
        version=version,
        base_version=base_version,
        title_id=title_id,
        platform=raw[16],
        executable_type=raw[17],
        disc_number=raw[18],
        disc_count=raw[19],
    )


def inspect_image(path: Path, require_xex: bool = True) -> ImageInfo:
    source_size = path.stat().st_size
    with path.open("rb") as handle:
        iso_format = detect_iso_format(handle)
        descriptor_offset = iso_format.root_offset + 0x20 * SECTOR_SIZE
        descriptor = read_exact_at(handle, descriptor_offset, 0x24)
        if descriptor[:20] != MEDIA_MAGIC:
            raise ConversionError("invalid Xbox media descriptor")
        root_sector, root_size = struct.unpack_from("<II", descriptor, 20)
        entries = parse_directory_table(
            handle, iso_format.root_offset, root_sector, root_size, set()
        )
        default_xex = find_entry(entries, "default.xex")
        execution: ExecutionInfo | None = None
        if default_xex is not None and not default_xex.is_directory:
            xex_offset = iso_format.root_offset + default_xex.sector * SECTOR_SIZE
            execution = parse_xex_execution_info(handle, xex_offset, default_xex.size)
        elif require_xex:
            raise ConversionError(
                "default.xex was not found in the GDF root "
                "(original Xbox games cannot be converted to GOD - extract them instead)"
            )

    return ImageInfo(
        iso_format=iso_format,
        source_size=source_size,
        root_directory_sector=root_sector,
        root_directory_size=root_size,
        entries=entries,
        execution=execution,
    )


def sha1(data: bytes | bytearray) -> bytes:
    return hashlib.sha1(data).digest()


def make_hash_table(hashes: list[bytes]) -> bytes:
    if len(hashes) > 204:
        raise ConversionError("too many hashes for one SVOD hash table")
    table = bytearray(BLOCK_SIZE)
    for index, digest in enumerate(hashes):
        if len(digest) != 20:
            raise ConversionError("invalid SHA-1 digest length")
        start = index * 20
        table[start : start + 20] = digest
    return bytes(table)


def write_part(
    source: BinaryIO,
    source_offset: int,
    byte_count: int,
    output_path: Path,
) -> int:
    source.seek(source_offset)
    remaining = byte_count
    master_hashes: list[bytes] = []

    with output_path.open("w+b") as output:
        output.write(b"\0" * BLOCK_SIZE)  # master hash table placeholder
        for _ in range(SUBPARTS_PER_PART):
            if remaining <= 0:
                break
            subpart_size = min(remaining, SUBPART_DATA_SIZE)
            subpart = source.read(subpart_size)
            if len(subpart) != subpart_size:
                raise ConversionError("source ISO ended while writing a GOD data part")

            block_hashes = [
                sha1(subpart[offset : offset + BLOCK_SIZE])
                for offset in range(0, len(subpart), BLOCK_SIZE)
            ]
            sub_hash_table = make_hash_table(block_hashes)
            output.write(sub_hash_table)
            output.write(subpart)
            master_hashes.append(sha1(sub_hash_table))
            remaining -= len(subpart)

        output.seek(0)
        output.write(make_hash_table(master_hashes))
        output.flush()
        return output.seek(0, os.SEEK_END)


def read_master_hash_table(path: Path) -> bytearray:
    with path.open("rb") as handle:
        data = handle.read(BLOCK_SIZE)
    if len(data) != BLOCK_SIZE:
        raise ConversionError(f"short master hash table in {path.name}")
    return bytearray(data)


def append_hash_to_table(table: bytearray, digest: bytes) -> None:
    for offset in range(0, 204 * 20, 20):
        if table[offset : offset + 20] == b"\0" * 20:
            table[offset : offset + 20] = digest
            return
    raise ConversionError("no free entry in SVOD master hash table")


def chain_part_hashes(data_dir: Path, part_count: int) -> bytes:
    current = read_master_hash_table(data_dir / f"Data{part_count - 1:04d}")
    for part_index in range(part_count - 2, -1, -1):
        previous_path = data_dir / f"Data{part_index:04d}"
        previous = read_master_hash_table(previous_path)
        append_hash_to_table(previous, sha1(current))
        with previous_path.open("r+b") as handle:
            handle.write(previous)
        current = previous
    return sha1(current)


def put_u16_be(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">H", buffer, offset, value)


def put_u24_be(buffer: bytearray, offset: int, value: int) -> None:
    buffer[offset : offset + 3] = value.to_bytes(3, "big")


def put_u32_be(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into(">I", buffer, offset, value)


def put_u32_le(buffer: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", buffer, offset, value)


def put_utf16_be(buffer: bytearray, offset: int, text: str, max_units: int) -> None:
    encoded = text.encode("utf-16-be")
    encoded = encoded[: max_units * 2]
    if len(encoded) >= 2 and 0xD800 <= int.from_bytes(encoded[-2:], "big") <= 0xDBFF:
        encoded = encoded[:-2]
    buffer[offset : offset + len(encoded) + 2] = encoded + b"\0\0"


def build_live_header(
    execution: ExecutionInfo,
    block_count: int,
    part_count: int,
    total_parts_size: int,
    root_master_hash: bytes,
    title: str,
) -> bytes:
    buffer = bytearray(zlib.decompress(base64.b64decode(EMPTY_LIVE_ZLIB_B64)))
    if len(buffer) != 0xB000:
        raise ConversionError("internal LIVE header template is corrupt")

    put_u24_be(buffer, 0x0392, block_count)
    put_u16_be(buffer, 0x0395, 0)
    put_u32_be(buffer, 0x0344, CONTENT_TYPE_GOD)
    put_u32_le(buffer, 0x03A0, part_count)  # This field is little-endian.
    put_u32_be(buffer, 0x03A4, total_parts_size // 0x100)
    put_u32_be(buffer, 0x0354, execution.media_id)
    put_u32_be(buffer, 0x0360, execution.title_id)
    buffer[0x0364:0x0368] = bytes(
        (
            execution.platform,
            execution.executable_type,
            execution.disc_number,
            execution.disc_count,
        )
    )
    buffer[0x037D:0x0391] = root_master_hash

    # Both the display-name and title-name fields receive the same safe title.
    put_utf16_be(buffer, 0x0411, title, 63)
    put_utf16_be(buffer, 0x1691, title, 63)

    # No icon was supplied.
    put_u32_be(buffer, 0x1712, 0)
    put_u32_be(buffer, 0x1716, 0)

    # Match community ISO-to-GOD headers: normalize these template bytes before
    # calculating the metadata hash.
    buffer[0x035B] = 0
    buffer[0x035F] = 0
    buffer[0x0391] = 0
    buffer[0x032C:0x0340] = sha1(buffer[0x0344 : 0x0344 + 0xACBC])
    return bytes(buffer)


def part_data_lengths(total_size: int) -> Iterator[tuple[int, int]]:
    part_capacity = BLOCKS_PER_PART * BLOCK_SIZE
    offset = 0
    while offset < total_size:
        length = min(part_capacity, total_size - offset)
        yield offset, length
        offset += length


def verify_package(
    data_dir: Path,
    header_path: Path,
    data_size: int,
    expected_root_hash: bytes,
) -> None:
    lengths = list(part_data_lengths(data_size))
    next_mht_digest: bytes | None = None

    for part_index in range(len(lengths) - 1, -1, -1):
        _, part_data_size = lengths[part_index]
        path = data_dir / f"Data{part_index:04d}"
        with path.open("rb") as handle:
            master = handle.read(BLOCK_SIZE)
            if len(master) != BLOCK_SIZE:
                raise ConversionError(f"verification failed: short MHT in {path.name}")
            remaining = part_data_size
            master_index = 0
            while remaining:
                sub_size = min(remaining, SUBPART_DATA_SIZE)
                sub_hashes = handle.read(BLOCK_SIZE)
                data = handle.read(sub_size)
                if len(sub_hashes) != BLOCK_SIZE or len(data) != sub_size:
                    raise ConversionError(f"verification failed: short {path.name}")
                expected_sub_hashes = make_hash_table(
                    [
                        sha1(data[offset : offset + BLOCK_SIZE])
                        for offset in range(0, len(data), BLOCK_SIZE)
                    ]
                )
                if sub_hashes != expected_sub_hashes:
                    raise ConversionError(
                        f"verification failed: data hash mismatch in {path.name}"
                    )
                expected_master = sha1(sub_hashes)
                actual_master = master[master_index * 20 : (master_index + 1) * 20]
                if actual_master != expected_master:
                    raise ConversionError(
                        f"verification failed: master hash mismatch in {path.name}"
                    )
                master_index += 1
                remaining -= sub_size

            if next_mht_digest is not None:
                actual_next = master[master_index * 20 : (master_index + 1) * 20]
                if actual_next != next_mht_digest:
                    raise ConversionError(
                        f"verification failed: split-file chain mismatch in {path.name}"
                    )
            next_mht_digest = sha1(master)

    if next_mht_digest != expected_root_hash:
        raise ConversionError("verification failed: root master hash mismatch")
    header = header_path.read_bytes()
    if len(header) != 0xB000 or header[:4] != b"LIVE":
        raise ConversionError("verification failed: invalid LIVE header")
    if header[0x037D:0x0391] != expected_root_hash:
        raise ConversionError("verification failed: LIVE header root hash mismatch")
    if header[0x032C:0x0340] != sha1(header[0x0344 : 0x0344 + 0xACBC]):
        raise ConversionError("verification failed: LIVE metadata hash mismatch")


def format_version(version: int) -> str:
    return f"{version >> 28}.{(version >> 24) & 0xF}.{(version >> 8) & 0xFFFF}.{version & 0xFF}"


def print_image_info(info: ImageInfo, data_size: int) -> None:
    execution = info.execution
    print(f"Format:       {info.iso_format.name}")
    print(f"Title ID:     {execution.title_id:08X}")
    print(f"Media ID:     {execution.media_id:08X}")
    print(f"Version:      {format_version(execution.version)}")
    print(f"Disc:         {execution.disc_number} of {execution.disc_count}")
    print(f"Data volume:  {data_size:,} bytes")
    print(f"GOD blocks:   {math.ceil(data_size / BLOCK_SIZE):,}")
    print(f"Data files:   {math.ceil(data_size / (BLOCKS_PER_PART * BLOCK_SIZE)):,}")


def convert(args: argparse.Namespace) -> tuple[Path, Path]:
    source = args.source.resolve()
    output = args.output.resolve()
    info = inspect_image(source)
    available_volume_size = info.source_size - info.root_offset
    data_size = available_volume_size if args.no_trim else info.trimmed_volume_size()
    if not 0 < data_size <= available_volume_size:
        raise ConversionError("calculated GDF data-volume size is invalid")

    print_image_info(info, data_size)
    if args.inspect:
        return Path(), Path()

    execution = info.execution
    assert execution is not None  # inspect_image(require_xex=True) guarantees it
    title_id = f"{execution.title_id:08X}"
    media_id = f"{execution.media_id:08X}"
    content_dir = output / title_id / f"{CONTENT_TYPE_GOD:08X}"
    data_dir = content_dir / f"{media_id}.data"
    header_path = content_dir / media_id

    if data_dir.exists() or header_path.exists():
        if not args.overwrite:
            raise ConversionError(
                f"output already exists: {header_path}\n"
                "use --overwrite to replace this exact package"
            )
        if data_dir.is_dir():
            shutil.rmtree(data_dir)
        elif data_dir.exists():
            data_dir.unlink()
        if header_path.is_dir():
            raise ConversionError(f"refusing to replace directory: {header_path}")
        if header_path.exists():
            header_path.unlink()

    data_dir.mkdir(parents=True, exist_ok=False)
    lengths = list(part_data_lengths(data_size))
    part_sizes: list[int] = []
    try:
        with source.open("rb") as source_handle:
            for part_index, (relative_offset, part_data_size) in enumerate(lengths):
                print(f"Writing Data{part_index:04d} ({part_index + 1}/{len(lengths)})")
                part_sizes.append(
                    write_part(
                        source_handle,
                        info.root_offset + relative_offset,
                        part_data_size,
                        data_dir / f"Data{part_index:04d}",
                    )
                )

        print("Chaining split-file hash tables")
        root_hash = chain_part_hashes(data_dir, len(lengths))
        block_count = math.ceil(data_size / BLOCK_SIZE)
        title = args.title or title_id
        header = build_live_header(
            execution,
            block_count,
            len(lengths),
            sum(part_sizes),
            root_hash,
            title,
        )
        content_dir.mkdir(parents=True, exist_ok=True)
        header_path.write_bytes(header)

        if not args.no_verify:
            print("Verifying data and hash chains")
            verify_package(data_dir, header_path, data_size, root_hash)
    except Exception:
        # Leave the incomplete data directory for diagnosis, but never leave a
        # header that could make it look like a complete package.
        if header_path.exists() and header_path.is_file():
            header_path.unlink()
        raise

    print(f"Header: {header_path}")
    print(f"Data:   {data_dir}")
    return header_path, data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert an Xbox 360 XGD2/XGD3 ISO backup to GOD/SVOD split format."
    )
    parser.add_argument("source", type=Path, help="source XGD2 or XGD3 .iso")
    parser.add_argument("output", type=Path, help="destination Content-style directory")
    parser.add_argument("--title", help="game title stored in the GOD header")
    parser.add_argument(
        "--no-trim",
        action="store_true",
        help="include the entire game partition instead of trimming unused tail space",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="show detected metadata without writing output",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the exact matching Title ID/Media ID output package",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the read-back SHA-1 verification pass",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.source.is_file():
            raise ConversionError(f"source ISO does not exist: {args.source}")
        convert(args)
        return 0
    except (ConversionError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
