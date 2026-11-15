"""Private native file-dialog and report-grid reference fixture; no files written."""
import ctypes
from ctypes import wintypes as w
import win32gui,win32con,win32api
win32gui.InitCommonControls()
class OPENFILENAME(ctypes.Structure):
    _fields_=[('lStructSize',w.DWORD),('hwndOwner',w.HWND),('hInstance',w.HINSTANCE),('lpstrFilter',w.LPCWSTR),('lpstrCustomFilter',w.LPWSTR),('nMaxCustFilter',w.DWORD),('nFilterIndex',w.DWORD),('lpstrFile',w.LPWSTR),('nMaxFile',w.DWORD),('lpstrFileTitle',w.LPWSTR),('nMaxFileTitle',w.DWORD),('lpstrInitialDir',w.LPCWSTR),('lpstrTitle',w.LPCWSTR),('Flags',w.DWORD),('nFileOffset',w.WORD),('nFileExtension',w.WORD),('lpstrDefExt',w.LPCWSTR),('lCustData',ctypes.c_ssize_t),('lpfnHook',ctypes.c_void_p),('lpTemplateName',w.LPCWSTR),('pvReserved',ctypes.c_void_p),('dwReserved',w.DWORD),('FlagsEx',w.DWORD)]
def proc(hwnd,msg,wp,lp):
    if msg==win32con.WM_COMMAND and (wp&65535)==1101:
        buf=ctypes.create_unicode_buffer(1024)
        spec=OPENFILENAME(lStructSize=ctypes.sizeof(OPENFILENAME),hwndOwner=hwnd,lpstrFile=ctypes.cast(buf,w.LPWSTR),nMaxFile=1024,lpstrTitle='Actuator private file dialog',Flags=0x80000|0x8|0x1000)
        ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(spec));return 0
    if msg==win32con.WM_CLOSE:win32gui.DestroyWindow(hwnd);return 0
    if msg==win32con.WM_DESTROY:win32gui.PostQuitMessage(0);return 0
    return win32gui.DefWindowProc(hwnd,msg,wp,lp)
wc=win32gui.WNDCLASS();wc.lpszClassName='ActuatorAdvancedWorkspace';wc.lpfnWndProc=proc;wc.hInstance=win32api.GetModuleHandle(None);win32gui.RegisterClass(wc)
hwnd=win32gui.CreateWindowEx(0,wc.lpszClassName,'Advanced private controls',win32con.WS_OVERLAPPEDWINDOW,0,0,600,450,0,0,wc.hInstance,None)
common=win32con.WS_CHILD|win32con.WS_VISIBLE
win32gui.CreateWindowEx(0,'BUTTON','Open file dialog',common,10,10,200,30,hwnd,1101,wc.hInstance,None)
grid=win32gui.CreateWindowEx(0,'SysListView32','Report grid',common|win32con.WS_BORDER|1,10,60,520,300,hwnd,1102,wc.hInstance,None)
class COLUMN(ctypes.Structure):
    _fields_=[('mask',w.UINT),('fmt',ctypes.c_int),('cx',ctypes.c_int),('pszText',w.LPWSTR),('cchTextMax',ctypes.c_int),('iSubItem',ctypes.c_int),('iImage',ctypes.c_int),('iOrder',ctypes.c_int)]
class ITEM(ctypes.Structure):
    _fields_=[('mask',w.UINT),('iItem',ctypes.c_int),('iSubItem',ctypes.c_int),('state',w.UINT),('stateMask',w.UINT),('pszText',w.LPWSTR),('cchTextMax',ctypes.c_int),('iImage',ctypes.c_int),('lParam',ctypes.c_ssize_t),('iIndent',ctypes.c_int),('iGroupId',ctypes.c_int),('cColumns',w.UINT),('puColumns',ctypes.c_void_p),('piColFmt',ctypes.c_void_p),('iGroup',ctypes.c_int)]
label=ctypes.create_unicode_buffer('Row');col=COLUMN(mask=6,cx=400,pszText=ctypes.cast(label,w.LPWSTR));win32gui.SendMessage(grid,0x1061,0,ctypes.addressof(col))
for i in range(20):
    text=ctypes.create_unicode_buffer('Grid row '+str(i+1));item=ITEM(mask=1,iItem=i,pszText=ctypes.cast(text,w.LPWSTR));win32gui.SendMessage(grid,0x104d,0,ctypes.addressof(item))
win32gui.ShowWindow(hwnd,win32con.SW_SHOWNOACTIVATE);win32gui.PumpMessages()
