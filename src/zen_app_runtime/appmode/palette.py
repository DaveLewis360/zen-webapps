"""Minimal Material 3 palette derivation (Python mirror of addon Palette.sys.mjs).

Only used to seed first-paint colours in the generated userChrome.css; the
running app refreshes the full palette itself.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path


def scheme_paths() -> list[str]:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return [
        str(state / "caelestia" / "scheme.json"),
        str(cache / "matugen" / "scheme.json"),
    ]


def _srgb_to_linear(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    value = min(1.0, max(0.0, value))
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def _hex_to_oklch(color: str) -> tuple[float, float, float]:
    red, green, blue = (_srgb_to_linear(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))
    l = (0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue) ** (1 / 3)
    m = (0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue) ** (1 / 3)
    s = (0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue) ** (1 / 3)
    lightness = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return lightness, math.hypot(a, b), math.degrees(math.atan2(b, a))


def _oklch_to_linear(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def tone_color(hue: float, chroma: float, tone: float) -> str:
    if tone >= 100:
        return "#ffffff"
    if tone <= 0:
        return "#000000"
    y = ((tone + 16) / 116) ** 3 if tone > 8 else tone / 903.2963
    lightness = y ** (1 / 3)
    low, high = 0.0, chroma
    best = _oklch_to_linear(lightness, 0.0, hue)
    for _ in range(18):
        mid = (low + high) / 2
        rgb = _oklch_to_linear(lightness, mid, hue)
        if all(-0.0005 <= channel <= 1.0005 for channel in rgb):
            best, low = rgb, mid
        else:
            high = mid
    return "#" + "".join(f"{round(_linear_to_srgb(channel) * 255):02x}" for channel in best)


def seed_roles(accent: str, dark: bool = True) -> dict[str, str]:
    _, chroma, hue = _hex_to_oklch(accent)
    primary_chroma = min(max(chroma, 0.05), 0.17)
    neutral = min(chroma * 0.14, 0.018)
    if dark:
        return {
            "primary": tone_color(hue, primary_chroma, 80),
            "on-primary": tone_color(hue, primary_chroma, 20),
            "surface": tone_color(hue, neutral, 6),
            "surface-container": tone_color(hue, neutral, 12),
            "surface-container-high": tone_color(hue, neutral, 17),
            "on-surface": tone_color(hue, neutral, 90),
        }
    return {
        "primary": tone_color(hue, primary_chroma, 40),
        "on-primary": "#ffffff",
        "surface": tone_color(hue, neutral, 98),
        "surface-container": tone_color(hue, neutral, 94),
        "surface-container-high": tone_color(hue, neutral, 92),
        "on-surface": tone_color(hue, neutral, 10),
    }


_SCHEME_KEYS = {
    "primary": "primary",
    "onPrimary": "on-primary",
    "surface": "surface",
    "surfaceContainer": "surface-container",
    "surfaceContainerHigh": "surface-container-high",
    "onSurface": "on-surface",
}


def initial_roles(accent: str, palette: str, color_scheme: str) -> dict[str, str]:
    """Best-effort first-paint colours; the app refines them at runtime."""
    want_dark = color_scheme != "light"
    if palette == "system":
        for path in scheme_paths():
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            colours = data.get("colours") or data.get("colors") or {}
            mode = data.get("mode")
            if color_scheme == "system" and mode in ("dark", "light"):
                # The scheme's own mode, as the runtime and the Caelestia shell use it.
                want_dark = mode == "dark"
            if mode and (mode == "dark") != want_dark:
                continue
            roles = {}
            for key, name in _SCHEME_KEYS.items():
                value = colours.get(key)
                if isinstance(value, str) and len(value.lstrip("#")) == 6:
                    roles[name] = "#" + value.lstrip("#").lower()
            if len(roles) == len(_SCHEME_KEYS):
                return roles
    return seed_roles(accent, want_dark)
