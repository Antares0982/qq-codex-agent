import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from openai_codex.generated.v2_all import ImageGenerationThreadItem
from PIL import Image

import image_assets
import image_tool
import qq_codex_agent as app
from test_agent import PNG, event, item_done, turn_done


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = app.Settings(
            {"1"},
            {"10": {"1"}},
            "ws://127.0.0.1:3001",
            root / "token",
            root / "state",
            root / "work",
            root / "AGENTS.md",
        )
        self.bot = NS(send=AsyncMock(return_value={"message_id": 123}))
        self.codex = NS(account=AsyncMock(return_value=NS(account=object())))
        self.codex._client = NS(
            request=AsyncMock(return_value={"status": "unsubscribed"})
        )
        self.agent = app.Agent(self.settings, self.bot, self.codex)
        self.folder = self.settings.workspace_dir / "session"
        self.folder.mkdir()
        self.message = app.parse_message(event(), self.settings)
        self.context = app.ImageTurn(self.message, self.folder)
        self.agent.image_contexts[self.message.key] = self.context

    def tearDown(self):
        self.agent.db.close()
        self.temp.cleanup()

    def save(self, data=PNG, item="frame"):
        path = image_assets.save_image(self.folder, data)
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO generated_images (folder, item_id, path, size) VALUES (?, ?, ?, ?)",
                (self.folder.name, item, path, len(data)),
            )
        return path

    async def test_default_send_keeps_file_and_receipt(self):
        path = self.save()
        result = await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.assertEqual(result["message_id"], 123)
        self.assertEqual((self.folder / path).read_bytes(), PNG)
        self.bot.send.assert_awaited_once_with(self.message, image=PNG)
        self.assertEqual(
            self.agent.list_images(self.context)["images"][0]["path"], path
        )

    async def test_concurrent_send_is_deduplicated(self):
        self.save()
        first, second = await asyncio.gather(
            *(
                self.agent.deliver_image(self.context, {"action": "send_image"})
                for _ in range(2)
            )
        )
        self.assertTrue(first["ok"] and second["already_sent"])
        self.bot.send.assert_awaited_once()

    async def test_unknown_delivery_survives_restart(self):
        self.save()
        self.bot.send.side_effect = TimeoutError()
        with self.assertRaisesRegex(ValueError, "结果未知"):
            await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.agent.db.close()
        self.agent = app.Agent(self.settings, self.bot, self.codex)
        self.context = app.ImageTurn(self.message, self.folder)
        self.agent.image_contexts[self.message.key] = self.context
        with self.assertRaisesRegex(ValueError, "结果未知"):
            await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.bot.send.assert_awaited_once()
        self.bot.send.side_effect = None
        result = await self.agent.deliver_image(
            self.context, {"action": "send_image", "resend": True}
        )
        self.assertTrue(result["ok"])

    async def test_later_turn_can_resend_original(self):
        path = self.save()
        await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.agent.image_contexts[self.message.key] = app.ImageTurn(
            self.message, self.folder
        )
        result = await self.agent.deliver_image(
            self.agent.image_contexts.get(self.message.key),
            {"action": "send_image", "path": path},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.bot.send.await_count, 2)

    async def test_stale_turn_cannot_send(self):
        self.save()
        self.agent.image_contexts[self.message.key] = app.ImageTurn(
            self.message, self.folder
        )
        with self.assertRaisesRegex(ValueError, "本轮已结束"):
            await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.bot.send.assert_not_awaited()

    async def test_other_session_cannot_list_or_send(self):
        path = self.save()
        other = self.settings.workspace_dir / "other"
        other.mkdir()
        context = app.ImageTurn(self.message, other)
        self.agent.image_contexts[self.message.key] = context
        self.assertEqual(self.agent.list_images(context)["images"], [])
        for request in (
            {"action": "send_image"},
            {"action": "send_image", "path": str(self.folder / path)},
        ):
            with self.assertRaises(ValueError):
                await self.agent.deliver_image(context, request)
        self.bot.send.assert_not_awaited()

    async def test_path_validation(self):
        valid = self.save()
        (self.folder / "link.png").symlink_to(self.folder / valid)
        (self.folder / "linked").symlink_to(
            self.folder / "artifacts", target_is_directory=True
        )
        (self.folder / "fake.png").write_text("<svg/>")
        os.mkfifo(self.folder / "pipe.png")
        for value in (
            "../state/token",
            str(self.settings.state_dir / "token"),
            "link.png",
            "linked/" + Path(valid).name,
            "fake.png",
            "pipe.png",
            "artifacts",
            "missing.png",
            None,
            1,
        ):
            with self.subTest(path=value), self.assertRaises(ValueError):
                await self.agent.deliver_image(
                    self.context, {"action": "send_image", "path": value}
                )
        self.bot.send.assert_not_awaited()
        result = await self.agent.deliver_image(
            self.context, {"action": "send_image", "path": str(self.folder / valid)}
        )
        self.assertTrue(result["ok"])

    def test_artifact_directory_symlink_is_rejected(self):
        (self.folder / "artifacts").symlink_to(
            self.settings.state_dir, target_is_directory=True
        )
        with self.assertRaises(OSError):
            image_assets.save_image(self.folder, PNG)
        self.assertEqual(list(self.settings.state_dir.glob("*.png")), [])

    def test_truncated_and_oversize_images_are_rejected(self):
        with self.assertRaises(ValueError):
            image_assets.save_image(self.folder, PNG[:15])
        path = self.save()
        with patch.object(image_assets, "OUTPUT_LIMIT", len(PNG) - 1):
            with self.assertRaises(ValueError):
                image_assets.read_image(self.folder, path)

    async def test_frames_are_usable_for_real_gif_processing(self):
        first = self.save(item="first")
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(stream, format="PNG")
        second = self.save(stream.getvalue(), item="second")
        images = self.agent.list_images(self.context)["images"]
        self.assertEqual(
            [image["generation_id"] for image in images], ["first", "second"]
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from PIL import Image; import sys; frames=[Image.open(p).convert('RGB') for p in sys.argv[1:]]; frames[0].save('animation.gif', save_all=True, append_images=frames[1:], duration=100, loop=0)",
                first,
                second,
            ],
            cwd=self.folder,
            check=True,
            timeout=10,
        )
        self.bot.send.assert_not_awaited()
        result = await self.agent.deliver_image(
            self.context, {"action": "send_image", "path": "animation.gif"}
        )
        self.assertTrue(result["ok"])
        data = self.bot.send.call_args.kwargs["image"]
        self.assertEqual(data, (self.folder / "animation.gif").read_bytes())
        with Image.open(io.BytesIO(data)) as gif:
            self.assertEqual(gif.n_frames, 2)

    async def test_generation_is_saved_without_shell_or_automatic_delivery(self):
        async def stream():
            for item_id in ("frame1", "frame1", "frame2"):
                yield item_done(
                    ImageGenerationThreadItem(
                        id=item_id,
                        type="imageGeneration",
                        status="completed",
                        result=base64.b64encode(PNG).decode(),
                    )
                )
            images = self.agent.list_images(
                self.agent.image_contexts.get(self.message.key)
            )["images"]
            self.assertEqual(
                [image["generation_id"] for image in images], ["frame1", "frame2"]
            )
            for image in images:
                self.assertEqual(
                    (
                        self.agent.image_contexts.get(self.message.key).folder
                        / image["path"]
                    ).read_bytes(),
                    PNG,
                )
            self.assertFalse(
                any("image" in call.kwargs for call in self.bot.send.call_args_list)
            )
            yield turn_done()

        self.codex.thread_start = AsyncMock(
            return_value=NS(
                id="thread",
                turn=AsyncMock(return_value=NS(stream=stream, interrupt=AsyncMock())),
            )
        )
        await self.agent.execute(self.message)
        self.assertIsNone(self.agent.image_contexts.get(self.message.key))
        self.assertEqual(
            self.agent.db.execute("SELECT COUNT(*) FROM generated_images").fetchone()[
                0
            ],
            2,
        )

    async def test_new_removes_files_and_image_metadata(self):
        self.save()
        await self.agent.deliver_image(self.context, {"action": "send_image"})
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (self.message.key, "thread", self.folder.name),
            )
        await self.agent.control(app.parse_message(event(text="/new"), self.settings))
        self.assertFalse(self.folder.exists())
        for table in ("generated_images", "image_deliveries"):
            self.assertEqual(
                self.agent.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )

    async def test_rpc_binds_context_before_reading_request(self):
        self.save()

        async def readline():
            self.agent.image_contexts[self.message.key] = app.ImageTurn(
                self.message, self.folder
            )
            return b'{"action":"send_image"}\n'

        writer = NS(
            write=lambda data: output.append(json.loads(data)),
            drain=AsyncMock(),
            close=lambda: None,
            wait_closed=AsyncMock(),
        )
        output = []
        await self.agent.send_image(NS(readline=readline), writer)
        self.assertIn("error", output[0])
        self.bot.send.assert_not_awaited()

    async def test_rpc_rejects_other_session_and_returns_current_paths(self):
        path = self.save()
        for session in ("other", self.folder.name):
            output = []
            writer = NS(
                write=lambda data: output.append(json.loads(data)),
                drain=AsyncMock(),
                close=lambda: None,
                wait_closed=AsyncMock(),
            )
            reader = NS(
                readline=AsyncMock(
                    return_value=json.dumps(
                        {"action": "list_images", "session": session}
                    ).encode()
                )
            )
            await self.agent.send_image(reader, writer)
            if session == "other":
                self.assertIn("error", output[0])
            else:
                self.assertEqual(output[0]["images"][0]["path"], path)

    async def test_generating_during_send_does_not_remove_new_frame(self):
        first = self.save(item="first")

        async def send(*args, **kwargs):
            self.save(item="second")
            await asyncio.sleep(0)

        self.bot.send.side_effect = send
        result = await self.agent.deliver_image(self.context, {"action": "send_image"})
        self.assertEqual(result["path"], first)
        self.assertEqual(
            [
                image["generation_id"]
                for image in self.agent.list_images(self.context)["images"]
            ],
            ["first", "second"],
        )

    async def test_canceled_delivery_remains_unknown(self):
        self.save()
        started = asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await asyncio.Future()

        self.bot.send.side_effect = send
        task = asyncio.create_task(
            self.agent.deliver_image(self.context, {"action": "send_image"})
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(ValueError, "结果未知"):
            await self.agent.deliver_image(self.context, {"action": "send_image"})

    async def test_slow_progress_does_not_delay_image_files(self):
        progress = asyncio.Event()
        release = asyncio.Event()

        async def send(message, text=None, **kwargs):
            if text == "正在生成图片……":
                progress.set()
                await release.wait()

        self.bot.send.side_effect = send
        image = ImageGenerationThreadItem(
            id="frame",
            type="imageGeneration",
            status="completed",
            result=base64.b64encode(PNG).decode(),
        )

        async def stream():
            yield NS(method="item/started", payload=NS(item=NS(root=image)))
            await asyncio.wait_for(progress.wait(), 1)
            yield item_done(image)
            self.assertEqual(
                len(
                    self.agent.list_images(
                        self.agent.image_contexts.get(self.message.key)
                    )["images"]
                ),
                1,
            )
            release.set()
            yield turn_done()

        self.codex.thread_start = AsyncMock(
            return_value=NS(
                id="thread",
                turn=AsyncMock(return_value=NS(stream=stream, interrupt=AsyncMock())),
            )
        )
        await asyncio.wait_for(self.agent.execute(self.message), 2)
        self.assertTrue(release.is_set())

    def test_stdio_tools_and_path_arguments(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "send_image",
                    "arguments": {"path": "animation.gif"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_images", "arguments": {}},
            },
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["image_tool.py", "socket", "session"]),
            patch.object(
                sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests)))
            ),
            patch.object(sys, "stdout", output),
            patch.object(image_tool, "call", return_value={"content": []}) as call,
        ):
            image_tool.main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            {tool["name"] for tool in responses[0]["result"]["tools"]},
            {"list_images", "send_image"},
        )
        self.assertEqual(
            call.call_args_list[0].args,
            ("socket", "session", "send_image", {"path": "animation.gif"}),
        )
        self.assertEqual(
            call.call_args_list[1].args, ("socket", "session", "list_images", {})
        )


if __name__ == "__main__":
    unittest.main()
