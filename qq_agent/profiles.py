import json
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from .config import qq_id
from .prompts import (
    PROFILE_FIELDS_ERROR,
    PROFILE_INDEX_ERROR,
    PROFILE_NEW_ERROR,
    PROFILE_TEXT_ERROR,
)
from .stickers import unique_names


@dataclass
class ProfileSummary:
    key: str
    start: datetime
    end: datetime

    @property
    def target(self):
        return {"group_id": int(self.key[6:])}


def parse_profile(text, entries, limit):
    value = json.loads(text, object_pairs_hook=unique_names)
    if not isinstance(value, dict) or value.keys() != {"valid", "new"}:
        raise ValueError(PROFILE_FIELDS_ERROR)
    valid, new = value["valid"], value["new"]
    if (
        not isinstance(valid, list)
        or any(
            type(index) is not int or not 1 <= index <= len(entries) for index in valid
        )
        or len(set(valid)) != len(valid)
    ):
        raise ValueError(PROFILE_INDEX_ERROR)
    if not isinstance(new, list) or any(not isinstance(item, str) for item in new):
        raise ValueError(PROFILE_NEW_ERROR)
    new = [item.strip() for item in new]
    if any(
        not item
        or len(item) > limit
        or any(unicodedata.category(char).startswith("C") for char in item)
        for item in new
    ):
        raise ValueError(PROFILE_TEXT_ERROR.format(limit=limit))
    if len(set(new)) < 3:
        raise ValueError(PROFILE_NEW_ERROR)
    return {"valid": valid, "new": sorted(set(new))}


class Profiles:
    def __init__(self, db, bot, settings):
        self.db = db
        self.bot = bot
        self.settings = settings

    async def member_info(self, group, user):
        info = await self.bot.call(
            "get_group_member_info",
            {"group_id": int(group), "user_id": int(user), "no_cache": True},
        )
        if (
            not isinstance(info, dict)
            or qq_id(info.get("group_id")) != str(group)
            or qq_id(info.get("user_id")) != user
        ):
            raise ValueError("无法确认当前群成员身份。")
        return info

    def entries(self, group):
        return [
            row[0]
            for row in self.db.execute(
                "SELECT text FROM group_profiles WHERE group_id=? ORDER BY text",
                (str(group),),
            )
        ]

    def group_profile(self, group):
        return "\n".join(self.entries(group))

    def apply(self, group, entries, result):
        settings = self.settings.groups[str(group)]
        today = date.today().isoformat()
        refreshed = {entries[index - 1] for index in result["valid"]} | set(
            result["new"]
        )
        with self.db:
            self.db.executemany(
                "INSERT INTO group_profiles VALUES (?,?,?) ON CONFLICT(group_id,text) DO UPDATE SET date=excluded.date",
                [(str(group), text, today) for text in refreshed],
            )
            self.db.execute(
                "DELETE FROM group_profiles WHERE group_id=? AND text IN "
                "(SELECT text FROM group_profiles WHERE group_id=? ORDER BY date DESC,text DESC LIMIT -1 OFFSET ?)",
                (str(group), str(group), settings.profile_max_entries),
            )
