import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from openai_codex import ApprovalMode, CodexConfig, Sandbox
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import (
    ConfigReadResponse,
    MessagePhase,
    ReasoningEffort,
    SkillsExtraRootsSetResponse,
    ThreadInjectItemsResponse,
    ThreadUnsubscribeResponse,
    TurnStatus,
)

import image_tool
from image_assets import workspace_directory

from .logging import LOG, log_text
from .profiles import parse_profile
from .prompts import (
    ADMIN_IDENTITY,
    APPLICATION_LABEL,
    BOT_IDENTITY,
    COMMON_INSTRUCTIONS,
    GROUP_BASE_INSTRUCTIONS,
    GROUP_INSTRUCTIONS,
    GROUP_MEDIA_INSTRUCTIONS,
    GROUP_PROMPT_PREFIX,
    HANDOFF_INSTRUCTIONS,
    HANDOFF_LABEL,
    IMAGE_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
    MEMORY_LABEL,
    PILLOW_INSTRUCTIONS,
    PRIVATE_INSTRUCTIONS,
    PROFILE_CORRECTION,
    PROFILE_EMPTY_ERROR,
    PROFILE_INSTRUCTIONS,
    SNAPSHOT_PREFIX,
    TIME_INSTRUCTIONS,
)
from .stickers import load_stickers, sticker_instructions
from .transcript import Transcripts

BUILTIN_SKILLS = Path(__file__).with_name("skills")


HANDOFF_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "valid": {"type": "array", "items": {"type": "integer"}},
        "new": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["valid", "new"],
    "additionalProperties": False,
}


async def load_skills(codex):
    await codex._client.request(
        "skills/extraRoots/set",
        {"extraRoots": [str(BUILTIN_SKILLS.resolve())]},
        response_model=SkillsExtraRootsSetResponse,
    )


