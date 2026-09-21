import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ImageGenerationThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)

import qq_codex_agent as app

PNG = b"\x89PNG\r\n\x1a\n" + b"sample"


def turn_done(status=TurnStatus.completed):
    return NS(
        method="turn/completed",
        payload=TurnCompletedNotification(
            thread_id="test-thread", turn=Turn(id="turn1", items=[], status=status)
        ),
    )


def item_done(item):
    return NS(
        method="item/completed",
        payload=ItemCompletedNotification(
            thread_id="test-thread",
            turn_id="turn1",
            item=ThreadItem(root=item),
            completed_at_ms=0,
        ),
    )


def event(user=1, group=None, mention=True, identifier=1, text="hello", reply=None):
    segments = [{"type": "text", "data": {"text": text}}]
    if reply is not None:
        segments.insert(0, {"type": "reply", "data": {"id": reply}})
    if mention:
        segments.append({"type": "at", "data": {"qq": "99"}})
    return {
        "post_type": "message",
        "self_id": 99,
        "user_id": user,
        "message_id": identifier,
        "message_type": "private" if group is None else "group",
        "group_id": group,
        "message": segments,
    }


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = app.Settings(
            {"1", "2"},
            {"10": {"1", "2"}, "11": {"1", "2"}},
            "ws://127.0.0.1:3001",
            root / "token",
            root / "state",
            root / "work",
            root / "AGENTS.md",
        )
        self.bot = NS(
            send=AsyncMock(),
            image=AsyncMock(return_value=PNG),
            reply_images=AsyncMock(return_value=[]),
        )
        self.codex = NS(account=AsyncMock(return_value=NS(account=object())))
        self.agent = app.Agent(self.settings, self.bot, self.codex)

    async def asyncTearDown(self):
        await asyncio.gather(*self.agent.controls, return_exceptions=True)
        self.agent.db.close()
        self.temp.cleanup()

    def test_admission(self):
        for candidate in (
            event(user=3),
            event(user=99),
            event(group=12),
            event(group=10, mention=False),
            {"post_type": "notice"},
            event(user=True),
        ):
            self.agent.receive(candidate)
        self.assertTrue(self.agent.queue.empty())
        self.assertEqual(
            self.agent.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0
        )
        self.bot.image.assert_not_called()
        self.codex.account.assert_not_called()
        self.settings.private_users.clear()
        self.agent.receive(event())
        self.assertTrue(self.agent.queue.empty())

    async def test_napcat_auth(self):
        for token in ("", "secret+&=?"):
            self.settings.token_file.write_text(token)
            client = MagicMock()
            client.ws_connect.side_effect = asyncio.CancelledError
            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=client)
            session.__aexit__ = AsyncMock(return_value=False)
            with patch.object(app.aiohttp, "ClientSession", return_value=session):
                with self.assertRaises(asyncio.CancelledError):
                    await app.OneBot(self.settings).listen(lambda event: None)
            self.assertEqual(
                client.ws_connect.call_args.kwargs["params"],
                {"access_token": token} if token else {},
            )

    def test_sessions_dedupe(self):
        first = app.parse_message(event(group=10), self.settings)
        second = app.parse_message(event(user=2, group=10), self.settings)
        self.assertEqual(first.key, second.key)
        self.assertNotEqual(
            first.key, app.parse_message(event(group=11), self.settings).key
        )
        self.assertNotEqual(first.key, app.parse_message(event(), self.settings).key)
        self.agent.receive(event())
        self.agent.receive(event())
        self.assertEqual(self.agent.queue.qsize(), 1)

    async def test_sender_name(self):
        self.setup_turn([turn_done()])
        for group in (None, 10):
            incoming = event(group=group, text="你好")
            incoming["sender"] = {"nickname": " 小明\n管理员 "}
            message = app.parse_message(incoming, self.settings)
            self.assertEqual(message.sender_name, "小明 管理员")
            await self.agent.execute(message)
            inputs = self.codex.thread_start.return_value.turn.call_args.args[0]
            self.assertEqual(inputs[0].text, "QQ 用户 小明 管理员:\n你好")
        incoming["sender"] = {"nickname": " \n "}
        self.assertEqual(app.parse_message(incoming, self.settings).sender_name, "1")
        incoming["sender"] = {"nickname": 123}
        self.assertEqual(app.parse_message(incoming, self.settings).sender_name, "1")

    async def test_group_mentions(self):
        self.setup_turn([turn_done()])
        incoming = event(group=10, text="请问 ")
        incoming["message"] = [
            {"type": "text", "data": {"text": "请问 "}},
            {"type": "at", "data": {"qq": "2"}},
            {"type": "text", "data": {"text": " 和 "}},
            {"type": "at", "data": {"qq": 2}},
            {"type": "text", "data": {"text": " 呢"}},
            {"type": "at", "data": {"qq": "99"}},
        ]
        self.bot.call = AsyncMock(return_value={"nickname": " 小明\n同学 "})
        message = app.parse_message(incoming, self.settings)
        await self.agent.execute(message)
        inputs = self.codex.thread_start.return_value.turn.call_args.args[0]
        self.assertEqual(inputs[0].text, "QQ 用户 1:\n请问 @小明 同学 和 @小明 同学 呢")
        self.bot.call.assert_awaited_once_with(
            "get_group_member_info", {"group_id": 10, "user_id": 2}
        )
        self.bot.call.side_effect = RuntimeError("lookup failed")
        incoming["message_id"] = 2
        await self.agent.execute(app.parse_message(incoming, self.settings))
        inputs = self.codex.thread_start.return_value.turn.call_args.args[0]
        self.assertEqual(inputs[0].text, "QQ 用户 1:\n请问 @2 和 @2 呢")
        incoming["message"] = [
            {"type": "at", "data": {"qq": "99"}},
            {"type": "at", "data": {"qq": "all"}},
        ]
        incoming["message_id"] = 3
        message = app.parse_message(incoming, self.settings)
        self.assertEqual(message.text, "@all")
        await self.agent.execute(message)
        inputs = self.codex.thread_start.return_value.turn.call_args.args[0]
        self.assertEqual(inputs[0].text, "QQ 用户 1:\n@全体成员")

    def test_reply_admission(self):
        message = app.parse_message(event(group=10, text="", reply="42"), self.settings)
        self.assertEqual(message.reply, "42")
        self.agent.receive(event(group=10, text="", reply="42"))
        self.assertEqual(self.agent.queue.qsize(), 1)

    def test_invalid_config(self):
        path = Path(self.temp.name) / "config.toml"
        path.write_text(
            'napcat_url="ws://127.0.0.1:3001"\ntoken_file="/token"\n'
            'state_dir="/state"\nworkspace_dir="/work"\nagents_file="/AGENTS.md"\n'
        )
        settings = app.Settings.load(path)
        self.assertEqual(settings.private_users, set())
        self.assertEqual(settings.groups, {})
        for value in ('"all"', "[true]", '["*"]', "[-1]"):
            path.write_text(
                path.read_text().split("private_users")[0] + f"private_users={value}\n"
            )
            with self.assertRaises((ValueError, TypeError)):
                app.Settings.load(path)

    async def test_queue_limits(self):
        self.settings.queue_limit = 1
        self.agent.queue = asyncio.Queue(1)
        self.agent.receive(event(identifier=1))
        self.agent.receive(event(identifier=2))
        await asyncio.gather(*self.agent.controls)
        self.assertEqual(self.agent.queue.qsize(), 1)
        self.assertIn("已满", self.bot.send.call_args.kwargs["text"])

    def test_external_allowlist(self):
        root = Path(self.temp.name)
        allowlist = root / "allowlist.toml"
        path = root / "config.toml"
        path.write_text(
            f'allowlist_file="{allowlist}"\nprivate_users=["999"]\n'
            'napcat_url="ws://127.0.0.1:3001"\ntoken_file="/token"\n'
            'state_dir="/state"\nworkspace_dir="/work"\nagents_file="/AGENTS.md"\n'
        )
        with self.assertRaises(FileNotFoundError):
            app.Settings.load(path)
        allowlist.write_text('private_users=["1"]\n[groups."10"]\nusers=["2", "all"]\n')
        self.assertEqual(app.Settings.load(path).private_users, {"1"})
        self.assertEqual(app.Settings.load(path).groups, {"10": {"2", "all"}})
        allowlist.write_text("")
        self.assertEqual(app.Settings.load(path).private_users, set())
        self.assertEqual(app.Settings.load(path).groups, {})
        for invalid in (
            "private_users=[true]",
            'private_users="all"',
            'private_users=["all"]',
            'model="other"',
            "allowed_users=[]",
            "allowed_groups=[]",
            "groups=[]",
            '[groups."10"]\nusers="all"',
            '[groups."10"]\nusers=["all", true]',
            '[groups."10"]\nusers=["*"]',
            '[groups."all"]\nusers=["all"]',
            '[groups."10"]\nother=[]',
            'groups={"10"=[]}',
            '[groups."01"]\nusers=[]\n[groups."1"]\nusers=[]',
        ):
            allowlist.write_text(invalid)
            with self.assertRaises(ValueError):
                app.Settings.load(path)

    def test_scoped_permissions(self):
        self.settings.private_users = {"1"}
        self.settings.groups = {"10": {"2"}, "11": {"all"}, "12": set()}
        for user, group, allowed in (
            (1, None, True),
            (2, None, False),
            (3, None, False),
            (1, 10, False),
            (2, 10, True),
            (3, 10, False),
            (1, 11, True),
            (2, 11, True),
            (3, 11, True),
            (2, 12, False),
            (3, 13, False),
            (99, 11, False),
        ):
            with self.subTest(user=user, group=group):
                self.assertEqual(
                    app.parse_message(event(user=user, group=group), self.settings)
                    is not None,
                    allowed,
                )
        self.assertIsNone(
            app.parse_message(event(group=11, mention=False), self.settings)
        )

    async def test_scoped_commands(self):
        self.settings.private_users = set()
        self.settings.groups = {"10": {"2"}, "11": {"all"}}
        for command in ("/help", "/status", "/model", "/model alpha", "/new", "/stop"):
            for candidate in (
                event(user=2, text=command),
                event(user=1, group=10, text=command),
                event(user=2, group=12, text=command),
                event(user=3, group=11, mention=False, text=command),
            ):
                self.agent.receive(candidate)
        self.assertFalse(self.agent.controls)
        self.assertTrue(self.agent.queue.empty())
        self.bot.send.assert_not_called()
        self.codex.account.assert_not_called()
        for user, group in ((2, 10), (3, 11)):
            self.agent.receive(event(user=user, group=group, text="/help"))
        await asyncio.gather(*self.agent.controls)
        self.assertEqual(self.bot.send.call_count, 2)

    async def test_help_dispatch(self):
        self.agent.receive(event(text="/help"))
        await asyncio.gather(*self.agent.controls)
        self.assertTrue(self.agent.queue.empty())
        self.assertIn("/model", self.bot.send.call_args.kwargs["text"])
        self.codex.account.assert_not_called()
        self.bot.send.reset_mock()
        self.agent.receive(event(user=3, text="/help", identifier=2))
        self.agent.receive(event(user=3, text="/model alpha", identifier=3))
        self.bot.send.assert_not_called()

    async def test_model_selection(self):
        from openai_codex.generated.v2_all import ModelListResponse

        response = ModelListResponse.model_validate(
            {
                "data": [
                    {
                        "id": name,
                        "model": name,
                        "displayName": name,
                        "description": "test",
                        "hidden": hidden,
                        "isDefault": False,
                        "defaultReasoningEffort": effort,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": effort, "description": "test"}
                        ],
                    }
                    for name, hidden, effort in (
                        ("alpha", False, "medium"),
                        ("beta", False, "medium"),
                        ("hidden", True, "medium"),
                        ("low-only", False, "low"),
                    )
                ]
            }
        )
        self.codex.models = AsyncMock(return_value=response)
        self.agent.receive(event(text="/model"))
        await asyncio.gather(*self.agent.controls)
        self.assertTrue(self.agent.queue.empty())
        text = self.bot.send.call_args.kwargs["text"]
        self.assertIn("alpha", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("low-only", text)
        for index, text in enumerate(
            ("/model hidden", "/model low-only", "/model alpha extra"), 2
        ):
            self.agent.receive(event(text=text, identifier=index))
            await asyncio.gather(*self.agent.controls)
            self.assertIsNone(self.agent.selected_model("private-1"))
        self.agent.receive(event(text="/model\talpha", identifier=5))
        await asyncio.gather(*self.agent.controls)
        self.assertEqual(self.agent.selected_model("private-1"), "alpha")
        self.assertIsNone(self.agent.selected_model("private-2"))
        self.agent.receive(event(text="/model beta", group=10, identifier=6))
        await asyncio.gather(*self.agent.controls)
        self.assertEqual(self.agent.selected_model("group-10"), "beta")
        self.assertIsNone(self.agent.selected_model("group-11"))
        self.agent.db.close()
        self.agent = app.Agent(self.settings, self.bot, self.codex)
        self.assertEqual(self.agent.selected_model("private-1"), "alpha")
        await self.agent.control(app.parse_message(event(text="/new"), self.settings))
        self.assertEqual(self.agent.selected_model("private-1"), "alpha")
        self.codex.models.side_effect = TimeoutError
        await self.agent.control(
            app.parse_message(event(text="/model beta"), self.settings)
        )
        self.assertEqual(self.agent.selected_model("private-1"), "alpha")

    async def test_reset_scope(self):
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions VALUES ('group-10', 'old-thread', 'missing-folder')"
            )
        self.agent.receive(event(group=10, identifier=1))
        self.agent.receive(event(group=11, identifier=2))
        command = app.parse_message(event(group=10, text="/new"), self.settings)
        await self.agent.control(command)
        self.assertEqual(self.agent.queue.qsize(), 1)
        self.assertEqual(self.agent.queue.get_nowait().key, "group-11")
        self.assertIsNone(self.agent.db.execute("SELECT key FROM sessions").fetchone())

    def setup_turn(self, events):
        async def stream():
            for item in events:
                yield item

        turn = NS(stream=stream, interrupt=AsyncMock())
        thread = NS(id="test-thread", turn=AsyncMock(return_value=turn))
        self.codex.thread_start = AsyncMock(return_value=thread)
        self.codex.thread_resume = AsyncMock(return_value=thread)
        return thread, turn

    async def test_images_resume(self):
        image = ImageGenerationThreadItem(
            id="image1",
            type="imageGeneration",
            status="completed",
            result=base64.b64encode(PNG).decode(),
        )
        answer = AgentMessageThreadItem(
            id="text1",
            type="agentMessage",
            phase=MessagePhase.final_answer,
            text="done",
        )
        notifications = [item_done(item) for item in (image, image, answer)]
        notifications.append(turn_done())
        thread, turn = self.setup_turn(notifications)
        with self.agent.db:
            self.agent.db.execute("INSERT INTO models VALUES ('private-1', 'alpha')")
        message = app.parse_message(event(), self.settings)
        message.images = ["image-id"]
        await self.agent.execute(message)
        inputs = thread.turn.call_args.args[0]
        self.assertEqual(
            thread.turn.call_args.kwargs["effort"], app.ReasoningEffort.medium
        )
        self.assertEqual(thread.turn.call_args.kwargs["model"], "alpha")
        self.assertEqual(self.codex.thread_start.call_args.kwargs["model"], "alpha")
        self.assertIsInstance(inputs[1], app.LocalImageInput)
        self.assertEqual(Path(inputs[1].path).read_bytes(), PNG)
        image_calls = [
            call for call in self.bot.send.call_args_list if "image" in call.kwargs
        ]
        self.assertEqual(image_calls, [])
        self.assertEqual(self.bot.send.call_args.kwargs["text"], "done")
        self.assertEqual(
            self.codex.thread_start.call_args.kwargs["approval_mode"],
            app.ApprovalMode.auto_review,
        )
        with self.agent.db:
            self.agent.db.execute(
                "UPDATE models SET model='beta' WHERE key='private-1'"
            )
        await self.agent.execute(message)
        self.codex.thread_resume.assert_awaited_once()
        self.assertEqual(self.codex.thread_resume.call_args.kwargs["model"], "beta")
        self.assertEqual(thread.turn.call_args.kwargs["model"], "beta")
        self.assertEqual(
            thread.turn.call_args.kwargs["effort"], app.ReasoningEffort.medium
        )
        turn.interrupt.assert_not_called()

    async def test_reply_image(self):
        self.setup_turn([turn_done()])
        self.bot.reply_images.return_value = ["quoted-image"]
        message = app.parse_message(
            event(group=10, text="解释图片", reply="42"), self.settings
        )
        await self.agent.execute(message)
        self.bot.reply_images.assert_awaited_once_with("42", {"group_id": 10})
        self.bot.image.assert_awaited_once_with("quoted-image")
        inputs = self.codex.thread_start.return_value.turn.call_args.args[0]
        self.assertIsInstance(inputs[1], app.LocalImageInput)

    async def test_empty_reply(self):
        message = app.parse_message(event(group=10, text="", reply="42"), self.settings)
        await self.agent.execute(message)
        self.assertIn("没有可读取", self.bot.send.call_args.kwargs["text"])
        self.assertFalse(hasattr(self.codex, "thread_start"))

    async def test_thread_idle(self):
        self.setup_turn([turn_done()])
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions VALUES ('private-1', 'old-thread', 'folder')"
            )
            self.agent.db.execute(
                "INSERT INTO activity VALUES ('private-1', 1000, 0, 0)"
            )
        with patch.object(app.time, "time", return_value=1000 + app.THREAD_IDLE + 1):
            self.agent.receive(event(identifier=2))
        message = self.agent.queue.get_nowait()
        await self.agent.execute(message)
        self.codex.thread_resume.assert_not_awaited()
        self.codex.thread_start.assert_awaited_once()
        self.assertEqual(
            self.agent.db.execute(
                "SELECT thread, folder FROM sessions WHERE key='private-1'"
            ).fetchone(),
            ("test-thread", "folder"),
        )
        self.assertEqual(
            [call.kwargs["text"] for call in self.bot.send.call_args_list],
            [
                "距上一条消息已超过两小时，已新建一个 thread。",
                "开始处理……",
                "任务完成。",
            ],
        )

    async def test_thread_idle_group_silent(self):
        self.setup_turn([turn_done()])
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions VALUES ('group-10', 'old-thread', 'folder')"
            )
            self.agent.db.execute(
                "INSERT INTO activity VALUES ('group-10', 1000, 0, 0)"
            )
        with patch.object(app.time, "time", return_value=1000 + app.THREAD_IDLE + 1):
            self.agent.receive(event(group=10, identifier=2))
        await self.agent.execute(self.agent.queue.get_nowait())
        self.codex.thread_resume.assert_not_awaited()
        self.codex.thread_start.assert_awaited_once()
        self.assertEqual(
            [call.kwargs for call in self.bot.send.call_args_list],
            [{"text": "任务完成。"}],
        )

    async def test_group_silence(self):
        image = ImageGenerationThreadItem(
            id="image1",
            type="imageGeneration",
            status="completed",
            result=base64.b64encode(PNG).decode(),
        )
        commentary = AgentMessageThreadItem(
            id="text1",
            type="agentMessage",
            phase=MessagePhase.commentary,
            text="thinking",
        )
        first = AgentMessageThreadItem(
            id="text2",
            type="agentMessage",
            phase=MessagePhase.final_answer,
            text="first",
        )
        last = AgentMessageThreadItem(
            id="text3",
            type="agentMessage",
            phase=MessagePhase.final_answer,
            text="last",
        )
        progress = NS(method="item/started", payload=NS(item=ThreadItem(root=image)))
        self.setup_turn(
            [
                progress,
                *(item_done(i) for i in (commentary, image, first, last)),
                turn_done(),
            ]
        )
        self.agent.receive(event(group=10))
        message = self.agent.queue.get_nowait()
        self.agent.mark(message, "running")
        await self.agent.execute(message)
        self.assertEqual(
            [call.kwargs for call in self.bot.send.call_args_list],
            [{"text": "last"}],
        )
        self.assertEqual(
            self.agent.db.execute(
                "SELECT status FROM messages WHERE id=?", (message.identifier,)
            ).fetchone()[0],
            "completed",
        )
        self.bot.send.reset_mock()
        await self.agent.execute(app.parse_message(event(), self.settings))
        texts = [
            c.kwargs["text"] for c in self.bot.send.call_args_list if "text" in c.kwargs
        ]
        self.assertEqual(texts, ["开始处理……", "正在生成图片……", "last"])

    async def test_progress_once(self):
        kinds = (
            "commandExecution",
            "imageGeneration",
            "webSearch",
        )
        progress = [
            NS(method="item/started", payload=NS(item=NS(root=NS(type=kind))))
            for kind in kinds
            for _ in range(3)
        ]
        self.setup_turn([*progress, turn_done()])
        expected = [
            "开始处理……",
            "正在执行代码……",
            "正在生成图片……",
            "正在检索资料……",
            "任务完成。",
        ]
        for identifier in (1, 2):
            await self.agent.execute(
                app.parse_message(event(identifier=identifier), self.settings)
            )
        self.assertEqual(
            [call.kwargs["text"] for call in self.bot.send.call_args_list],
            expected * 2,
        )

    async def test_image_tool(self):
        ready = asyncio.Event()
        finish = asyncio.Event()
        image = ImageGenerationThreadItem(
            id="image1",
            type="imageGeneration",
            status="completed",
            result=base64.b64encode(PNG).decode(),
        )

        async def stream():
            yield item_done(image)
            ready.set()
            await finish.wait()
            yield turn_done()

        turn = NS(stream=stream, interrupt=AsyncMock())
        thread = NS(id="test-thread", turn=AsyncMock(return_value=turn))
        self.codex.thread_start = AsyncMock(return_value=thread)
        self.codex.thread_resume = AsyncMock(return_value=thread)
        socket_path = self.settings.state_dir / "image.sock"
        server = await asyncio.start_unix_server(self.agent.send_image, path=socket_path)
        task = asyncio.create_task(
            self.agent.execute(app.parse_message(event(group=10), self.settings))
        )
        try:
            await ready.wait()
            self.bot.send.assert_not_awaited()
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(Path(app.__file__).with_name("image_tool.py")),
                str(socket_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            calls = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "send_image", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "send_image", "arguments": {}}},
            ]
            output, _ = await process.communicate(
                "".join(json.dumps(call) + "\n" for call in calls).encode()
            )
            responses = [json.loads(line) for line in output.splitlines()]
            self.assertEqual(process.returncode, 0)
            self.assertEqual(responses[1]["result"]["tools"][0]["name"], "send_image")
            self.assertNotIn("isError", responses[2]["result"])
            self.assertTrue(responses[3]["result"]["isError"])
            self.bot.send.assert_awaited_once()
            self.assertEqual(self.bot.send.call_args.kwargs, {"image": PNG})
        finally:
            finish.set()
            await task
            server.close()
            await server.wait_closed()

    async def test_thread_status(self):
        message = app.parse_message(event(group=10, text="/status"), self.settings)
        with patch.object(app.AsyncThread, "read", new_callable=AsyncMock) as read:
            await self.agent.control(message)
            read.assert_not_called()
            self.assertIn("用户消息：0 条", self.bot.send.call_args.kwargs["text"])
            with self.agent.db:
                self.agent.db.execute(
                    "INSERT INTO sessions VALUES ('group-10', 'thread1', 'folder')"
                )
            items = [
                ThreadItem.model_validate(
                    {"type": "userMessage", "id": name, "content": []}
                )
                for name in ("user1", "user2")
            ]
            items.append(
                ThreadItem.model_validate(
                    {"type": "agentMessage", "id": "answer", "text": "done"}
                )
            )
            read.return_value = NS(
                thread=NS(
                    name="图片测试",
                    turns=[
                        Turn(id="turn1", items=items, status=TurnStatus.completed),
                    ],
                )
            )
            self.agent.receive(event(group=10, identifier=2))
            await self.agent.control(message)
            read.assert_awaited_once_with(include_turns=True)
            text = self.bot.send.call_args.kwargs["text"]
            self.assertIn("图片测试", text)
            self.assertIn("用户消息：2 条", text)
            read.return_value.thread.name = None
            await self.agent.control(message)
            self.assertIn("未命名", self.bot.send.call_args.kwargs["text"])
            read.side_effect = TimeoutError
            await self.agent.control(message)
            self.assertIn("暂时无法读取", self.bot.send.call_args.kwargs["text"])
            await self.agent.control(
                app.parse_message(event(group=10, text="/new"), self.settings)
            )
            read.reset_mock()
            await self.agent.control(message)
            read.assert_not_called()
            self.assertIn("用户消息：0 条", self.bot.send.call_args.kwargs["text"])

    async def test_interrupt_failure(self):
        _, turn = self.setup_turn([])
        message = app.parse_message(event(), self.settings)
        await self.agent.execute(message)
        turn.interrupt.assert_awaited_once()
        self.assertIn("未完成", self.bot.send.call_args.kwargs["text"])

    async def test_timeout(self):
        self.settings.task_timeout = 0.02

        async def stream():
            await asyncio.sleep(10)
            yield None

        turn = NS(stream=stream, interrupt=AsyncMock())
        self.codex.thread_start = AsyncMock(
            return_value=NS(id="t", turn=AsyncMock(return_value=turn))
        )
        with self.assertRaises(TimeoutError):
            await self.agent.execute(app.parse_message(event(), self.settings))
        turn.interrupt.assert_awaited_once()

    async def test_no_login(self):
        self.codex.account.return_value = NS(account=None)
        await self.agent.execute(app.parse_message(event(), self.settings))
        self.assertIn("设备码", self.bot.send.call_args.kwargs["text"])
        self.bot.image.assert_not_called()

    async def test_image_validation(self):
        bot = app.OneBot(self.settings)
        bot.call = AsyncMock(return_value={"url": "https://127.0.0.1/secret"})
        with self.assertRaises(ValueError):
            await bot.image("id")
        bot.call.return_value = {"file": "/etc/passwd"}
        with self.assertRaises(ValueError):
            await bot.image("id")
        bot.call.return_value = {"base64": base64.b64encode(b"not an image").decode()}
        with self.assertRaises(ValueError):
            await bot.image("id")
        bot.call.return_value = {"base64": base64.b64encode(PNG).decode()}
        self.assertEqual(await bot.image("id"), PNG)

    async def test_reply_images(self):
        bot = app.OneBot(self.settings)
        bot.call = AsyncMock(
            return_value={
                "message_type": "group",
                "group_id": 10,
                "message": [
                    {"type": "text", "data": {"text": "caption"}},
                    {"type": "image", "data": {"file": "image-id"}},
                ],
            }
        )
        self.assertEqual(await bot.reply_images("42", {"group_id": 10}), ["image-id"])
        bot.call.return_value["group_id"] = 11
        with self.assertRaises(ValueError):
            await bot.reply_images("42", {"group_id": 10})

    async def test_action_response(self):
        bot = app.OneBot(self.settings)

        async def send(packet):
            bot.pending[packet["echo"]].set_result(
                {"status": "ok", "retcode": 0, "data": {"ok": True}}
            )

        bot.ws = NS(closed=False, send_json=send)
        self.assertEqual(await bot.call("get_status", {}), {"ok": True})
        self.assertFalse(bot.pending)

    async def test_stop_running(self):
        entered = asyncio.Event()

        async def stream():
            entered.set()
            await asyncio.sleep(10)
            yield None

        turn = NS(stream=stream, interrupt=AsyncMock())
        self.codex.thread_start = AsyncMock(
            return_value=NS(id="t", turn=AsyncMock(return_value=turn))
        )
        self.agent.receive(event(group=10))
        worker = asyncio.create_task(self.agent.work())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.agent.control(
                app.parse_message(event(group=10, text="/stop"), self.settings)
            )
            turn.interrupt.assert_awaited_once()
            self.assertEqual(
                [call.kwargs for call in self.bot.send.call_args_list],
                [{"text": "已停止本会话任务并清空队列。"}],
            )
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_restart_persistence(self):
        self.agent.receive(event())
        with self.agent.db:
            self.agent.db.execute(
                "INSERT INTO sessions VALUES ('private-1', 'thread-old', 'folder')"
            )
        self.agent.db.close()
        self.agent = app.Agent(self.settings, self.bot, self.codex)
        self.agent.receive(event())
        self.assertTrue(self.agent.queue.empty())
        self.assertEqual(
            self.agent.db.execute("SELECT thread FROM sessions").fetchone()[0],
            "thread-old",
        )
        self.assertEqual(
            self.agent.db.execute("SELECT status FROM messages").fetchone()[0],
            "interrupted",
        )

    async def test_runtime_error(self):
        _, turn = self.setup_turn([turn_done(TurnStatus.failed)])
        await self.agent.execute(app.parse_message(event(), self.settings))
        turn.interrupt.assert_not_called()
        self.assertIn("额度", self.bot.send.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
