"""Canonicalize resolved Windows paths without accepting device namespaces."""
from pathlib import Path
import re


_EXTENDED_DOS = re.compile(r"^\\\\\?\\([A-Za-z]:\\.*)$", re.DOTALL)
_EXTENDED_UNC = re.compile(r"^\\\\\?\\UNC\\(.*)$", re.IGNORECASE | re.DOTALL)


def canonical_resolved_path(path: str | Path) -> Path:
    """Remove only Win32's standard extended DOS/UNC prefix after resolution.

    Volume GUID paths, GLOBALROOT, and all other device namespaces remain
    untouched so canonicalization cannot turn them into ordinary user paths.
    """
    value = str(path)
    unc = _EXTENDED_UNC.match(value)
    if unc:
        value = "\\\\" + unc.group(1)
    else:
        dos = _EXTENDED_DOS.match(value)
        if dos:
            value = dos.group(1)
    return Path(value)


def resolve_canonical(path: str | Path, *, strict: bool) -> Path:
    """Resolve filesystem links first, then normalize equivalent Win32 forms."""
    return canonical_resolved_path(Path(path).expanduser().resolve(strict=strict))
