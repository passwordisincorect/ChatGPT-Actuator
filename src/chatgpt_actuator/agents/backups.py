"""Owner-local queue snapshots and isolated, nonexecuting restore drills."""
from contextlib import closing
from contextlib import contextmanager
import os
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
from . import clocks

MAX_BYTES = 64 * 1024 * 1024


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class AgentBackups:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.base = self.home / 'backups/agent-state'
        self.base.mkdir(parents=True, exist_ok=True)

    def location(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not re.fullmatch('[a-f0-9]{32}', snapshot_id):
            raise ValueError('Invalid snapshot ID')
        original = self.base / snapshot_id
        target = original.resolve()
        if target.parent != self.base.resolve() or original.is_symlink():
            raise ValueError('Snapshot path escapes backup root')
        return target

    def inventory(self):
        records = []
        for folder in self.base.iterdir():
            if re.fullmatch('[a-f0-9]{32}', folder.name) and not folder.is_symlink():
                try:
                    doc = json.loads((self.location(folder.name) / 'manifest.json').read_text())
                    records.append({**{k: doc[k] for k in ('id', 'created', 'bytes', 'version', 'sha256')},
                                    'generation': doc.get('generation', 0), 'clock': doc.get('clock')})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(records, key=lambda d: (d['generation'], d['created'], d['id']), reverse=True)

    @contextmanager
    def locked(self):
        with (self.base / '.lock').open('a+b') as stream:
            if stream.seek(0, 2) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def create(self):
        with self.locked():
            self.reap_pending()
            return self._create()

    def reap_pending(self):
        removed = 0
        for folder in self.base.iterdir():
            if removed >= 10:
                break
            if not re.fullmatch(r'\.pending-[a-f0-9]{32}', folder.name) or folder.is_symlink() or folder.resolve().parent != self.base.resolve():
                continue
            allowed = {'missions.sqlite3', 'missions.sqlite3-journal', 'manifest.json'}
            if any(p.name not in allowed or not p.is_file() or p.is_symlink() for p in folder.iterdir()):
                continue
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
            removed += 1
        return removed

    def _create(self):
        source = self.home / 'config/missions.sqlite3'
        if not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise RuntimeError('Queue database unavailable or exceeds 64 MiB snapshot limit')
        if shutil.disk_usage(self.base).free < MAX_BYTES * 2:
            raise RuntimeError('Insufficient space for a safe backup')
        pointer = (self.home / 'runtime-current.json').read_bytes()
        snapshot_id = uuid.uuid4().hex
        final = self.location(snapshot_id)
        folder = self.base / ('.pending-' + snapshot_id)
        folder.mkdir()
        target = folder / 'missions.sqlite3'
        deadline = time.monotonic() + 15
        try:
            def progress(status, remaining, total):
                if total * page_size > MAX_BYTES or time.monotonic() > deadline:
                    raise RuntimeError('Snapshot size/time limit reached')
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=2)) as src, \
                 closing(sqlite3.connect(target)) as dst:
                page_size = src.execute('PRAGMA page_size').fetchone()[0]
                if page_size * src.execute('PRAGMA page_count').fetchone()[0] > MAX_BYTES:
                    raise RuntimeError('Snapshot page limit reached')
                src.backup(dst, pages=128, progress=progress, sleep=.01)
                if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise RuntimeError('Snapshot SQLite integrity check failed')
            if pointer != (self.home / 'runtime-current.json').read_bytes() or target.stat().st_size > MAX_BYTES:
                raise RuntimeError('Runtime changed during backup or snapshot oversized')
            doc = {'id': snapshot_id, 'created': time.time(), 'version': json.loads(pointer)['version'],
                   'generation': max((r['generation'] for r in self.inventory()), default=0) + 1, 'clock': clocks.point(),
                   'bytes': target.stat().st_size, 'sha256': digest(target),
                   'scope': 'queue_missions_archives_keys_only; workspace files and credentials excluded', 'status': 'PENDING'}
            (folder / 'manifest.json').write_text(json.dumps(doc), encoding='utf-8')
            drill = self._verify(folder, snapshot_id)
            doc.update(status='VERIFIED', verified_at=time.time())
            (folder / 'manifest.json').write_text(json.dumps(doc), encoding='utf-8')
            folder.rename(final)  # Publish only after the complete restore drill.
        except Exception:
            for name in ('missions.sqlite3', 'manifest.json', 'missions.sqlite3-journal'):
                (folder / name).unlink(missing_ok=True)
            folder.rmdir()
            raise
        # Legacy snapshots must also pass a drill before becoming rotation candidates.
        verified, deferred = [], 0
        for old in self.inventory():
            try:
                self._verify(self.location(old['id']), old['id'])
                verified.append(old)
            except Exception:
                deferred += 1  # Preserve uncertain old data; never sacrifice a good copy.
        for old in verified[3:]:
            old_folder = self.location(old['id'])
            try:
                for name in ('manifest.json', 'missions.sqlite3'):
                    (old_folder / name).unlink()
                old_folder.rmdir()
            except OSError:
                deferred += 1
        self.record_drill(drill)
        return {**doc, 'state': 'PASS', 'retention_deferred': deferred}

    def drill(self, snapshot_id):
        with self.locked():
            return self._drill(snapshot_id)

    def _drill(self, snapshot_id):
        folder = self.location(snapshot_id)
        report = self._verify(folder, snapshot_id)
        self.record_drill(report)
        return report

    def _verify(self, folder, snapshot_id):
        doc = json.loads((folder / 'manifest.json').read_text())
        source = folder / 'missions.sqlite3'
        if doc['id'] != snapshot_id or source.stat().st_size > MAX_BYTES or digest(source) != doc['sha256']:
            raise RuntimeError('Snapshot integrity mismatch')
        # A temporary copy is opened read-only; never activate a restored worker.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='agent-restore-', dir=self.base) as temp:
            deadline = time.monotonic() + 15
            target = Path(temp) / 'restored.sqlite3'
            shutil.copyfile(source, target)
            with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
                    raise RuntimeError('Restore consistency failed')
                counts = {}
                for table in ('agent_jobs', 'missions', 'agent_archives'):
                    counts[table] = db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('agent_jobs', 'missions'):
                    if db.execute('SELECT 1 FROM ' + table + ' GROUP BY owner,request_key HAVING COUNT(*)>1').fetchone():
                        raise RuntimeError('Duplicate retained keys')
                for job, payload, raw_size in db.execute('SELECT job,payload,raw_size FROM agent_archives WHERE payload IS NOT NULL'):
                    if time.monotonic() > deadline:
                        raise RuntimeError('Restore drill time limit reached')
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                        raw = stream.read(4 * 1024 * 1024 + 1)
                    if len(raw) != raw_size or len(raw) > 4 * 1024 * 1024 or json.loads(raw)['job']['id'] != job:
                        raise RuntimeError('Archive payload mismatch')
                quarantine = db.execute('SELECT COUNT(*) FROM agent_workspace_locks').fetchone()[0]
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_artifacts'").fetchone():
                    from .artifacts import unpack
                    for before_sha,after_sha,payload,raw_size in db.execute('SELECT before_sha,after_sha,payload,raw_size FROM agent_artifacts WHERE payload IS NOT NULL'):
                        if time.monotonic()>deadline:
                            raise RuntimeError('Artifact restore drill time limit reached')
                        bundle=unpack(payload,raw_size)
                        if hashlib.sha256(bundle['after'].encode('utf-8')).hexdigest()!=after_sha or (bundle['before'] is not None and hashlib.sha256(bundle['before'].encode('utf-8')).hexdigest()!=before_sha):
                            raise RuntimeError('Artifact restoration hash mismatch')
            report = {'state': 'PASS', 'snapshot_id': snapshot_id, 'counts': counts,
                    'retained_resource_locks': quarantine, 'executed_jobs': 0,
                    'live_database_replaced': False, 'workspace_files_restored': False}
            return report

    def record_drill(self, report):
        pending = self.base / 'last-drill.pending.json'
        pending.write_text(json.dumps({**report, 'checked_at': time.time()}), encoding='utf-8')
        pending.replace(self.base / 'last-drill.json')

    def maybe_due(self):
        records = self.inventory()
        due = not records
        if records:
            old, now = records[0].get('clock'), clocks.point()
            due = (not old or old['boot'] != now['boot'] or now['tick'] < old['tick'] or now['tick'] - old['tick'] >= 21600)
        if due:
            return self.create()
        return {'state': 'not_due'}
