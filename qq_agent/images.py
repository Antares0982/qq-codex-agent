import asyncio
import base64
import hashlib
import io
import json
import time

import aiohttp
from openai_codex import ImageInput, TextInput
from PIL import Image

from image_assets import OUTPUT_LIMIT, read_image, save_image

from .compression import compress_bytes
from .logging import LOG, log_text
from .prompts import (
    IMAGE_FAILED,
    IMAGE_LABEL,
    IMAGE_NOTICE,
    IMAGE_OMITTED,
    IMAGE_REPEATED,
    IMAGE_REVIEW,
)


def image_preview(data):
    with Image.open(io.BytesIO(data)) as picture:
        animated = getattr(picture, "n_frames", 1) > 1
        if animated:
            picture.seek(0)
            preview = io.BytesIO()
            picture.convert("RGBA").save(preview, format="PNG")
            data = preview.getvalue()
    data, format, _, _ = compress_bytes(data)
    return {
        "type": "image",
        "mimeType": "image/" + format.lower(),
        "data": base64.b64encode(data).decode(),
    }, animated


class Images:
    def __init__(self, db, bot, contexts):
        self.db = db
        self.bot = bot
        self.contexts = contexts

    async def view_image(self, context, request):
        if (
            request.keys() != {"action", "image_id", "token"}
            or "group_id" not in context.message.target
        ):
            raise ValueError("查看群图片需要 image_id 和当前任务 token。")
        if not isinstance(request["image_id"], str):
            raise ValueError("image_id 必须是字符串。")
        return await self.read_received(context, request["image_id"], request["token"])

    async def read_received(self, context, image_id, token):
        async with context.lock:
            if (
                self.contexts.get(context.message.key) is not context
                or token != context.token
            ):
                raise ValueError("图片工具不属于当前群任务。")
            row = self.db.execute(
                "SELECT source,folder,path,created FROM received_images WHERE id=? AND key=?",
                (image_id, context.message.key),
            ).fetchone()
            if not row:
                raise ValueError("图片索引不存在或不属于当前群。")
            source, folder, path, created = row
            if path:
                if folder != context.folder.name:
                    raise ValueError("图片不属于当前工作区。")
                path, data = read_image(context.folder, path)
            else:
                if not source or time.time() - created > 14 * 86400:
                    raise ValueError("图片索引已过期。")
                data = await self.bot.image(source)
                if self.contexts.get(context.message.key) is not context:
                    raise ValueError("任务已结束。")
                path = save_image(context.folder, data)
                with self.db:
                    self.db.execute(
                        "UPDATE received_images SET folder=?,path=? WHERE id=?",
                        (context.folder.name, path, image_id),
                    )
            preview, animated = await asyncio.to_thread(image_preview, data)
            if (
                self.contexts.get(context.message.key) is not context
                or token != context.token
            ):
                raise ValueError("任务已结束。")
            return {
                "ok": True,
                "path": path,
                "image_id": image_id,
                "first_frame": animated,
                "image": preview,
            }

    async def attach_images(self, context, text, seen, limit):
        inputs: list[TextInput | ImageInput] = [TextInput(text)]
        attached, omitted, failed, repeated = [], [], [], []
        visited = set()
        payload = json.loads(text.split("\n", 1)[1])
        for message in payload["messages"]:
            for quoted, body in ((True, message.get("quote", {})), (False, message)):
                for part in body.get("content", []):
                    if not isinstance(part, dict) or "image_id" not in part:
                        continue
                    image_id = part["image_id"]
                    if image_id in visited:
                        continue
                    visited.add(image_id)
                    if image_id in seen:
                        repeated.append(image_id)
                        continue
                    if len(attached) >= limit:
                        omitted.append(image_id)
                        continue
                    try:
                        result = await self.read_received(
                            context, image_id, context.token
                        )
                    except (
                        ValueError,
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        aiohttp.ClientError,
                    ) as error:
                        LOG.warning(
                            "Group image unavailable id=%s error=%s",
                            image_id,
                            log_text(error),
                        )
                        failed.append(image_id)
                        continue
                    label = json.dumps(
                        {
                            "image_id": image_id,
                            "message_id": body["message_id"],
                            "quoted": quoted,
                            "first_frame": result["first_frame"],
                        },
                        ensure_ascii=False,
                    )
                    inputs.extend(
                        [
                            TextInput(IMAGE_LABEL + label),
                            ImageInput(
                                "data:"
                                + result["image"]["mimeType"]
                                + ";base64,"
                                + result["image"]["data"]
                            ),
                        ]
                    )
                    attached.append(image_id)
        notices = []
        for ids, reason in (
            (repeated, IMAGE_REPEATED),
            (omitted, IMAGE_OMITTED),
            (failed, IMAGE_FAILED),
        ):
            if ids:
                notices.append(
                    IMAGE_NOTICE.format(
                        reason=reason, ids=json.dumps(ids, ensure_ascii=False)
                    )
                )
        if notices:
            inputs.append(TextInput("\n".join(notices) + IMAGE_REVIEW))
        return inputs, attached

    def list_images(self, context):
        rows = self.db.execute(
            "SELECT sequence, item_id, path, size FROM generated_images WHERE folder=? ORDER BY sequence DESC LIMIT 100",
            (context.folder.name,),
        ).fetchall()
        return {
            "ok": True,
            "workspace": str(context.folder),
            "images": [
                {"sequence": seq, "generation_id": item, "path": path, "size": size}
                for seq, item, path, size in reversed(rows)
            ],
            "message": "最近 100 张生成原图，按生成顺序排列；文件可能已被后续代码修改或删除。加工后的图片可直接按路径发送。",
        }

    async def deliver_image(self, context, request):
        if request.keys() - {"action", "path", "resend"}:
            raise ValueError("未知的图片发送参数。")
        resend = request.get("resend", False)
        if type(resend) is not bool:
            raise ValueError("resend 必须为布尔值。")
        async with context.lock:
            if self.contexts.get(context.message.key) is not context:
                raise ValueError("本轮已结束，请在当前轮重新调用工具。")
            path = request.get("path")
            if "path" not in request:
                row = self.db.execute(
                    "SELECT path FROM generated_images WHERE folder=? ORDER BY sequence DESC LIMIT 1",
                    (context.folder.name,),
                ).fetchone()
                if not row:
                    raise ValueError(
                        "尚无生成图片。请先生成，或用 path 指定已保存的工作区图片。"
                    )
                path = row[0]
            path, data = read_image(context.folder, path)
            digest = hashlib.sha256(data).hexdigest()
            identity = (context.folder.name, path, digest)
            previous = self.db.execute(
                "SELECT turn, status FROM image_deliveries WHERE folder=? AND path=? AND digest=?",
                identity,
            ).fetchone()
            if previous and not resend:
                if previous[1] == "unknown":
                    raise ValueError(
                        "此图片上次发送结果未知，不会自动重试；仅用户明确要求补发时使用 resend=true。"
                    )
                if previous[0] == context.token:
                    return {
                        "ok": True,
                        "path": path,
                        "already_sent": True,
                        "message": "此图片本轮已发送成功，未重复发送。",
                    }
            # Persist uncertainty before sending bytes.
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO image_deliveries VALUES (?, ?, ?, ?, 'unknown')",
                    (*identity, context.token),
                )
            try:
                receipt = await self.bot.send(context.message, image=data)
                context.chat_output = True
            except Exception as error:
                LOG.warning(
                    "Image delivery failed chat=%s error=%s",
                    context.message.key,
                    log_text(error),
                )
                raise ValueError(
                    "未取得 QQ 发送成功回执，发送结果未知；不要自动重试或声称已发送。"
                ) from error
            with self.db:
                self.db.execute(
                    "UPDATE image_deliveries SET status='sent' WHERE folder=? AND path=? AND digest=?",
                    identity,
                )
            LOG.info(
                "Image sent chat=%s message=%s path=%s bytes=%d",
                context.message.key,
                context.message.identifier,
                path,
                len(data),
            )
            return {
                "ok": True,
                "path": path,
                "message": "图片已发送到当前 QQ 会话。",
                "message_ids": [item.get("message_id") for item in receipt],
            }

    def save_generated(self, folder, item):
        if len(item.result) > (OUTPUT_LIMIT + 2) // 3 * 4:
            raise ValueError("Generated image too large")
        data = base64.b64decode(item.result, validate=True)
        if not data or len(data) > OUTPUT_LIMIT:
            raise ValueError("Invalid generated image")
        path = save_image(folder, data)
        with self.db:
            self.db.execute(
                "INSERT INTO generated_images (folder, item_id, path, size) VALUES (?, ?, ?, ?)",
                (folder.name, item.id, path, len(data)),
            )
