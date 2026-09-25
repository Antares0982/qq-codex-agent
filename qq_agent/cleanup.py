import asyncio
import os
import sqlite3
import stat
import time
from pathlib import Path

from image_assets import workspace_directory

from .logging import LOG, log_text

FILE_RETENTION = 14 * 24 * 60 * 60
CLEANUP_INTERVAL = 24 * 60 * 60


class Cleanup:
    def __init__(self, settings, db, jobs, resetting, contexts):
        self.settings = settings
        self.db = db
        self.jobs = jobs
        self.resetting = resetting
        self.contexts = contexts

    def clean_folder(self, folder, cutoff):
        def report(error):
            LOG.warning("Workspace cleanup failed error=%s", log_text(error))

        with workspace_directory(folder) as root:
            for path, _, files, directory in os.fwalk(
                ".", follow_symlinks=False, dir_fd=root, onerror=report
            ):
                for name in files:
                    try:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if (
                            stat.S_ISREG(info.st_mode)
                            and max(info.st_atime, info.st_mtime) < cutoff
                        ):
                            os.unlink(name, dir_fd=directory)
                            LOG.info(
                                "Expired file removed folder=%s path=%s",
                                folder.name,
                                log_text(str(Path(path) / name)),
                            )
                    except OSError as error:
                        report(error)
        for table in ("generated_images", "image_deliveries"):
            rows = self.db.execute(
                f"SELECT DISTINCT path FROM {table} WHERE folder=?", (folder.name,)
            ).fetchall()
            for (value,) in rows:
                path = Path(value)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    continue
                try:
                    with workspace_directory(folder, path.parts[:-1]) as directory:
                        os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    with self.db:
                        self.db.execute(
                            f"DELETE FROM {table} WHERE folder=? AND path=?",
                            (folder.name, value),
                        )
                except OSError as error:
                    report(error)

    def clean_files(self):
        cutoff = time.time() - FILE_RETENTION
        busy = set(self.jobs) | self.resetting | set(self.contexts)
        folders = {
            folder
            for key, folder in self.db.execute("SELECT key, folder FROM sessions")
            if key in busy
        }
        folders.update(context.folder.name for context in self.contexts.values())
        with workspace_directory(self.settings.workspace_dir) as root:
            for name in os.listdir(root):
                if name in folders:
                    continue
                try:
                    info = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        self.clean_folder(self.settings.workspace_dir / name, cutoff)
                except OSError as error:
                    LOG.warning("Workspace cleanup failed error=%s", log_text(error))

    async def cleanup_files(self):
        while True:
            try:
                self.clean_files()
            except (OSError, sqlite3.Error) as error:
                LOG.warning("Workspace cleanup failed error=%s", log_text(error))
            await asyncio.sleep(CLEANUP_INTERVAL)
