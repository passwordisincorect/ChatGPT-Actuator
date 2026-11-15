"""Owner-defined fixed commands. Profiles are not an OS sandbox."""
import math
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from .background_process import run_bounded
from .tool_policy import principal_context

class CommandProfiles:
    def __init__(self, runtime):
        self.runtime = runtime
    def _profiles(self):
        value = self.runtime.raw_config().get("security", {}).get("command_profiles", {})
        if not isinstance(value, dict):
            raise ValueError("command_profiles must be an object")
        return value
    def list(self):
        return {"profiles": sorted(self._profiles()),
                "configuration": "owner-only security.command_profiles",
                "details": [{"name":n,"background_approved":v.get("background_approved") is True,
                    "timeout_seconds":v.get("timeout_seconds",30)} for n,v in self._profiles().items() if isinstance(v,dict)],
                "os_sandbox": False}
    def run(self, profile, *, attempt_key, sessions):
        profiles = self._profiles()
        if profile not in profiles:
            raise PermissionError("Unknown command profile")
        cfg = profiles[profile]
        if not isinstance(cfg, dict):
            raise ValueError("Invalid command profile")
        if cfg.get("background_approved") is not True:
            raise PermissionError("Profile requires owner background_approved=true; no visible fallback")
        exe_raw = Path(cfg["executable"])
        cwd_raw = Path(cfg["cwd"])
        if not exe_raw.is_absolute() or not cwd_raw.is_absolute():
            raise ValueError("Profile executable and cwd must be absolute")
        exe = exe_raw.resolve(strict=True)
        expected_executable=cfg.get('executable_sha256')
        if expected_executable is not None and hashlib.sha256(exe.read_bytes()).hexdigest()!=expected_executable:
            raise PermissionError('Approved executable changed; owner must approve it again')
        cwd = cwd_raw.resolve(strict=True)
        if not exe.is_file() or not cwd.is_dir():
            raise ValueError("Invalid executable or cwd")
        roots = [Path(p).resolve(strict=True) for p in self.runtime.config.filesystem.allowed_roots]
        if not any(cwd == p or p in cwd.parents for p in roots):
            raise PermissionError("Profile cwd outside workspace")
        args = cfg.get("arguments", [])
        guards=cfg.get('input_sha256',{})
        if not isinstance(guards,dict):raise ValueError('Invalid profile input checksums')
        for name,expected in guards.items():
            path=Path(name)
            if not path.is_absolute() or not isinstance(expected,str) or len(expected)!=64:
                raise ValueError('Invalid approved input')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise PermissionError('Approved workload changed; owner must approve it again')
        env_cfg = cfg.get("environment", {})
        if not isinstance(args,list) or not all(isinstance(v,str) for v in args):
            raise ValueError("Profile arguments must be fixed strings")
        if not isinstance(env_cfg,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env_cfg.items()):
            raise ValueError("Invalid profile environment")
        timeout = float(cfg.get("timeout_seconds",30))
        limit = cfg.get("max_output_bytes",1048576)
        if not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Profile timeout must be between 0 and 300 seconds")
        if type(limit) is not int or not 1 <= limit <= 4194304:
            raise ValueError("Invalid output limit")
        # No inherited PATH, PYTHONPATH, proxy or arbitrary user environment.
        env = {k:os.environ[k] for k in ("SystemRoot","WINDIR","TEMP","TMP") if k in os.environ}
        env.update(env_cfg)
        principal=principal_context.get()
        if principal is None: raise PermissionError("Authenticated session required")
        sid=sessions.require(principal)
        with sessions.db() as c:
            worktrees=c.execute("SELECT value FROM handles WHERE session=? AND kind='worktree'",(sid,)).fetchall()
        if worktrees:
            if cfg.get('worktree_cwd') is not True:
                raise PermissionError('Session worktree requires a worktree_cwd-approved profile')
            cwd=Path(json.loads(worktrees[0][0])).resolve(strict=True)
            if not any(cwd==p or p in cwd.parents for p in roots):raise PermissionError('Worktree cwd escaped configured roots')
        elif cfg.get('worktree_cwd') is True:raise PermissionError('Open a session worktree before running this profile')
        fingerprint=hashlib.sha256(json.dumps({"profile":profile,"config":cfg},sort_keys=True).encode()).hexdigest()
        attempt=sessions.attempt_begin(sid,attempt_key,fingerprint,label=profile)
        if not attempt["created"]:
            return {"attempt_id":attempt["id"],"status":attempt["status"],
                    "replayed":True,"result":attempt["result"]}
        try:
            if not sessions.attempt_start(attempt['id']):
                result={'attempt_id':attempt['id'],'status':'cancelled','phase':'finished','cancelled':True,'dispatched':False,'replayed':False}
                sessions.attempt_finish(attempt['id'],'cancelled',result)
                return result
            result=run_bounded([str(exe),*args],str(cwd),env,timeout,limit,
                               lambda:sessions.attempt_cancelled(attempt["id"]),
                               limits=self.runtime.raw_config().get("security",{}).get("resource_limits"))
            result.update(profile=profile,attempt_id=attempt["id"],replayed=False)
            status=('cancelled' if result['cancelled'] else 'timed_out' if result['timed_out']
                    else 'succeeded' if result['exit_code']==0 else 'failed')
            result['status']=status
            result['phase']='finished'
            sessions.attempt_finish(attempt["id"],status,result)
            self.runtime.audit.record("command_profile_run",profile=profile,attempt_id=attempt["id"],status=status)
            return result
        except BaseException as exc:
            sessions.attempt_finish(attempt["id"],'failed',{"error_type":type(exc).__name__})
            raise
