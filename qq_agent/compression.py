"""Size-limited images with validated animation and alpha."""

import io
import math
import warnings

from PIL import Image, ImageChops

BYTE_LIMIT = 200_000
PIXEL_LIMIT = 640_000
FORMATS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}


def resize_frame(frame, size, gif):
    resized = frame.resize(size, Image.Resampling.LANCZOS)
    if gif:
        resized.putalpha(frame.getchannel("A").resize(size, Image.Resampling.NEAREST))
    return resized


def encode_frames(frames, format, durations, loop, level, default_image=False):
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
        options.update(quality=100 if level is None else level, subsampling=0)
    if format == "WEBP":
        options.update(
            lossless=level is None,
            quality=100 if level is None else level,
            method=6,
            exact=True,
        )
    if len(encoded) > 1:
        options.update(
            save_all=True,
            append_images=encoded[1:],
            duration=durations[1:] if default_image else durations,
        )
        if default_image:
            options["default_image"] = True
        if loop is not None:
            options["loop"] = loop
    output = io.BytesIO()
    encoded[0].save(output, format=format, **options)
    return output.getvalue()


def validate_output(data, format, frames, durations, loop, default_image=False):
    if len(data) > BYTE_LIMIT:
        raise ValueError("Compressed image exceeds byte limit")
    with Image.open(io.BytesIO(data)) as result:
        if result.format != format or result.size != frames[0].size:
            raise ValueError("Image format or size changed unexpectedly")
        if result.width * result.height > PIXEL_LIMIT:
            raise ValueError("Compressed image exceeds pixel limit")
        if bool(result.info.get("default_image")) != default_image:
            raise ValueError("Animation default image changed")
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


def fit_frames(
    frames, format, durations, loop, default_image
) -> tuple[bytes, str] | None:
    def attempt(output_format, level):
        if default_image and output_format != "PNG":
            return None
        try:
            data = encode_frames(
                frames, output_format, durations, loop, level, default_image
            )
            validate_output(data, output_format, frames, durations, loop, default_image)
            return data, output_format
        except (OSError, ValueError):
            return None

    lossless = [
        result
        for candidate in dict.fromkeys(("PNG" if format == "JPEG" else format, "WEBP"))
        if (result := attempt(candidate, None)) is not None
    ]
    if lossless:
        return min(lossless, key=lambda result: len(result[0]))
    best = attempt("WEBP", 100)
    if best:
        return best
    best = attempt("WEBP", 0)
    if best:
        low, high = 0, 99
        # Codec sizes vary; keep validated candidates.
        while low < high:
            level = (low + high + 1) // 2
            result = attempt("WEBP", level)
            if result:
                low, best = level, result
            else:
                high = level - 1
        return best
    levels = (
        (256, 128)
        if format == "PNG"
        else (128, 64)
        if format == "GIF"
        else (95, 80, 50, 0)
    )
    for level in levels:
        result = attempt(format, level)
        if result:
            return result
    return None


def compress_bytes(original) -> tuple[bytes, str, tuple[int, int], tuple[int, int]]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(original)) as source:
            format = source.format
            if format not in FORMATS:
                raise ValueError("Unsupported image format")
            size = source.size
            loop = source.info.get("loop")
            default_image = bool(source.info.get("default_image"))
            frames, durations = [], []
            for index in range(getattr(source, "n_frames", 1)):
                source.seek(index)
                frame = source.convert("RGBA")
                frames.append(frame)
                durations.append(source.info.get("duration", 0))
    if len(original) <= BYTE_LIMIT and size[0] * size[1] <= PIXEL_LIMIT:
        return original, format, size, size
    scale = min(1.0, math.sqrt(PIXEL_LIMIT / (size[0] * size[1])))
    longest = max(1, int(max(size) * scale))

    def attempt(edge):
        target = (
            max(1, size[0] * edge // max(size)),
            max(1, size[1] * edge // max(size)),
        )
        resized = [resize_frame(frame, target, format == "GIF") for frame in frames]
        result = fit_frames(resized, format, durations, loop, default_image)
        return (*result, size, target) if result else None

    result = attempt(longest)
    if result:
        return result
    best = None
    low, high = 1, longest - 1
    while low <= high:
        edge = (low + high) // 2
        result = attempt(edge)
        if result:
            best, low = result, edge + 1
        else:
            high = edge - 1
    if best is None:
        raise ValueError("Cannot meet size limit while preserving animation and alpha")
    return best
