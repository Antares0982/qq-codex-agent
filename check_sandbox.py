import os
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import CommandExecResponse

from qq_codex_agent import Settings, codex_config


def check_app_server_sandbox(settings, directory, denied_paths, readable_paths=()):
    with CodexClient(config=codex_config(settings)) as client:
        client.initialize()
        result = client.request(
            "command/exec",
            {
                "command": [
                    "/bin/sh",
                    "-ec",
                    'count=$1; shift; while [ "$count" -gt 0 ]; do '
                    'test -r "$1"; test ! -w "$1"; shift; count=$((count - 1)); done; '
                    'for path do test ! -r "$path"; done; '
                    'printf ok > app-server-probe; test "$(cat app-server-probe)" = ok',
                    "sandbox-check",
                    str(len(readable_paths)),
                    *map(str, readable_paths),
                    *map(str, denied_paths),
                ],
                "cwd": str(directory),
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(directory)],
                    "networkAccess": False,
                },
                "timeoutMs": 10000,
            },
            response_model=CommandExecResponse,
        )
        if result.exit_code:
            raise RuntimeError(f"App-server sandbox self-check failed: {result.stderr}")


def check_thread_start(settings, directory):
    with CodexClient(config=codex_config(settings)) as client:
        client.initialize()
        client.thread_start(
            {
                "cwd": str(directory),
                "sandbox": "workspace-write",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
                "ephemeral": True,
                "config": {"projects": {str(directory): {"trust_level": "trusted"}}},
            }
        )


def main():
    settings = Settings.load("/etc/qq-codex-agent/config.toml")
    settings.check_prompts(required=True)
    state = Path("/var/lib/qq-codex-agent")
    work = Path("/var/lib/qq-codex-work")
    for path in (
        "/home/antares",
        "/home/napcat",
        "/home/agent",
        "/etc/shadow",
        "/run/agenix",
        "/var/lib/antares-agent",
    ):
        if Path(path).exists():
            raise RuntimeError(f"Unexpected host exposure: {path}")
    if not Path("/etc/codex/requirements.toml").is_file():
        raise RuntimeError("Missing managed Codex requirements")
    env = os.environ.copy()
    env["CODEX_HOME"] = str(state / "codex")
    plugins = state / "codex/plugins"
    (plugins / "cache").mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=work) as directory,
        tempfile.NamedTemporaryFile(dir=state) as canary,
        tempfile.NamedTemporaryFile(dir=state / "codex") as codex_canary,
        tempfile.NamedTemporaryFile(dir=state / "codex/tmp") as tmp_canary,
        tempfile.NamedTemporaryFile(dir=plugins) as plugin_canary,
        tempfile.NamedTemporaryFile(dir=plugins / "cache") as resource,
    ):
        result = subprocess.run(
            [
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
                (
                    'test ! -r "$1"; '
                    "test ! -r /var/lib/qq-codex-agent/codex/auth.json; "
                    "test ! -r /etc/qq-codex-agent/napcat-token; "
                    'printf ok > probe; test "$(cat probe)" = ok'
                ),
                "sandbox-check",
                canary.name,
            ],
            cwd=directory,
            env=env,
            timeout=30,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            print(result.stderr.decode(errors="replace"), file=sys.stderr)
            raise RuntimeError("Codex sandbox self-check failed")
        check_thread_start(settings, directory)
        check_app_server_sandbox(
            settings,
            directory,
            [
                canary.name,
                codex_canary.name,
                tmp_canary.name,
                plugin_canary.name,
                state / "codex/auth.json",
                settings.token_file,
                "/etc/qq-codex-agent/allowlist.toml",
            ],
            [state / "codex/skills/.system/imagegen/SKILL.md", resource.name],
        )
    print("Filesystem and Codex sandbox checks passed")


if __name__ == "__main__":
    main()
