from .automation_schema import digest
def describe(version,tools):
    schemas=sorted([{'name':t['name'],'inputSchema':t.get('inputSchema',{})} for t in tools if t['name'].startswith(('uia_','dom_'))],key=lambda t:t['name'])
    return {'runtime_version':version,'automation_schema_fingerprint':digest(schemas),'automation_tools':len(schemas),
            'backend_catalog_state':'observed','application_catalog_state':'unverified',
            'note':'Backend catalog is current; no evidence of the app cached tools/list has been supplied'}
