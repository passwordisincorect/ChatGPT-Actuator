"""Owner-only entry point; secrets arrive in local process environment, never argv."""
import json
import os
from pathlib import Path
import subprocess
import sys
from .tunnel_credentials import rotate, ValidationError

def restart(home, profile, health):
    exe=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    env=dict(os.environ);env.pop('CONTROL_PLANE_API_KEY',None)
    result=subprocess.run([str(exe),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
        '-File',str(home/'scripts/tunnel-restart-owner.ps1'),'-ProfileName',profile,'-HealthUrl',health],
        env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=55)
    if result.returncode: raise RuntimeError('Tunnel restart failed')

def run(home, profile, settings, key):
    from .work_sessions import WorkSessions
    sessions=WorkSessions(home/'config/work-sessions.sqlite3')
    lease=sessions.acquire_operation(None,'node.rotate_tunnel_key',exclusive=True)
    try:
        return rotate(home,profile,settings,key,lambda p,h:restart(home,p,h))
    finally:
        sessions.release_operation(lease)

def main():
    home=Path(os.environ['CHATGPT_ACTUATOR_HOME'])
    settings=json.loads((home/'deployment/tunnel-organizations.json').read_text())['profiles']['chatgpt-actuator']
    key=os.environ.pop('CONTROL_PLANE_API_KEY','')
    try:
        result=run(home,'chatgpt-actuator',settings,key)
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({'state':'failed','error_code':exc.code if isinstance(exc,ValidationError) else type(exc).__name__}))
        return 1
    return 0

if __name__=='__main__': sys.exit(main())
