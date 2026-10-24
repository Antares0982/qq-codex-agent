import asyncio
import base64
import io
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from openai_codex.generated.v2_all import ImageGenerationThreadItem
from PIL import Image
from PIL.GifImagePlugin import GifImageFile

import compress_stickers
import image_assets
import image_tool
from qq_agent import cleanup, replies, stickers
from qq_agent.agent import Agent
from qq_agent.config import GroupSettings, Settings
from qq_agent.messages import ImageTurn, parse_message
from test_agent import PNG, event, item_done, turn_done


class TestImages:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.root = root = tmp_path
        self.settings = Settings(
            {"1"},
            {"10": GroupSettings()},
            "ws://127.0.0.1:3001",
            root / "token",
            root / "state",
            root / "work",
            root / "AGENTS.md",
        )
        self.bot = NS(send=AsyncMock(return_value=[{"message_id": 123}]))
        self.codex = NS(account=AsyncMock(return_value=NS(account=object())))
        self.codex._client = NS(
            request=AsyncMock(
                return_value=NS(
                    status="unsubscribed", config=NS(sandbox_workspace_write=None)
                )
            )
        )
        self.agent = Agent(self.settings, self.bot, self.codex)
        self.folder = self.settings.workspace_dir / "session"
        self.folder.mkdir()
        self.message = parse_message(event(), self.settings)
        self.context = ImageTurn(self.message, self.folder)
        self.agent.turns.contexts[self.message.key] = self.context

        try:
            yield
        finally:
            self.agent.db.close()

    def save(self, data=PNG, item="frame"):
        path = image_assets.save_image(self.folder, data)
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO generated_images (folder, item_id, path, size) VALUES (?, ?, ?, ?)",
                (self.folder.name, item, path, len(data)),
            )
        return path

    def group_context(self):
        self.agent.turns.contexts.clear()
        self.message = parse_message(event(group=10), self.settings)
        self.context = ImageTurn(self.message, self.folder)
        self.agent.turns.contexts[self.message.key] = self.context

    async def test_default_send_keeps_file_and_receipt(self):
        path = self.save()
        result = await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image"}
        )
        assert result["message_ids"] == [123]
        assert (self.folder / path).read_bytes() == PNG
        self.bot.send.assert_awaited_once_with(self.message, image=PNG)
        assert (
            self.agent.turns.images.list_images(self.context)["images"][0]["path"]
            == path
        )

    async def test_concurrent_send_is_deduplicated(self):
        self.save()
        first, second = await asyncio.gather(
            *(
                self.agent.turns.images.deliver_image(
                    self.context, {"action": "send_image"}
                )
                for _ in range(2)
            )
        )
        assert first["ok"] and second["already_sent"]
        self.bot.send.assert_awaited_once()

    async def test_unknown_delivery_survives_restart(self):
        self.save()
        self.bot.send.side_effect = TimeoutError()
        with pytest.raises(ValueError, match="结果未知"):
            await self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image"}
            )
        self.agent.db.close()
        self.agent = Agent(self.settings, self.bot, self.codex)
        self.context = ImageTurn(self.message, self.folder)
        self.agent.turns.contexts[self.message.key] = self.context
        with pytest.raises(ValueError, match="结果未知"):
            await self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image"}
            )
        self.bot.send.assert_awaited_once()
        self.bot.send.side_effect = None
        result = await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image", "resend": True}
        )
        assert result["ok"]

    async def test_later_turn_can_resend_original(self):
        path = self.save()
        await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image"}
        )
        self.agent.turns.contexts[self.message.key] = ImageTurn(
            self.message, self.folder
        )
        result = await self.agent.turns.images.deliver_image(
            self.agent.turns.contexts.get(self.message.key),
            {"action": "send_image", "path": path},
        )
        assert result["ok"]
        assert self.bot.send.await_count == 2

    async def test_stale_turn_cannot_send(self):
        self.save()
        self.agent.turns.contexts[self.message.key] = ImageTurn(
            self.message, self.folder
        )
        with pytest.raises(ValueError, match="本轮已结束"):
            await self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image"}
            )
        self.bot.send.assert_not_awaited()

    async def test_other_session_cannot_list_or_send(self):
        path = self.save()
        other = self.settings.workspace_dir / "other"
        other.mkdir()
        context = ImageTurn(self.message, other)
        self.agent.turns.contexts[self.message.key] = context
        assert self.agent.turns.images.list_images(context)["images"] == []
        for request in (
            {"action": "send_image"},
            {"action": "send_image", "path": str(self.folder / path)},
        ):
            with pytest.raises(ValueError):
                await self.agent.turns.images.deliver_image(context, request)
        self.bot.send.assert_not_awaited()

    async def test_path_validation(self):
        valid = self.save()
        result = await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image", "path": str(self.folder / valid)}
        )
        assert result["ok"]

    @pytest.mark.parametrize(
        "value",
        (
            "../state/token",
            "{state}/token",
            "link.png",
            "linked/{filename}",
            "fake.png",
            "pipe.png",
            "artifacts",
            "missing.png",
            None,
            1,
        ),
    )
    async def test_invalid_path(self, value):
        valid = self.save()
        (self.folder / "link.png").symlink_to(self.folder / valid)
        (self.folder / "linked").symlink_to(
            self.folder / "artifacts", target_is_directory=True
        )
        (self.folder / "fake.png").write_text("<svg/>")
        os.mkfifo(self.folder / "pipe.png")
        if isinstance(value, str):
            value = value.format(
                state=self.settings.state_dir, filename=Path(valid).name
            )
        with pytest.raises(ValueError):
            await self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image", "path": value}
            )
        self.bot.send.assert_not_awaited()

    def test_artifact_directory_symlink_is_rejected(self):
        (self.folder / "artifacts").symlink_to(
            self.settings.state_dir, target_is_directory=True
        )
        with pytest.raises(OSError):
            image_assets.save_image(self.folder, PNG)
        assert list(self.settings.state_dir.glob("*.png")) == []

    def test_truncated_and_oversize_images_are_rejected(self):
        with pytest.raises(ValueError):
            image_assets.save_image(self.folder, PNG[:15])
        path = self.save()
        with patch.object(image_assets, "OUTPUT_LIMIT", len(PNG) - 1):
            with pytest.raises(ValueError):
                image_assets.read_image(self.folder, path)

    async def test_frames_are_usable_for_real_gif_processing(self):
        first = self.save(item="first")
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(stream, format="PNG")
        second = self.save(stream.getvalue(), item="second")
        images = self.agent.turns.images.list_images(self.context)["images"]
        assert [image["generation_id"] for image in images] == ["first", "second"]
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
        result = await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image", "path": "animation.gif"}
        )
        assert result["ok"]
        data = self.bot.send.call_args.kwargs["image"]
        assert data == (self.folder / "animation.gif").read_bytes()
        with Image.open(io.BytesIO(data)) as gif:
            assert isinstance(gif, GifImageFile)
            assert gif.n_frames == 2

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
            context = self.agent.turns.contexts.get(self.message.key)
            assert context is not None
            images = self.agent.turns.images.list_images(context)["images"]
            assert [image["generation_id"] for image in images] == ["frame1", "frame2"]
            for image in images:
                assert (context.folder / image["path"]).read_bytes() == PNG
            assert not any(
                ("image" in call.kwargs for call in self.bot.send.call_args_list)
            )
            yield turn_done()

        self.codex.thread_start = AsyncMock(
            return_value=NS(
                id="thread",
                turn=AsyncMock(return_value=NS(stream=stream, interrupt=AsyncMock())),
            )
        )
        await self.agent.turns.execute(self.message)
        assert self.agent.turns.contexts.get(self.message.key) is None
        assert (
            self.agent.db.execute("SELECT COUNT(*) FROM generated_images").fetchone()[0]
            == 2
        )

    async def test_new_preserves_images(self):
        self.save()
        await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image"}
        )
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions (key, thread, folder) VALUES (?, ?, ?)",
                (self.message.key, "thread", self.folder.name),
            )
        await self.agent.commands.control(
            parse_message(event(text="/new"), self.settings)
        )
        assert self.folder.exists()
        for table in ("generated_images", "image_deliveries"):
            assert (
                self.agent.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == 1
            )

    async def test_file_cleanup(self):
        path = self.save()
        await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image"}
        )
        missing = self.save(item="missing")
        (self.folder / missing).unlink()
        self.agent.turns.contexts.clear()
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions (key, thread, folder) VALUES (?, ?, ?)",
                (self.message.key, "thread", self.folder.name),
            )
        cutoff = 2_000_000_000 - cleanup.FILE_RETENTION
        skills = (
            ".agents/skills/custom/SKILL.md",
            ".agents/skills/custom/scripts/helper.py",
            "project/.agents/skills/custom/references/guide.md",
        )
        for name, times in {
            path: (cutoff - 1, cutoff - 1),
            "accessed": (cutoff + 1, cutoff - 1),
            "modified": (cutoff - 1, cutoff + 1),
            "boundary": (cutoff, cutoff),
            ".agents/expired": (cutoff - 1, cutoff - 1),
            **{name: (cutoff - 1, cutoff - 1) for name in skills},
        }.items():
            file = self.folder / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(PNG)
            os.utime(file, times)
        outside = self.root / "outside"
        outside.mkdir()
        protected = outside / "protected"
        protected.write_bytes(PNG)
        os.utime(protected, (cutoff - 1, cutoff - 1))
        (self.folder / "link").symlink_to(outside, target_is_directory=True)
        (self.folder / "file-link").symlink_to(protected)
        (self.settings.workspace_dir / "outside-link").symlink_to(outside)
        os.mkfifo(self.folder / "pipe")
        with patch.object(time, "time", return_value=2_000_000_000):
            for busy in (self.agent.jobs, self.agent.resetting):
                if isinstance(busy, dict):
                    busy[self.message.key] = object()
                else:
                    busy.add(self.message.key)
                self.agent.cleanup.clean_files()
                assert (self.folder / path).exists()
                busy.clear()
            self.agent.cleanup.clean_files()
        assert not (self.folder / path).exists()
        assert not (self.folder / ".agents/expired").exists()
        assert all((self.folder / name).is_file() for name in skills)
        for name in ("accessed", "modified", "boundary", "link", "file-link", "pipe"):
            assert (self.folder / name).exists()
        assert protected.exists()
        assert self.folder.exists()
        assert self.agent.turns.images.list_images(self.context)["images"] == []
        assert (
            self.agent.db.execute("SELECT COUNT(*) FROM image_deliveries").fetchone()[0]
            == 0
        )
        assert (
            self.agent.db.execute("SELECT folder FROM sessions").fetchone()[0]
            == self.folder.name
        )

    async def test_cleanup_schedule(self):
        with (
            patch.object(self.agent.cleanup, "clean_files") as clean,
            patch.object(asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            sleep.side_effect = [None, asyncio.CancelledError]
            with pytest.raises(asyncio.CancelledError):
                await self.agent.cleanup.cleanup_files()
            assert clean.call_count == 2
            assert sleep.await_count == 2
            sleep.assert_awaited_with(24 * 60 * 60)

    async def test_rpc_binds_context_before_reading_request(self):
        self.save()

        async def readline():
            self.agent.turns.contexts[self.message.key] = ImageTurn(
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
        await self.agent.tools.send_image(NS(readline=readline), writer)
        assert "error" in output[0]
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
            await self.agent.tools.send_image(reader, writer)
            if session == "other":
                assert "error" in output[0]
            else:
                assert output[0]["images"][0]["path"] == path

    async def test_generating_during_send_does_not_remove_new_frame(self):
        first = self.save(item="first")

        async def send(*args, **kwargs):
            self.save(item="second")
            await asyncio.sleep(0)
            return [{"message_id": 123}]

        self.bot.send.side_effect = send
        result = await self.agent.turns.images.deliver_image(
            self.context, {"action": "send_image"}
        )
        assert result["path"] == first
        assert [
            image["generation_id"]
            for image in self.agent.turns.images.list_images(self.context)["images"]
        ] == ["first", "second"]

    async def test_canceled_delivery_remains_unknown(self):
        self.save()
        started = asyncio.Event()

        async def send(*args, **kwargs):
            started.set()
            await asyncio.Future()

        self.bot.send.side_effect = send
        task = asyncio.create_task(
            self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image"}
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ValueError, match="结果未知"):
            await self.agent.turns.images.deliver_image(
                self.context, {"action": "send_image"}
            )

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
            assert (
                len(
                    self.agent.turns.images.list_images(
                        self.agent.turns.contexts.get(self.message.key)
                    )["images"]
                )
                == 1
            )
            release.set()
            yield turn_done()

        self.codex.thread_start = AsyncMock(
            return_value=NS(
                id="thread",
                turn=AsyncMock(return_value=NS(stream=stream, interrupt=AsyncMock())),
            )
        )
        await asyncio.wait_for(self.agent.turns.execute(self.message), 2)
        assert release.is_set()

    @pytest.mark.parametrize("group", (False, True))
    def test_stdio_tools_and_path_arguments(self, group):
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
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "view_image", "arguments": {"image_id": "image-id"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "view_image", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "view_image",
                    "arguments": {"image_id": "image-id", "path": "angry.png"},
                },
            },
        ]
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["image_tool.py", "socket", "session"]
                + (["--group", "token"] if group else []),
            ),
            patch.object(
                sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests)))
            ),
            patch.object(sys, "stdout", output),
            patch.object(image_tool, "call", return_value={"content": []}) as call,
        ):
            image_tool.main()
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        assert {tool["name"] for tool in responses[0]["result"]["tools"]} == {
            "list_images",
            "send_image",
        } | ({"view_image"} if group else set())
        assert call.call_args_list[0].args == (
            "socket",
            "session",
            "send_image",
            {"path": "animation.gif"},
        )
        assert call.call_args_list[1].args == ("socket", "session", "list_images", {})
        if group:
            assert call.call_args_list[2].args == (
                "socket",
                "session",
                "view_image",
                {"image_id": "image-id"},
            )
        else:
            assert "error" in responses[3]
        assert call.call_count == (3 if group else 2)
        assert all("error" in response for response in responses[4:])


