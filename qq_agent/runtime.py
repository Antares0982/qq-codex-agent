import asyncio
import contextlib
import json
import sys
import time

from openai_codex import ApprovalMode, CodexConfig, Sandbox
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse, TurnStatus

import image_tool

from .images import IMAGE_INSTRUCTIONS
from .logging import LOG, log_text
from .profiles import MEMBER_INSTRUCTIONS


class Runtime:
    def __init__(self, settings, db, codex, group_name):
        self.settings = settings
        self.db = db
        self.codex = codex
        self.group_name = group_name
        self.compactions = {}
        self.usage_ready = {}

    @contextlib.contextmanager
    def watch_codex(self):
        loop = asyncio.get_running_loop()
        router = self.codex._client._sync._router
        route = router.route_notification
        active = True

        def deliver(event):
            if active:
                self.observe_codex(event)

        def observe(event):
            route(event)
            if active and event.method in {
                "thread/tokenUsage/updated",
                "turn/started",
                "turn/completed",
                "error",
            }:
                loop.call_soon_threadsafe(deliver, event)

        router.route_notification = observe
        try:
            yield
        finally:
            active = False
            router.route_notification = route

    def observe_codex(self, event):
        payload = event.payload
        thread_id = getattr(payload, "thread_id", None)
        if event.method == "thread/tokenUsage/updated":
            tokens = payload.token_usage.last.total_tokens
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO context_usage VALUES (?, ?)",
                    (thread_id, tokens),
                )
            if thread_id in self.usage_ready:
                self.usage_ready[thread_id].set()
            return
        state = self.compactions.get(thread_id)
        if state is None:
            return
        if event.method == "turn/started":
            state["turn"] = payload.turn.id
        elif event.method == "error":
            if getattr(payload, "turn_id", None) == state["turn"] and not getattr(
                payload, "will_retry", False
            ):
                state["failed"] = True
                LOG.error(
                    "Compact error thread=%s error=%s", thread_id, log_text(payload)
                )
        elif (
            event.method == "turn/completed"
            and payload.turn.id == state["turn"]
            and not state["done"].done()
        ):
            state["done"].set_result(
                payload.turn.status == TurnStatus.completed
                and not payload.turn.error
                and not state["failed"]
            )

    async def context_tokens(self, thread_id):
        row = self.db.execute(
            "SELECT tokens FROM context_usage WHERE thread=?", (thread_id,)
        ).fetchone()
        if row:
            return row[0]
        ready = self.usage_ready.setdefault(thread_id, asyncio.Event())
        try:
            await asyncio.wait_for(ready.wait(), 2)
        except TimeoutError:
            LOG.warning("Context usage unavailable thread=%s", thread_id)
            return None
        finally:
            self.usage_ready.pop(thread_id, None)
        row = self.db.execute(
            "SELECT tokens FROM context_usage WHERE thread=?", (thread_id,)
        ).fetchone()
        return row[0] if row else None

    async def compact(self, thread):
        state = {
            "turn": None,
            "failed": False,
            "done": asyncio.get_running_loop().create_future(),
        }
        self.compactions[thread.id] = state
        started = time.monotonic()
        try:
            try:
                await thread.compact()
            except JsonRpcError as error:
                if state["turn"] is not None:
                    raise
                LOG.error(
                    "Compact rejected thread=%s error=%s", thread.id, log_text(error)
                )
                state["done"].set_result(False)
                return False
            result = await asyncio.shield(state["done"])
            LOG.info(
                "Compact completed thread=%s success=%s seconds=%.1f",
                thread.id,
                result,
                time.monotonic() - started,
            )
            return result
        finally:
            try:
                if not state["done"].done() and state["turn"] is not None:
                    async with asyncio.timeout(10):
                        await self.codex._client.turn_interrupt(
                            thread.id, state["turn"]
                        )
                        await asyncio.shield(state["done"])
                elif not state["done"].done():
                    raise RuntimeError("Compact start outcome unknown")
            except (Exception, asyncio.CancelledError) as error:
                LOG.error(
                    "Compact interrupt failed; runtime must restart: %s",
                    log_text(error),
                )
                raise SystemExit(1)
            finally:
                self.compactions.pop(thread.id, None)

    async def release_thread(self, thread_id):
        try:
            result = await asyncio.wait_for(
                self.codex._client.request(
                    "thread/unsubscribe",
                    {"threadId": thread_id},
                    response_model=ThreadUnsubscribeResponse,
                ),
                10,
            )
            LOG.info("Thread released thread=%s result=%s", thread_id, log_text(result))
        except Exception as error:
            LOG.error(
                "Thread release failed thread=%s error=%s", thread_id, log_text(error)
            )

    def selected_model(self, key):
        row = self.db.execute("SELECT model FROM models WHERE key=?", (key,)).fetchone()
        return row[0] if row else self.settings.model

    async def thread_options(self, message, folder):
        options = {
            "cwd": str(folder),
            "sandbox": Sandbox.workspace_write,
            "approval_mode": ApprovalMode.auto_review,
            "developer_instructions": IMAGE_INSTRUCTIONS
            + f"\n图片加工可使用已安装 Pillow 的 Python：{sys.executable}",
            "config": {
                "model_reasoning_effort": "medium",
                "model_auto_compact_token_limit": 100000,
                "model_auto_compact_token_limit_scope": "total",
                "projects": {str(folder): {"trust_level": "trusted"}},
                "mcp_servers": {
                    "qq_image": {
                        "command": sys.executable,
                        "args": [
                            image_tool.__file__,
                            str(self.settings.state_dir / "image.sock"),
                            folder.name,
                        ],
                        "required": True,
                        "default_tools_approval_mode": "approve",
                    }
                },
            },
        }
        agents_file = (
            self.settings.group_agents_file
            if "group_id" in message.target
            else self.settings.private_agents_file
        )
        if agents_file:
            instructions = agents_file.read_text()
            if "group_id" in message.target and "{nickname}" in instructions:
                nickname = await self.group_name(message.target, message.bot_id)
                instructions = instructions.replace("{nickname}", nickname)
            options["developer_instructions"] += "\n" + instructions
        model = self.selected_model(message.key)
        if model:
            options["model"] = model
        return options, model

    async def prepare_thread(self, message, thread_id, context, options):
        folder = context.folder
        if "group_id" in message.target:
            options["developer_instructions"] += (
                "\n"
                + MEMBER_INSTRUCTIONS
                + "\n"
                + json.dumps(context.profiles, ensure_ascii=False)
            )
            options["config"]["mcp_servers"]["qq_member"] = {
                "command": sys.executable,
                "args": [
                    image_tool.__file__,
                    str(self.settings.state_dir / "image.sock"),
                    folder.name,
                    "--members",
                    context.token,
                ],
                "required": True,
                "default_tools_approval_mode": "approve",
            }
        if thread_id:
            return await self.codex.thread_resume(thread_id, **options)
        return await self.codex.thread_start(**options)


def codex_config(settings):
    overrides = (
        'forced_login_method="chatgpt"',
        'cli_auth_credentials_store="file"',
        'approval_policy="on-request"',
        'approvals_reviewer="auto_review"',
        'model_reasoning_effort="medium"',
        "model_auto_compact_token_limit=100000",
        'model_auto_compact_token_limit_scope="total"',
        f'projects.{json.dumps(str(settings.workspace_dir))}.trust_level="trusted"',
        "project_root_markers=[]",
        f"mcp_servers.qq_image.command={json.dumps(sys.executable)}",
        f"mcp_servers.qq_image.args={json.dumps([image_tool.__file__, str(settings.state_dir / 'image.sock')])}",
        "mcp_servers.qq_image.required=true",
        'mcp_servers.qq_image.default_tools_approval_mode="approve"',
    )
    return CodexConfig(
        config_overrides=overrides,
        env={"CODEX_HOME": str(settings.state_dir / "codex")},
    )
