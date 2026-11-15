"""Fixed profile CLI: no executable, PID, path or environment input accepted."""
import json
from pathlib import Path
import sys
from .service_control import request

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in {'start','status','stop'}:raise ValueError('Fixed service action required')
    from .config import project_root
    result=request(project_root()/'deployment/services/mailbox.sqlite3',sys.argv[1])
    print(json.dumps(result))
    return 1 if result.get('state') in {'failed','denied'} else 0

if __name__=='__main__':sys.exit(main())
