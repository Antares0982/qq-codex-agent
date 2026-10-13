"""Compress bundled stickers in place."""

import argparse
import io
import math
import os
import tempfile
from pathlib import Path

from PIL import Image, ImageChops

BYTE_LIMIT = 200_000
PIXEL_LIMIT = 640_000
EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def resize_frame(frame, size, gif):
    resized = frame.resize(size, Image.Resampling.LANCZOS)
    if gif:
        resized.putalpha(frame.getchannel("A").resize(size, Image.Resampling.NEAREST))
    return resized


def encode_frames(frames, format, durations, loop, level):
    encoded = []
    for frame in frames:
        if format == "GIF":
            picture = frame.convert("RGB").quantize(colors=level or 255)
            picture.paste(
                255, mask=frame.getchannel("A").point(lambda a: 255 if a == 0 else 0)
            )
            picture.info["transparency"] = 255
        elif format == "PNG" and level:
            picture = frame.convert("RGB").quantize(colors=level)
            if frame.getchannel("A").getextrema() != (255, 255):
                picture = picture.convert("RGB")
                picture.putalpha(frame.getchannel("A"))
        else:
            picture = (
                frame
                if frame.getchannel("A").getextrema() != (255, 255)
                else frame.convert("RGB")
            )
        encoded.append(picture)
    options = {}
    if format in {"PNG", "GIF", "JPEG"}:
        options["optimize"] = True
    if format == "GIF":
        options.update(transparency=255, background=255, disposal=2)
    if format == "JPEG":
        options.update(quality=level or 100, subsampling=0)
    if format == "WEBP":
        options.update(
            lossless=level is None, quality=level or 100, method=6, exact=True
        )
    if len(encoded) > 1:
        options.update(save_all=True, append_images=encoded[1:], duration=durations)
        if loop is not None:
            options["loop"] = loop
    output = io.BytesIO()
    encoded[0].save(output, format=format, **options)
    return output.getvalue()


def validate_output(data, format, frames, durations, loop):
    if len(data) > BYTE_LIMIT:
        raise ValueError("Compressed image exceeds byte limit")
    with Image.open(io.BytesIO(data)) as result:
        if result.format != format or result.size != frames[0].size:
            raise ValueError("Image format or size changed unexpectedly")
        if result.width * result.height > PIXEL_LIMIT:
            raise ValueError("Compressed image exceeds pixel limit")
        if getattr(result, "n_frames", 1) != len(frames):
            raise ValueError("Animation frame count changed")
        if len(frames) > 1 and result.info.get("loop") != loop:
            raise ValueError("Animation loop changed")
        for index, frame in enumerate(frames):
            result.seek(index)
            decoded = result.convert("RGBA")
            if len(frames) > 1 and result.info.get("duration", 0) != durations[index]:
                raise ValueError("Animation timing changed")
            if ImageChops.difference(
                decoded.getchannel("A"), frame.getchannel("A")
            ).getbbox():
                raise ValueError("Image transparency changed")


def compress_image(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular image file")
    original = path.read_bytes()
    with Image.open(io.BytesIO(original)) as source:
        format = source.format
        if format not in {"PNG", "JPEG", "GIF", "WEBP"}:
            raise ValueError("Unsupported image format")
        size = source.size
        if len(original) <= BYTE_LIMIT and size[0] * size[1] <= PIXEL_LIMIT:
            return len(original), len(original), size, size
        loop = source.info.get("loop")
        frames, durations = [], []
        for index in range(getattr(source, "n_frames", 1)):
            source.seek(index)
            frames.append(source.convert("RGBA"))
            durations.append(source.info.get("duration", 0))
    scale = min(1.0, math.sqrt(PIXEL_LIMIT / (size[0] * size[1])))
    levels = (
        (None, 256, 128)
        if format == "PNG"
        else (None, 128, 64)
        if format == "GIF"
        else (None, 95, 90, 85, 80)
    )
    while True:
        target = (max(1, int(size[0] * scale)), max(1, int(size[1] * scale)))
        resized = [resize_frame(frame, target, format == "GIF") for frame in frames]
        for level in levels:
            data = encode_frames(resized, format, durations, loop, level)
            if len(data) <= BYTE_LIMIT:
                validate_output(data, format, resized, durations, loop)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=path.parent, prefix=".sticker-", delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(data)
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                    os.replace(temporary, path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return len(original), len(data), size, target
        if target == (1, 1):
            raise ValueError("Cannot meet size limit while preserving animation")
        scale *= 0.9


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("stickers"))
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error("Image directory does not exist")
    failed = False
    for path in sorted(args.directory.rglob("*")):
        if path.suffix.lower() not in EXTENSIONS:
            continue
        try:
            before, after, old_size, new_size = compress_image(path)
            print(
                f"{path}: {before:,} -> {after:,} bytes; {old_size} -> {new_size}",
                flush=True,
            )
        except (OSError, ValueError) as error:
            failed = True
            print(f"{path}: ERROR: {error}", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
