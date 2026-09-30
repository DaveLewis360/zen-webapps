"""Filesystem layout used by app mode.

App mode deliberately uses its own roots (``zen-apps``) so it never touches the
sentinel-marked App Shell store/profile roots of the legacy workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

ROOT_OVERRIDE_ENV = "ZEN_APP_MODE_ROOT"


def desktop_id(app_id: str) -> str:
    """Desktop-entry basename, Wayland app_id and X11 WM_CLASS of an app."""
    return f"zen-app-{app_id}"


def _xdg_dir(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


@dataclass(frozen=True)
class AppModePaths:
    library: Path
    data: Path
    applications: Path
    icons: Path
    bin: Path

    @classmethod
    def default(cls) -> "AppModePaths":
        override = os.environ.get(ROOT_OVERRIDE_ENV, "")
        if override:
            if not os.path.isabs(override):
                raise ValueError(f"{ROOT_OVERRIDE_ENV} must be an absolute path")
            return cls.under(Path(override))
        config = _xdg_dir("XDG_CONFIG_HOME", ".config")
        data = _xdg_dir("XDG_DATA_HOME", ".local/share")
        return cls(
            library=config / "zen-apps",
            data=data / "zen-apps",
            applications=data / "applications",
            icons=data / "icons",
            bin=Path.home() / ".local" / "bin",
        )

    @classmethod
    def under(cls, root: Path) -> "AppModePaths":
        """Self-contained layout below ``root`` (tests and isolated trials)."""
        return cls(
            library=root / "library",
            data=root / "data",
            applications=root / "applications",
            icons=root / "icons",
            bin=root / "bin",
        )

    def app_dir(self, app_id: str) -> Path:
        return self.data / app_id

    def profile_dir(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "profile"

    def log_path(self, app_id: str) -> Path:
        return self.app_dir(app_id) / "zen.log"

    def desktop_path(self, app_id: str) -> Path:
        return self.applications / f"{desktop_id(app_id)}.desktop"

    def generated_icon_path(self, app_id: str) -> Path:
        return self.icons / "hicolor" / "scalable" / "apps" / f"{desktop_id(app_id)}.svg"

    def shim_path(self) -> Path:
        return self.bin / "zen-app"

    def custom_css_path(self, app_id: str) -> Path:
        """The user's own CSS for an app, next to its definition."""
        return self.library / f"{app_id}.css"

    def stored_icon_path(self, app_id: str, extension: str) -> Path:
        """An icon the user picked for an app (png or svg), next to its definition."""
        return self.library / f"{app_id}.{extension}"

    # The Web Apps manager: a Zen profile of its own, outside the app ids.
    def manager_dir(self) -> Path:
        return self.data / ".manager"

    def manager_profile(self) -> Path:
        return self.manager_dir() / "profile"

    def manager_definition(self) -> Path:
        return self.manager_dir() / "manager.json"

    def manager_log(self) -> Path:
        return self.manager_dir() / "zen.log"

    def manager_desktop_path(self) -> Path:
        return self.applications / "zen-apps.desktop"

    def manager_icon_path(self) -> Path:
        return self.icons / "hicolor" / "scalable" / "apps" / "zen-apps.svg"