class Runtime:
    def __init__(self, settings, db, codex, group_name):
        self.settings = settings
        self.db = db
        self.codex = codex
        self.group_name = group_name
        self.stickers = load_stickers()
        self.compactions = {}
        self.image_inputs = {}
        self.image_compactions = set()
        self.usage_ready = {}
        self.transcripts = Transcripts(settings.state_dir)

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
                "item/completed",
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
        if event.method == "turn/completed":
            self.transcripts.completed(thread_id, payload.turn.id)
        if event.method == "item/completed":
            item = payload.item.root
            if item.type == "contextCompaction":
                with self.db:
                    self.db.execute(
                        "DELETE FROM group_snapshots WHERE thread=?", (thread_id,)
                    )
                    identity = (thread_id, item.id)
                    if identity not in self.image_compactions:
                        self.db.execute(
                            "DELETE FROM group_image_seen WHERE thread=?", (thread_id,)
                        )
                        self.image_compactions.add(identity)
            elif item.type == "userMessage":
                pending = self.image_inputs.get(thread_id, {})
                for content in item.content:
                    part = content.root
                    if part.type == "text" and part.text in pending:
                        with self.db:
                            self.db.executemany(
                                "INSERT OR IGNORE INTO group_image_seen VALUES (?, ?)",
                                [
                                    (thread_id, image_id)
                                    for image_id in pending.pop(part.text)
                                ],
                            )
            return
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

    def seen_images(self, thread_id):
        seen = {
            row[0]
            for row in self.db.execute(
                "SELECT image_id FROM group_image_seen WHERE thread=?", (thread_id,)
            )
        }
        for ids in self.image_inputs.get(thread_id, {}).values():
            seen.update(ids)
        return seen

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

    async def summarize(self, message, thread_id, folder, profiles=None):
        turn = reader = None
        starting = completed = False
        entries = None
        group = str(message.target["group_id"])
        instructions = HANDOFF_INSTRUCTIONS
        schema = HANDOFF_SCHEMA
        if profiles is not None:
            entries = profiles.entries(group)
            instructions = PROFILE_INSTRUCTIONS.format(
                start=message.start.isoformat(sep=" "),
                end=message.end.isoformat(sep=" "),
                entries=json.dumps(
                    {str(index): text for index, text in enumerate(entries, 1)},
                    ensure_ascii=False,
                ),
                limit=self.settings.groups[group].profile_max_chars,
            )
            schema = PROFILE_SCHEMA

        async def collect():
            nonlocal completed
            assert turn is not None
            final = fallback = None
            status = None
            failed = False
            async for event in turn.stream():
                payload = event.payload
                if event.method == "error" and not getattr(
                    payload, "will_retry", False
                ):
                    failed = True
                    LOG.error(
                        "Summary error thread=%s error=%s", thread_id, log_text(payload)
                    )
                elif event.method == "item/completed":
                    item = payload.item.root
                    if item.type == "agentMessage":
                        if item.phase == MessagePhase.final_answer:
                            final = item.text
                        elif item.phase is None:
                            fallback = item.text
                elif event.method == "turn/completed":
                    completed = True
                    status = payload.turn.status
                    failed = failed or bool(payload.turn.error)
                    LOG.info(
                        "Summary completed thread=%s turn=%s status=%s error=%s",
                        thread_id,
                        payload.turn.id,
                        status,
                        log_text(payload.turn.error),
                    )
            if not completed or status != TurnStatus.completed or failed:
                raise RuntimeError("交接总结未成功完成")
            text = final if final is not None else fallback
            if entries is not None:
                if text is None:
                    raise ValueError(PROFILE_EMPTY_ERROR)
                return parse_profile(
                    text, entries, self.settings.groups[group].profile_max_chars
                )
            result = json.loads(text or "")
            summary = (
                result["summary"]
                if isinstance(result, dict) and set(result) == {"summary"}
                else None
            )
            if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2000:
                raise ValueError("交接摘要须为 1 至 2000 字符")
            return summary.strip()

        try:
            async with asyncio.timeout(min(self.settings.task_timeout, 900)):
                if (await self.codex.account()).account is None:
                    raise RuntimeError("尚未登录")
                options, model = await self.thread_options(message, folder)
                thread = await self.codex.thread_resume(thread_id, **options)
                if profiles is not None:
                    await self.update_group(thread, message, profiles)
                for attempt in range(3 if profiles is not None else 1):
                    starting = True
                    completed = False
                    try:
                        turn = await thread.turn(
                            instructions if attempt == 0 else [],
                            model=model,
                            effort=ReasoningEffort.medium,
                            approval_mode=ApprovalMode.auto_review,
                            output_schema=schema,
                        )
                    except JsonRpcError:
                        starting = False
                        raise
                    starting = False
                    LOG.info(
                        "Summary started chat=%s thread=%s turn=%s model=%s attempt=%d",
                        message.key,
                        thread_id,
                        turn.id,
                        model or "default",
                        attempt + 1,
                    )
                    reader = asyncio.create_task(collect())
                    try:
                        result = await asyncio.shield(reader)
                    except (ValueError, TypeError, RecursionError) as error:
                        if profiles is None or attempt == 2:
                            raise
                        LOG.warning(
                            "Profile summary invalid chat=%s attempt=%d error=%s",
                            message.key,
                            attempt + 1,
                            log_text(error),
                        )
                        await self.inject(
                            thread, PROFILE_CORRECTION.format(reason=error)
                        )
                        continue
                    if profiles is not None:
                        profiles.apply(group, entries, result)
                    return result
        finally:
            try:
                if starting:
                    raise RuntimeError("Summary start outcome unknown")
                if turn is not None and not completed:
                    async with asyncio.timeout(10):
                        await turn.interrupt()
                        if reader is not None:
                            await asyncio.gather(
                                asyncio.shield(reader), return_exceptions=True
                            )
                        if not completed:
                            raise RuntimeError("Summary interrupt outcome unknown")
            except (Exception, asyncio.CancelledError) as error:
                LOG.error(
                    "Summary cleanup failed; runtime must restart: %s", log_text(error)
                )
                raise SystemExit(1)
            finally:
                if reader is not None:
                    if not reader.done():
                        reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
                await self.release_thread(thread_id)

    def selected_model(self, key):
        row = self.db.execute("SELECT model FROM models WHERE key=?", (key,)).fetchone()
        return row[0] if row else self.settings.model

    async def thread_options(self, message, folder):
        with workspace_directory(folder) as root:
            with contextlib.suppress(FileExistsError):
                os.mkdir(".agents", mode=0o700, dir_fd=root)
        with workspace_directory(folder, (".agents",)) as directory:
            with contextlib.suppress(FileExistsError):
                os.mkdir("skills", mode=0o700, dir_fd=directory)
        with workspace_directory(folder, (".agents", "skills")):
            skills = str(folder / ".agents/skills")
        config = await self.codex._client.request(
            "config/read", {"cwd": str(folder)}, response_model=ConfigReadResponse
        )
        sandbox = config.config.sandbox_workspace_write
        roots = (sandbox.writable_roots or []) if sandbox else []
        sandbox_options = sandbox.model_dump(exclude_none=True) if sandbox else {}
        sandbox_options["writable_roots"] = [*roots, skills]
        options = {
            "cwd": str(folder),
            "sandbox": Sandbox.workspace_write,
            "approval_mode": ApprovalMode.auto_review,
            "config": {
                "sandbox_workspace_write": sandbox_options,
                "model_reasoning_effort": "medium",
                "model_auto_compact_token_limit": self.settings.model_auto_compact_token_limit,
                "model_auto_compact_token_limit_scope": "total",
                "include_collaboration_mode_instructions": False,
                "features": {
                    "recommended_plugins": False,
                    "tool_suggest": False,
                    "multi_agent_v2": {
                        "enabled": True,
                        "root_agent_usage_hint_text": "",
                        "subagent_usage_hint_text": "",
                    },
                },
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
        model = self.selected_model(message.key)
        if model:
            options["model"] = model
        if "group_id" in message.target:
            options["config"]["mcp_servers"]["qq_image"]["args"].append("--group")
            options["config"]["web_search"] = "live"
            options["config"]["features"]["retain_client_developer_messages"] = True
        else:
            options["developer_instructions"] = await self.developer_instructions(
                message
            )
        return options, model

    async def developer_instructions(self, message):
        rules = IMAGE_INSTRUCTIONS + PILLOW_INSTRUCTIONS.format(
            executable=sys.executable
        )
        rules += "\n" + COMMON_INSTRUCTIONS
        if "group_id" in message.target:
            rules += TIME_INSTRUCTIONS.format(timezone=datetime.now().astimezone())
            rules += GROUP_MEDIA_INSTRUCTIONS
            rules += (
                "\n" + MEMORY_INSTRUCTIONS + "\n" + sticker_instructions(self.stickers)
            )
        else:
            rules += "\n" + PRIVATE_INSTRUCTIONS
        return rules

    async def prepare_thread(self, message, thread_id, context, options):
        folder = context.folder
        if "group_id" in message.target:
            options["config"]["mcp_servers"]["qq_image"]["args"].append(context.token)
        if thread_id:
            return await self.codex.thread_resume(thread_id, **options)
        if "group_id" in message.target:
            nickname = await self.group_name(message.target, message.bot_id)
            instructions = GROUP_BASE_INSTRUCTIONS + BOT_IDENTITY.format(
                bot_id=message.bot_id, nickname=nickname
            )
            instructions += "\n" + GROUP_INSTRUCTIONS
            prompt = self.db.execute(
                "SELECT prompt FROM group_prompts WHERE group_id=?",
                (str(message.target["group_id"]),),
            ).fetchone()
            if prompt and prompt[0]:
                instructions += GROUP_PROMPT_PREFIX + prompt[0]
            if self.settings.agent_admin_qq is not None:
                instructions += ADMIN_IDENTITY.format(
                    agent_admin_qq=self.settings.agent_admin_qq
                )
            options["base_instructions"] = instructions
        return await self.codex.thread_start(**options)

    async def update_group(self, thread, message, profiles):
        group = str(message.target["group_id"])
        handoff = self.db.execute(
            "SELECT summary FROM group_handoffs WHERE group_id=?", (group,)
        ).fetchone()
        instructions = SNAPSHOT_PREFIX + json.dumps(
            {
                APPLICATION_LABEL: await self.developer_instructions(message),
                MEMORY_LABEL: profiles.group_profile(group),
                HANDOFF_LABEL: handoff[0] if handoff else "",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        previous = self.db.execute(
            "SELECT instructions FROM group_snapshots WHERE thread=?", (thread.id,)
        ).fetchone()
        if previous and previous[0] == instructions:
            return
        await self.inject(thread, instructions)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO group_snapshots VALUES (?, ?)",
                (thread.id, instructions),
            )

    async def inject(self, thread, text):
        await self.codex._client.request(
            "thread/inject_items",
            {
                "threadId": thread.id,
                "items": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": text}],
                    }
                ],
            },
            response_model=ThreadInjectItemsResponse,
        )


def prepare_home(settings):
    (settings.state_dir / "codex").mkdir(parents=True, exist_ok=True, mode=0o700)
    if settings.resources_dir is None:
        return
    for relative in ("skills", "plugins/cache"):
        source = (settings.state_dir / "codex") / relative
        target = settings.resources_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_symlink():
            if source.resolve() != target.resolve():
                raise ValueError(f"资源链接冲突：{source}")
            continue
        if source.exists():
            if target.exists():
                raise ValueError(f"资源目录冲突：{source} → {target}")
            shutil.move(str(source), str(target))
        else:
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(target, target_is_directory=True)


def codex_config(settings):
    overrides = (
        'forced_login_method="chatgpt"',
        'cli_auth_credentials_store="ephemeral"'
        if settings.auth_socket
        else 'cli_auth_credentials_store="file"',
        'approval_policy="on-request"',
        'approvals_reviewer="auto_review"',
        'model_reasoning_effort="medium"',
        f"model_auto_compact_token_limit={settings.model_auto_compact_token_limit}",
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
