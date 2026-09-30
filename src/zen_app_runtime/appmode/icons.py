"""Icon resolution for app-mode desktop entries.

Preference order:
1. an icon name available in the user's current icon theme chain (keeps the
   look consistent and follows theme changes),
2. the same icon copied from another installed theme into the user's hicolor
   directory,
3. a generated Material-3-style "cookie" shape with the app's initial.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess
from xml.sax.saxutils import escape

from .paths import AppModePaths, desktop_id

_EXTENSIONS = (".svg", ".png")


@dataclass(frozen=True)
class IconChoice:
    name: str
    file: Path | None
    source: str  # "theme" | "copied" | "custom" | "generated"


def _icon_base_dirs() -> list[Path]:
    home = Path.home()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    data_dirs = [Path(p) for p in (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p]
    bases = [home / ".icons", data_home / "icons", *[d / "icons" for d in data_dirs]]
    unique: list[Path] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def _current_theme_names() -> list[str]:
    names: list[str] = []
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        value = result.stdout.strip().strip("'\"")
        if result.returncode == 0 and value:
            names.append(value)
    except (OSError, subprocess.SubprocessError):
        pass
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for relative, section, key in (
        ("gtk-3.0/settings.ini", "Settings", "gtk-icon-theme-name"),
        ("gtk-4.0/settings.ini", "Settings", "gtk-icon-theme-name"),
        ("kdeglobals", "Icons", "Theme"),
    ):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(config_home / relative, encoding="utf-8")
            value = parser.get(section, key, fallback="").strip()
        except (configparser.Error, OSError, UnicodeDecodeError):
            value = ""
        if value and value not in names:
            names.append(value)
    return names


def _theme_dirs(theme: str) -> list[Path]:
    return [base / theme for base in _icon_base_dirs() if (base / theme).is_dir()]


def _inherits(theme: str) -> list[str]:
    for directory in _theme_dirs(theme):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(directory / "index.theme", encoding="utf-8")
            value = parser.get("Icon Theme", "Inherits", fallback="")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        return [name.strip() for name in value.split(",") if name.strip()]
    return []


def _theme_chain() -> list[str]:
    chain: list[str] = []
    pending = _current_theme_names()
    while pending and len(chain) < 12:
        theme = pending.pop(0)
        if theme in chain:
            continue
        chain.append(theme)
        pending.extend(_inherits(theme))
    if "hicolor" not in chain:
        chain.append("hicolor")
    return chain


def _find_in_theme(theme: str, name: str) -> Path | None:
    best: tuple[int, Path] | None = None
    for directory in _theme_dirs(theme):
        for pattern in (f"*/apps/{name}", f"apps/*/{name}", f"*/*/apps/{name}"):
            for extension in _EXTENSIONS:
                for candidate in directory.glob(pattern + extension):
                    if not candidate.is_file():
                        continue
                    text = str(candidate)
                    score = 10_000 if extension == ".svg" else 0
                    for token in candidate.parts:
                        size = token.split("x")[0].split("@")[0]
                        if size.isdigit():
                            score += int(size)
                        elif token == "scalable":
                            score += 5_000
                    if best is None or score > best[0]:
                        best = (score, Path(text))
    return best[1] if best else None


def _all_installed_themes() -> list[str]:
    themes: list[str] = []
    for base in _icon_base_dirs():
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and entry.name not in themes and (entry / "index.theme").is_file():
                themes.append(entry.name)
    return themes


def _contrast_text(accent: str) -> str:
    red, green, blue = (int(accent[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)
    return "#1b1b1f" if luminance > 0.42 else "#ffffff"


def cookie_path(lobes: int = 9, amplitude: float = 0.075, radius: float = 108.0, center: float = 128.0) -> str:
    """SVG path of an M3 Expressive "cookie" shape in a 256×256 box."""
    points = []
    for step in range(216):
        angle = 2 * math.pi * step / 216 - math.pi / 2
        r = radius * (1 + amplitude * math.cos(lobes * (angle + math.pi / 2)))
        points.append(f"{center + r * math.cos(angle):.2f},{center + r * math.sin(angle):.2f}")
    return "M" + " L".join(points) + " Z"


def generated_icon_svg(name: str, accent: str) -> str:
    """A 9-sided M3 Expressive cookie shape with the app's initial."""
    path = cookie_path()
    initial = escape((name.strip()[:1] or "?").upper())
    text = _contrast_text(accent)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">\n'
        f'  <path d="{path}" fill="{accent}"/>\n'
        f'  <text x="128" y="128" dy="0.35em" text-anchor="middle" fill="{text}" '
        'font-family="Google Sans Flex, Google Sans, Roboto Flex, Roboto, sans-serif" '
        f'font-size="118" font-weight="700">{initial}</text>\n'
        "</svg>\n"
    )


def manager_icon_svg() -> str:
    """The Web Apps icon: a 12-lobed cookie holding a grid of three apps and a plus."""
    path = cookie_path(lobes=12, amplitude=0.055)
    tiles = "\n".join(
        f'  <rect x="{x}" y="{y}" width="50" height="50" rx="16" fill="{fill}"/>'
        for x, y, fill in ((71, 71, "#ffffff"), (135, 71, "#e8def8"), (71, 135, "#e8def8"))
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">\n'
        "  <defs>\n"
        '    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">\n'
        '      <stop offset="0" stop-color="#8e6fe8"/>\n'
        '      <stop offset="1" stop-color="#5b3fbf"/>\n'
        "    </linearGradient>\n"
        "  </defs>\n"
        f'  <path d="{path}" fill="url(#g)"/>\n'
        f"{tiles}\n"
        '  <circle cx="160" cy="160" r="25" fill="#ffd8e4"/>\n'
        '  <path d="M160 147v26M147 160h26" stroke="#5b3fbf" stroke-width="7" stroke-linecap="round"/>\n'
        "</svg>\n"
    )


def find_icon_file(icon_names: tuple[str, ...]) -> Path | None:
    """The file an icon name resolves to (current theme first), without copying anything."""
    chain = _theme_chain()
    for icon in icon_names:
        for theme in chain:
            found = _find_in_theme(theme, icon)
            if found:
                return found
    for icon in icon_names:
        for theme in _all_installed_themes():
            if theme in chain:
                continue
            found = _find_in_theme(theme, icon)
            if found:
                return found
    return None


def resolve_icon(
    paths: AppModePaths,
    app_id: str,
    name: str,
    accent: str,
    icon_names: tuple[str, ...] = (),
    custom: str | None = None,
) -> IconChoice:
    target_name = desktop_id(app_id)
    target_dir = paths.icons / "hicolor" / "scalable" / "apps"
    if custom and custom.startswith("/"):
        source = Path(custom)
        if source.is_file() and source.suffix.lower() in _EXTENSIONS:
            target = target_dir / f"{target_name}{source.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return IconChoice(target_name, target, "custom")
    elif custom:
        icon_names = (custom, *icon_names)

    chain = _theme_chain()
    for icon in icon_names:
        for theme in chain:
            found = _find_in_theme(theme, icon)
            if found:
                return IconChoice(icon, found, "theme")
    for icon in icon_names:
        for theme in _all_installed_themes():
            if theme in chain:
                continue
            found = _find_in_theme(theme, icon)
            if found:
                target = target_dir / f"{target_name}{found.suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(found, target)
                return IconChoice(target_name, target, "copied")
    target = target_dir / f"{target_name}.svg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generated_icon_svg(name, accent), encoding="utf-8")
    return IconChoice(target_name, target, "generated")
