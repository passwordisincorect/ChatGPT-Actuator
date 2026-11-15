"""Fixed owner-native commands. Never opens a browser, GUI or console window."""
import json,os
from pathlib import Path
import subprocess,time,re,uuid,tempfile
import httpx,psutil

PUBLIC_ENV=('SystemRoot','WINDIR','TEMP','TMP','LOCALAPPDATA','APPDATA','USERPROFILE','ProgramFiles','ProgramFiles(x86)','ProgramW6432','HOMEDRIVE','HOMEPATH','ComSpec','PSModulePath','USERDOMAIN','USERNAME','PATH')
def tunnel_profile_matches(argv,exe):
    import ntpath
    if len(argv)<2 or ntpath.normcase(argv[0])!=ntpath.normcase(str(exe)) or argv[1].casefold()!='run':return False
    profiles=[]
    for i,arg in enumerate(argv[2:],2):
        if arg.casefold()=='--profile':
            if i+1>=len(argv):return False
            profiles.append(argv[i+1])
        elif arg.casefold().startswith('--profile='):profiles.append(arg.split('=',1)[1])
    return len(profiles)==1 and profiles[0].casefold()=='chatgpt-actuator'

def processes_for_script(script,home=None):
    from .process_identity import python_target,target_for
    expected=target_for(script)
    result=[]
    for p in psutil.process_iter(['name']):
        if (p.info['name'] or '').casefold() not in {'python.exe','pythonw.exe'}:continue
        try:
            if python_target(p.cmdline(),p.cwd())!=expected:continue
            if expected[0]=='module' and (home is None or Path(p.cwd()).resolve()!=Path(home).resolve()):continue
            result.append(p)
        except (psutil.NoSuchProcess,psutil.AccessDenied):pass
    return result

def kill_owned(process):
    try:
        started,argv=process.create_time(),process.cmdline()
        current=psutil.Process(process.pid)
        if current.create_time()!=started or current.cmdline()!=argv:
            raise RuntimeError('Owned process identity changed')
        current.terminate()
        try:current.wait(5)
        except psutil.TimeoutExpired:
            if psutil.Process(current.pid).create_time()!=started:
                raise RuntimeError('Owned process PID was reused')
            current.kill();current.wait(5)
    except psutil.NoSuchProcess:pass

def powershell_file(exe,script,cwd,env,timeout=50,args=()):
    # A service can inherit a capture pipe and keep communicate() blocked after its launcher exits.
    # Use an actual file handle so readiness and timeouts do not depend on descendants closing pipes.
    with tempfile.TemporaryFile() as output:
        result=subprocess.run([str(exe),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),*args],cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=timeout)
        output.seek(0);signals=output.read(16384).decode(errors='replace')
    safe=[]
    for line in signals.splitlines():
        if re.fullmatch(r'(?:Started tunnel-client PID [0-9]+\. Waiting for health\.\.\.|Tunnel health is ready\.|Running:\s+(?:True|False)|PIDs:\s+[0-9, ]*|Profile:\s+chatgpt-actuator|Control Center (?:is already running at|started\.).*)',line):safe.append(line)
    directory=Path(cwd)/'deployment/owner-command-history';directory.mkdir(parents=True,exist_ok=True)
    (directory/(uuid.uuid4().hex+'.json')).write_text(json.dumps({'script':Path(script).name,'exit_code':result.returncode,'signals':safe,'checked_at':time.time()}))
    if result.returncode:raise RuntimeError('Owner PowerShell entry failed')

