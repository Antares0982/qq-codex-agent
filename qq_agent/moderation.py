import asyncio
import base64
import hashlib
import json
import os
import time

import aiohttp

from image_assets import read_image, relative_image_path, workspace_directory

from .images import image_preview
from .logging import LOG


class Moderation:
    def __init__(self, settings, db, bot):
        self.url = settings.moderation_url
        self.workspace = settings.workspace_dir
        self.db = db
        self.bot = bot
        self.requests = {}
        if self.url:
            for (image_id,) in db.execute(
                "SELECT i.id FROM moderation_images i JOIN received_images r ON r.id=i.id WHERE i.url=? AND i.blocked=1 AND r.path IS NOT NULL",
                (self.url,),
            ).fetchall():
                self.purge(image_id)

    def kind(self, image_id):
        row = self.db.execute(
            "SELECT kind FROM received_images WHERE id=?", (image_id,)
        ).fetchone()
        if row and row[0] != "unknown":
            return row[0]
        uses = self.db.execute(
            "SELECT j.key,CASE WHEN j.fullkey LIKE '$.quote.%' THEN json_extract(m.body,'$.quote.sender_id')=substr(m.id,1,instr(m.id,':')-1) ELSE m.own END FROM group_messages m,json_tree(m.body) j WHERE j.key IN ('image_id','sticker_id') AND j.value=?",
            (image_id,),
        ).fetchall()
        if uses and all(kind == "sticker_id" for kind, _ in uses):
            return "sticker"
        if uses and all(own for _, own in uses):
            return "outgoing"
        return "image"

    def blocked(self, image_id):
        return bool(
            self.url
            and self.kind(image_id) == "image"
            and self.db.execute(
                "SELECT 1 FROM moderation_images WHERE url=? AND id=? AND blocked=1",
                (self.url, image_id),
            ).fetchone()
        )

    def known(self, image_id):
        return (
            self.db.execute(
                "SELECT 1 FROM moderation_images i JOIN moderation_results r USING(url,digest) WHERE i.url=? AND i.id=? AND i.blocked=0 AND r.flagged=0",
                (self.url, image_id),
            ).fetchone()
            is not None
        )

    def purge(self, image_id):
        row = self.db.execute(
            "SELECT folder,path FROM received_images WHERE id=?", (image_id,)
        ).fetchone()
        if not row or not row[1]:
            return
        folder, value = row
        relative = relative_image_path(self.workspace, folder)
        path = relative_image_path(self.workspace / relative, value)
        try:
            with workspace_directory(
                self.workspace, (*relative.parts, *path.parts[:-1])
            ) as directory:
                os.unlink(path.name, dir_fd=directory)
        except FileNotFoundError:
            pass
        with self.db:
            self.db.execute(
                "UPDATE received_images SET folder=NULL,path=NULL WHERE folder=? AND path=?",
                (folder, value),
            )
            self.db.execute(
                "DELETE FROM image_deliveries WHERE folder=? AND path=?",
                (folder, value),
            )

    def block(self, ids):
        with self.db:
            self.db.executemany(
                "INSERT INTO moderation_images (url,id,blocked) VALUES (?,?,1) ON CONFLICT(url,id) DO UPDATE SET blocked=1",
                [(self.url, image_id) for image_id in ids],
            )
        for image_id in ids:
            self.purge(image_id)

    def purge_copies(self, data, preview):
        digests = {
            hashlib.sha256(content).digest()
            for content in (data, base64.b64decode(preview["data"], validate=True))
        }
        matches = []
        # Linear cache scan; index if large.
        for image_id, folder, path in self.db.execute(
            "SELECT id,folder,path FROM received_images WHERE path IS NOT NULL"
        ).fetchall():
            if self.kind(image_id) != "image":
                continue
            try:
                relative = relative_image_path(self.workspace, folder)
                _, content = read_image(self.workspace / relative, path)
            except (ValueError, OSError):
                continue
            if hashlib.sha256(content).digest() in digests:
                matches.append(image_id)
        self.block(matches)

    async def load(self, image_id):
        row = self.db.execute(
            "SELECT source,folder,path,created FROM received_images WHERE id=?",
            (image_id,),
        ).fetchone()
        if not row:
            raise ValueError("Image index unavailable")
        source, folder, path, created = row
        if path:
            relative = relative_image_path(self.workspace, folder)
            _, data = read_image(self.workspace / relative, path)
        else:
            if not source or time.time() - created > 14 * 86400:
                raise ValueError("Image index expired")
            data = await self.bot.image(source)
        preview, animated = await asyncio.to_thread(image_preview, data)
        return data, preview, animated

    async def classify(self, digest, preview):
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=70)
        ) as client:
            async with client.post(
                self.url,
                json={"image": f"data:{preview['mimeType']};base64,{preview['data']}"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError(f"Moderation HTTP {response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError("Moderation response too large")
                try:
                    result = json.loads(raw)
                except (ValueError, RecursionError):
                    raise ValueError("Invalid moderation result") from None
        categories = result.get("categories") if isinstance(result, dict) else None
        if (
            not isinstance(categories, dict)
            or set(categories) != {"self-harm", "sexual", "violence"}
            or any(type(value) is not bool for value in categories.values())
            or type(result.get("flagged")) is not bool
            or result["flagged"] != any(categories.values())
        ):
            raise ValueError("Invalid moderation result")
        flagged = result["flagged"]
        if flagged:
            LOG.warning(
                "Moderation flagged digest=%s categories=%s",
                digest,
                ",".join(sorted(key for key, value in categories.items() if value)),
            )
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO moderation_results VALUES (?,?,?)",
                (self.url, digest, flagged),
            )
        return flagged

    async def check(self, image_id, preview, data):
        if not self.url or self.kind(image_id) != "image":
            return True
        if self.blocked(image_id):
            self.purge(image_id)
            return False
        digest = hashlib.sha256(
            base64.b64decode(preview["data"], validate=True)
        ).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT INTO moderation_images (url,id,digest) VALUES (?,?,?) ON CONFLICT(url,id) DO UPDATE SET digest=excluded.digest",
                (self.url, image_id, digest),
            )
        row = self.db.execute(
            "SELECT flagged FROM moderation_results WHERE url=? AND digest=?",
            (self.url, digest),
        ).fetchone()
        if row:
            flagged = row[0]
        else:
            task = self.requests.get(digest)
            if task is None:
                task = asyncio.create_task(self.classify(digest, preview))
                self.requests[digest] = task

                def finished(task):
                    self.requests.pop(digest, None)
                    if not task.cancelled():
                        task.exception()

                task.add_done_callback(finished)
            flagged = await asyncio.shield(task)
        if flagged:
            self.block(
                [
                    row[0]
                    for row in self.db.execute(
                        "SELECT id FROM moderation_images WHERE url=? AND digest=?",
                        (self.url, digest),
                    )
                ]
            )
            self.purge_copies(data, preview)
        return not self.blocked(image_id)

    async def inspect(self, image_id):
        if self.blocked(image_id):
            self.purge(image_id)
            return False
        if self.known(image_id):
            return True
        data, preview, _ = await self.load(image_id)
        return await self.check(image_id, preview, data)
