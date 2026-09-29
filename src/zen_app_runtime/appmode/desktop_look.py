"""Read how the rest of the desktop looks so app windows can match it exactly.

* Glass backgrounds: Ghostty (``background`` + ``background-opacity``), the
  enabled Vesktop theme (``#app-mount`` layer) and the main Zen profile
  (Transparent Zen's ``mod.sameerasw.zen_transparency_color``).
* Rendering preferences of the main Zen profile (GPU, DMA-BUF, VA-API), so an
  app renders exactly like the user's browser does on this GPU.

Everything here is read-only and best effort: missing or unparsable files just
mean "no such source".
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
from typing import Any

_HEX6 = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX8 = re.compile(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})$")
_PREF_LINE = re.compile(r'^\s*user_pref\("([^"]+)",\s*(.+?)\);\s*$')

# Preferences that decide how Gecko renders on this machine's GPU.
_INHERITED_PREFIXES = (
    "gfx.webrender.",
    "gfx.x11-egl.",
    "gfx.canvas.accelerated",
    "gfx.webgpu.",
    "layers.acceleration.",
    "layers.gpu-process.",
    "widget.dmabuf",
    "widget.wayland.",
    "media.ffmpeg.",
    "media.hardware-video-decoding.",
    "media.rdd-",
    "media.av1.",
    "webgl.",
    "dom.webgpu.",
    "layout.frame_rate",
)


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _glass(color: str, alpha: float, label: str, path: Path | None, **extra: Any) -> dict[str, Any]:
    return {
        "color": color.lower(),
        "alpha": round(min(1.0, max(0.0, alpha)), 4),
        "label": label,
        "path": str(path) if path else None,
        **extra,
    }


# ------------------------------------------------------------------ Ghostty


def _ghostty_values(path: Path, depth: int = 0, seen: set[Path] | None = None) -> dict[str, str]:
    seen = seen or set()
    if depth > 6 or path in seen:
        return {}
    seen.add(path)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    includes: list[Path] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip('"')
        if key == "config-file":
            optional = value.startswith("?")
            target = Path(os.path.expanduser(value.lstrip("?")))
            if not target.is_absolute():
                target = path.parent / target
            if target.exists() or not optional:
                includes.append(target)
        else:
            values[key] = value
    # Ghostty loads config-file includes after the file itself, so they win.
    for include in includes:
        values.update(_ghostty_values(include, depth + 1, seen))
    return values


def ghostty_glass() -> dict[str, Any] | None:
    for name in ("config", "config.ghostty"):
        path = _config_home() / "ghostty" / name
        if not path.is_file():
            continue
        values = _ghostty_values(path)
        match = _HEX6.match(values.get("background", "#282c34"))
        color = "#" + match.group(1) if match else "#282c34"
        try:
            alpha = float(values.get("background-opacity", "1"))
        except ValueError:
            alpha = 1.0
        return _glass(color, alpha, "Ghostty", path)
    return None


# ------------------------------------------------------------------ Vesktop

_RGBA = re.compile(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)")
_APP_MOUNT = re.compile(r"#app-mount\s*\{[^}]*?background(?:-color)?\s*:\s*([^;}!]+)", re.S)
_APP_LAYER = re.compile(r"--app-layer\s*:\s*([^;]+);")


def _parse_css_color(value: str) -> tuple[str, float] | None:
    value = value.strip()
    match = _RGBA.search(value)
    if match:
        red, green, blue = (int(round(float(match.group(i)))) for i in (1, 2, 3))
        alpha_text = match.group(4)
        if alpha_text is None:
            alpha = 1.0
        elif alpha_text.endswith("%"):
            alpha = float(alpha_text[:-1]) / 100
        else:
            alpha = float(alpha_text)
        return f"#{red:02x}{green:02x}{blue:02x}", alpha
    match = _HEX8.match(value)
    if match:
        return "#" + match.group(1), int(match.group(2), 16) / 255
    match = _HEX6.match(value)
    if match:
        return "#" + match.group(1), 1.0
    if value == "transparent":
        return "#000000", 0.0
    return None


def vesktop_glass() -> dict[str, Any] | None:
    for base in (_config_home() / "vesktop", Path.home() / ".var/app/dev.vencord.Vesktop/config/vesktop"):
        settings_path = base / "settings" / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not settings.get("transparent", False):
            return _glass("#000000", 1.0, "Vesktop", settings_path, note="window transparency is off")
        for theme in settings.get("enabledThemes") or []:
            path = base / "themes" / str(theme)
            try:
                css = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = None
            for match in _APP_MOUNT.finditer(css):
                found = _parse_css_color(match.group(1)) or found
            if not found:
                layer = _APP_LAYER.search(css)
                found = _parse_css_color(layer.group(1)) if layer else None
            if found:
                return _glass(found[0], found[1], "Vesktop", path, theme=str(theme))
        return _glass("#000000", 0.0, "Vesktop", settings_path, note="no background layer in the enabled theme")
    return None


# ------------------------------------------------------------ main Zen profile


def main_zen_profile() -> Path | None:
    root = Path.home() / ".zen"
    for name in ("installs.ini", "profiles.ini"):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read(root / name, encoding="utf-8")
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in parser.sections():
            items = dict(parser.items(section))
            if name == "installs.ini" and items.get("default"):
                candidate = root / items["default"]
            elif name == "profiles.ini" and section.startswith("Install") and items.get("default"):
                candidate = root / items["default"]
            elif name == "profiles.ini" and items.get("default") == "1" and items.get("path"):
                candidate = root / items["path"] if items.get("isrelative", "1") == "1" else Path(items["path"])
            else:
                continue
            if (candidate / "prefs.js").is_file():
                return candidate
    return None


def _parse_pref_value(raw: str) -> Any:
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return None


def read_prefs(profile: Path) -> dict[str, Any]:
    prefs: dict[str, Any] = {}
    for filename in ("prefs.js", "user.js"):
        try:
            text = (profile / filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            match = _PREF_LINE.match(line)
            if match:
                value = _parse_pref_value(match.group(2))
                if value is not None:
                    prefs[match.group(1)] = value
    return prefs


def inherited_render_prefs(profile: Path | None = None) -> tuple[dict[str, Any], Path | None]:
    profile = profile or main_zen_profile()
    if not profile:
        return {}, None
    prefs = read_prefs(profile)
    chosen = {
        name: value
        for name, value in sorted(prefs.items())
        if name.startswith(_INHERITED_PREFIXES) and isinstance(value, (bool, int, str))
    }
    return chosen, profile


def zen_glass(profile: Path | None = None) -> dict[str, Any] | None:
    """The background the main Zen window really paints.

    Transparent Zen's colour applies only while its "custom background
    colour" switch is on. Otherwise Zen's own translucent background is used:
    ZenGradientGenerator paints rgba(0, 0, 0, 0.4) for a dark space without
    theme colours and nothing for a light one (themed spaces blend their
    colours on top).
    """
    profile = profile or main_zen_profile()
    if not profile:
        return None
    prefs = read_prefs(profile)
    if not prefs.get("zen.widget.linux.transparency", False):
        return _glass("#000000", 1.0, "Zen", profile, note="Zen transparency is off")
    if prefs.get("mod.sameerasw.zen_bg_color_enabled", False):
        value = prefs.get("mod.sameerasw.zen_transparency_color")
        parsed = _parse_css_color(value) if isinstance(value, str) else None
        if parsed:
            return _glass(parsed[0], parsed[1], "Zen", profile, mod="Transparent Zen")
    if prefs.get("zen.view.window.scheme", 2) == 1:
        return _glass("#000000", 0.0, "Zen", profile, note="Zen default, light")
    return _glass("#000000", 0.4, "Zen", profile, note="Zen default")


def glass_sources() -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for key, reader in (("ghostty", ghostty_glass), ("vesktop", vesktop_glass), ("zen", zen_glass)):
        try:
            value = reader()
        except Exception:  # best effort: a broken foreign config must never break an app
            value = None
        if value:
            sources[key] = value
    return sources


def resolve_glass(choice: str, sources: dict[str, dict[str, Any]]) -> str:
    if choice != "auto":
        return choice if choice == "custom" or choice in sources else "custom"
    for key in ("ghostty", "vesktop", "zen"):
        if key in sources:
            return key
    return "custom"