class NativeRunner:
    def __init__(self,home,version):
        if os.name!='nt':raise RuntimeError('Windows owner launcher required')
        self.home=Path(home);self.version=version
        self.env={k:os.environ[k] for k in PUBLIC_ENV if k in os.environ}
        self.env.update(PYTHONPATH=str(self.home/'src'),PYTHONDONTWRITEBYTECODE='1',CHATGPT_ACTUATOR_HOME=str(self.home),CHATGPT_ACTUATOR_CONFIG=str(self.home/'config/config.json'))
        self.python=self.home/'.venv/Scripts/pythonw.exe'
        self.ps=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        # PowerShell 7's inherited module path can make Windows PowerShell load incompatible assemblies.
        self.env['PSModulePath']=str(self.ps.parent/'Modules')+os.pathsep+str(Path(os.environ['ProgramFiles'])/'WindowsPowerShell/Modules')
        self.env['PATH']=os.pathsep.join([str(self.ps.parent),str(Path(os.environ['SystemRoot'])/'System32'),self.env.get('PATH','')])
        self.gateway=self.home.parent/'MCP-Funnel-Gateway'
        self.http=Path(os.environ['LOCALAPPDATA'])/'ChatGPT-Actuator-Autorecovery/http_mcp.py'
    def integrity(self):
        from .runtime_integrity import verify_active
        verify_active(self.home,self.version)
    def tunnels(self):
        exe=Path(os.environ['LOCALAPPDATA'])/'OpenAI/TunnelClient/tunnel-client.exe';found=[]
        for p in psutil.process_iter(['name']):
            if (p.info['name'] or '').casefold()!='tunnel-client.exe':continue
            try:
                if tunnel_profile_matches(p.cmdline(),exe):found.append(p)
            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
        return found
    def supervisor(self):
        slot=self.home/'runtime-versions'/self.version/'src/chatgpt_actuator/service_supervisor.py'
        if not slot.is_file() or not (self.home/'deployment/study-service.json').is_file():return
        result=subprocess.run([str(self.home/'.venv/Scripts/python.exe'),'-I','-B',str(self.home/'scripts/ensure-service-supervisor.py')],cwd=self.home,env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=35)
        if result.returncode:raise RuntimeError('Supervisor readiness failed')
    def stop(self):
        resume=False;marker=self.home/'deployment/service-supervisor.json'
        if marker.is_file():
            data=json.loads(marker.read_text())
            try:
                p=psutil.Process(data['pid'])
                if p.create_time()!=data['created'] or not any(os.path.normcase(a)==os.path.normcase(str(self.home/'scripts/service-supervisor.pyw')) for a in p.cmdline()):raise RuntimeError('Supervisor ownership changed')
                from .service_control import request
                resume=request(self.home/'deployment/services/mailbox.sqlite3','status',timeout=3).get('managed') is True
                kill_owned(p)
            except psutil.NoSuchProcess:pass
        for p in self.tunnels():kill_owned(p)
        for script in (self.http,self.gateway/'gateway.py','chatgpt_actuator.control_center'):
            for p in processes_for_script(script,self.home):kill_owned(p)
        return resume
    def spawn_script(self,script):
        if not Path(script).is_file():raise RuntimeError('Required local service script missing')
        if not processes_for_script(script,self.home):subprocess.Popen([str(self.python),'-B',str(script)],cwd=Path(script).parent,env=self.env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    def local_services(self):
        self.spawn_script(self.http);self.spawn_script(self.gateway/'gateway.py')
        self.health('http://127.0.0.1:8188/healthz',30)
    def health(self,url,timeout):
        deadline=time.monotonic()+timeout
        with httpx.Client(timeout=2,trust_env=False,follow_redirects=False) as client:
            while time.monotonic()<deadline:
                try:
                    if client.get(url).status_code==200:return
                except httpx.HTTPError:pass
                time.sleep(.2)
        raise TimeoutError('Local service readiness deadline exceeded')
    def control_center(self):
        powershell_file(self.ps,self.home/'scripts/start-control-center.ps1',self.home,self.env,20)
        self.health('http://127.0.0.1:8764/healthz',10)
    def openai_start(self):
        script=self.home/'scripts/tunnel-start-protected.ps1'
        if not script.is_file():
            # Legacy autostart calls a helper with exit 0, which can end its shell before starting the tunnel.
            script=self.home/'scripts/tunnel-restart-owner.ps1'
            powershell_file(self.ps,script,self.home,self.env,50,('-ProfileName','chatgpt-actuator','-HealthUrl','http://127.0.0.1:8081/healthz'))
            return
        powershell_file(self.ps,script,self.home,self.env,30)
    def local_mcp(self):
        from .update_health import verify_backend
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            try:verify_backend('http://127.0.0.1:8090/mcp',self.version);return
            except Exception:time.sleep(.2)
        raise RuntimeError('Local MCP version verification failed')
    def gateway_mcp(self):
        from .update_health import verify_gateway
        verify_gateway(self.gateway,'http://127.0.0.1:8090/mcp')
    def owned_tunnel(self):
        selected=self.tunnels()
        if len(selected)!=1:raise RuntimeError('Expected exactly one owner tunnel')
        self.health('http://127.0.0.1:8081/healthz',15)
        listeners={c.pid for c in psutil.net_connections(kind='tcp') if c.status==psutil.CONN_LISTEN and c.laddr.port==8081}
        if listeners!={selected[0].pid}:raise RuntimeError('Tunnel health listener ownership mismatch')
    def resume_study(self):
        from .service_control import request
        result=request(self.home/'deployment/services/mailbox.sqlite3','start')
        if result.get('healthy') is not True:raise RuntimeError('Managed Study Coach restart failed')
