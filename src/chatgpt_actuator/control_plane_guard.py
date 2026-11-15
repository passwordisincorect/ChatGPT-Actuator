"""Project clients cannot rewrite the agent or its own security configuration."""
from pathlib import Path
from .tool_policy import scope_for
from .security.path_canonical import resolve_canonical

PATH_KEYS={"path","source","destination","root","ir_path","schematic_path","project_dir",
           "repository","project_path","output_dir","report_path","netlist_path"}

def protect_control_plane(runtime,name,principal,arguments):
    if not hasattr(runtime,"config_path"): return
    from .config import project_root
    installed=resolve_canonical(project_root(), strict=False)
    config_parent=resolve_canonical(runtime.config_path, strict=False).parent
    gateway=installed.parent/"MCP-Funnel-Gateway"
    protected=[installed,config_parent,gateway]
    destructive=scope_for(name)!="mcp:read"
    for key,value in arguments.items():
        if key not in PATH_KEYS or not isinstance(value,str): continue
        path=resolve_canonical(Path(value).expanduser(), strict=False)
        filename=path.name.casefold()
        if (filename.endswith("signing-private.pem") or filename.endswith('.dpapi')
                or filename=='.chatgpt-dom-broker-token'):
            raise PermissionError("Local service credentials are not accessible through MCP")
        if destructive and not principal.full_access:
            for root in protected:
                if path==root or root in path.parents or path in root.parents:
                    raise PermissionError("Project mode cannot modify the agent control plane")
