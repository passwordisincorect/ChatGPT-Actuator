"""Read-only bounded previews; digest binds path, baseline and proposed bytes."""
import difflib
import hashlib
import json

def fingerprint(path, baseline, content):
    payload=[str(path).casefold(),baseline,hashlib.sha256(content.encode('utf-8')).hexdigest()]
    return hashlib.sha256(json.dumps(payload,separators=(',',':')).encode()).hexdigest()

def preview(path, snapshot, content, can_write):
    diff=[]; size=0; truncated=False
    for line in difflib.unified_diff(snapshot['content'].splitlines(True),content.splitlines(True),
                                     fromfile='current',tofile='proposed',n=3):
        if len(diff)>=120 or size+len(line)>16384:
            truncated=True;break
        diff.append(line);size+=len(line)
    return {'expected_sha256':snapshot['sha256'],'proposed_sha256':hashlib.sha256(content.encode()).hexdigest(),
            'preview_sha256':fingerprint(path,snapshot['sha256'],content),
            'diff':''.join(diff),'diff_truncated':truncated,'scope_allowed':True,
            'local_edit_allowed':can_write,'written':False,'approval_granted':False}
