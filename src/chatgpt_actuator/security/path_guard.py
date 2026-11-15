from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..errors import AccessDeniedError, ConfigError
from .path_canonical import canonical_resolved_path, resolve_canonical


class PathGuard:
    """Resolve paths and allow access only below configured roots."""

    def __init__(self, allowed_roots: Iterable[str | Path]) -> None:
        roots: list[Path] = []

        for raw_root in allowed_roots:
            root = Path(raw_root).expanduser()
            try:
                resolved = resolve_canonical(root, strict=True)
            except FileNotFoundError as exc:
                raise ConfigError(f"Allowed root does not exist: {root}") from exc

            if not resolved.is_dir():
                raise ConfigError(f"Allowed root is not a directory: {resolved}")

            roots.append(resolved)

        if not roots:
            raise ConfigError("At least one allowed root is required.")

        self._roots = tuple(roots)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def _ensure_allowed(self, resolved: Path) -> Path:
        from ..tool_policy import principal_context
        workspace = getattr(principal_context.get(), 'autonomous_workspace', None)
        if workspace is not None:
            delegated = resolve_canonical(workspace, strict=True)
            resolved = canonical_resolved_path(resolved)
            if resolved != delegated and delegated not in resolved.parents:
                raise AccessDeniedError('Path is outside the delegated agent workspace')
        for root in self._roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue

        raise AccessDeniedError(
            f"Access denied: '{resolved}' is outside the configured allowed roots."
        )

    def resolve_existing(self, raw_path: str | Path) -> Path:
        path = Path(raw_path).expanduser()
        resolved = resolve_canonical(path, strict=True)
        return self._ensure_allowed(resolved)

    def resolve_target(self, raw_path: str | Path) -> Path:
        """Resolve a path that may not exist yet, while honoring existing symlinks/junctions."""
        path = Path(raw_path).expanduser()
        resolved = resolve_canonical(path, strict=False)
        return self._ensure_allowed(resolved)

    def is_allowed_root(self, raw_path: str | Path) -> bool:
        try:
            resolved = resolve_canonical(raw_path, strict=True)
        except FileNotFoundError:
            return False
        return any(resolved == root for root in self._roots)
