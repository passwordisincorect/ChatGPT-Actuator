from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock

from .config import project_root


class AuditLogger:
    def __init__(self, enabled: bool, log_file: str) -> None:
        self.enabled = enabled
        path = Path(log_file)
        self.path = path if path.is_absolute() else project_root() / path
        self._lock = Lock()

    def record(self, action: str, **fields: object) -> None:
        if not self.enabled:
            return

        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)

        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
