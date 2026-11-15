"""Owned native LVS_OWNERDATA grid; no provider realization or physical input."""
import ctypes
from ctypes import wintypes as w
import win32gui,win32con,win32api
win32gui.InitCommonControls()
class HEADER(ctypes.Structure):
    _fields_=[('hwndFrom',w.HWND),('idFrom',ctypes.c_size_t),('code',w.UINT)]
class ITEM(ctypes.Structure):
    _fields_=[('mask',w.UINT),('iItem',ctypes.c_int),('iSubItem',ctypes.c_int),('state',w.UINT),('stateMask',w.UINT),('pszText',w.LPWSTR),('cchTextMax',ctypes.c_int),('iImage',ctypes.c_int),('lParam',ctypes.c_ssize_t),('iIndent',ctypes.c_int),('iGroupId',ctypes.c_int),('cColumns',w.UINT),('puColumns',ctypes.c_void_p),('piColFmt',ctypes.c_void_p),('iGroup',ctypes.c_int)]
class DISPLAY(ctypes.Structure):_fields_=[('hdr',HEADER),('item',ITEM)]
class COLUMN(ctypes.Structure):
    _fields_=[('mask',w.UINT),('fmt',ctypes.c_int),('cx',ctypes.c_int),('pszText',w.LPWSTR),('cchTextMax',ctypes.c_int),('iSubItem',ctypes.c_int),('iImage',ctypes.c_int),('iOrder',ctypes.c_int)]
labels={i:ctypes.create_unicode_buffer('Virtual row '+str(i+1)) for i in range(128)}
def proc(hwnd,msg,wp,lp):
    if msg==win32con.WM_NOTIFY:
        display=ctypes.cast(lp,ctypes.POINTER(DISPLAY)).contents
        if ctypes.c_int(display.hdr.code).value==-177 and display.item.iItem in labels:
            display.item.pszText=ctypes.cast(labels[display.item.iItem],w.LPWSTR);return 0
    if msg==win32con.WM_CLOSE:win32gui.DestroyWindow(hwnd);return 0
    if msg==win32con.WM_DESTROY:win32gui.PostQuitMessage(0);return 0
    return win32gui.DefWindowProc(hwnd,msg,wp,lp)
wc=win32gui.WNDCLASS();wc.lpszClassName='ActuatorVirtualWorkspace';wc.lpfnWndProc=proc;wc.hInstance=win32api.GetModuleHandle(None);win32gui.RegisterClass(wc)
hwnd=win32gui.CreateWindowEx(0,wc.lpszClassName,'Virtual private grid',win32con.WS_OVERLAPPEDWINDOW,0,0,500,300,0,0,wc.hInstance,None)
grid=win32gui.CreateWindowEx(0,'SysListView32','Virtual records',win32con.WS_CHILD|win32con.WS_VISIBLE|win32con.WS_BORDER|1|0x1000,10,10,450,180,hwnd,1202,wc.hInstance,None)
name=ctypes.create_unicode_buffer('Record');col=COLUMN(mask=6,cx=400,pszText=ctypes.cast(name,w.LPWSTR));win32gui.SendMessage(grid,0x1061,0,ctypes.addressof(col));win32gui.SendMessage(grid,0x102f,128,0)
win32gui.ShowWindow(hwnd,win32con.SW_SHOWNOACTIVATE);win32gui.PumpMessages()
