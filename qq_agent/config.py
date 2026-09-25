import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def qq_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("QQ IDs must be positive decimal numbers")
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("QQ IDs must be positive decimal numbers")
    return str(int(value))


@dataclass
class Settings:
    private_users: set[str]
    groups: dict[str, set[str]]
    napcat_url: str
    token_file: Path
    state_dir: Path
    workspace_dir: Path
    agents_file: Path
    private_agents_file: Path | None = None
    group_agents_file: Path | None = None
    queue_limit: int = 8
    task_timeout: int = 900
    model: str | None = None

    def check_prompts(self, required=False):
        for key in ("agents_file", "private_agents_file", "group_agents_file"):
            path = getattr(self, key)
            if path is None:
                if required:
                    raise ValueError(f"Missing {key}")
                continue
            if not path.read_text().strip():
                raise ValueError(f"Empty prompt: {path}")

    @classmethod
    def load(cls, path):
        raw = tomllib.loads(Path(path).read_text())
        if raw.keys() & {"allowed_users", "allowed_groups"}:
            raise ValueError(
                "Migrate allowed_users/allowed_groups to private_users/groups"
            )
        allowlist = raw.pop("allowlist_file", None)
        if allowlist is not None:
            if not isinstance(allowlist, str) or not Path(allowlist).is_absolute():
                raise ValueError("allowlist_file must be absolute")
            entries = tomllib.loads(Path(allowlist).read_text())
            if entries.keys() - {"private_users", "groups"}:
                raise ValueError("Unknown allowlist fields")
            raw["private_users"] = entries.get("private_users", [])
            raw["groups"] = entries.get("groups", {})
        private_users = raw.get("private_users", [])
        if not isinstance(private_users, list):
            raise ValueError("private_users must be an array")
        raw["private_users"] = {qq_id(value) for value in private_users}
        groups = raw.get("groups", {})
        if not isinstance(groups, dict):
            raise ValueError("groups must be a table")
        raw["groups"] = {}
        for group, entry in groups.items():
            group = qq_id(group)
            if group in raw["groups"]:
                raise ValueError("Duplicate normalized group ID")
            if not isinstance(entry, dict) or entry.keys() - {"users"}:
                raise ValueError("Group entries must contain only users")
            users = entry.get("users", [])
            if not isinstance(users, list):
                raise ValueError("Group users must be an array")
            raw["groups"][group] = {
                "all" if value == "all" else qq_id(value) for value in users
            }
        for key in ("token_file", "state_dir", "workspace_dir", "agents_file"):
            raw[key] = Path(raw[key])
            if not raw[key].is_absolute():
                raise ValueError(f"{key} must be absolute")
        for key in ("private_agents_file", "group_agents_file"):
            if key in raw:
                raw[key] = Path(raw[key])
                if not raw[key].is_absolute():
                    raise ValueError(f"{key} must be absolute")
        settings = cls(**raw)
        url = urlsplit(settings.napcat_url)
        if (
            url.scheme != "ws"
            or url.hostname not in {"127.0.0.1", "::1"}
            or url.query
            or url.username
        ):
            raise ValueError("napcat_url must be a loopback ws URL without credentials")
        for key in ("queue_limit", "task_timeout"):
            value = getattr(settings, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be positive")
        return settings
