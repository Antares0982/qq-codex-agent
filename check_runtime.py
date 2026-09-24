import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex import AsyncCodex
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse

from check_sandbox import check_app_server_sandbox
from qq_codex_agent import Settings, codex_config


def check_permissions(root, settings):
    requirements = root / "requirements.toml"
    requirements.write_text(
        'allowed_approval_policies = ["on-request"]\n'
        'allowed_approvals_reviewers = ["auto_review"]\n'
        'allowed_sandbox_modes = ["workspace-write", "read-only"]\n'
        f'[permissions.filesystem]\ndeny_read = ["{settings.state_dir}"]\n'
    )
    canary = settings.state_dir / "canary"
    canary.write_text("test fixture")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(settings.state_dir / "codex")
    command = [
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
        "--bind",
        str(root),
        str(root),
        "--",
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
        'test ! -r "$1"; printf ok > probe; test "$(cat probe)" = ok',
        "check",
        str(canary),
    ]
    subprocess.run(command, cwd=settings.workspace_dir, env=env, check=True, timeout=30)
    # Reuse the same mount restrictions for the app-server path, without a model call.
    runtime_command = command[: command.index("--") + 1] + [
        sys.executable,
        str(Path(__file__).resolve()),
        "--app-server-probe",
        str(root),
    ]
    subprocess.run(
        runtime_command, cwd=settings.workspace_dir, env=env, check=True, timeout=30
    )
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
            root / "AGENTS.md",
        )
        (settings.state_dir / "codex").mkdir(parents=True)
        settings.workspace_dir.mkdir()
        async with AsyncCodex(config=codex_config(settings)) as codex:
            account = await codex.account()
            assert account.account is None
            thread = await codex.thread_start(cwd=str(settings.workspace_dir))
            result = await codex._client.request(
                "thread/unsubscribe",
                {"threadId": thread.id},
                response_model=ThreadUnsubscribeResponse,
            )
            assert result.status.value == "unsubscribed"
            print("Pinned runtime initialized; isolated account is logged out.")
        if sandbox:
            check_permissions(root, settings)


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
            root / "AGENTS.md",
        )
        check_app_server_sandbox(
            settings, settings.workspace_dir, [settings.state_dir / "canary"]
        )
    else:
        asyncio.run(main(args.sandbox))
