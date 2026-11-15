"""Read-only capability evidence; registration never implies end-to-end readiness."""
from datetime import datetime,timezone
import importlib.util
from pathlib import Path
import shutil
from .tool_policy import BACKGROUND_BLOCKED_TOOLS

def readiness(runtime,server,connector_names=None,connector_tools=None):
    config=runtime.config
    browser=getattr(config,'browser_dom',None)
    executable=Path(__import__('os').path.expandvars(getattr(browser,'edge_executable','')))
    browser_ready=bool(browser and browser.enabled and importlib.util.find_spec('playwright') and executable.is_file())
    from .evidence import Evidence
    from . import __version__
    from .connection_observation import observation,FRESH_SECONDS
    current=observation(runtime)
    evidence=Evidence(runtime.config_path.parent/'runtime-evidence.sqlite3').snapshot(__version__,max_age=FRESH_SECONDS) if hasattr(runtime,'config_path') else {}
    from .tunnel_diagnostics import metadata_report
    metadata=metadata_report(runtime.config_path.parent.parent/'deployment/tunnel-diagnostics.json',settings_path=runtime.config_path.parent.parent/'deployment/tunnel-organizations.json') if hasattr(runtime,'config_path') else {'state':'unverified'}
    from .tool_catalog import catalog
    from .tool_policy import current_principal
    tools=catalog(runtime,server,current_principal(runtime),browser_ready=browser_ready,connector_names=connector_names,connector_tools=connector_tools)
    from .provider_readiness import groups
    configured={'headless_dom':browser_ready,'filesystem':config.filesystem.enabled,
        'uia_read':config.ui_automation.enabled,'eda_kicad':config.eda.enabled,
        'windows_background':config.powershell.enabled,'git_worktree':bool(shutil.which('git'))}
    providers=groups(runtime,tools,__version__,configured) if hasattr(runtime,'config_path') else []
    return {'connection_observation':current,'evidence_freshness_seconds':FRESH_SECONDS,'provider_groups':providers,'verification_note':'Versioned representative tests; registration and dependencies alone do not prove all tools.', 'tool_catalog':tools,'openai_metadata':metadata,'runtime_version':__version__,'evidence':evidence,'checked_at':datetime.now(timezone.utc).isoformat(),
        'execution_policy':'scoped_desktop_patterns' if runtime.raw_config().get('security',{}).get('desktop_uia',{}).get('enabled') else 'background_only','capabilities':[
            {'name':'physical_input','state':'unavailable','reason':'Removed by immutable policy'},
            {'name':'desktop_mutation','state':'configured_only' if runtime.raw_config().get('security',{}).get('desktop_uia',{}).get('enabled') else 'unavailable','reason':'Selected application leases and exposed UIA patterns; browser uses DOM; physical input removed'},
            {'name':'headless_dom','state':next((p['state'] for p in providers if p['name']=='headless_dom'),'configured_only' if browser_ready else 'unavailable'),
             'evidence':'See provider_groups coverage and checked_at; dependencies alone are configuration only'},
            {'name':'git_worktree','state':'supported' if shutil.which('git') else 'unavailable'},
            {'name':'command_profiles','state':'supported','reason':'Fixed owner commands; each needs background approval'}],
        'registered_tool_count':len(server._tool_manager.list_tools()),
        'blocked_tool_names':sorted(BACKGROUND_BLOCKED_TOOLS),
        'openai_e2e':'unverified','tailscale_oauth_e2e':'unverified'}
