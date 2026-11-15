"""Owner-local checks return labels only, never arguments or environment values."""
from pathlib import Path
import hashlib
import os
from .resource_limits import validated

VERSION_FLAGS={'python.exe':['--version'],'pythonw.exe':['--version'],'git.exe':['--version'],
 'cmake.exe':['--version'],'ctest.exe':['--version'],'g++.exe':['--version'],
 'arm-none-eabi-gcc.exe':['--version'],'kicad-cli.exe':['version']}

def inspect_profile(profile,roots,budget=None):
    result={'background_approved':profile.get('background_approved') is True,'executable':False,
            'executable_checksum':False,'cwd':False,'source_checksums':False,'resource_settings':False,'runtime_probe':'unverified'}
    exe=Path(profile.get('executable',''));cwd=Path(profile.get('cwd',''))
    result['executable']=exe.is_absolute() and exe.is_file()
    result['executable_checksum']=result['executable'] and profile.get('executable_sha256')==hashlib.sha256(exe.read_bytes()).hexdigest()
    result['cwd']=cwd.is_absolute() and cwd.is_dir() and any(cwd.resolve()==Path(p).resolve() or Path(p).resolve() in cwd.resolve().parents for p in roots)
    guards=profile.get('input_sha256',{})
    result['source_checksums']=isinstance(guards,dict) and all(Path(p).is_absolute() and Path(p).is_file() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in guards.items())
    try:validated(budget);result['resource_settings']=True
    except ValueError:pass
    result['configuration_ready']=all(result[k] for k in ('background_approved','executable','executable_checksum','cwd','source_checksums','resource_settings'))
    return result

def probe_profile(profile,roots,budget=None):
    result=inspect_profile(profile,roots,budget)
    if not result['configuration_ready']:return result
    exe=Path(profile['executable'])
    flags=VERSION_FLAGS.get(exe.name.lower())
    if flags is None:return result
    from .background_process import run_bounded
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP') if k in os.environ}
    env.update(profile.get('environment',{}))
    probe=run_bounded([str(exe),*flags],profile['cwd'],env,10,4096,limits=budget)
    result['runtime_probe']='verified' if probe['exit_code']==0 and not probe['timed_out'] else 'failed'
    return result
