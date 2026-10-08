import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from aiohttp import web
from openai_codex import AsyncCodex, CodexConfig, TextInput
from openai_codex.generated.v2_all import TurnStatus

from qq_agent.config import Settings
from qq_agent.messages import ImageTurn, Message
from qq_agent.profiles import Profiles
from qq_agent.runtime import GROUP_BASE_INSTRUCTIONS, Runtime, load_skills
from qq_agent.storage import open_database


async def test_runtime_prompts(tmp_path):
    requests = []

    async def respond(request):
        requests.append(await request.json())
        events = [
            {"type": "response.created", "response": {"id": "probe"}},
            {
                "type": "response.completed",
                "response": {
                    "id": "probe",
                    "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
                },
            },
        ]
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
    config = CodexConfig(
        env={"CODEX_HOME": str(home)},
        config_overrides=(
            'model="gpt-5.4"',
            'model_provider="probe"',
            'model_providers.probe.name="Local probe"',
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
    try:
        async with asyncio.timeout(45), AsyncCodex(config=config) as codex:
            assert (await codex.account()).account is None
            await load_skills(codex)
            runtime = Runtime(settings, db, codex, AsyncMock(return_value="bot"))
            thread = None
            for index, prompt in enumerate(("PROMPT_ALPHA", "PROMPT_BETA", "")):
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO group_prompts VALUES ('10', ?, '1', 0)",
                        (prompt,),
                    )
                options, model = await runtime.thread_options(message, workspace)
                context = ImageTurn(message, workspace)
                if index != 1:
                    thread = await runtime.prepare_thread(
                        message, thread.id if thread else None, context, options
                    )
                assert thread is not None
                await runtime.update_group(thread, message, profiles, model)
                turn = await thread.turn([TextInput(f"probe-{index}")])
                completed = False
                async for event in turn.stream():
                    if event.method == "turn/completed":
                        assert event.payload.turn.status == TurnStatus.completed
                        completed = True
                assert completed
                if index != 0:
                    await runtime.release_thread(thread.id)
            assert len(requests) == 3
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
            for index, request in enumerate(requests):
                assert request["instructions"] == GROUP_BASE_INSTRUCTIONS
                content = json.dumps(request["input"], ensure_ascii=False)
                assert "prompt-probe" in content
                assert "harness-maintenance" in content
                assert "qq_member" in content
                assert f"probe-{index}" in content
                assert "probe-0" in content
            assert "PROMPT_ALPHA" in json.dumps(requests[0])
            assert "PROMPT_BETA" in json.dumps(requests[1])
            updates = [
                part["text"]
                for item in requests[2]["input"]
                if item.get("role") == "developer"
                for part in item.get("content", [])
                if "以下是本轮最新群设定" in part.get("text", "")
            ]
            assert '"群设定": ""' in updates[-1]
            assert "PROMPT_BETA" not in updates[-1]
    finally:
        db.close()
        await runner.cleanup()
