"""Git worktrees stay inside configured roots and belong to a single Work Session."""
from pathlib import Path
import subprocess
from .control_plane_guard import protect_control_plane

def git(args,cwd):
    return subprocess.run(['git',*args],cwd=cwd,stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15,check=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def dirty_status(path):
    try: return 'dirty' if git(['status','--porcelain','--untracked-files=all'],path).stdout else 'clean'
    except (OSError,subprocess.SubprocessError): return 'unavailable'

class Worktrees:
    def __init__(self,runtime,sessions): self.runtime,self.sessions=runtime,sessions
    def create(self,principal,repository):
        repo=Path(repository).resolve(strict=True)
        roots=[Path(p).resolve(strict=True) for p in self.runtime.config.filesystem.allowed_roots]
        if not any(repo==p or p in repo.parents for p in roots): raise PermissionError('Repository outside configured roots')
        protect_control_plane(self.runtime,'worktree_create',principal,{'path':str(repo)})
        sid=self.sessions.require(principal)
        git(['rev-parse','--show-toplevel'],repo)
        base=(repo.parent/(repo.name+'.actuator-worktrees')).resolve()
        if not any(base==p or p in base.parents for p in roots): raise PermissionError('Worktree base escaped workspace')
        base.mkdir(exist_ok=True)
        dest=base/sid
        if dest.exists():
            raise FileExistsError('Session already has a worktree; inspect it instead of overwriting')
        branch='actuator/'+sid
        git(['worktree','add','-b',branch,str(dest),'HEAD'],repo)
        try: self.sessions.bind(sid,'worktree',str(dest))
        except BaseException:
            # No implicit cleanup of potentially modified content.
            raise RuntimeError('Worktree was created but registration failed; owner cleanup required')
        build=dest/'.actuator-build';build.mkdir()
        return {'session_id':sid,'worktree':str(dest),'branch':branch,'build_dir':str(build),
                'cleanup':'owner-only; dirty worktrees are never deleted automatically'}
