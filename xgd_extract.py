#!/usr/bin/env python3
"""Extract files from an Xbox XISO or XGD1/XGD2/XGD3 backup."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys

import xgd_to_god as core


COPY_BUFFER_SIZE = 4 * 1024 * 1024
WINDOWS_INVALID_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_component(name: str) -> None:
    if not name or name in {".", ".."}:
        raise core.ConversionError(f"unsafe empty or relative GDF name: {name!r}")
    if "\0" in name or "/" in name or "\\" in name:
        raise core.ConversionError(f"unsafe GDF path component: {name!r}")
    if os.name == "nt":
        if any(character in WINDOWS_INVALID_CHARS for character in name):
            raise core.ConversionError(
                f"GDF name cannot be represented on Windows: {name!r}"
            )
        if name.endswith((" ", ".")):
            raise core.ConversionError(
                f"GDF name has an unsupported trailing space/dot: {name!r}"
            )
        stem = name.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise core.ConversionError(
                f"GDF name is reserved by Windows: {name!r}"
            )


def iter_entries(
    entries: list[core.DirectoryEntry],
    relative_parent: Path = Path(),
):
    for entry in entries:
        validate_component(entry.name)
        relative_path = relative_parent / entry.name
        yield entry, relative_path
        if entry.is_directory:
            yield from iter_entries(entry.children, relative_path)


def ensure_within(root: Path, candidate: Path) -> None:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise core.ConversionError(
            f"refusing to write outside the extraction directory: {candidate}"
        ) from error


def validate_tree(entries: list[core.DirectoryEntry], source_volume_size: int) -> None:
    seen: set[str] = set()
    for entry, relative_path in iter_entries(entries):
        key = str(relative_path).casefold() if os.name == "nt" else str(relative_path)
        if key in seen:
            raise core.ConversionError(
                f"duplicate output path in GDF directory: {relative_path}"
            )
        seen.add(key)
        extent_end = entry.sector * core.SECTOR_SIZE + entry.size
        if extent_end > source_volume_size:
            raise core.ConversionError(
                f"GDF extent exceeds the game partition: {relative_path}"
            )


def prepare_destination(destination: Path, overwrite: bool) -> None:
    if destination.exists() and not destination.is_dir():
        raise core.ConversionError(f"destination is not a directory: {destination}")
    if not destination.exists():
        destination.mkdir(parents=True)
        return
    if not overwrite and any(destination.iterdir()):
        raise core.ConversionError(
            f"destination is not empty: {destination}\n"
            "use --overwrite to replace colliding files"
        )


def copy_extent(
    source,
    source_offset: int,
    size: int,
    destination: Path,
    overwrite: bool,
) -> None:
    if destination.exists():
        if destination.is_dir():
            raise core.ConversionError(
                f"cannot replace directory with a file: {destination}"
            )
        if not overwrite:
            raise core.ConversionError(f"output file already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".xgd-partial")
    ensure_within(destination.parent, temporary)
    if temporary.exists():
        if not overwrite:
            raise core.ConversionError(f"partial output already exists: {temporary}")
        if temporary.is_dir():
            raise core.ConversionError(f"partial output is a directory: {temporary}")
        temporary.unlink()

    source.seek(source_offset)
    remaining = size
    try:
        with temporary.open("xb") as output:
            while remaining:
                chunk = source.read(min(remaining, COPY_BUFFER_SIZE))
                if not chunk:
                    raise core.ConversionError(
                        f"source ISO ended while extracting {destination.name}"
                    )
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists() and temporary.is_file():
            temporary.unlink()
        raise


def print_listing(info: core.ImageInfo) -> None:
    print(f"Format:   {info.iso_format.name}")
    if info.execution is not None:
        print(f"Title ID: {info.execution.title_id:08X}")
        print(f"Media ID: {info.execution.media_id:08X}")
    for entry, relative_path in iter_entries(info.entries):
        marker = "DIR " if entry.is_directory else "FILE"
        print(f"{marker} {entry.size:12,}  {relative_path}")


def extract_image(
    source_path: Path,
    destination: Path,
    *,
    overwrite: bool = False,
    quiet: bool = False,
) -> tuple[int, int]:
    source_path = source_path.resolve()
    destination = destination.resolve()
    info = core.inspect_image(source_path, require_xex=False)
    source_volume_size = info.source_size - info.root_offset
    validate_tree(info.entries, source_volume_size)
    prepare_destination(destination, overwrite)
    ensure_within(destination, destination)

    files = 0
    directories = 0
    with source_path.open("rb") as source:
        for entry, relative_path in iter_entries(info.entries):
            output_path = destination / relative_path
            ensure_within(destination, output_path)
            if entry.is_directory:
                if output_path.exists() and not output_path.is_dir():
                    raise core.ConversionError(
                        f"cannot replace file with a directory: {output_path}"
                    )
                output_path.mkdir(parents=True, exist_ok=True)
                directories += 1
                if not quiet:
                    print(f"Directory  {relative_path}")
                continue

            if not quiet:
                print(f"Extracting {relative_path} ({entry.size:,} bytes)")
            copy_extent(
                source,
                info.root_offset + entry.sector * core.SECTOR_SIZE,
                entry.size,
                output_path,
                overwrite,
            )
            files += 1

    print(
        f"Extracted {files:,} files and {directories:,} directories to {destination}"
    )
    return files, directories


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract XDVDFS files from an Xbox XISO or XGD1/XGD2/XGD3 image."
    )
    parser.add_argument(
        "source", type=Path, help="source XISO or XGD1/XGD2/XGD3 image"
    )
    parser.add_argument("destination", type=Path, help="folder to receive extracted files")
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the image contents without writing anything",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="allow replacement of colliding files in an existing directory",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="hide per-file extraction messages"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.source.is_file():
            raise core.ConversionError(f"source ISO does not exist: {args.source}")
        if args.list:
            print_listing(core.inspect_image(args.source.resolve(), require_xex=False))
        else:
            extract_image(
                args.source,
                args.destination,
                overwrite=args.overwrite,
                quiet=args.quiet,
            )
        return 0
    except (core.ConversionError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
