"""Read-only import source discovery. A telescope is a mounted filesystem."""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

from m110 import config, ingest


_LINUX_USER_MOUNTS = (Path("/media"), Path("/run/media"))
_LINUX_MOUNTS = Path("/mnt")


def _directories(root: Path) -> list[Path]:
    """One level only; disconnected/inaccessible mounts are not sources."""
    directories = []
    try:
        for path in root.iterdir():
            if config.is_hidden_name(path.name):
                continue
            try:
                if path.is_dir():
                    directories.append(path)
            except OSError:
                continue             # one broken child must not hide accessible siblings
    except OSError:
        pass
    return sorted(directories, key=str)


def mounted_roots() -> list[Path]:
    """Candidate drive roots, without walking their contents or reading images.

    Windows logical drives include mapped shares. Unix probes standard mount
    directories; unconventional mount points remain available through Browse.
    """
    if sys.platform == "win32":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [Path(f"{chr(65 + n)}:/") for n in range(26) if mask & (1 << n)]
    roots = _directories(config.VOLUMES_DIR)
    if sys.platform.startswith("linux"):
        for base in _LINUX_USER_MOUNTS:
            for user in _directories(base):
                roots.extend(_directories(user))
        roots.extend(_directories(_LINUX_MOUNTS))
    return roots


def find_origin_roots(roots: Iterable[Path | str] | None = None,
                      should_cancel=None) -> list[Path]:
    """Offer roots with a direct Origin session child, independent of drive name.

    Reuse ingest's structured ``StackedInfo`` recognizer. This shallow probe
    reads only bounded JSON, never FITS/pixels or a recursive import plan.
    ``roots`` also accepts copied drive roots (e.g. remembered Browse locations).
    """
    found, seen = [], set()
    for candidate in mounted_roots() if roots is None else roots:
        if should_cancel and should_cancel():
            raise ingest.IngestCancelled()
        root = Path(candidate)
        if root in seen:
            continue
        seen.add(root)
        for session in _directories(root):
            if should_cancel and should_cancel():
                raise ingest.IngestCancelled()
            if ingest._origin_info(session) is not None:
                found.append(root)
                break
    return found
