import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, TextInput
from openai_codex.generated.v2_all import (
    CommandExecResponse,
    ReasoningEffort,
    ThreadInjectItemsResponse,
    ThreadResumeResponse,
    ThreadStartResponse,
    TurnStatus,
)

from qq_agent.config import GroupSettings, Settings
from qq_agent.logging import read_diagnostics
from qq_agent.messages import ImageTurn, Message
from qq_agent.images import Images
from qq_agent.profiles import Profiles
from qq_agent.runtime import (
    GROUP_BASE_INSTRUCTIONS,
    HANDOFF_INSTRUCTIONS,
    HANDOFF_SCHEMA,
    Runtime,
    load_skills,
)
from qq_agent.storage import open_database
from test_agent import PNG


@pytest.mark.parametrize("target", [{}, {"group_id": 10}])
async def test_skill_permissions(tmp_path, target):
    settings = Settings(
        set(),
        {},
        "ws://127.0.0.1:3001",
        tmp_path / "token",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "AGENTS.md",
    )
    db = open_database(settings)
    workspace = settings.workspace_dir
    cache = tmp_path / "cache"
    cache.mkdir()
    (tmp_path / "codex").mkdir()
    for name in (".git", ".codex"):
        (workspace / name).mkdir()
    config = CodexConfig(
        # Legacy sandbox policies use the server cwd.
        cwd=str(workspace),
        env={"CODEX_HOME": str(tmp_path / "codex")},
        config_overrides=(
            "features.shell_snapshot=false",
            f"sandbox_workspace_write.writable_roots={json.dumps([str(cache)])}",
            "sandbox_workspace_write.network_access=true",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        ),
    )
    try:
        async with AsyncCodex(config=config) as codex:
            assert (await codex.account()).account is None
            runtime = Runtime(settings, db, codex, AsyncMock())
            message = NS(key="probe", target=target)
            thread_id = None
            for method, response in (
                ("thread/start", ThreadStartResponse),
                ("thread/resume", ThreadResumeResponse),
            ):
                options, _ = await runtime.thread_options(message, workspace)
                assert options["config"]["features"]["recommended_plugins"] is False
                assert options["config"]["features"]["tool_suggest"] is False
                options["config"]["mcp_servers"] = {}
                params = {
                    "cwd": str(workspace),
                    "sandbox": "workspace-write",
                    "config": options["config"],
                }
                if thread_id:
                    params["threadId"] = thread_id
                started = await codex._client.request(
                    method, params, response_model=response
                )
                thread_id = started.thread.id
                policy = started.sandbox.model_dump(mode="json", by_alias=True)
                assert policy["networkAccess"] is True
                assert policy["excludeSlashTmp"] is True
                assert policy["excludeTmpdirEnvVar"] is True
                assert set(policy["writableRoots"]) == {
                    str(cache),
                    str(workspace / ".agents/skills"),
                }
                result = await codex._client.request(
                    "command/exec",
                    {
                        "cwd": str(workspace),
                        "sandboxPolicy": policy,
                        "command": [
                            "/bin/sh",
                            "-ec",
                            "printf ok > workspace-probe; "
                            "mkdir -p .agents/skills/probe; "
                            "printf ok > .agents/skills/probe/SKILL.md; "
                            'test "$(cat .agents/skills/probe/SKILL.md)" = ok; '
                            "for path in .agents .git .codex; do "
                            'if (printf probe > "$path/write-probe"); then '
                            'echo "Unexpectedly writable: $path" >&2; exit 1; fi; done',
                        ],
                    },
                    response_model=CommandExecResponse,
                )
                assert result.exit_code == 0, result.stderr
                await codex._client.request(
                    "thread/inject_items",
                    {
                        "threadId": thread_id,
                        "items": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "probe"}],
                            }
                        ],
                    },
                    response_model=ThreadInjectItemsResponse,
                )
    finally:
        db.close()


