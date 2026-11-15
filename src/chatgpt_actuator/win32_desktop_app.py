"""A small native Win32 reference application for private UIA workspaces."""
import win32gui,win32con,win32api,ctypes
win32gui.InitCommonControls()
controls={}
def proc(hwnd,msg,wparam,lparam):
    if msg==win32con.WM_COMMAND and (wparam&65535)==1002 and (wparam>>16)==win32con.EN_CHANGE and win32gui.GetWindowText(controls.get('edit',0))=='revert-later':ctypes.windll.user32.SetTimer(hwnd,1,900,None)
    if msg==win32con.WM_TIMER:
        ctypes.windll.user32.KillTimer(hwnd,1);win32gui.SetWindowText(controls['edit'],'initial');return 0
    if msg==win32con.WM_COMMAND and (wparam&65535)==1001:
        text=win32gui.GetWindowText(controls['edit'])
        win32gui.SetWindowText(controls['status'],'Submitted: '+text)
    if msg==win32con.WM_COMMAND and (wparam&65535)==1101:win32gui.SetWindowText(controls['status'],'Menu invoked')
    if msg==win32con.WM_CLOSE:win32gui.DestroyWindow(hwnd);return 0
    if msg==win32con.WM_DESTROY:win32gui.PostQuitMessage(0);return 0
    return win32gui.DefWindowProc(hwnd,msg,wparam,lparam)
wc=win32gui.WNDCLASS();wc.lpszClassName='ActuatorWin32Workspace';wc.lpfnWndProc=proc;wc.hInstance=win32api.GetModuleHandle(None);win32gui.RegisterClass(wc)
hwnd=win32gui.CreateWindowEx(0,wc.lpszClassName,'Win32 control workspace',win32con.WS_OVERLAPPEDWINDOW,0,0,650,650,0,0,wc.hInstance,None)
common=win32con.WS_CHILD|win32con.WS_VISIBLE
controls['button']=win32gui.CreateWindowEx(0,'BUTTON','Submit',common,10,10,180,30,hwnd,1001,wc.hInstance,None)
controls['edit']=win32gui.CreateWindowEx(0,'EDIT','initial',common|win32con.WS_BORDER,10,50,180,30,hwnd,1002,wc.hInstance,None)
controls['check']=win32gui.CreateWindowEx(0,'BUTTON','Enabled',common|win32con.BS_AUTOCHECKBOX,10,90,180,30,hwnd,1003,wc.hInstance,None)
controls['status']=win32gui.CreateWindowEx(0,'STATIC','Ready',common,10,130,360,30,hwnd,1004,wc.hInstance,None)
controls['list']=win32gui.CreateWindowEx(0,'LISTBOX','Choices',common|win32con.WS_BORDER|win32con.WS_VSCROLL|win32con.LBS_NOTIFY,10,175,200,80,hwnd,1005,wc.hInstance,None)
for name in ['Alpha','Beta',*(f'Row {i}' for i in range(60))]:win32gui.SendMessage(controls['list'],win32con.LB_ADDSTRING,0,name)
controls['combo']=win32gui.CreateWindowEx(0,'COMBOBOX','Options',common|win32con.CBS_DROPDOWNLIST,230,175,200,160,hwnd,1006,wc.hInstance,None)
for name in ['First option','Second option']:win32gui.SendMessage(controls['combo'],win32con.CB_ADDSTRING,0,name)
win32gui.SendMessage(controls['combo'],win32con.CB_SETCURSEL,0,0)
controls['tree']=win32gui.CreateWindowEx(0,'SysTreeView32','Hierarchy',common|win32con.WS_BORDER|1|2|4,10,270,200,120,hwnd,1007,wc.hInstance,None)
class TVITEM(ctypes.Structure):
    _fields_=[('mask',ctypes.c_uint),('hItem',ctypes.c_void_p),('state',ctypes.c_uint),('stateMask',ctypes.c_uint),('pszText',ctypes.c_wchar_p),('cchTextMax',ctypes.c_int),('iImage',ctypes.c_int),('iSelectedImage',ctypes.c_int),('cChildren',ctypes.c_int),('lParam',ctypes.c_ssize_t)]
class TVINSERT(ctypes.Structure):
    _fields_=[('parent',ctypes.c_void_p),('after',ctypes.c_void_p),('item',TVITEM)]
def tree_item(parent,text):
    item=TVINSERT(parent,ctypes.c_void_p(-0x10002).value,TVITEM(mask=1,pszText=text))
    return win32gui.SendMessage(controls['tree'],0x1132,0,ctypes.addressof(item))
root_item=tree_item(ctypes.c_void_p(-0x10000).value,'Root node');tree_item(root_item,'Child node')
controls['tabs']=win32gui.CreateWindowEx(0,'SysTabControl32','Pages',common|win32con.WS_BORDER,230,270,300,100,hwnd,1008,wc.hInstance,None)
class TCITEM(ctypes.Structure):
    _fields_=[('mask',ctypes.c_uint),('state',ctypes.c_uint),('stateMask',ctypes.c_uint),('pszText',ctypes.c_wchar_p),('cchTextMax',ctypes.c_int),('iImage',ctypes.c_int),('lParam',ctypes.c_ssize_t)]
for index,name in enumerate(['Page one','Page two']):
    item=TCITEM(mask=1,pszText=name);win32gui.SendMessage(controls['tabs'],0x133e,index,ctypes.addressof(item))
controls['slider']=win32gui.CreateWindowEx(0,'msctls_trackbar32','Level',common,10,420,300,50,hwnd,1009,wc.hInstance,None)
win32gui.SendMessage(controls['slider'],0x406,1,100<<16)
win32gui.SendMessage(controls['slider'],0x405,1,20)
menu=win32gui.CreateMenu();popup=win32gui.CreatePopupMenu();win32gui.AppendMenu(popup,win32con.MF_STRING,1101,'Mark');win32gui.AppendMenu(menu,win32con.MF_POPUP,popup,'Actions');win32gui.SetMenu(hwnd,menu)
win32gui.ShowWindow(hwnd,win32con.SW_SHOWNOACTIVATE);win32gui.PumpMessages()
