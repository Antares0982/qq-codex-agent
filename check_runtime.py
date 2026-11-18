import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_cli_bin import bundled_codex_path
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import SkillsListResponse, ThreadUnsubscribeResponse

from check_sandbox import check_app_server_sandbox
from qq_agent import runtime
from qq_codex_agent import Settings, codex_config


async def check_skills(codex, root, workspace):
    assert runtime.BUILTIN_SKILLS.is_dir()
    await runtime.load_skills(codex)
    installed = await codex._client.request(
        "skills/list",
        {"cwds": [str(workspace)], "forceReload": True},
        response_model=SkillsListResponse,
    )
    assert not installed.data[0].errors, installed.data[0].errors
    assert any(
        skill.name == "harness-maintenance" for skill in installed.data[0].skills
    )
    builtin = root / "builtin-skills"
    other = root / "other-work"
    other.mkdir()
    for directory, name in (
        (builtin, "qq-builtin-probe"),
        (workspace / ".agents/skills", "qq-workspace-probe"),
    ):
        skill = directory / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: Runtime self-check only.\n---\n"
            "Temporary discovery fixture.\n"
        )
    with patch.object(runtime, "BUILTIN_SKILLS", builtin):
        await runtime.load_skills(codex)
    result = await codex._client.request(
        "skills/list",
        {"cwds": [str(workspace), str(other)], "forceReload": True},
        response_model=SkillsListResponse,
    )
    assert len(result.data) == 2
    for entry in result.data:
        assert not entry.errors, entry.errors
        skills = {skill.name: skill for skill in entry.skills}
        assert "qq-builtin-probe" in skills
        assert ("qq-workspace-probe" in skills) == (entry.cwd == str(workspace))
        assert (
            Path(skills["qq-builtin-probe"].path.root)
            .read_text()
            .endswith("Temporary discovery fixture.\n")
        )
    await runtime.load_skills(codex)
    print("Builtin and workspace skills discovered independently.")


def check_permissions(root):
    # Codex skips aliases under /tmp.
    guest = Path("/var/lib/qq-codex-smoke")
    empty = root / "empty"
    empty.mkdir()
    private = root / "private"
    private.mkdir()
    config = private / "config.toml"
    config.write_text('private_users=[]\nagent_admin_qq="123456789"\n')
    requirements = root / "requirements.toml"
    requirements.write_text(
        'allowed_approval_policies = ["on-request"]\n'
        'allowed_approvals_reviewers = ["auto_review"]\n'
        'allowed_sandbox_modes = ["workspace-write", "read-only"]\n'
        f'[permissions.filesystem]\ndeny_read = ["{guest}/state", "{guest}/private"]\n'
    )
    (root / "state/canary").write_text("test fixture")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(guest / "state/codex")
    sandbox = [
        "bwrap",
        "--die-with-parent",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/etc",
        "--dir",
        "/etc/codex",
        "--ro-bind",
        str(requirements),
        "/etc/codex/requirements.toml",
        "--tmpfs",
        "/var/lib",
        "--bind",
        str(root),
        str(guest),
        "--ro-bind",
        str(empty),
        str(guest / "state/codex/tmp/arg0-linux"),
        "--chdir",
        str(guest / "work"),
        "--",
    ]
    command = [
        str(bundled_codex_path()),
        "-c",
        'sandbox_mode="workspace-write"',
        "-c",
        'approval_policy="on-request"',
        "-c",
        'approvals_reviewer="auto_review"',
        "sandbox",
        "--",
        "/bin/sh",
        "-ec",
        'test ! -r "$1"; test ! -r "$2"; printf ok > probe; test "$(cat probe)" = ok',
        "check",
        str(guest / "state/canary"),
        str(guest / "private/config.toml"),
    ]
    subprocess.run(sandbox + command, env=env, check=True, timeout=30)
    runtime_command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--app-server-probe",
        str(guest),
    ]
    subprocess.run(sandbox + runtime_command, env=env, check=True, timeout=30)
    print("Managed deny-read and nested workspace sandbox passed.")


async def main(sandbox=False):
    with tempfile.TemporaryDirectory(prefix="qq-codex-smoke-") as directory:
        root = Path(directory)
        settings = Settings(
            set(),
            {},
            "ws://127.0.0.1:3001",
            root / "token",
            root / "state",
            root / "work",
        )
        (settings.state_dir / "codex").mkdir(parents=True)
        settings.workspace_dir.mkdir()
        async with AsyncCodex(config=codex_config(settings)) as codex:
            account = await codex.account()
            assert account.account is None
            await check_skills(codex, root, settings.workspace_dir)
            thread = await codex.thread_start(cwd=str(settings.workspace_dir))
            result = await codex._client.request(
                "thread/unsubscribe",
                {"threadId": thread.id},
                response_model=ThreadUnsubscribeResponse,
            )
            assert result.status.value == "unsubscribed"
            print("Pinned runtime initialized; isolated account is logged out.")
        if sandbox:
            check_permissions(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--app-server-probe", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.app_server_probe:
        root = args.app_server_probe
        settings = Settings(
            set(),
            {},
            "ws://127.0.0.1:3001",
            root / "token",
            root / "state",
            root / "work",
        )
        check_app_server_sandbox(
            settings,
            settings.workspace_dir,
            [settings.state_dir / "canary", root / "private/config.toml"],
        )
    else:
        asyncio.run(main(args.sandbox))
