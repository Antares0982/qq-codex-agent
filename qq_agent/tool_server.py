import asyncio
import contextlib
import json
import sqlite3


class ToolServer:
    def __init__(self, contexts, images, profiles, send):
        self.contexts = contexts
        self.images = images
        self.profiles = profiles
        self.safe_send = send

    async def send_image(self, reader, writer):
        contexts = {context.folder.name: context for context in self.contexts.values()}
        context = None
        task = asyncio.current_task()
        try:
            try:
                request = json.loads(await asyncio.wait_for(reader.readline(), 5))
                if not isinstance(request, dict):
                    raise ValueError("Invalid tool request")
                context = contexts.get(request.pop("session", None))
                if (
                    context is None
                    or self.contexts.get(context.message.key) is not context
                ):
                    raise ValueError(
                        "当前没有可用的 QQ 任务；请在处理用户消息时调用图片工具。"
                    )
                context.tasks.add(task)
                if request == {"action": "list_images"}:
                    response = self.images.list_images(context)
                elif request.get("action") in {"list_profiles", "replace_profile"}:
                    response = self.profiles.member_request(context, request)
                    if request["action"] == "replace_profile" and any(
                        response["profile"].values()
                    ):
                        await self.safe_send(
                            context.message,
                            f"📝正在给{context.message.sender_name}记进小本本……",
                        )
                elif (
                    isinstance(request, dict) and request.get("action") == "send_image"
                ):
                    response = await self.images.deliver_image(context, request)
                else:
                    raise ValueError("未知图片工具请求。")
            except (
                ValueError,
                OSError,
                RuntimeError,
                TimeoutError,
                sqlite3.Error,
            ) as error:
                response = {"error": str(error)}
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            if context:
                context.tasks.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
