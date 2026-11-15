"""Load adapters on demand; inspecting a core capability need not import Windows UI."""
from importlib import import_module
_EXPORTS={
    'BrowserDOMService':'browser_dom','FileSystemService':'filesystem','EDAService':'eda',
    'ClipboardService':'input_control','KeyboardService':'input_control','MouseService':'input_control',
    'PowerShellService':'powershell','ProcessSystemService':'process_system',
    'ScreenService':'screen_window','WindowService':'screen_window',
    'UIAutomationService':'ui_automation','VerifiedInputService':'ui_text'}
__all__=list(_EXPORTS)
def __getattr__(name):
    if name not in _EXPORTS:raise AttributeError(name)
    value=getattr(import_module('.'+_EXPORTS[name],__name__),name)
    globals()[name]=value
    return value
