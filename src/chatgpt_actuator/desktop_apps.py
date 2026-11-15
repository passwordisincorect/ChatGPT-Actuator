"""Explicit visible app launch and target leases; no shell, keyboard or mouse."""
import os,shutil,subprocess,time
from pathlib import Path
import psutil,win32gui,win32process,winreg
BROWSERS={'msedge.exe','chrome.exe','firefox.exe','brave.exe','opera.exe','vivaldi.exe'}

def require(runtime):
    if runtime.raw_config().get('security',{}).get('desktop_uia',{}).get('enabled') is not True:raise PermissionError('Desktop UIA is not enabled by owner policy')

def resolve(application):
    if not isinstance(application,str) or not 1<=len(application)<=1024:raise ValueError('Provide an executable path or installed app name')
    path=Path(os.path.expandvars(application.strip())).expanduser()
    if path.is_file():candidate=path.resolve()
    else:
        name=application.strip();name=name if name.casefold().endswith('.exe') else name+'.exe'
        found=shutil.which(name)
        if not found:
            for root in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY,winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(root,'Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\'+name,0,winreg.KEY_READ|view) as key:found=winreg.QueryValue(key,None)
                        break
                    except OSError:pass
                if found:break
        if not found:raise FileNotFoundError('Executable not found; supply its installed .exe path')
        candidate=Path(os.path.expandvars(found.strip().strip('"'))).resolve(strict=True)
    if candidate.suffix.casefold()!='.exe':raise ValueError('Launch an executable, not a shell command')
    if candidate.name.casefold() in BROWSERS:raise PermissionError('Use DOM browser tools for browsers')
    return candidate

def open_app(runtime,application,arguments):
    require(runtime);exe=resolve(application)
    if not isinstance(arguments,list) or len(arguments)>32 or any(not isinstance(v,str) or len(v)>4096 for v in arguments):raise ValueError('Bounded argument list required')
    env={k:os.environ[k] for k in ('SystemRoot','WINDIR','PATH','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','ProgramFiles','ProgramFiles(x86)') if k in os.environ}
    startup=subprocess.STARTUPINFO();startup.dwFlags=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=1
    process=subprocess.Popen([str(exe),*arguments],cwd=str(exe.parent),env=env,shell=False,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,startupinfo=startup)
    # Some packaged apps hand off to an existing process. Return windows by
    # executable identity, without claiming ownership or permission to kill it.
    windows=[];deadline=time.monotonic()+5
    while time.monotonic()<deadline and not windows:
        try:related={process.pid,*(p.pid for p in psutil.Process(process.pid).children(recursive=True))}
        except psutil.Error:related={process.pid}
        def collect(hwnd,_):
            if not win32gui.IsWindowVisible(hwnd):return
            pid=win32process.GetWindowThreadProcessId(hwnd)[1]
            try:
                if pid in related or (process.poll() is not None and Path(psutil.Process(pid).exe()).resolve()==exe):windows.append({'target_hwnd':hwnd,'pid':pid,'title':win32gui.GetWindowText(hwnd)[:512]})
            except (psutil.Error,OSError):pass
        win32gui.EnumWindows(collect,None)
        if not windows:time.sleep(.05)
    return {'launched':True,'launcher_pid':process.pid,'executable':str(exe),'windows':windows,'mode':'visible_desktop','may_activate_window':True,'terminate_on_release':False}
