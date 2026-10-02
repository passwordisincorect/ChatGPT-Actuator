from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..audit import AuditLogger
from ..config import FileSystemConfig
from ..errors import BinaryFileError, FileTooLargeError
from ..security import PathGuard


class FileSystemService:
    def __init__(self, config: FileSystemConfig, audit: AuditLogger) -> None:
        self.config = config
        self.audit = audit
        self.guard = PathGuard(config.allowed_roots)

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PermissionError("Filesystem tools are disabled.")

    def _require(self, permission: str) -> None:
        self._require_enabled()
        if self.config.read_only:
            raise PermissionError("Filesystem is configured as read-only.")

        allowed = {
            "create": self.config.allow_create,
            "edit": self.config.allow_edit,
            "move": self.config.allow_move,
            "delete": self.config.allow_delete,
        }
        if not allowed.get(permission, False):
            raise PermissionError(f"Filesystem permission '{permission}' is disabled.")

    def list_roots(self) -> dict:
        self._require_enabled()
        roots = [str(path) for path in self.guard.roots]
        self.audit.record("filesystem_list_roots", count=len(roots))
        return {
            "read_only": self.config.read_only,
            "permissions": {
                "create": self.config.allow_create,
                "edit": self.config.allow_edit,
                "move": self.config.allow_move,
                "delete": self.config.allow_delete,
            },
            "allowed_roots": roots,
        }

    def list_directory(self, path: str, max_items: int | None = None) -> dict:
        self._require_enabled()
        resolved = self.guard.resolve_existing(path)
        if not resolved.is_dir():
            raise NotADirectoryError(str(resolved))

        limit = self._bounded_limit(max_items, self.config.max_list_items, self.config.max_list_items)
        entries = []
        truncated = False
        ordered = sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))

        for item in ordered:
            if len(entries) >= limit:
                truncated = True
                break
            try:
                safe_item = self.guard.resolve_existing(item)
            except Exception:
                continue
            try:
                stat = safe_item.stat()
                size = stat.st_size if safe_item.is_file() else None
                modified = stat.st_mtime
            except OSError:
                size = None
                modified = None

            entries.append({
                "name": safe_item.name,
                "path": str(safe_item),
                "type": "directory" if safe_item.is_dir() else "file",
                "size_bytes": size,
                "modified_unix": modified,
            })

        self.audit.record("filesystem_list", path=str(resolved), returned=len(entries), truncated=truncated)
        return {"path": str(resolved), "read_only": self.config.read_only, "items": entries, "truncated": truncated}

    def stat_path(self, path: str) -> dict:
        self._require_enabled()
        resolved = self.guard.resolve_existing(path)
        stat = resolved.stat()
        result = {
            "path": str(resolved),
            "name": resolved.name,
            "type": "directory" if resolved.is_dir() else "file",
            "size_bytes": stat.st_size if resolved.is_file() else None,
            "created_unix": stat.st_ctime,
            "modified_unix": stat.st_mtime,
            "read_only_server": self.config.read_only,
        }
        self.audit.record("filesystem_stat", path=str(resolved))
        return result

    def read_text(self, path: str, max_bytes: int | None = None) -> dict:
        self._require_enabled()
        resolved = self.guard.resolve_existing(path)
        if not resolved.is_file():
            raise IsADirectoryError(str(resolved))

        limit = self._bounded_limit(max_bytes, self.config.max_read_bytes, self.config.max_read_bytes)
        size = resolved.stat().st_size
        if size > limit:
            raise FileTooLargeError(f"File is {size} bytes; read limit is {limit} bytes.")

        data = resolved.read_bytes()
        if b"\x00" in data[:8192]:
            raise BinaryFileError(f"Binary file refused: {resolved}")

        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise BinaryFileError(f"File is not valid UTF-8 text: {resolved}") from exc

        self.audit.record("filesystem_read_text", path=str(resolved), size_bytes=size)
        return {"path": str(resolved), "encoding": "utf-8", "size_bytes": size, "content": text}

    def search(self, root: str, query: str, recursive: bool = True, max_results: int | None = None) -> dict:
        self._require_enabled()
        resolved_root = self.guard.resolve_existing(root)
        if not resolved_root.is_dir():
            raise NotADirectoryError(str(resolved_root))

        query = query.strip()
        if not query:
            raise ValueError("query must not be empty.")

        limit = self._bounded_limit(max_results, self.config.max_search_results, self.config.max_search_results)
        needle = query.casefold()
        iterator = resolved_root.rglob("*") if recursive else resolved_root.glob("*")
        results = []
        truncated = False

        for item in iterator:
            if needle not in item.name.casefold():
                continue
            try:
                safe_item = self.guard.resolve_existing(item)
            except Exception:
                continue

            results.append({
                "name": safe_item.name,
                "path": str(safe_item),
                "type": "directory" if safe_item.is_dir() else "file",
            })
            if len(results) >= limit:
                truncated = True
                break

        self.audit.record("filesystem_search", root=str(resolved_root), query=query, returned=len(results), truncated=truncated)
        return {"root": str(resolved_root), "query": query, "recursive": recursive, "results": results, "truncated": truncated}

    def create_directory(self, path: str, parents: bool = True, exist_ok: bool = False) -> dict:
        self._require("create")
        target = self.guard.resolve_target(path)
        target.mkdir(parents=parents, exist_ok=exist_ok)
        resolved = self.guard.resolve_existing(target)
        self.audit.record("filesystem_create_directory", path=str(resolved))
        return {"created": str(resolved), "type": "directory"}

    def write_text(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        create_parents: bool = True,
    ) -> dict:
        target = self.guard.resolve_target(path)

        if target.exists():
            self._require("edit")
            resolved_existing = self.guard.resolve_existing(target)
            if not resolved_existing.is_file():
                raise IsADirectoryError(str(resolved_existing))
            if not overwrite:
                raise FileExistsError(f"File already exists: {resolved_existing}. Set overwrite=true to replace it.")
            target = resolved_existing
            action = "overwrite"
        else:
            self._require("create")
            action = "create"
            if create_parents:
                parent = self.guard.resolve_target(target.parent)
                parent.mkdir(parents=True, exist_ok=True)
                self.guard.resolve_existing(parent)
            elif not target.parent.exists():
                raise FileNotFoundError(str(target.parent))

        target.write_text(content, encoding="utf-8")
        resolved = self.guard.resolve_existing(target)
        size = resolved.stat().st_size
        self.audit.record("filesystem_write_text", path=str(resolved), write_mode=action, size_bytes=size)
        return {"path": str(resolved), "action": action, "size_bytes": size, "encoding": "utf-8"}

    def replace_in_file(self, path: str, old_text: str, new_text: str, max_replacements: int = 1) -> dict:
        self._require("edit")
        if not old_text:
            raise ValueError("old_text must not be empty.")
        if max_replacements < 1:
            raise ValueError("max_replacements must be at least 1.")

        resolved = self.guard.resolve_existing(path)
        if not resolved.is_file():
            raise IsADirectoryError(str(resolved))

        original = self.read_text(str(resolved))["content"]
        occurrences = original.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found in the file.")

        replacements = min(occurrences, max_replacements)
        updated = original.replace(old_text, new_text, replacements)
        resolved.write_text(updated, encoding="utf-8")
        self.audit.record(
            "filesystem_replace_in_file",
            path=str(resolved),
            occurrences_found=occurrences,
            replacements_made=replacements,
        )
        return {
            "path": str(resolved),
            "occurrences_found": occurrences,
            "replacements_made": replacements,
        }

    def move_path(self, source: str, destination: str, overwrite: bool = False) -> dict:
        self._require("move")
        src = self.guard.resolve_existing(source)
        if self.guard.is_allowed_root(src):
            raise PermissionError("Configured allowed roots cannot be moved or renamed.")

        dst = self.guard.resolve_target(destination)

        if dst.exists():
            dst_existing = self.guard.resolve_existing(dst)
            if not overwrite:
                raise FileExistsError(f"Destination already exists: {dst_existing}")
            if dst_existing.is_dir():
                if any(dst_existing.iterdir()):
                    raise OSError("Refusing to overwrite a non-empty destination directory.")
                dst_existing.rmdir()
            else:
                dst_existing.unlink()

        parent = self.guard.resolve_target(dst.parent)
        parent.mkdir(parents=True, exist_ok=True)
        self.guard.resolve_existing(parent)

        shutil.move(str(src), str(dst))
        moved = self.guard.resolve_existing(dst)
        self.audit.record("filesystem_move", source=str(src), destination=str(moved))
        return {"source": str(src), "destination": str(moved)}

    def delete_path(self, path: str, recursive: bool = False) -> dict:
        self._require("delete")
        resolved = self.guard.resolve_existing(path)
        if self.guard.is_allowed_root(resolved):
            raise PermissionError("Configured allowed roots cannot be deleted.")

        item_type = "directory" if resolved.is_dir() else "file"

        if resolved.is_dir():
            if recursive:
                shutil.rmtree(resolved)
            else:
                resolved.rmdir()
        else:
            resolved.unlink()

        self.audit.record("filesystem_delete", path=str(resolved), recursive=recursive, type=item_type)
        return {"deleted": str(resolved), "type": item_type, "recursive": recursive}

    @staticmethod
    def _bounded_limit(value: int | None, default: int, maximum: int) -> int:
        limit = default if value is None else int(value)
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        return min(limit, maximum)
