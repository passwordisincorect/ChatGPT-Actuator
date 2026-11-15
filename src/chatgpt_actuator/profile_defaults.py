"""Owner-local discovery; never enables an arbitrary project build script."""
from pathlib import Path
import os
import shutil
import hashlib

def discover(home,package):
    home=Path(home);workspace=home.parent/'Actuator-Workspace'
    python=home/'.venv/Scripts/python.exe'
    result={}
    def add(name,exe,args,cwd=workspace,**extra):
        if exe and Path(exe).is_file():
            result[name]={'executable':str(Path(exe).resolve()),'arguments':args,'cwd':str(cwd),
                'background_approved':True,'timeout_seconds':60,'max_output_bytes':131072,**extra}
    add('python-version',python,['-I','-B','--version'])
    add('background-selftest',python,['-I','-B',str(home/'runtime-versions/background2r1-20261011/src/chatgpt_actuator/profile_selftest.py'),str(home)])
    git=shutil.which('git')
    for folder in ('ChatGPT-Actuator-GitHub-Correct-Staging','ChatGPT-Actuator-GitHub-Staging','ChatGPT-Actuator-GitHub-Compare'):
        repo=home.parent/folder
        if (repo/'.git').exists():
            add('actuator-git-status',git,['--no-optional-locks','-c','core.fsmonitor=false','-c','core.untrackedCache=false','-c','core.hooksPath='+str(workspace/'empty-hooks'),'status','--short'],cwd=repo,
                environment={'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull})
            break
    add('arm-toolchain-version',shutil.which('arm-none-eabi-gcc'),['--version'])
    pointer=home/'runtime-current.json'
    version='reliability1-20261011'
    runner=home/'runtime-versions'/version/'src/chatgpt_actuator/profile_workloads.py'
    # Runner is installed into the candidate immutable slot before activation.
    study=home.parent/'Study-Coach'
    if (study/'server.py').is_file():
        add('study-coach-health',python,['-I','-B',str(runner),'study-health'])
        guards={str(p.resolve()):hashlib.sha256(p.read_bytes()).hexdigest() for p in study.glob('*.py')}
        add('study-coach-tests',python,['-I','-B',str(runner),'study-tests',str(study)],
            cwd=study,input_sha256=guards)
    kicad=shutil.which('kicad-cli')
    if not kicad:
        for path in sorted((Path(os.environ.get('ProgramFiles','C:/Program Files'))/'KiCad').glob('*/bin/kicad-cli.exe'),reverse=True):kicad=str(path);break
    add('kicad-cli-version',kicad,['version'])
    return result
