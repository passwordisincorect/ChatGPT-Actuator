import hashlib
import json
from pathlib import Path
from .managed_versions import VERSION

def verify_active(home,version):
    home=Path(home).resolve()
    if not isinstance(version,str) or not VERSION.fullmatch(version):raise ValueError('Invalid version')
    if json.loads((home/'runtime-current.json').read_text()).get('version')!=version:raise ValueError('Selected version differs')
    slot=(home/'runtime-versions'/version).resolve(strict=True)
    if (home/'runtime-versions').resolve() not in slot.parents:raise ValueError('Invalid slot')
    manifest=json.loads((slot/'manifest.json').read_text())
    actual={p.relative_to(slot).as_posix() for p in (slot/'src').rglob('*') if p.is_file()}
    if actual!=set(manifest):raise ValueError('Runtime file set changed')
    for name,expected in manifest.items():
        path=(slot/name).resolve(strict=True)
        if slot not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('Runtime changed')
    return True