@pytest.mark.parametrize(
    "mode", ["plain", "manual", "pre_turn", "mid_turn", "local", "upgrade"]
)
async def test_runtime_prompts(tmp_path, mode):
    requests = []
    compactions = []
    local_compact = False
    tool_sent = False
    diagnose_failure = False
    handoff_response = False

    async def respond(request):
        nonlocal tool_sent
        body = await request.json()
        if diagnose_failure:
            return web.json_response(
                {"detail": "Bad Request"},
                status=400,
                headers={"x-oai-request-id": "req_failure"},
            )
        compacting = local_compact or any(
            item.get("type") == "compaction_trigger" for item in body["input"]
        )
        if compacting:
            compactions.append(body)
        else:
            requests.append(body)
        call_tool = (
            mode == "mid_turn"
            and len(requests) == 2
            and not tool_sent
            and not compacting
        )
        if call_tool:
            tool_sent = True
        tokens = (
            250000
            if call_tool
            or (mode == "pre_turn" and len(requests) == 1 and not compacting)
            else 1
        )
        events = [
            {"type": "response.created", "response": {"id": "probe"}},
            {
                "type": "response.completed",
                "response": {
                    "id": "probe",
                    "usage": {
                        "input_tokens": tokens,
                        "output_tokens": 0,
                        "total_tokens": tokens,
                    },
                },
            },
        ]
        if handoff_response and not compacting:
            events.insert(
                1,
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "handoff-answer",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"summary":"HANDOFF_BACKGROUND"}',
                            }
                        ],
                    },
                },
            )
        elif compacting and mode != "local":
            events.insert(
                1,
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "compaction",
                        "encrypted_content": "probe-compact",
                    },
                },
            )
        elif call_tool:
            events.insert(
                1,
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "name": "exec_command",
                        "call_id": "probe-call",
                        "arguments": json.dumps({"cmd": "pwd"}),
                    },
                },
            )
        return web.Response(
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            content_type="text/event-stream",
            headers={"x-oai-request-id": "req_probe"},
        )

    app = web.Application()
    app.router.add_post("/responses", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    settings = Settings(
        set(),
        {"10": GroupSettings()},
        "ws://127.0.0.1:3001",
        tmp_path / "token",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "AGENTS.md",
        agent_admin_qq="123456789",
        group_agents_file=tmp_path / "group.md",
        model_auto_compact_token_limit=210000,
    )
    group_template = tmp_path / "group.md"
    group_template.write_text("GROUP_TEMPLATE_ALPHA")
    db = open_database(settings)
    home = settings.state_dir / "codex"
    home.mkdir()
    workspace = settings.workspace_dir
    skill = workspace / ".agents/skills/probe/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: prompt-probe\ndescription: Local prompt probe.\n---\nRead local files.\n"
    )
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-5.4",
                        "display_name": "Prompt probe",
                        "supported_reasoning_levels": [
                            {"effort": "medium", "description": "Probe"}
                        ],
                        "shell_type": "unified_exec",
                        "visibility": "list",
                        "supported_in_api": True,
                        "priority": 1,
                        "support_verbosity": False,
                        "truncation_policy": {"mode": "tokens", "limit": 10000},
                        "experimental_supported_tools": [],
                        "context_window": 272000,
                        "include_skills_usage_instructions": True,
                        "model_messages": {
                            "instructions_template": "Local probe.",
                            "collaboration_modes": {"default": "CATALOG_MODE_MARKER"},
                        },
                    }
                ]
            }
        )
    )
    config = CodexConfig(
        env={"CODEX_HOME": str(home)},
        config_overrides=(
            'model="gpt-5.4"',
            f"model_catalog_json={json.dumps(str(catalog))}",
            'model_provider="probe"',
            'model_providers.probe.name="Local probe"'
            if mode == "local"
            else 'model_providers.probe.name="OpenAI"',
            f'model_providers.probe.base_url="http://127.0.0.1:{port}"',
            'model_providers.probe.wire_api="responses"',
            "model_providers.probe.requires_openai_auth=false",
            "model_providers.probe.supports_websockets=false",
            "features.shell_snapshot=false",
        ),
    )
    message = Message(
        "group-10",
        "probe",
        {"group_id": 10},
        "probe",
        "hello",
        [("text", "hello")],
        [],
        None,
        False,
        "99",
        "1",
    )
    profiles = Profiles(db, {}, AsyncMock(), NS(call=AsyncMock()))
    codex = AsyncCodex(config=config)
    try:
        async with asyncio.timeout(45):
            assert (await codex.account()).account is None
            await load_skills(codex)
            runtime = Runtime(settings, db, codex, AsyncMock(return_value="bot"))
            thread = None
            for index, prompt in enumerate(
                ("PROMPT_ALPHA", "PROMPT_ALPHA", "PROMPT_ALPHA", "", "")
                if mode == "upgrade"
                else ("PROMPT_ALPHA", "PROMPT_BETA", "PROMPT_BETA", "", "")
            ):
                if index == 1:
                    settings.agent_admin_qq = "987654321"
                    settings.group_agents_file = group_template
                    group_template.write_text("GROUP_TEMPLATE_BETA")
                if index == 2:
                    await runtime.transcripts.close()
                    await codex.close()
                    db.close()
                    db = open_database(settings)
                    profiles = Profiles(db, {}, AsyncMock(), NS(call=AsyncMock()))
                    codex = AsyncCodex(config=config)
                    assert (await codex.account()).account is None
                    await load_skills(codex)
                    runtime = Runtime(
                        settings, db, codex, AsyncMock(return_value="bot")
                    )
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO group_prompts VALUES ('10', ?, '1', 0)",
                        (prompt,),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO group_profiles VALUES ('10', ?, 0)",
                        (f"GROUP_MEMORY_{min(index, 1)}" if prompt else "",),
                    )
                    db.execute("DELETE FROM member_profiles")
                    if prompt:
                        db.execute(
                            "INSERT INTO member_profiles VALUES ('10', '1', 'Member', ?, 0)",
                            (json.dumps({"兴趣": "MEMBER_MEMORY"}),),
                        )
                options, model = await runtime.thread_options(message, workspace)
                if mode == "upgrade" and index == 0:
                    options["developer_instructions"] = "LEGACY_APPLICATION_RULES"
                    settings.agent_admin_qq = None
                    settings.group_agents_file = None
                    with db:
                        db.execute("DELETE FROM group_prompts WHERE group_id='10'")
                context = ImageTurn(message, workspace)
                if index != 1:
                    thread = await runtime.prepare_thread(
                        message, thread.id if thread else None, context, options
                    )
                assert thread is not None
                if mode == "upgrade" and index == 0:
                    with db:
                        db.execute(
                            "INSERT INTO group_snapshots VALUES (?, ?)",
                            (thread.id, "legacy group-only snapshot"),
                        )
                if index == 2 and mode == "local":
                    local_compact = True
                    with runtime.watch_codex():
                        assert await runtime.compact(thread)
                    local_compact = False
                with patch.object(
                    codex._client, "request", wraps=codex._client.request
                ) as rpc:
                    if mode != "upgrade" or index != 0:
                        await runtime.update_group(thread, message, profiles)
                    injected = [
                        call
                        for call in rpc.call_args_list
                        if call.args[0] == "thread/inject_items"
                    ]
                    assert len(injected) == int(
                        (index == 0 and mode != "upgrade")
                        or index in {1, 3}
                        or (index == 2 and mode not in {"plain", "upgrade"})
                    )
                    rpc.reset_mock()
                    if mode != "upgrade" or index != 0:
                        await runtime.update_group(thread, message, profiles)
                    rpc.assert_not_called()
                if index == 1 and mode == "manual":
                    with runtime.watch_codex():
                        assert await runtime.compact(thread)
                with db:
                    db.execute(
                        "INSERT OR IGNORE INTO received_images (id,key,source,created) VALUES ('probe-image','group-10','source',strftime('%s','now'))"
                    )
                images = Images(
                    db, NS(image=AsyncMock(return_value=PNG)), {message.key: context}
                )
                text = "群聊消息记录：\n" + json.dumps(
                    {
                        "messages": [
                            {
                                "message_id": f"probe-{index}",
                                "content": [{"image_id": "probe-image"}],
                            }
                        ]
                    }
                )
                inputs, image_ids = await images.attach_images(
                    context,
                    text,
                    runtime.seen_images(thread.id),
                    settings.group_image_limit,
                )
                expected_image = (
                    index == 0
                    or (index == 1 and mode == "manual")
                    or (index == 2 and mode in {"pre_turn", "mid_turn", "local"})
                )
                assert bool(image_ids) == expected_image
                if image_ids:
                    runtime.image_inputs.setdefault(thread.id, {})[text] = image_ids
                with runtime.watch_codex():
                    turn = await thread.turn(
                        inputs,
                        approval_mode=ApprovalMode.auto_review,
                        model=model,
                        effort=ReasoningEffort.medium,
                    )
                    completed = False
                    async for event in turn.stream():
                        if event.method == "turn/completed":
                            assert event.payload.turn.status == TurnStatus.completed
                            completed = True
                assert completed
                await asyncio.sleep(0)
                assert not runtime.image_inputs.get(thread.id)
                assert runtime.seen_images(thread.id) == (
                    set()
                    if index == 1 and mode in {"pre_turn", "mid_turn"}
                    else {"probe-image"}
                )
                if expected_image:
                    content = next(
                        item["content"]
                        for item in requests[-1]["input"]
                        if item.get("role") == "user"
                        and any(part.get("text") == text for part in item["content"])
                    )
                    image_index = next(
                        i
                        for i, part in enumerate(content)
                        if part["type"] == "input_image"
                    )
                    assert "probe-image" in content[image_index - 1]["text"]
                    assert content[image_index]["image_url"].startswith("data:image/")
                await runtime.transcripts.close()
                saved = (settings.state_dir / "latest-transcript.md").read_text()
                assert turn.id in saved and "请求 token 统计" in saved
                usage = json.loads(
                    saved.split("## 请求 token 统计", 1)[1]
                    .split("```json\n", 1)[1]
                    .split("\n```", 1)[0]
                )
                assert usage[-1]["usage"]["input_tokens"] == (
                    250000 if mode == "pre_turn" and index == 0 else 1
                )
                assert len(usage) == (
                    3
                    if mode == "mid_turn" and index == 1
                    else 2
                    if mode == "pre_turn" and index == 1
                    else 1
                )
                if mode in {"plain", "upgrade"} and index:
                    assert usage[-1]["input_tokens_delta"] == 0
                if index != 0:
                    await runtime.release_thread(thread.id)
            assert bool(compactions) == (mode not in {"plain", "upgrade"})
            assert len(requests) == (6 if mode == "mid_turn" else 5)
            names = {tool.get("name", tool["type"]) for tool in requests[0]["tools"]}
            assert {
                "exec_command",
                "web_search",
                "mcp__qq_image",
                "mcp__qq_member",
            } <= names
            member_tools = next(
                tool
                for tool in requests[0]["tools"]
                if tool.get("name") == "mcp__qq_member"
            )
            assert {
                "list_profiles",
                "replace_profile",
                "get_group_profile",
                "replace_group_profile",
            } <= {tool["name"] for tool in member_tools["tools"]}
            update_counts = []
            image_tools = next(
                tool
                for tool in requests[0]["tools"]
                if tool.get("name") == "mcp__qq_image"
            )
            assert "view_image" in {tool["name"] for tool in image_tools["tools"]}
            for index, request in enumerate(requests):
                assert request["instructions"].startswith(GROUP_BASE_INSTRUCTIONS)
                assert ("PROMPT_ALPHA" in request["instructions"]) == (
                    mode != "upgrade"
                )
                assert "PROMPT_BETA" not in request["instructions"]
                assert ("管理员 QQ 号为 123456789" in request["instructions"]) == (
                    mode != "upgrade"
                )
                assert "987654321" not in request["instructions"]
                assert ("GROUP_TEMPLATE_ALPHA" in request["instructions"]) == (
                    mode != "upgrade"
                )
                assert "GROUP_TEMPLATE_BETA" not in request["instructions"]
                assert "{nickname}" not in request["instructions"]
                content = json.dumps(request["input"], ensure_ascii=False)
                assert "GROUP_TEMPLATE_" not in content
                assert "PROMPT_ALPHA" not in content
                assert "PROMPT_BETA" not in content
                assert "管理员 QQ 号为" not in content
                assert "prompt-probe" in content
                assert "harness-maintenance" in content
                assert "<multi_agent_role>" not in content
                assert "<collaboration_mode>" not in content
                assert "<recommended_plugins>" not in content
                assert "CATALOG_MODE_MARKER" not in content
                assert "<multi_agent_mode>" in content
                assert "<permissions instructions>" in content
                if mode == "upgrade" and index == 0:
                    assert "LEGACY_APPLICATION_RULES" in content
                    assert "可选表情名称" not in content
                    continue
                assert "qq_member" in content
                assert (
                    "每条 assistant 消息（过程说明和最终回复）必须仅包含一个 JSON 对象"
                    in content
                )
                assert all(name in content for name in runtime.stickers)
                assert "可选表情名称" in content
                assert "值表示是否发送图片" not in content
                assert "probe-0" in content
                updates = [
                    part["text"]
                    for item in request["input"]
                    if item.get("role") == "developer"
                    for part in item.get("content", [])
                    if "以下是本轮最新应用提示词" in part.get("text", "")
                ]
                assert updates
                update_counts.append(len(updates))
                snapshot = json.loads(updates[-1].split("\n", 1)[1])
                cleared = "probe-3" in content
                assert "群设定" not in snapshot
                assert "基础提示词" not in snapshot
                assert snapshot["群共同记录"] == (
                    "" if cleared else f"GROUP_MEMORY_{int('probe-1' in content)}"
                )
                assert bool(snapshot["成员记录"]) != cleared
                assert ("MEMBER_MEMORY" in updates[-1]) != cleared
            if mode == "plain":
                assert update_counts == [1, 2, 2, 3, 3]
            if mode in {"pre_turn", "mid_turn"}:
                metadata = json.loads(
                    compactions[0]["client_metadata"]["x-codex-turn-metadata"]
                )
                assert metadata["compaction"]["trigger"] == "auto"
                assert metadata["compaction"]["phase"] == (
                    "pre_turn" if mode == "pre_turn" else "mid_turn"
                )
            if mode == "plain":
                diagnose_failure = True
                assert thread is not None
                options, _ = await runtime.thread_options(message, workspace)
                thread = await runtime.prepare_thread(
                    message, thread.id, ImageTurn(message, workspace), options
                )
                turn = await thread.turn([TextInput("diagnostic probe")])
                async for event in turn.stream():
                    if event.method == "turn/completed":
                        assert event.payload.turn.status == TurnStatus.failed
                async with asyncio.timeout(10):
                    while True:
                        _, diagnostics = read_diagnostics(home / "logs_2.sqlite", 0)
                        responses = [
                            record
                            for record in diagnostics
                            if record["event"] == "response"
                        ]
                        if any(record["status"] == 400 for record in responses):
                            break
                        await asyncio.sleep(0.1)
                assert {record["status"] for record in responses} == {200, 400}
                failure = next(
                    record for record in responses if record["status"] == 400
                )
                assert failure["request_id"] == "req_failure"
                assert failure["thread"] == thread.id
                assert failure["turn"] == turn.id
                diagnose_failure = False
                handoff_response = True
                previous_thread = thread.id
                with patch.object(
                    codex, "account", AsyncMock(return_value=NS(account=object()))
                ):
                    summary = await runtime.summarize(
                        message, previous_thread, workspace
                    )
                assert summary == "HANDOFF_BACKGROUND"
                assert "PROMPT_ALPHA" in requests[-1]["instructions"]
                assert "管理员 QQ 号为 123456789" in requests[-1]["instructions"]
                assert "GROUP_TEMPLATE_ALPHA" in requests[-1]["instructions"]
                assert any(
                    part.get("text") == HANDOFF_INSTRUCTIONS
                    for item in requests[-1]["input"]
                    for part in item.get("content", [])
                )
                assert requests[-1]["text"]["format"]["schema"] == HANDOFF_SCHEMA
                with db:
                    db.execute(
                        "INSERT INTO group_handoffs VALUES ('10', ?)", (summary,)
                    )
                options, _ = await runtime.thread_options(message, workspace)
                thread = await runtime.prepare_thread(
                    message, None, ImageTurn(message, workspace), options
                )
                assert thread.id != previous_thread
                await runtime.update_group(thread, message, profiles)
                handoff_response = False
                result = await thread.run([TextInput("fresh-handoff-turn")])
                assert result.status == TurnStatus.completed
                content = json.dumps(requests[-1]["input"], ensure_ascii=False)
                assert "HANDOFF_BACKGROUND" in content
                assert "fresh-handoff-turn" in content
                assert "probe-0" not in content
                assert "请根据当前可见上下文" not in content
                assert "probe-compact" not in content
                assert "PROMPT_ALPHA" not in requests[-1]["instructions"]
                assert "管理员 QQ 号为 987654321" in requests[-1]["instructions"]
                assert "GROUP_TEMPLATE_BETA" in requests[-1]["instructions"]
                assert "GROUP_TEMPLATE_" not in content
                await runtime.release_thread(thread.id)
                for prompt in ("PROMPT_BETA", ""):
                    with db:
                        db.execute(
                            "UPDATE group_prompts SET prompt=? WHERE group_id='10'",
                            (prompt,),
                        )
                    settings.agent_admin_qq = None
                    options, _ = await runtime.thread_options(message, workspace)
                    thread = await runtime.prepare_thread(
                        message, None, ImageTurn(message, workspace), options
                    )
                    await runtime.update_group(thread, message, profiles)
                    result = await thread.run([TextInput("new-system-probe")])
                    assert result.status == TurnStatus.completed
                    instructions = requests[-1]["instructions"]
                    assert ("PROMPT_BETA" in instructions) == bool(prompt)
                    assert "PROMPT_ALPHA" not in instructions
                    assert "管理员 QQ 号为" not in instructions
                    assert "PROMPT_BETA" not in json.dumps(requests[-1]["input"])
                    await runtime.release_thread(thread.id)
            encoded = json.dumps(requests, ensure_ascii=False)
            for forbidden in (
                "observation_interval_seconds",
                "message_ttl_seconds",
                "conversation_gap_seconds",
                "backoff_until",
                "silences",
                "参与度偏低",
            ):
                assert forbidden not in encoded
    finally:
        await codex.close()
        db.close()
        await runner.cleanup()


