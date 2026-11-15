from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from urllib.parse import urlsplit,urlunsplit
from .atomic_files import file_lock
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
            **self._redact(fields),
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)

        with self._lock,file_lock(self.path):
            if self.path.exists() and self.path.stat().st_size >= 10*1024*1024:
                for index in (3,2,1):
                    older=self.path.with_name(self.path.name+'.'+str(index))
                    if index==3:older.unlink(missing_ok=True)
                    else:
                        newer=self.path.with_name(self.path.name+'.'+str(index+1))
                        if older.exists():older.replace(newer)
                self.path.replace(self.path.with_name(self.path.name+'.1'))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    @staticmethod
    def _redact(fields):
        out={}
        for key,value in fields.items():
            lowered=key.casefold()
            if any(word in lowered for word in ('token','cookie','password','secret','authorization','api_key','private_key')):
                out[key]='[redacted]'
            elif lowered in {'content','text','value','script','arguments','stdout','stderr'}:
                data=json.dumps(value,ensure_ascii=False,default=str).encode()
                out[key+'_bytes']=len(data)
                out[key+'_sha256']=hashlib.sha256(data).hexdigest()
            elif 'url' in lowered and isinstance(value,str):
                u=urlsplit(value)
                out[key]=urlunsplit((u.scheme,u.hostname or '',u.path,'',''))
            else:out[key]=value
        return out
