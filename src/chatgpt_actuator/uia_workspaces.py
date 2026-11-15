"""Owner-defined apps in private desktops, held Jobs and session-scoped handles."""
import os,sys,secrets,threading
from pathlib import Path
from .background_process import private_desktop
from .windows_process import WindowsProcess
from .windows_job import WindowsJob
from .automation_errors import PolicyDenied

class UIAWorkspaces:
    def __init__(self,runtime,worker):
        self.runtime,self.worker=runtime,worker;self.items={};self.lock=threading.RLock()
    def profiles(self):
        return {'profiles':['win32_controls','win32_advanced_controls','win32_virtual_controls',*self.runtime.raw_config().get('security',{}).get('uia_profiles',{})],
                'mode':'private_desktop','arbitrary_launch':False}
    def open(self,owner,session,profile):
        with self.lock:
            if sum(item.get('mode')!='visible_desktop' for item in self.items.values())>=4:raise PermissionError('UIA workspace budget reached')
            home=self.runtime.config_path.parent.parent
            if profile in ('win32_controls','win32_advanced_controls','win32_virtual_controls'):
                fixture={'win32_controls':'win32_workspace_app.py','win32_advanced_controls':'win32_advanced_app.py','win32_virtual_controls':'win32_virtual_app.py'}[profile]
                argv=[str(Path(sys.executable).with_name('python.exe')),'-I','-B',str(Path(__file__).with_name(fixture))]
                cwd=home
            else:
                cfg=self.runtime.raw_config().get('security',{}).get('uia_profiles',{}).get(profile)
                if not isinstance(cfg,dict) or cfg.get('background_approved') is not True:raise PermissionError('Owner-approved UIA profile required')
                exe=Path(cfg['executable']).resolve(strict=True);cwd=Path(cfg['cwd']).resolve(strict=True)
                roots=[Path(p).resolve() for p in self.runtime.config.filesystem.allowed_roots]
                if not exe.is_file() or not any(cwd==p or p in cwd.parents for p in roots):raise PermissionError('Invalid owner profile path')
                args=cfg.get('arguments',[])
                if not isinstance(args,list) or any(not isinstance(a,str) for a in args):raise ValueError('Fixed arguments required')
                argv=[str(exe),*args]
            context=private_desktop();desktop=context.__enter__();process=job=None
            try:
                env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','ProgramFiles','ProgramFiles(x86)') if k in os.environ}
                process=WindowsProcess(argv,str(cwd),env,desktop);job=WindowsJob(process,self.worker.resource_limits);process.resume()
                key=secrets.token_urlsafe(24);self.items[key]={'owner':owner,'session':session,'desktop':desktop,'context':context,'process':process,'job':job,'profile':profile,'lock':threading.RLock()}
                return key
            except BaseException:
                if job:job.close()
                if process:process.close()
                context.__exit__(None,None,None);raise
    def require(self,key,owner,session):
        item=self.items.get(key)
        if not item or item['owner']!=owner or item['session']!=session:raise PolicyDenied('wrong_owner')
        return item
    def release_desktop_session(self,owner,session):
        with self.lock:
            for key,item in list(self.items.items()):
                if item.get('mode')=='visible_desktop' and item['owner']==owner and item['session']==session:
                    with item['lock']:self.items.pop(key,None)
    def attach_desktop(self,owner,session,hwnd):
        from .desktop_apps import require,BROWSERS
        require(self.runtime)
        from .uia_control import identity
        import psutil
        current=identity(hwnd)
        if psutil.Process(current['pid']).name().casefold() in BROWSERS:raise PermissionError('Use DOM for browser windows')
        with self.lock:
            for key,item in list(self.items.items()):
                if item.get('mode')=='visible_desktop' and item.get('expires',0)<__import__('time').time():
                    with item['lock']:self.items.pop(key,None)
            if len(self.items)>=16:raise PermissionError('Target lease budget reached')
            key=secrets.token_urlsafe(24);item={'owner':owner,'session':session,'desktop':None,'mode':'visible_desktop','lock':threading.RLock()}
            self.items[key]=item
            try:
                target=self.worker.workspace_bind({'desktop':None,'desktop_control':True},hwnd)
                item['expires']=target['expires']
                return {**target,'workspace':key,'desktop':None,'desktop_control':True}
            except BaseException:self.items.pop(key,None);raise
    def windows(self,key,owner,session):
        item=self.require(key,owner,session)
        with item['lock']:
            result=self.worker.workspace_windows({'desktop':item['desktop']})
            windows=[w for w in result['windows'] if item['job'].contains_pid(w['pid'])]
            windows.sort(key=lambda w:(not w.get('visible',False),bool(w.get('owner_hwnd')),not bool(w['name'])))
            return {'windows':windows}
    def bind(self,key,owner,session,hwnd):
        item=self.require(key,owner,session)
        with item['lock']:
            if not any(w['target_hwnd']==hwnd for w in self.windows(key,owner,session)['windows']):raise PolicyDenied('ownership_lost')
            target=self.worker.workspace_bind({'desktop':item['desktop']},hwnd)
            return {**target,'workspace':key,'private_desktop':True,'desktop':item['desktop']}
    def call(self,target,owner,session,method,element_ref=None,**kwargs):
        item=self.require(target['workspace'],owner,session)
        with item['lock']:
            if item.get('mode')=='visible_desktop':
                from .desktop_apps import require
                require(self.runtime)
                if not target.get('desktop_control'):raise PolicyDenied('ownership_lost')
            elif target['desktop']!=item['desktop'] or not item['job'].contains_pid(target['pid']):raise PolicyDenied('ownership_lost')
            return self.worker.control_read(target,method,**kwargs) if element_ref is None else self.worker.control_action(target,method,element_ref,**kwargs)
    def close(self,key,owner,session):
        item=self.require(key,owner,session)
        with item['lock']:
            if item.get('mode')=='visible_desktop':
                self.items.pop(key,None);return {'released':True,'closed':False,'application_terminated':False}
            item['job'].close();item['process'].wait(timeout=3);item['process'].close()
            item['process'].stdout.close();item['process'].stderr.close();item['context'].__exit__(None,None,None)
            self.items.pop(key,None)
        return {'closed':True}
