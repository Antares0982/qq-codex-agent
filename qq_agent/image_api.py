import base64
import io
import json
import re
from pathlib import Path

import aiohttp
from PIL import Image

from image_assets import OUTPUT_LIMIT, read_image
from image_tool import (
    COMPLEXITIES,
    FORMATS,
    GENERATION_TIMEOUT,
    GENERATION_TOOL,
    MODELS,
    QUALITIES,
)

ENDPOINT = "https://api.openai.com/v1/images"
ENCODED_LIMIT = (OUTPUT_LIMIT + 2) // 3 * 4


def image_request(folder, arguments):
    fields = arguments.copy()
    if fields.keys() - GENERATION_TOOL["inputSchema"]["properties"].keys():
        raise ValueError("未知的生图参数。")
    prompt = fields.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt 不能为空。")
    fields.setdefault("model", MODELS[0])
    output = fields.setdefault("output_format", "png")
    for name, choices in (
        ("model", MODELS),
        ("output_format", FORMATS),
        ("quality", QUALITIES),
        ("complexity", COMPLEXITIES),
    ):
        if name in fields and fields[name] not in choices:
            raise ValueError(f"{name} 不支持此值。")
    for name, minimum, maximum in (
        ("frame_count", 2, 16),
        ("frame_duration_ms", 20, 2000),
    ):
        if name in fields and (
            output != "gif"
            or type(fields[name]) is not int
            or not minimum <= fields[name] <= maximum
        ):
            raise ValueError(f"{name} 仅用于 GIF，须为 {minimum}–{maximum} 的整数。")
    size = fields.get("size", "auto")
    if size != "auto":
        if not isinstance(size, str) or not re.fullmatch(
            r"[1-9][0-9]{0,3}x[1-9][0-9]{0,3}", size
        ):
            raise ValueError("size 应为 auto 或 WIDTHxHEIGHT。")
        width, height = map(int, size.split("x"))
        if (
            width % 16
            or height % 16
            or max(width, height) > 3840
            or (
                output != "gif"
                and (
                    max(width, height) > min(width, height) * 3
                    or not 655360 <= width * height <= 8294400
                )
            )
        ):
            raise ValueError("图片尺寸不受支持。")
    paths = fields.pop("images", [])
    if not isinstance(paths, list) or len(paths) > 5:
        raise ValueError("images 须为最多 5 个参考图路径。")
    if not paths:
        if "markup" in fields:
            raise ValueError("标注编辑需要参考图。")
        return "generations", fields
    overlay = fields.pop("markup", None)
    if "markup" in arguments and not isinstance(overlay, str):
        raise ValueError("markup 须为 PNG 路径。")
    form = aiohttp.FormData()
    for name, value in fields.items():
        form.add_field(name, str(value))
    first_size = None
    total = 0
    for index, path in enumerate(paths + ([overlay] if overlay is not None else [])):
        filename, data = read_image(folder, path)
        total += len(data)
        if total > OUTPUT_LIMIT:
            raise ValueError("输入图片总大小不能超过 32 MiB。")
        with Image.open(io.BytesIO(data)) as picture:
            if picture.format not in ("JPEG", "PNG", "WEBP") or getattr(
                picture, "is_animated", False
            ):
                raise ValueError("参考图须为静态 PNG/JPEG/WebP。")
            size = picture.size
            if picture.getexif().get(274) in (5, 6, 7, 8):
                size = size[::-1]
            if index == 0:
                first_size = size
            field = "image" if len(paths) == 1 else "image[]"
            if index == len(paths):
                if (
                    picture.format != "PNG"
                    or size != first_size
                    or picture.width * picture.height > 16777216
                    or picture.convert("RGBA").getchannel("A").getextrema()[0] == 255
                ):
                    raise ValueError(
                        "标注层须为与首图等大的透明 PNG，最多 16777216 像素。"
                    )
                field = "markup"
            form.add_field(
                field,
                data,
                filename=Path(filename).name,
                content_type="image/" + picture.format.lower(),
            )
    return "edits", form


async def request_image(key_file, endpoint, body):
    key = key_file.read_bytes().strip()
    if not key or not re.fullmatch(rb"[!-~]+", key):
        raise ValueError("未配置有效的 OpenAI 生图凭据。")
    headers = {"Authorization": "Bearer " + key.decode("ascii")}
    async with aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=GENERATION_TIMEOUT)
    ) as session:
        try:
            async with session.post(
                f"{ENDPOINT}/{endpoint}",
                headers=headers,
                allow_redirects=False,
                json=body if endpoint == "generations" else None,
                data=body if endpoint == "edits" else None,
            ) as response:
                if response.status != 200:
                    raise ValueError(
                        f"OpenAI 生图失败（HTTP {response.status}）；不要自动重试。"
                    )
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > ENCODED_LIMIT + 262144:
                        raise ValueError("生图响应超出大小限制。")
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise ValueError(
                "OpenAI 生图连接失败或超时，结果未知，不要自动重试。"
            ) from None
    try:
        encoded = json.loads(raw)["data"][0]["b64_json"]
        if not isinstance(encoded, str) or len(encoded) > ENCODED_LIMIT:
            raise ValueError
        return base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError, RecursionError):
        raise ValueError("OpenAI 未返回有效图片；不要自动重试。") from None
