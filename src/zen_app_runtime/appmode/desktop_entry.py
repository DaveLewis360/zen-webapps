"""Desktop entries for app-mode apps (installed into $XDG_DATA_HOME/applications)."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from ..desktop import render_exec
from .definition import AppDefinition
from .errors import AppModeError
from .icons import IconChoice
from .library import write_private_file
from .paths import AppModePaths, desktop_id
from .presets import get_preset

_GENERIC_NAMES = {
    "AudioVideo": {"en": "Music player", "hu": "Zenelejátszó"},
    "InstantMessaging": {"en": "Messaging", "hu": "Üzenetküldő"},
}


def desktop_value(text: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise AppModeError("desktop entry values cannot contain control characters")
    return text.replace("\\", "\\\\")


def _list(values: tuple[str, ...]) -> str:
    return "".join(desktop_value(item).replace(";", "\\;") + ";" for item in values)


def render_desktop_entry(definition: AppDefinition, icon: IconChoice, shim: Path) -> str:
    identity = desktop_id(definition.id)
    preset = get_preset(definition.preset) if definition.preset else None
    comment_en = (preset.tagline.get("en") if preset else "") or f"{definition.name} as a native app"
    comment_hu = (preset.tagline.get("hu") if preset else "") or f"{definition.name} natív appként"
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.5",
        f"Name={desktop_value(definition.name)}",
        f"Comment={desktop_value(comment_en)}",
        f"Comment[hu]={desktop_value(comment_hu)}",
    ]
    for category, names in _GENERIC_NAMES.items():
        if category in definition.categories:
            lines.append(f"GenericName={desktop_value(names['en'])}")
            lines.append(f"GenericName[hu]={desktop_value(names['hu'])}")
            break
    lines += [
        f"Exec={render_exec([str(shim), 'run', definition.id])} %U",
        f"Icon={desktop_value(icon.name)}",
        "Terminal=false",
        "StartupNotify=true",
        f"StartupWMClass={identity}",
        f"Categories={_list(definition.categories or ('Network',))}",
    ]
    if definition.keywords:
        lines.append(f"Keywords={_list(definition.keywords)}")
    lines += [
        f"X-Zen-App-Id={definition.id}",
        "Actions=settings;",
        "",
        "[Desktop Action settings]",
        "Name=Settings",
        "Name[hu]=Beállítások",
        f"Exec={render_exec([str(shim), 'settings', definition.id])}",
        "",
    ]
    return "\n".join(lines)


def install_desktop_entry(paths: AppModePaths, definition: AppDefinition, icon: IconChoice) -> Path:
    target = paths.desktop_path(definition.id)
    content = render_desktop_entry(definition, icon, paths.shim_path())
    if "zen-browser" in content.split("[Desktop Action", 1)[0].split("Exec=", 1)[1].split("\n", 1)[0]:
        raise AppModeError("desktop entry Exec must not reference zen-browser (xdg-settings would treat it as a browser)")
    changed = write_private_file(target, content.encode("utf-8"), mode=0o644)
    if changed:
        refresh_desktop_caches(paths)
    return target


def remove_desktop_entry(paths: AppModePaths, app_id: str) -> None:
    for path in (paths.desktop_path(app_id), paths.generated_icon_path(app_id)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for extension in (".png",):
        try:
            paths.generated_icon_path(app_id).with_suffix(extension).unlink()
        except FileNotFoundError:
            pass
    refresh_desktop_caches(paths)


def refresh_desktop_caches(paths: AppModePaths) -> None:
    tool = shutil.which("update-desktop-database")
    if tool and paths.applications.is_dir():
        subprocess.run([tool, "-q", str(paths.applications)], check=False, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