def test_sticker_resources():
    catalog = stickers.load_stickers()
    assert catalog
    assert catalog["平静"] is None
    for name, data in catalog.items():
        if data is None:
            continue
        assert len(data) <= compress_stickers.BYTE_LIMIT
        with Image.open(io.BytesIO(data)) as picture:
            assert picture.width * picture.height <= compress_stickers.PIXEL_LIMIT
            if name in {"生气", "开心", "逗我呢", "让我想想"}:
                assert picture.convert("RGBA").getchannel("A").getextrema() == (0, 255)
            if name == "让我想想":
                assert isinstance(picture, GifImageFile)
                assert picture.n_frames == 8 and picture.info["loop"] == 0
                for index in range(picture.n_frames):
                    picture.seek(index)
                    assert picture.info["duration"] == 120


@pytest.mark.parametrize("linked", (False, True))
def test_sticker_config(tmp_path, linked):
    root = tmp_path / "package"
    root.symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "ok.png").write_bytes(PNG)
    (tmp_path / "link.png").symlink_to(tmp_path / "ok.png")
    (tmp_path / "bad.png").write_bytes(b"invalid")
    config = tmp_path / "sticker_config.json"
    with patch.object(stickers, "files", return_value=root if linked else tmp_path):
        for value in (
            "[]",
            '{"": "ok.png"}',
            '{"x": "ok.png", "x": "ok.png"}',
            '{"x": "missing.png"}',
            '{"x": "../ok.png"}',
            '{"x": "/ok.png"}',
            '{"x": "link.png"}',
            '{"x": "bad.png"}',
            '{"x": 1}',
            '{"x": "ok.png",}',
        ):
            config.write_text(value)
            with pytest.raises(ValueError):
                stickers.load_stickers()
        config.write_text('{"新增表情": "ok.png"}')
        loaded = stickers.load_stickers()
        assert loaded == {"新增表情": PNG}
        config.write_text("{}")
        assert "新增表情" in stickers.sticker_instructions(loaded)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_compress_images(tmp_path, monkeypatch, format):
    rng = random.Random(42)
    frames = [
        Image.frombytes("RGB", (80, 80), rng.randbytes(80 * 80 * 3)).convert("RGBA")
        for _ in range(3 if format == "GIF" else 1)
    ]
    for frame in frames:
        if format != "JPEG":
            alpha = Image.new("L", frame.size, 255)
            alpha.paste(0, (0, 0, 20, 80))
            if format != "GIF":
                alpha.paste(128, (20, 0, 40, 80))
            frame.putalpha(alpha)
    durations = [80, 120, 200] if format == "GIF" else [0]
    data = compress_stickers.encode_frames(frames, format, durations, 0, None)
    path = tmp_path / ("test." + format.lower())
    path.write_bytes(data)
    monkeypatch.setattr(compress_stickers, "BYTE_LIMIT", 5000)
    monkeypatch.setattr(compress_stickers, "PIXEL_LIMIT", 3600)
    before, after, _, size = compress_stickers.compress_image(path)
    assert before == len(data) and after <= 5000
    assert size[0] * size[1] <= 3600
    with Image.open(io.BytesIO(data)) as source:
        expected = []
        for index in range(getattr(source, "n_frames", 1)):
            source.seek(index)
            expected.append(
                compress_stickers.resize_frame(
                    source.convert("RGBA"), size, format == "GIF"
                )
            )
    compress_stickers.validate_output(
        path.read_bytes(), format, expected, durations, 0 if format == "GIF" else None
    )
    compressed = path.read_bytes()
    compress_stickers.compress_image(path)
    assert path.read_bytes() == compressed


def test_compress_failure(tmp_path):
    path = tmp_path / "test.png"
    Image.new("RGBA", (1000, 1000), (255, 0, 0, 128)).save(path)
    original = path.read_bytes()
    with patch.object(
        compress_stickers, "validate_output", side_effect=ValueError("invalid output")
    ):
        with pytest.raises(ValueError, match="invalid output"):
            compress_stickers.compress_image(path)
    assert path.read_bytes() == original
    link = tmp_path / "link.png"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular"):
        compress_stickers.compress_image(link)
