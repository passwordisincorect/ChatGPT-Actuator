"""Fixed diagnostic, owner-installed; no project scripts or desktop operations."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(sys.argv[1])/'src'))
from chatgpt_actuator.tool_policy import BACKGROUND_BLOCKED_TOOLS,Principal,authorize
for name in BACKGROUND_BLOCKED_TOOLS:
    try:authorize(name,Principal(full_access=True))
    except PermissionError:continue
    raise RuntimeError('Background policy failed')
print('BACKGROUND_POLICY_SELFTEST_OK')
