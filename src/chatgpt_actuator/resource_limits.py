"""Validated finite budgets; settings may lower or raise bounded defaults."""
DEFAULTS={'max_sessions_per_owner':8,'max_sessions_total':32,'max_operations_total':8,
 'max_operations_per_session':4,'max_tabs_per_session':8,'max_browser_contexts':8,
 'job_memory_mb':1024,'process_memory_mb':512,'max_child_processes':8,'cpu_percent':25}
MAXIMUM={'max_sessions_per_owner':64,'max_sessions_total':256,'max_operations_total':32,
 'max_operations_per_session':16,'max_tabs_per_session':32,'max_browser_contexts':32,
 'job_memory_mb':8192,'process_memory_mb':8192,'max_child_processes':64,'cpu_percent':100}
def validated(values=None):
    values={} if values is None else values
    if not isinstance(values,dict):raise ValueError('Resource limits must be an object')
    result=dict(DEFAULTS)
    for key,value in values.items():
        if key not in DEFAULTS or type(value) is not int or not 1<=value<=MAXIMUM[key]:raise ValueError('Invalid resource limit: '+key)
        result[key]=value
    if result['process_memory_mb']>result['job_memory_mb']:raise ValueError('Process memory exceeds job budget')
    return result
