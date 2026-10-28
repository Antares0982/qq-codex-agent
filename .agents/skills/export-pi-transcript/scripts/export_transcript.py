"""Copy the latest service-generated transcript."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def export(host):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise ValueError("Expected an SSH host alias")
    ssh = ["ssh"]
    config = Path.home() / ".ssh/config"
    if config.is_file():
        ssh.extend(["-F", str(config)])
    ssh.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host])
    result = subprocess.run(
        [*ssh, "sudo -n cat /var/lib/qq-codex-agent/latest-transcript.md"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("Saved transcript unavailable; check service, SSH and sudo")
    if not result.stdout.startswith("# 最近 LLM 请求：重建 transcript\n"):
        raise ValueError("Unexpected remote output; no transcript written")
    return result.stdout


def save_output(text, path):
    if path is None:
        descriptor, name = tempfile.mkstemp(
            prefix="qq-transcript-", suffix=".md", dir="/tmp"
        )
        path = Path(name)
    else:
        path = path.expanduser().absolute()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="pi")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        print(save_output(export(args.host), args.output))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
