"""Discovery of the package-managed Zen runtime used by app mode.

App mode intentionally follows the distribution package (security updates land
without re-pinning).  The privileged App Shell path keeps its own pinned-lock
model and is unaffected.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import os
from pathlib import Path
import shutil

from .errors import AppModeError

BINARY_OVERRIDE_ENV = "ZEN_APP_ZEN_BINARY"
_CANDIDATES = (
    "/opt/zen-browser-bin/zen-bin",
    "/opt/zen-browser/zen-bin",
    "/usr/lib/zen-browser/zen-bin",
    "/usr/lib/zen/zen-bin",
    "/opt/zen/zen-bin",
    "/opt/zen/zen",
)


@dataclass(frozen=True)
class ZenRuntime:
    binary: Path
    version: str
    build_id: str
    gecko: str

    @property
    def label(self) -> str:
        return f"Zen {self.version} (Gecko {self.gecko})" if self.gecko else f"Zen {self.version}"


def _read_ini(path: Path, section: str) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return {}
    if not parser.has_section(section):
        return {}
    return {key.lower(): value for key, value in parser.items(section)}


def _describe(binary: Path) -> ZenRuntime:
    app = _read_ini(binary.parent / "application.ini", "App")
    platform = _read_ini(binary.parent / "platform.ini", "Build")
    return ZenRuntime(
        binary=binary,
        version=app.get("version", "unknown"),
        build_id=app.get("buildid", platform.get("buildid", "")),
        gecko=platform.get("milestone", ""),
    )


def _usable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_zen() -> ZenRuntime:
    override = os.environ.get(BINARY_OVERRIDE_ENV, "")
    if override:
        path = Path(override)
        if not path.is_absolute() or not _usable(path):
            raise AppModeError(f"{BINARY_OVERRIDE_ENV} must name an absolute executable Zen binary")
        return _describe(path)
    for candidate in _CANDIDATES:
        path = Path(candidate)
        if _usable(path):
            return _describe(path.resolve())
    for name in ("zen-browser", "zen"):
        found = shutil.which(name)
        if not found:
            continue
        resolved = Path(found).resolve()
        # Distribution wrappers are shell scripts; prefer the real binary next to application.ini.
        sibling = resolved.parent / "zen-bin"
        if _usable(sibling):
            return _describe(sibling)
        if (resolved.parent / "application.ini").is_file():
            return _describe(resolved)
    raise AppModeError(
        "Zen Browser was not found; install it (e.g. zen-browser-bin) "
        f"or point {BINARY_OVERRIDE_ENV} at the zen-bin executable"
    )
