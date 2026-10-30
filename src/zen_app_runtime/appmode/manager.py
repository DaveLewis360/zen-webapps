"""Web Apps: the app that lists, opens, creates and removes web apps.

It is a Zen profile of its own, rendered like an app profile (same glass,
palette and add-on) with the role "manager": the add-on then shows its app
list instead of a website, and it drives everything through this CLI
(`zen-app … --json`), so the definitions stay the single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import TextIO

from ..desktop import render_exec
from .definition import AppDefinition, AppSettings
from .desktop_entry import refresh_desktop_caches, desktop_value
from .icons import IconChoice, manager_icon_svg
from .launcher import LaunchOutcome, identity_environment, launch_profile, process_link, running_pid
from .library import render_definition, write_private_file
from .paths import AppModePaths
from .render import RenderResult, render_profile
from .zen import ZenRuntime, find_zen

MANAGER_ID = "zen-apps"
MANAGER_IDENTITY = "zen-apps"
MANAGER_NAME = "Web Apps"
# Never loaded: the manager shows its own page. Every web link leaves it.
MANAGER_START_URL = "https://zen-apps.invalid/"


@dataclass
class PreparedManager:
    definition: AppDefinition
    render: RenderResult
    desktop_entry: Path


def manager_definition() -> AppDefinition:
    return AppDefinition(
        id=MANAGER_ID,
        name=MANAGER_NAME,
        start_url=MANAGER_START_URL,
        scope_hosts=("zen-apps.invalid",),
        accent="#7b6cff",
        settings=AppSettings(
            appearance="transparent",
            notifications=False,
            media=False,
            autoplay=False,
            drm=False,
            zen_internet=False,
            custom_css=False,
            adblock=False,
            webcompat=False,
            tray=False,
            background=False,
            unread_badge=False,
        ),
        onboarding_completed=True,
    )


def render_manager_entry(shim: Path, icon_name: str = MANAGER_IDENTITY) -> str:
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Version=1.5",
            f"Name={MANAGER_NAME}",
            "GenericName=Web app manager",
            "Comment=Turn websites into apps and manage them",
            f"Exec={render_exec([str(shim), 'studio'])}",
            f"Icon={desktop_value(icon_name)}",
            "Terminal=false",
            "StartupNotify=true",
            f"StartupWMClass={MANAGER_IDENTITY}",
            "Categories=Utility;",
            "Keywords=web;app;webapp;zen;pwa;",
            "Actions=new;",
            "",
            "[Desktop Action new]",
            "Name=New app",
            f"Exec={render_exec([str(shim), 'studio', '--new'])}",
            "",
        ]
    )


def install_manager_entry(paths: AppModePaths) -> tuple[Path, IconChoice]:
    icon_path = paths.manager_icon_path()
    changed = write_private_file(icon_path, manager_icon_svg().encode("utf-8"), mode=0o644)
    entry = paths.manager_desktop_path()
    changed = write_private_file(entry, render_manager_entry(paths.shim_path()).encode("utf-8"), mode=0o644) or changed
    if changed:
        refresh_desktop_caches(paths)
    return entry, IconChoice(MANAGER_IDENTITY, icon_path, "generated")


def prepare_manager(paths: AppModePaths, zen: ZenRuntime) -> PreparedManager:
    """Render the manager's profile, launcher and icon (never while it runs)."""
    definition = manager_definition()
    entry, icon = install_manager_entry(paths)
    write_private_file(paths.manager_definition(), render_definition(definition))
    rendered = render_profile(
        definition,
        paths,
        zen,
        icon,
        paths.manager_definition(),
        profile=paths.manager_profile(),
        identity=MANAGER_IDENTITY,
        role="manager",
    )
    return PreparedManager(definition, rendered, entry)


def manager_running(paths: AppModePaths) -> int | None:
    return running_pid(paths.manager_profile())


def launch_manager(
    paths: AppModePaths,
    zen: ZenRuntime,
    *,
    new: bool = False,
    popen=None,
) -> LaunchOutcome:
    definition = manager_definition()
    options = {"popen": popen} if popen else {}
    return launch_profile(
        identity=MANAGER_IDENTITY,
        profile=paths.manager_profile(),
        executable=process_link(paths.manager_dir() / MANAGER_IDENTITY, zen),
        environment=identity_environment(
            MANAGER_IDENTITY, app_id=MANAGER_ID, name=definition.name, icon_name=MANAGER_IDENTITY
        ),
        log_path=paths.manager_log(),
        first_url="about:blank",
        action="new" if new else None,
        write_pending=new,
        **options,
    )


def cmd_studio(
    *,
    new: bool = False,
    out: TextIO | None = None,
    paths: AppModePaths | None = None,
    zen: ZenRuntime | None = None,
    popen=None,
) -> LaunchOutcome:
    """Open Web Apps (focus it when it already runs); `new` goes straight to the wizard."""
    out = out or sys.stdout
    paths = paths or AppModePaths.default()
    zen = zen or find_zen()
    if manager_running(paths) is None:
        prepare_manager(paths, zen)
    return launch_manager(paths, zen, new=new, popen=popen)
