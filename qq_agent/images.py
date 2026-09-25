import base64
import hashlib

from image_assets import OUTPUT_LIMIT, read_image, save_image

from .logging import LOG, log_text

IMAGE_INSTRUCTIONS = """本会话通过 QQ 交付结果。用户无法直接访问你的硬盘、工作区路径、sandbox 链接或工具图片预览。
生成图片完成后，应用自动把原图保存到当前会话工作区。调用 qq_image.list_images 查询真实路径、生成 ID 和顺序；不要猜测工具内部路径，不要自行搬运或解码 Base64。
需要用户收到图片时，必须显式调用 qq_image.send_image。path 指定当前工作区中的 PNG/JPEG/GIF/WebP（最大 32 MiB）；省略 path 则发送最近生成的原图。此工具由应用直接上传，不依赖命令沙箱，也不要求 QQ 读取本地磁盘。
多帧拼接、动图等任务：先生成所需帧，查询路径，使用代码加工并保存最终 GIF，再调用 send_image(path=最终文件路径)。中间帧不必发送；不能用 SVG 包装代替 PNG/GIF。工作区图片跨轮及 /new 保留，可以补发；超过 14 天未访问或修改的文件会被每日清理。
只有 send_image 返回成功才能声称图片已发送。发送失败或结果未知时如实说明；不要自动重试结果未知的发送。resend=true 仅用于用户明确要求再次发送。
命令沙箱不可用时，原图仍可通过上述工具查询和发送；需要加工的任务应明确说明加工失败，不声称动图已经完成。"""


class Images:
    def __init__(self, db, bot, contexts):
        self.db = db
        self.bot = bot
        self.contexts = contexts

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
                "message_id": receipt.get("message_id")
                if isinstance(receipt, dict)
                else None,
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
