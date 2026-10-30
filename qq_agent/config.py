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
class GroupSettings:
    observation_interval_seconds: int = 60
    message_ttl_seconds: int = 120
    conversation_gap_seconds: int = 300

    def __post_init__(self):
        values = (
            self.observation_interval_seconds,
            self.message_ttl_seconds,
            self.conversation_gap_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("Group intervals must be positive integers")
        if not values[0] <= values[1] <= values[2]:
            raise ValueError("Group intervals must satisfy observation <= ttl <= gap")


@dataclass
class Settings:
    private_users: set[str]
    groups: dict[str, GroupSettings]
    napcat_url: str
    token_file: Path
    state_dir: Path
    workspace_dir: Path
    agents_file: Path
    private_agents_file: Path | None = None
    group_agents_file: Path | None = None
    queue_limit: int = 8
    task_timeout: float = 3600
    model: str | None = None
    auth_socket: Path | None = None
    resources_dir: Path | None = None
    group_prompt_allow_members: bool = False
    agent_admin_qq: str | None = None
    model_auto_compact_token_limit: int = 258000
    group_image_limit: int = 5

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
            if isinstance(entry, dict) and entry.keys() & {
                "request_interval_minutes",
                "request_message_count",
                "participation_ratio",
            }:
                raise ValueError(
                    "Migrate group settings to observation_interval_seconds, message_ttl_seconds, conversation_gap_seconds"
                )
            if not isinstance(entry, dict) or entry.keys() - {
                "observation_interval_seconds",
                "message_ttl_seconds",
                "conversation_gap_seconds",
            }:
                raise ValueError("Unknown group settings; users is no longer supported")
            raw["groups"][group] = GroupSettings(**entry)
        for key in ("token_file", "state_dir", "workspace_dir", "agents_file"):
            raw[key] = Path(raw[key])
            if not raw[key].is_absolute():
                raise ValueError(f"{key} must be absolute")
        for key in (
            "private_agents_file",
            "group_agents_file",
            "auth_socket",
            "resources_dir",
        ):
            if key in raw:
                raw[key] = Path(raw[key])
                if not raw[key].is_absolute():
                    raise ValueError(f"{key} must be absolute")
        if "agent_admin_qq" in raw:
            raw["agent_admin_qq"] = qq_id(raw["agent_admin_qq"])
        settings = cls(**raw)
        if type(settings.group_prompt_allow_members) is not bool:
            raise ValueError("group_prompt_allow_members must be boolean")
        url = urlsplit(settings.napcat_url)
        if (
            url.scheme != "ws"
            or url.hostname not in {"127.0.0.1", "::1"}
            or url.query
            or url.username
        ):
            raise ValueError("napcat_url must be a loopback ws URL without credentials")
        for key in (
            "queue_limit",
            "task_timeout",
            "model_auto_compact_token_limit",
            "group_image_limit",
        ):
            value = getattr(settings, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be positive")
        return settings
