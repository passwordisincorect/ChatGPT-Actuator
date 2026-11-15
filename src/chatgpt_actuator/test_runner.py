"""On-demand, owner-approved Maven jobs. Build code has the user's file rights."""
from contextvars import copy_context
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

from .background_process import run_bounded
from .resource_limits import validated
from .security.path_canonical import resolve_canonical
from .tool_policy import principal_context


def _redact(value, password=""):
    if isinstance(value, dict):
        return {k: _redact(v, password) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, password) for v in value]
    if not isinstance(value, str):
        return value
    if password:
        for secret in (password, quote(password, safe="")):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(password|passwd|token|secret|authorization)(\s*[=:]\s*)([^\s,;]+)", r"\1\2[REDACTED]", value)
    return re.sub(r"(\w+://[^\s/:]+:)[^\s@]+@", r"\1[REDACTED]@", value)


class TestRunner:
    __test__ = False

    def __init__(self, runtime, sessions):
        self.runtime, self.sessions = runtime, sessions
        self.root = sessions.path.parent / "test-runs"
        self.lock = threading.RLock()
        with sessions.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS test_runs(
                id TEXT PRIMARY KEY, session TEXT, profile TEXT, fingerprint TEXT,
                project TEXT, state TEXT, phase TEXT, cleanup_pending INTEGER,
                compose_project TEXT, result TEXT, created REAL)""")
            db.execute("CREATE TABLE IF NOT EXISTS test_cleanup_leases(run_id TEXT PRIMARY KEY,runtime_id TEXT NOT NULL)")

    def _principal(self):
        p = principal_context.get()
        if p is None:
            raise PermissionError("Authenticated session required")
        return p

    def _profile(self, name):
        settings = self.runtime.raw_config().get("security", {}).get("test_runner", {})
        if settings.get("enabled") is not True:
            raise PermissionError("Test runner disabled by owner policy")
        cfg = settings.get("profiles", {}).get(name)
        if not isinstance(cfg, dict) or cfg.get("background_approved") is not True:
            raise PermissionError("Unknown or unapproved test profile")
        cfg = dict(cfg)
        for key in ('project_dir', 'java_home', 'maven_home', 'docker_executable', 'docker_compose_executable'):
            if key in cfg and not isinstance(cfg[key], str): raise ValueError('Profile paths must be strings')
        project = Path(cfg.get("project_dir", ""))
        if not project.is_absolute():
            raise ValueError("Project directory must be absolute")
        project = resolve_canonical(project, strict=True)
        roots = [resolve_canonical(Path(p), strict=True) for p in self.runtime.config.filesystem.allowed_roots]
        if not project.is_dir() or not any(project == p or p in project.parents for p in roots):
            raise PermissionError("Test project outside allowed workspace")
        from .control_plane_guard import protect_control_plane
        protect_control_plane(self.runtime, "test_run", self._principal(), {"project_dir": str(project)})
        if not (project / "pom.xml").is_file():
            raise ValueError("Test project requires pom.xml")
        services = cfg.get("services", [])
        if not isinstance(services, list) or any(s not in ("mysql", "redis", "rabbitmq") for s in services) or len(set(services)) != len(services):
            raise ValueError("Invalid test services")
        timeout = cfg.get("timeout_seconds", 900)
        limit = cfg.get("max_output_bytes", 262144)
        heap = cfg.get("heap_mb", 384)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError("Test timeout must be 1..3600 seconds")
        if type(limit) is not int or not 1024 <= limit <= 1048576:
            raise ValueError("Test output limit must be 1024..1048576")
        if type(heap) is not int or not 128 <= heap <= 1024:
            raise ValueError("Test heap must be 128..1024 MB")
        if type(cfg.get("require_tests", True)) is not bool:
            raise ValueError("require_tests must be boolean")
        budget = validated(self.runtime.raw_config().get("security", {}).get("resource_limits"))
        if heap >= budget["process_memory_mb"]:
            raise ValueError("JVM heap must leave room inside process memory budget")
        cfg.update(profile=name, project_dir=str(project), services=services, timeout_seconds=timeout, max_output_bytes=limit, heap_mb=heap)
        return cfg

    @staticmethod
    def _tools(cfg):
        paths, missing = {}, []
        for name in ("java_home", "maven_home"):
            value = Path(cfg.get(name, ""))
            if not value.is_absolute() or not value.is_dir():
                missing.append(name)
            else:
                paths[name] = value.resolve()
        if "java_home" in paths:
            paths["java"] = paths["java_home"] / "bin" / ("java.exe" if os.name == "nt" else "java")
            if not paths["java"].is_file(): missing.append("java executable")
        if "maven_home" in paths:
            jars = list((paths["maven_home"] / "boot").glob("plexus-classworlds-*.jar"))
            if len(jars) != 1: missing.append("Maven classworlds boot jar")
            else: paths["boot"] = jars[0]
            paths["conf"] = paths["maven_home"] / "bin" / "m2.conf"
            if not paths["conf"].is_file(): missing.append("Maven m2.conf")
        if cfg["services"]:
            docker = Path(cfg.get("docker_executable", ""))
            if not docker.is_absolute() or not docker.is_file(): missing.append("Docker executable")
            else:
                paths["docker"] = docker.resolve()
                plugin_name = 'docker-compose.exe' if os.name == 'nt' else 'docker-compose'
                explicit = cfg.get('docker_compose_executable')
                candidates = [Path(explicit)] if explicit else [docker.parent.parent / 'cli-plugins' / plugin_name, docker.parent / plugin_name]
                plugins = [p.resolve() for p in candidates if p.is_absolute() and p.is_file() and p.name == plugin_name]
                if plugins: paths['compose_plugin'] = plugins[0]
                else: missing.append('Docker Compose plugin (set docker_compose_executable)')
        return paths, missing

    def preflight(self, profile):
        self._principal()
        cfg = self._profile(profile)
        _, missing = self._tools(cfg)
        return {"profile": profile, "status": "blocked" if missing else "ready_not_probed",
                "missing": missing, "services": cfg["services"], "daemon_probed": False,
                "note": "File availability only; run probes Java, Maven and Docker. No services started."}

    def _row(self, ident):
        p = self._principal()
        with self.sessions.db() as db:
            # Recovery and a new cleanup claim must not race across backends.
            db.execute('BEGIN IMMEDIATE')
            query = "SELECT t.*,a.status,a.cancel,a.runtime_id FROM test_runs t JOIN attempts a ON a.id=t.id JOIN sessions s ON s.id=t.session WHERE t.id=? AND s.owner=?"
            row = db.execute(query, (ident, p.owner)).fetchone()
            if not row:
                raise PermissionError("Unknown or foreign test run")
            held = db.execute("SELECT 1 FROM handles WHERE kind='test_run' AND value=?", (json.dumps(ident),)).fetchone()
            if row[5] == 'running' or row[11] == 'running' or (held and not row[7]):
                cleanup_owner = db.execute('SELECT runtime_id FROM test_cleanup_leases WHERE run_id=?', (ident,)).fetchone()
                runtime_id = cleanup_owner[0] if cleanup_owner else row[13]
                owner = db.execute("SELECT pid,started FROM runtimes WHERE id=?", (runtime_id,)).fetchone()
                if not owner or not self.sessions._runtime_alive(*owner):
                    state = 'interrupted' if row[5] == 'running' else row[5]
                    result = row[9] or json.dumps({'status': state})
                    db.execute("UPDATE test_runs SET state=?,phase='finished' WHERE id=?", (state, ident))
                    db.execute("UPDATE attempts SET status=?,result=? WHERE id=? AND status='running'", (state, result, ident))
                    db.execute('UPDATE attempt_lifecycle SET finished=? WHERE id=? AND finished IS NULL', (time.time(), ident))
                    db.execute('DELETE FROM test_cleanup_leases WHERE run_id=?', (ident,))
                    if not row[7]: db.execute("DELETE FROM handles WHERE kind='test_run' AND value=?", (json.dumps(ident),))
                    row = db.execute(query, (ident, p.owner)).fetchone()
            return row

    def _update(self, ident, **values):
        if not values or any(k not in {"state", "phase", "cleanup_pending", "result"} for k in values):
            raise ValueError("Invalid run update")
        with self.sessions.db() as db:
            db.execute("UPDATE test_runs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?", (*values.values(), ident))

    def status(self, run_id):
        r = self._row(run_id)
        # Publication finishes only after the attempt and resource lease settle.
        with self.sessions.db() as db:
            finishing = r[5] != 'running' and (r[11] == 'running' or (not r[7] and db.execute("SELECT 1 FROM handles WHERE kind='test_run' AND value=?", (json.dumps(run_id),)).fetchone()))
        state = 'running' if finishing else r[5]
        return {"run_id": r[0], "profile": r[2], "status": state, "phase": "finishing" if finishing else "cancelling" if r[12] and r[5] == "running" else r[6],
                "cleanup_pending": bool(r[7]), "cancel_requested": bool(r[12]), "created": r[10],
                "stop_confirmed": r[5] == "cancelled" and not r[7]}

    def report(self, run_id):
        row = self._row(run_id)
        return {**self.status(run_id), "result": json.loads(row[9]) if row[9] else None}

    def stop(self, run_id):
        self._row(run_id)
        self.sessions.attempt_cancel(self._principal().owner, run_id)
        return self.status(run_id)

    def run(self, profile, attempt_key):
        p = self._principal()
        sid = self.sessions.require(p)
        cfg = self._profile(profile)
        fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
        # BEGIN IMMEDIATE serializes leases across sessions and backend processes.
        with self.lock, self.sessions.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT t.id,t.fingerprint FROM test_runs t JOIN attempts a ON a.id=t.id WHERE t.session=? AND a.key=?", (sid, attempt_key)).fetchone()
            if old:
                if old[1] != fingerprint: raise ValueError("ATTEMPT_CONFLICT: profile changed")
                ident = old[0]
            else:
                # Stale runtimes are recovered by owner status, then cleanup is explicit.
                if db.execute("SELECT 1 FROM test_runs WHERE state='running' OR cleanup_pending=1 LIMIT 1").fetchone():
                    raise PermissionError("TEST_RUNNER_BUSY: finish or clean the previous run")
                ident = None
        if ident:
            return {**self.status(ident), "replayed": True}
        # The session attempt key is also a durable anti-replay reservation.
        with self.lock:
            attempt = self.sessions.attempt_begin(sid, attempt_key, fingerprint, label="test:" + profile)
            ident = attempt["id"]
            if not attempt["created"]:
                return {**self.status(ident), "replayed": True}
            try:
                with self.sessions.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM test_runs WHERE state='running' OR cleanup_pending=1 LIMIT 1").fetchone():
                        raise PermissionError("TEST_RUNNER_BUSY: finish or clean the previous run")
                    db.execute("INSERT INTO test_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident, sid, profile, fingerprint, cfg["project_dir"], "running", "received", 0, "actuator-test-" + secrets.token_hex(12), None, time.time()))
                self.sessions.bind(sid, "test_run", ident)
                context = copy_context()
                thread = threading.Thread(target=lambda: context.run(self._execute, ident, cfg), name="actuator-test-run", daemon=True)
                thread.start()
            except BaseException:
                self.sessions.attempt_finish(ident, "failed", {"error": "dispatch_failed"})
                self._release(ident)
                self._update(ident, state="failed", phase="finished")
                raise
        return {**self.status(ident), "replayed": False}

    def _directory(self, ident):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", ident): raise ValueError("Invalid run ID")
        root = resolve_canonical(self.root, strict=False)
        path = resolve_canonical(root / ident, strict=False)
        if path.parent != root: raise PermissionError("Run directory escaped state root")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _environment(self, cfg, paths):
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        env["JAVA_HOME"] = str(paths.get("java_home", ""))
        env["PATH"] = os.pathsep.join([str(paths.get("java_home", Path(".")) / "bin"), str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")])
        return env

    def _invoke(self, ident, argv, cfg, env, deadline, *, cleanup=False):
        remaining = (90 if cleanup else max(.01, deadline - time.monotonic()))
        # Cleanup does not depend on the project directory surviving the build.
        cwd = str(self._directory(ident)) if cleanup else cfg['project_dir']
        return run_bounded(argv, cwd, env, remaining, cfg["max_output_bytes"],
                           (lambda: False) if cleanup else lambda: self.sessions.attempt_cancelled(ident),
                           limits=self.runtime.raw_config().get("security", {}).get("resource_limits"))

    @staticmethod
    def _ok(result):
        return result.get("exit_code") == 0 and not result.get("timed_out") and not result.get("cancelled")

    def _compose(self, ident, cfg, paths):
        row = self._row(ident)
        return [str(paths["docker"]), "--host", "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock", "compose", "--project-name", row[8], "--project-directory", str(self._directory(ident)), "--file", str(self._directory(ident) / "compose.json")]

    def _execute(self, ident, cfg):
        password = secrets.token_urlsafe(24)
        result = {"verification": "not_run", "steps": [], "tests": {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}}
        state, cleanup_pending = "failed", False
        paths, missing = self._tools(cfg)
        env = self._environment(cfg, paths)
        env["ACTUATOR_TEST_PASSWORD"] = password
        deadline = time.monotonic() + cfg["timeout_seconds"]
        def invoke(label, argv):
            self._update(ident, phase=label)
            response = self._invoke(ident, argv, cfg, env, deadline)
            result["steps"].append({"step": label, **_redact(response, password)})
            if not self._ok(response):
                result.update(exit_code=response.get("exit_code"), timed_out=response.get("timed_out", False), cancelled=response.get("cancelled", False))
                raise RuntimeError("step_failed")
            return response
        try:
            if not self.sessions.attempt_start(ident):
                state = "cancelled"
                return
            if missing:
                state = "blocked"
                result["missing"] = missing
                return
            directory = self._directory(ident)
            home = directory / "home"
            home.mkdir(exist_ok=True)
            env.update(USERPROFILE=str(home), HOME=str(home), MAVEN_USER_HOME=str(home / '.m2'), DOCKER_CONFIG=str(directory / 'docker-config'))
            self._docker_config(paths, Path(env['DOCKER_CONFIG']))
            invoke("java_probe", [str(paths["java"]), "-version"])
            java = [str(paths["java"]), "-Xmx" + str(cfg["heap_mb"]) + "m", "-XX:ActiveProcessorCount=2", "-Duser.home=" + str(home), "-classpath", str(paths["boot"]), "-Dclassworlds.conf=" + str(paths["conf"]), "-Dmaven.home=" + str(paths["maven_home"]), "-Dmaven.multiModuleProjectDirectory=" + cfg["project_dir"], "org.codehaus.plexus.classworlds.launcher.Launcher"]
            invoke("maven_probe", [*java, "--version"])
            if cfg["services"]:
                from .test_services import build_compose, build_environment, SERVICE_PORTS
                (directory / "compose.json").write_text(json.dumps(build_compose(cfg["services"])), encoding="utf-8")
                compose = self._compose(ident, cfg, paths)
                invoke("docker_probe", [str(paths["docker"]), "--host", compose[2], "info", "--format", "{{.OSType}}"])
                # Persist cleanup obligation BEFORE any daemon mutation.
                cleanup_pending = True
                self._update(ident, cleanup_pending=1)
                invoke("services_start", [*compose, "up", "--detach", "--wait", "--wait-timeout", "120"])
                for service in cfg["services"]:
                    mapping = invoke("port_" + service, [*compose, "port", service, str(SERVICE_PORTS[service])])["stdout"].strip()
                    match = re.fullmatch(r"127\.0\.0\.1:(\d{1,5})", mapping)
                    if not match or not 1 <= int(match[1]) <= 65535: raise ValueError("Invalid loopback service mapping")
                    env.update(build_environment(service, int(match[1]), password))
            before = self._reports(Path(cfg["project_dir"]))
            # Local empty settings prevent reading the user's Maven credentials.
            settings = directory / "settings.xml"
            settings.write_text('<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"/>', encoding="utf-8")
            self._update(ident, phase="maven_verify")
            response = self._invoke(ident, [*java, "--batch-mode", "--no-transfer-progress", "--settings", str(settings), "--global-settings", str(settings), "-Dmaven.repo.local=" + str(self.root / "maven-cache"), "-DargLine=-Xmx" + str(cfg["heap_mb"]) + "m -XX:ActiveProcessorCount=2", "-DforkCount=1", "-DreuseForks=true", "clean", "verify"], cfg, env, deadline)
            result["steps"].append({"step": "maven_verify", **_redact(response, password)})
            result["exit_code"] = response["exit_code"]
            result.update(timed_out=response.get("timed_out", False), cancelled=response.get("cancelled", False))
            after = self._reports(Path(cfg["project_dir"]))
            counts = result["tests"]
            for name, (signature, content) in after.items():
                if before.get(name, (None,))[0] == signature: continue
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper(): raise ValueError("Unsupported test report XML")
                xml = ET.fromstring(content)
                suites = [xml] if xml.tag == "testsuite" else list(xml.findall("testsuite"))
                if not suites: raise ValueError("Invalid test report")
                for suite in suites:
                    for key in counts:
                        value = int(suite.attrib.get(key, "0"))
                        if not 0 <= value <= 1000000: raise ValueError("Invalid test count")
                        counts[key] += value
            if not self._ok(response):
                result["verification"] = "failed"
                state = "cancelled" if response.get("cancelled") else "timed_out" if response.get("timed_out") else "failed"
            elif counts["failures"] or counts["errors"]:
                result["verification"] = "failed"
            elif counts["tests"] > counts["skipped"]:
                result["verification"], state = "passed", "succeeded"
            elif cfg.get("require_tests", True):
                result["verification"] = "no_executed_tests"
            else:
                result["verification"], state = "build_only", "succeeded"
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            if self.sessions.attempt_cancelled(ident) or result.get("cancelled"): state = "cancelled"
            elif result.get("timed_out") or time.monotonic() >= deadline: state = "timed_out"
        finally:
            if cleanup_pending:
                self._update(ident, phase="cleanup")
                try:
                    down = self._invoke(ident, [*self._compose(ident, cfg, paths), "down", "--volumes", "--timeout", "10"], cfg, env, deadline, cleanup=True)
                    result["cleanup"] = _redact(down, password)
                    cleanup_pending = not self._ok(down)
                except Exception as exc:
                    result["cleanup"] = {"error_type": type(exc).__name__}
                if cleanup_pending: state = "cleanup_failed"
            if self.sessions.attempt_cancelled(ident) and state == "succeeded": state = "cancelled"
            result.update(status=state, cleanup_pending=cleanup_pending)
            result = _redact(result, password)
            self._bound_logs(result, cfg["max_output_bytes"])
            self._update(ident, state=state, phase="finished", cleanup_pending=int(cleanup_pending), result=json.dumps(result))
            self.sessions.attempt_finish(ident, state, result)
            if not cleanup_pending:
                self._release(ident)
            self.runtime.audit.record("test_run_finished", run_id=ident, status=state)

    def _release(self, ident):
        with self.sessions.db() as db:
            db.execute("DELETE FROM handles WHERE kind='test_run' AND value=?", (json.dumps(ident),))

    @staticmethod
    def _docker_config(paths, directory):
        if 'compose_plugin' in paths:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'config.json').write_text(json.dumps({'cliPluginsExtraDirs': [str(paths['compose_plugin'].parent)]}), encoding='utf-8')

    @staticmethod
    def _bound_logs(result, limit):
        remaining = limit
        for step in [*result.get("steps", []), result.get("cleanup", {}), result.get("cleanup_retry", {})]:
            for key in ("stdout", "stderr"):
                raw = step.get(key, "").encode("utf-8")
                step[key] = raw[:remaining].decode("utf-8", errors="ignore")
                if len(raw) > remaining: step["output_truncated"] = True
                remaining = max(0, remaining - len(raw))

    @staticmethod
    def _reports(project):
        reports, size, visited = {}, 0, 0
        for root, dirs, files in os.walk(project, followlinks=False):
            visited += 1
            if visited > 20000: raise ValueError("Project report scan limit exceeded")
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".m2", ".venv") and not (Path(root) / d).is_symlink() and not (hasattr(os.path, "isjunction") and os.path.isjunction(Path(root) / d))]
            directory = Path(root)
            if directory.name not in ("surefire-reports", "failsafe-reports") or directory.parent.name != "target": continue
            for name in files:
                if not name.startswith("TEST-") or not name.endswith(".xml"): continue
                path = directory / name
                canonical = resolve_canonical(path, strict=True)
                if project not in canonical.parents: raise PermissionError("Test report escaped project")
                stat = path.stat()
                size += stat.st_size
                if len(reports) >= 2000 or size > 16 * 1024 * 1024: raise ValueError("Test reports exceed budget")
                content = path.read_bytes()
                reports[str(path)] = ((stat.st_mtime_ns, hashlib.sha256(content).hexdigest()), content)
        return reports

    def cleanup(self, run_id):
        row = self._row(run_id)
        if row[5] == "running": raise PermissionError("Stop and await the run before cleanup")
        if not row[7]: return self.status(run_id)
        cfg = self._profile(row[2])
        if hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest() != row[3]:
            raise PermissionError("Approved profile changed; restore it before cleanup")
        paths, missing = self._tools(cfg)
        if "docker" not in paths: raise ValueError("Docker executable missing")
        if 'compose_plugin' not in paths: raise ValueError('Docker Compose plugin missing; cleanup not dispatched')
        env = self._environment(cfg, paths)
        env["ACTUATOR_TEST_PASSWORD"] = "cleanup-placeholder"
        env['DOCKER_CONFIG'] = str(self._directory(run_id) / 'docker-config')
        self._docker_config(paths, Path(env['DOCKER_CONFIG']))
        # Retain an exclusive lease while the daemon cleanup is dispatched.
        with self.sessions.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM test_runs WHERE id=? AND state='running'", (run_id,)).fetchone():
                raise PermissionError("Cleanup already running")
            db.execute("UPDATE test_runs SET state='running',phase='cleanup' WHERE id=?", (run_id,))
            db.execute('INSERT OR REPLACE INTO test_cleanup_leases VALUES(?,?)', (run_id, self.sessions.runtime_id))
        try:
            down = self._invoke(run_id, [*self._compose(run_id, cfg, paths), "down", "--volumes", "--timeout", "10"], cfg, env, time.monotonic(), cleanup=True)
            ok = self._ok(down)
            result = json.loads(row[9]) if row[9] else {}
            result["cleanup_retry"] = _redact(down)
            result.update(status="interrupted" if ok else "cleanup_failed", cleanup_pending=not ok)
            self._bound_logs(result, cfg['max_output_bytes'])
            with self.sessions.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE test_runs SET state=?,phase='finished',cleanup_pending=?,result=? WHERE id=?", (result['status'], int(not ok), json.dumps(result), run_id))
                db.execute('DELETE FROM test_cleanup_leases WHERE run_id=?', (run_id,))
                if ok: db.execute("DELETE FROM handles WHERE kind='test_run' AND value=?", (json.dumps(run_id),))
        except Exception:
            with self.sessions.db() as db:
                db.execute("UPDATE test_runs SET state='cleanup_failed',phase='finished' WHERE id=?", (run_id,))
                db.execute('DELETE FROM test_cleanup_leases WHERE run_id=?', (run_id,))
            raise
        return self.status(run_id)
