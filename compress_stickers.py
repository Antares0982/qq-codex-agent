"""Compress an image or directory, updating converted filenames."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from qq_agent.compression import FORMATS, compress_bytes
from qq_agent.stickers import unique_names

EXTENSIONS = {*FORMATS.values(), ".jpeg"}
STICKER_ROOT = Path(__file__).parent / "stickers"


def write_atomic(path, data, mode, replace=True):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".sticker-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.chmod(temporary, mode)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def compress_image(path):
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError("Expected a regular image file")
    original = path.read_bytes()
    data, format, old_size, size = compress_bytes(original)
    suffix = FORMATS[format]
    target = (
        path
        if path.suffix.lower() in ({".jpg", ".jpeg"} if format == "JPEG" else {suffix})
        else path.with_suffix(suffix)
    )
    config = None
    mapping = None
    if target != path:
        if target.exists() or target.is_symlink():
            raise ValueError(f"Converted file already exists: {target}")
        root = STICKER_ROOT.absolute()
        if path.is_relative_to(root):
            config = root / "sticker_config.json"
            if config.is_symlink():
                raise ValueError("Expected a regular sticker configuration")
            mapping = json.loads(config.read_text(), object_pairs_hook=unique_names)
            if not isinstance(mapping, dict):
                raise ValueError("Invalid sticker configuration")
            old_name, new_name = (
                path.relative_to(root).as_posix(),
                target.relative_to(root).as_posix(),
            )
            mapping = {
                key: new_name if value == old_name else value
                for key, value in mapping.items()
            }
    if data != original or target != path:
        write_atomic(target, data, path.stat().st_mode & 0o777, replace=target == path)
        if target != path:
            if config is not None:
                write_atomic(
                    config,
                    (json.dumps(mapping, ensure_ascii=False, indent=2) + "\n").encode(),
                    config.stat().st_mode & 0o777,
                )
            path.unlink()
    return target, len(original), len(data), old_size, size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path("stickers"))
    args = parser.parse_args()
    if args.path.is_symlink() or not args.path.exists():
        parser.error("Expected an existing regular image file or directory")
    paths = (
        sorted(
            path for path in args.path.rglob("*") if path.suffix.lower() in EXTENSIONS
        )
        if args.path.is_dir()
        else [args.path]
    )
    failed = False
    for path in paths:
        try:
            target, before, after, old_size, new_size = compress_image(path)
            print(
                f"{path} -> {target}: {before:,} -> {after:,} bytes; {old_size} -> {new_size}",
                flush=True,
            )
        except (OSError, ValueError) as error:
            failed = True
            print(f"{path}: ERROR: {error}", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