@pytest.mark.parametrize("steering", [False, True])
async def test_group_correction(tmp_path, steering):
    from collections import deque
    from qq_agent.agent import Agent
    from qq_agent.onebot import OneBot
    from test_agent import event

    requests = []
    injected = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()
    steered = asyncio.Event()

    async def respond(request):
        body = await request.json()
        requests.append(body)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        number = len(requests)

        async def emit(value):
            await response.write(("data: " + json.dumps(value) + "\n\n").encode())

        await emit({"type": "response.created", "response": {"id": str(number)}})
        if number == 1:
            if steering:
                entered.set()
                await release.wait()
            await emit(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "bad",
                        "phase": "final_answer" if steering else "commentary",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"response":null}'
                                if steering
                                else "invalid JSON",
                            }
                        ],
                    },
                }
            )
            entered.set()
            await release.wait()
        elif number == 2:
            await emit(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "corrected",
                        "phase": "final_answer",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"response":"已修正","sticker":"平静"}',
                            }
                        ],
                    },
                }
            )
        elif number == 3:
            await emit(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "bad-final",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "invalid final"}],
                    },
                }
            )
        else:
            await emit(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "silent",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": '{"response":null}'}
                        ],
                    },
                }
            )
        await emit(
            {
                "type": "response.completed",
                "response": {
                    "id": str(number),
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            }
        )
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/responses", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    home = tmp_path / "codex"
    home.mkdir()
    settings = Settings(
        set(),
        {"10": GroupSettings()},
        "ws://127.0.0.1:3001",
        tmp_path / "token",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "common.md",
    )
    settings.agents_file.write_text("Follow the group protocol.")
    config = CodexConfig(
        cwd=str(tmp_path),
        env={"CODEX_HOME": str(home)},
        config_overrides=(
            'model="gpt-5.4"',
            'model_provider="probe"',
            'model_providers.probe.name="OpenAI"',
            f'model_providers.probe.base_url="http://127.0.0.1:{port}"',
            'model_providers.probe.wire_api="responses"',
            "model_providers.probe.requires_openai_auth=false",
            "model_providers.probe.supports_websockets=false",
            "features.shell_snapshot=false",
        ),
    )
    bot = OneBot(settings)
    bot.call = AsyncMock(return_value={"message_id": 123, "member_count": 10})
    agent = None
    task = None
    try:
        async with AsyncCodex(config=config) as codex:
            assert (await codex.account()).account is None
            codex.account = AsyncMock(return_value=NS(account=object()))
            agent = Agent(settings, bot, codex)
            original_inject = agent.runtime.inject
            original_steer = codex._client.turn_steer

            async def steer(*args, **kwargs):
                result = await original_steer(*args, **kwargs)
                steered.set()
                return result

            codex._client.turn_steer = steer

            async def inject(thread, text):
                await original_inject(thread, text)
                if text.startswith("上一条回复解析失败"):
                    injected.set()

            agent.runtime.inject = inject

            agent.receive(event(group=10, mention=not steering))
            await asyncio.gather(*agent.receivers.values())
            await asyncio.gather(*agent.group_timers.values())
            message = agent.queue.get_nowait()
            if steering:
                agent.pending[message.key] = deque()
                agent.wake[message.key] = asyncio.Event()
            task = asyncio.create_task(agent.turns.execute(message))
            await asyncio.wait_for(entered.wait(), 20)
            if steering:
                agent.receive(event(group=10, identifier=2))
                await asyncio.gather(*agent.receivers.values())
                await asyncio.gather(*agent.group_timers.values())
                agent.pending[message.key].append(agent.queue.get_nowait())
                agent.wake[message.key].set()
                await asyncio.wait_for(steered.wait(), 10)
            else:
                await asyncio.wait_for(injected.wait(), 10)
            release.set()
            await asyncio.wait_for(task, 20)
            assert len(requests) == 2
            if steering:
                assert not injected.is_set()
                history = [
                    item
                    for item in requests[1]["input"]
                    if item.get("role") in {"user", "assistant"}
                ]
                assert [item["role"] for item in history[-3:]] == [
                    "user",
                    "assistant",
                    "user",
                ]
                assert history[-2]["content"][0]["text"] == '{"response":null}'
                assert agent.db.execute(
                    "SELECT DISTINCT status FROM group_batches"
                ).fetchall() == [("completed",)]
                return
            assert any(
                item.get("role") == "developer"
                and "上一条回复解析失败" in json.dumps(item, ensure_ascii=False)
                for item in requests[1]["input"]
            )
            assert (
                len(
                    [
                        item
                        for item in requests[1]["input"]
                        if item.get("role") == "user"
                        and "群聊消息记录" in json.dumps(item, ensure_ascii=False)
                    ]
                )
                == 1
            )
            assert (
                len(
                    [
                        call
                        for call in bot.call.call_args_list
                        if call.args[0] == "send_msg"
                    ]
                )
                == 1
            )
            assert agent.db.execute("SELECT status FROM group_batches").fetchone() == (
                "completed",
            )
            agent.receive(event(group=10, identifier=2, mention=False, reply="123"))
            await asyncio.gather(*agent.receivers.values())
            await asyncio.gather(*agent.group_timers.values())
            await asyncio.wait_for(agent.turns.execute(agent.queue.get_nowait()), 20)
            assert len(requests) == 4
            encoded = json.dumps(requests, ensure_ascii=False)
            for forbidden in (
                "observation_interval_seconds",
                "message_ttl_seconds",
                "conversation_gap_seconds",
                "backoff_until",
                "silences",
                "参与度偏低",
            ):
                assert forbidden not in encoded
            assert any(
                item.get("role") == "developer"
                and "上一条回复解析失败" in json.dumps(item, ensure_ascii=False)
                for item in requests[3]["input"]
            )
            assert all(
                not item.get("role") == "user"
                or "上一条回复解析失败" not in json.dumps(item, ensure_ascii=False)
                for body in requests
                for item in body["input"]
            )
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if agent:
            await agent.close_timers()
            agent.db.close()
        await runner.cleanup()
