import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, TextInput
from openai_codex.generated.v2_all import ReasoningEffort, TurnStatus

from qq_agent.config import Settings
from qq_agent.messages import ImageTurn, Message
from qq_agent.profiles import Profiles
from qq_agent.runtime import GROUP_BASE_INSTRUCTIONS, Runtime, load_skills
from qq_agent.storage import open_database


@pytest.mark.parametrize("mode", ["plain", "manual", "pre_turn", "mid_turn", "local"])
async def test_runtime_prompts(tmp_path, mode):
    requests = []
    compactions = []
    local_compact = False
    tool_sent = False

    async def respond(request):
        nonlocal tool_sent
        body = await request.json()
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
        if compacting and mode != "local":
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
        {"10": {"1"}},
        "ws://127.0.0.1:3001",
        tmp_path / "token",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "AGENTS.md",
    )
    db = open_database(settings)
    home = tmp_path / "codex"
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
                ("PROMPT_ALPHA", "PROMPT_BETA", "PROMPT_BETA", "")
            ):
                if index == 2:
                    await codex.close()
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
                        ("GROUP_MEMORY" if prompt else "",),
                    )
                    db.execute("DELETE FROM member_profiles")
                    if prompt:
                        db.execute(
                            "INSERT INTO member_profiles VALUES ('10', '1', 'Member', ?, 0)",
                            (json.dumps({"兴趣": "MEMBER_MEMORY"}),),
                        )
                options, model = await runtime.thread_options(message, workspace)
                context = ImageTurn(message, workspace)
                if index != 1:
                    thread = await runtime.prepare_thread(
                        message, thread.id if thread else None, context, options
                    )
                assert thread is not None
                if index == 1 and mode == "local":
                    local_compact = True
                    with runtime.watch_codex():
                        assert await runtime.compact(thread)
                    local_compact = False
                if index != 2:
                    await runtime.update_group(thread, message, profiles)
                if index == 1 and mode == "manual":
                    with runtime.watch_codex():
                        assert await runtime.compact(thread)
                turn = await thread.turn(
                    [TextInput(f"probe-{index}")],
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
                if index != 0:
                    await runtime.release_thread(thread.id)
            assert bool(compactions) == (mode != "plain")
            assert len(requests) == (5 if mode == "mid_turn" else 4)
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
            for request in requests:
                assert request["instructions"] == GROUP_BASE_INSTRUCTIONS
                content = json.dumps(request["input"], ensure_ascii=False)
                assert "prompt-probe" in content
                assert "harness-maintenance" in content
                assert "qq_member" in content
                assert "CATALOG_MODE_MARKER" in content
                assert "probe-0" in content
                updates = [
                    part["text"]
                    for item in request["input"]
                    if item.get("role") == "developer"
                    for part in item.get("content", [])
                    if "以下是本轮最新群设定" in part.get("text", "")
                ]
                assert updates
                snapshot = json.loads(updates[-1].split("\n", 1)[1])
                cleared = "probe-3" in content
                assert snapshot["群设定"] == (
                    ""
                    if cleared
                    else "PROMPT_BETA"
                    if "probe-1" in content
                    else "PROMPT_ALPHA"
                )
                assert snapshot["群共同记录"] == ("" if cleared else "GROUP_MEMORY")
                assert bool(snapshot["成员记录"]) != cleared
                assert ("MEMBER_MEMORY" in updates[-1]) != cleared
            if mode in {"pre_turn", "mid_turn"}:
                metadata = json.loads(
                    compactions[0]["client_metadata"]["x-codex-turn-metadata"]
                )
                assert metadata["compaction"]["trigger"] == "auto"
                assert metadata["compaction"]["phase"] == (
                    "pre_turn" if mode == "pre_turn" else "mid_turn"
                )
    finally:
        await codex.close()
        db.close()
        await runner.cleanup()
