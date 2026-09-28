"""Pure renderer for plan-only Desktop Entry previews."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Sequence

from .launch_plan import LaunchPlan

GENERIC_ICON = "application-x-executable"
PLAN_ONLY_MARKER = "X-Zen-App-Runtime-Plan-Only"


class DesktopError(ValueError):
    """Raised when a Desktop Entry cannot be rendered safely."""


@dataclass(frozen=True)
class DesktopPreview:
    name: str
    exec_argv: tuple[str, ...]
    terminal: bool = True
    icon: str = GENERIC_ICON
    plan_only: bool = True


def _reject_controls(value: str, location: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise DesktopError(f"{location} cannot contain control characters")


def _absolute_path(value: os.PathLike[str] | str, label: str) -> str:
    if isinstance(value, bytes):
        raise DesktopError(f"{label} must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise DesktopError(f"{label} must be path-like text") from error
    if not isinstance(rendered, str) or not Path(rendered).is_absolute():
        raise DesktopError(f"{label} must be absolute")
    _reject_controls(rendered, label)
    return rendered


def _quote_exec_token(token: str) -> str:
    if not isinstance(token, str):
        raise DesktopError("Exec argv tokens must be strings")
    _reject_controls(token, "Exec argv token")
    # Exec has two parsing layers: first Desktop Entry string-value
    # unescaping, then Exec argument parsing. Escape token characters for the
    # inner quoted argument, double percent field-code introducers, and finally
    # escape every resulting backslash through the outer value layer.
    exec_escaped = (
        token.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("%", "%%")
    )
    desktop_escaped = exec_escaped.replace("\\", "\\\\")
    return f'"{desktop_escaped}"'


def render_exec(argv: Sequence[str]) -> str:
    """Render argv as Desktop Entry Exec tokens, never as a shell command."""
    if isinstance(argv, (str, bytes)) or not argv:
        raise DesktopError("Exec argv must contain at least one token")
    return " ".join(_quote_exec_token(token) for token in argv)


def _desktop_string(value: str, location: str) -> str:
    if not isinstance(value, str):
        raise DesktopError(f"{location} must be text")
    _reject_controls(value, location)
    return value.replace("\\", "\\\\")


def build_desktop_preview(
    plan: LaunchPlan,
    store_path: os.PathLike[str] | str,
) -> DesktopPreview:
    """Describe a .desktop preview that invokes only the launcher's plan command."""
    store = _absolute_path(store_path, "store path")
    launcher = _absolute_path(plan.launcher_path, "launcher path")
    return DesktopPreview(
        name=plan.name,
        exec_argv=(
            launcher,
            "--store",
            store,
            "plan",
            plan.app_id,
            "--runtime",
            plan.runtime_path,
            "--launcher",
            launcher,
            "--profile-root",
            plan.profile_root,
        ),
    )


def render_desktop_preview(preview: DesktopPreview) -> str:
    """Render a deterministic, non-installed Desktop Entry with a trailing LF."""
    if preview.terminal is not True or preview.plan_only is not True:
        raise DesktopError("desktop preview must remain terminal-based and plan-only")
    if preview.icon != GENERIC_ICON:
        raise DesktopError("desktop preview must use the generic icon")
    lines = (
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={_desktop_string(preview.name, 'Name')}",
        f"Exec={render_exec(preview.exec_argv)}",
        "Terminal=true",
        f"Icon={GENERIC_ICON}",
        f"{PLAN_ONLY_MARKER}=true",
    )
    return "\n".join(lines) + "\n"
