"""Fixed entry points kept in the immutable runtime slot."""
import json
from pathlib import Path
import sys
import unittest
import urllib.request

def main():
    mode=sys.argv[1]
    if mode=='study-health':
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8780/',timeout=5) as response:
            if response.status!=200:raise RuntimeError('Study Coach unavailable')
        print(json.dumps({'study_coach':'ready','http_status':200}))
        return 0
    if mode=='study-tests':
        project=Path(sys.argv[2]).resolve(strict=True)
        sys.path.insert(0,str(project))
        suite=unittest.defaultTestLoader.discover(str(project),pattern='test_*.py')
        if suite.countTestCases()==0:raise RuntimeError('No Study Coach tests found')
        return 0 if unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful() else 1
    raise ValueError('Unknown fixed workload')

if __name__=='__main__':sys.exit(main())
