"""Local-only ZIP upload and atomic import of exported ChatGPT history.

This module does not restore native ChatGPT chats or Memory. It updates a local
SQLite knowledge database. ZIP members are never extracted to disk.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
from threading import Lock, Thread
from typing import Any
from urllib.parse import unquote
import zipfile

MAX_ZIP_BYTES = 2 * 1024 ** 3
MAX_JSON_BYTES = 8 * 1024 ** 3
JSON_MEMBER = re.compile(r"(?i)^conversations(?:-\d+)?\.json$")


class ArchiveImportError(ValueError):
    pass


class ArchiveImportBusy(ArchiveImportError):
    pass


class ArchiveImportManager:
    """One upload/import at a time, with recovery backup and atomic DB swap."""

    def __init__(self, workspace: Path, python_exe: str | None = None):
        self.workspace = Path(workspace)
        self.python_exe = python_exe or sys.executable
        self._busy = Lock()
        self._state_lock = Lock()
        self._state: dict[str, Any] = {
            "phase": "idle", "message": "No upload in this session.",
            "received_bytes": 0, "total_bytes": 0,
        }

    def _update(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def upload(self, stream: Any, content_length: str, encoded_name: str) -> dict[str, Any]:
        """Stream a raw ZIP into private storage and start importing asynchronously."""
        if not self._busy.acquire(blocking=False):
            raise ArchiveImportBusy("An archive upload/import is already running.")
        part: Path | None = None
        try:
            try:
                count = int(content_length)
            except (TypeError, ValueError) as exc:
                raise ArchiveImportError("Content-Length is required.") from exc
            if not (0 < count <= MAX_ZIP_BYTES):
                raise ArchiveImportError("ZIP must be nonempty and at most 2 GiB.")
            name = unquote(encoded_name or "").replace("\\", "/").split("/")[-1].strip()
            if not name.lower().endswith(".zip") or len(name) > 180:
                raise ArchiveImportError("Select a .zip file exported from ChatGPT.")

            incoming = self.workspace / "incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
            part = incoming / ("history-upload-" + tag + ".part")
            archive = incoming / ("history-upload-" + tag + ".zip")
            self._update(phase="uploading", message="Receiving ZIP...", received_bytes=0,
                         total_bytes=count, filename=name, error=None)
            remaining = count
            with part.open("xb") as out:
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ArchiveImportError("Incomplete ZIP upload.")
                    out.write(chunk)
                    remaining -= len(chunk)
                    self._update(received_bytes=count - remaining)
            try:
                with zipfile.ZipFile(part) as z:
                    members = [i for i in z.infolist() if not i.is_dir()
                               and JSON_MEMBER.fullmatch(i.filename.replace("\\", "/").split("/")[-1])]
                    if not members:
                        raise ArchiveImportError("No conversations.json inside the ZIP.")
                    if len(members) > 300 or sum(i.file_size for i in members) > MAX_JSON_BYTES:
                        raise ArchiveImportError("Export JSON exceeds the safe limit.")
            except zipfile.BadZipFile as exc:
                raise ArchiveImportError("Not a valid ZIP archive.") from exc
            os.replace(part, archive)
            part = None
            self._update(phase="processing", message="Importing into a safe SQLite copy...",
                         received_bytes=count, archive_path=str(archive), json_files=len(members))
            Thread(target=self._import, args=(archive,), daemon=True,
                   name="chatgpt-archive-import").start()
            return self.snapshot()
        except Exception as exc:
            if part is not None:
                part.unlink(missing_ok=True)
            self._update(phase="error", message="Upload rejected.", error=str(exc))
            self._busy.release()
            raise

    def _import(self, archive: Path) -> None:
        private = self.workspace / "private"
        private.mkdir(parents=True, exist_ok=True)
        main_db = private / "export-knowledge.sqlite"
        importer = self.workspace / "knowledge.py"
        inventory = self.workspace / "incoming" / "what-s-on-your-mind-today.chatgpt-sync.json"
        suffix = secrets.token_hex(6)
        staging = private / ("export-knowledge-staging-" + suffix + ".sqlite")
        backup: Path | None = None
        committed = False
        try:
            if not importer.is_file():
                raise ArchiveImportError("knowledge.py is missing from Migration Bridge.")
            before = self._counts(main_db) if main_db.is_file() else {
                "captured_chats": 0, "messages": 0, "fts_records": 0,
            }
            if main_db.is_file():
                backup_dir = private / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                backup = backup_dir / ("export-knowledge-" + stamp + "-" + suffix + ".sqlite")
                self._sqlite_copy(main_db, backup)
                self._sqlite_copy(main_db, staging)
            args = [self.python_exe, str(importer), "--db", str(staging), "ingest", str(archive)]
            if inventory.is_file():
                args += ["--inventory", str(inventory)]
            p = subprocess.run(args, cwd=str(self.workspace), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=1200,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if p.returncode:
                raise ArchiveImportError("Importer failed: " + (p.stderr or p.stdout)[-1200:])
            after = self._counts(staging)
            if after["sqlite_quick_check"] != "ok":
                raise ArchiveImportError("SQLite integrity check failed.")
            if after["messages"] != after["fts_records"]:
                raise ArchiveImportError("Search index count differs from message count.")
            if (after["messages"] < before["messages"] or
                    after["captured_chats"] < before["captured_chats"]):
                raise ArchiveImportError("Import would reduce existing archive counts.")
            os.replace(staging, main_db)
            committed = True
            report = {
                "phase": "complete", "message": "Local history archive updated.",
                "filename": self.snapshot().get("filename", ""),
                "archive_path": str(archive), "database_path": str(main_db),
                "backup_path": str(backup) if backup else None,
                "before": before, "after": after,
                "added_chats": after["captured_chats"] - before["captured_chats"],
                "added_messages": after["messages"] - before["messages"],
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "note": "Local archive only; ChatGPT account history and Memory are not restored.",
            }
            (private / "knowledge-import-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self._update(**report, error=None)
        except Exception as exc:
            self._update(phase="error",
                         message=("Database was updated, but the final report failed." if committed
                                  else "Import failed; main SQLite was not replaced."),
                         error=str(exc)[:1300], backup_path=str(backup) if backup else None)
        finally:
            staging.unlink(missing_ok=True)
            self._busy.release()

    @staticmethod
    def _sqlite_copy(source: Path, target: Path) -> None:
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(str(target))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()

    @staticmethod
    def _counts(path: Path) -> dict[str, Any]:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            return {
                "sqlite_quick_check": db.execute("PRAGMA quick_check").fetchone()[0],
                "captured_chats": db.execute("SELECT count(*) FROM chats WHERE captured=1").fetchone()[0],
                "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                "fts_records": db.execute("SELECT count(*) FROM kb_fts").fetchone()[0],
            }
        finally:
            db.close()
