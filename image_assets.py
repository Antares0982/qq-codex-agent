"""Host-side image files; no model-generated shell commands are needed for delivery."""

import contextlib
import io
import os
import stat
import uuid
import warnings
from pathlib import Path

from PIL import Image

OUTPUT_LIMIT = 32 * 1024 * 1024


def image_format(data):
    if not data or len(data) > OUTPUT_LIMIT:
        raise ValueError("图片为空或超过 32 MiB。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                suffix = {
                    "PNG": ".png",
                    "JPEG": ".jpg",
                    "GIF": ".gif",
                    "WEBP": ".webp",
                }.get(image.format)
                if suffix is None:
                    raise ValueError("仅支持 PNG、JPEG、GIF 和 WebP，不支持 SVG。")
                image.verify()
                return suffix
    except (
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise ValueError("图片损坏、格式不支持或像素尺寸过大。") from error


@contextlib.contextmanager
def workspace_directory(folder, parts=()):
    # Walk using directory descriptors so a symlink swap cannot escape the workspace.
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def relative_image_path(folder, value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise ValueError("请提供当前会话工作区中的图片文件路径。")
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(folder)
        except ValueError:
            raise ValueError("只能发送当前会话工作区中的图片。") from None
    if not path.parts or ".." in path.parts:
        raise ValueError("图片路径不能越出当前会话工作区。")
    return path


def read_image(folder, value):
    path = relative_image_path(folder, value)
    try:
        with workspace_directory(folder, path.parts[:-1]) as directory:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > OUTPUT_LIMIT
                ):
                    raise ValueError("图片必须是大小不超过 32 MiB 的普通文件。")
                data = stream.read(OUTPUT_LIMIT + 1)
    except OSError as error:
        raise ValueError(
            "图片不存在、不可读或路径包含符号链接；请用 list_images 查询原图路径。"
        ) from error
    image_format(data)
    return path.as_posix(), data


def save_image(folder, data):
    suffix = image_format(data)
    filename = uuid.uuid4().hex + suffix
    with workspace_directory(folder) as root:
        try:
            os.mkdir("artifacts", mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        directory = os.open(
            "artifacts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
        try:
            temporary = "." + filename
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                os.rename(
                    temporary, filename, src_dir_fd=directory, dst_dir_fd=directory
                )
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        finally:
            os.close(directory)
    return "artifacts/" + filename
