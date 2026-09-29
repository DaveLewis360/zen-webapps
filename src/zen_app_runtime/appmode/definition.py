"""App-mode definition schema (``zen-app/1``).

The library file ``<library>/<id>.json`` is the single source of truth for an
app.  The Python side creates and validates it; the privileged add-on inside
the running app may rewrite only the ``settings`` and ``onboarding`` members
(through the same schema, mirrored in ``addon/modules/Schema.sys.mjs``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from ..manifest import APP_ID_PATTERN
from ..onboarding import OnboardingError, canonicalize_web_app_url
from .errors import AppModeError
from .presets import Preset

SCHEMA = "zen-app/1"
ONBOARDING_VERSION = 1

APPEARANCES = ("transparent", "opaque")
GLASS_SOURCES = ("auto", "ghostty", "vesktop", "zen", "custom")
PALETTES = ("system", "app")
COLOR_SCHEMES = ("system", "dark", "light")
WINDOW_CHROMES = ("auto", "none", "titlebar")
TINT_RANGE = (0, 90)

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_HEX_COLOR = re.compile(r"^#[0-9a-f]{6}$")
_THEME_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")
_FEATURE_KEY = re.compile(r"^[^|\x00-\x1f\x7f]{1,120}\|[^\x00-\x1f\x7f]{1,120}$")
_ICON_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")
_PATH_PREFIX = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,200}$")

_TOP_KEYS = {
    "schema", "id", "name", "preset", "startUrl", "scope", "authHosts",
    "extras", "icon", "categories", "keywords", "accent", "settings", "onboarding",
}
_SCOPE_KEYS = {"hosts", "paths"}
_ONBOARDING_KEYS = {"completed", "version"}


@dataclass(frozen=True)
class AppSettings:
    appearance: str = "transparent"
    glass: str = "auto"
    tint: int = 40
    palette: str = "system"
    color_scheme: str = "system"
    window_chrome: str = "auto"
    notifications: bool = False
    media: bool = False
    autoplay: bool = True
    drm: bool = False
    find_in_page: bool = False
    developer: bool = False
    zen_internet: bool = True
    disabled_features: tuple[str, ...] = ()


_SETTINGS_FIELDS = {
    "appearance": "appearance",
    "glass": "glass",
    "tint": "tint",
    "palette": "palette",
    "colorScheme": "color_scheme",
    "windowChrome": "window_chrome",
    "notifications": "notifications",
    "media": "media",
    "autoplay": "autoplay",
    "drm": "drm",
    "findInPage": "find_in_page",
    "developer": "developer",
    "zenInternet": "zen_internet",
    "disabledFeatures": "disabled_features",
}


@dataclass(frozen=True)
class AppDefinition:
    id: str
    name: str
    start_url: str
    scope_hosts: tuple[str, ...]
    scope_paths: tuple[str, ...] = ()
    auth_hosts: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    icon: str | None = None
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    accent: str = "#7b6cff"
    preset: str | None = None
    settings: AppSettings = field(default_factory=AppSettings)
    onboarding_completed: bool = False
    onboarding_version: int = ONBOARDING_VERSION

    @property
    def start_host(self) -> str:
        return urlsplit(self.start_url).hostname or ""

    @property
    def needs_onboarding(self) -> bool:
        return not self.onboarding_completed or self.onboarding_version < ONBOARDING_VERSION


def validate_app_id(value: Any) -> str:
    if not isinstance(value, str) or not APP_ID_PATTERN.fullmatch(value):
        raise AppModeError("app id must be a lowercase slug of 1-64 characters (a-z, 0-9, -)")
    return value


def validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise AppModeError("name must contain 1-80 characters")
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise AppModeError("name cannot have surrounding whitespace or control characters")
    return value


def validate_start_url(value: Any) -> str:
    try:
        return canonicalize_web_app_url(value)
    except OnboardingError as error:
        raise AppModeError(str(error)) from error


def validate_host_pattern(value: Any, location: str) -> str:
    """Lowercase ASCII hostname, optionally prefixed by ``*.`` for subdomains."""
    if not isinstance(value, str):
        raise AppModeError(f"{location} must be a hostname string")
    host = value[2:] if value.startswith("*.") else value
    labels = host.split(".")
    if (
        not host
        or len(host) > 253
        or host != host.lower()
        or len(labels) < 2 and host != "localhost"
        or any(not _HOST_LABEL.fullmatch(label) for label in labels)
    ):
        raise AppModeError(f"{location} is not a valid lowercase hostname pattern: {value!r}")
    return value


def _string_tuple(value: Any, location: str, pattern: re.Pattern[str], limit: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise AppModeError(f"{location} must be a list of at most {limit} strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise AppModeError(f"{location}[{index}] is invalid: {item!r}")
        if item not in result:
            result.append(item)
    return tuple(result)


def _host_tuple(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise AppModeError(f"{location} must be a list of at most 64 hostnames")
    result: list[str] = []
    for index, item in enumerate(value):
        host = validate_host_pattern(item, f"{location}[{index}]")
        if host not in result:
            result.append(host)
    return tuple(result)


def validate_accent(value: Any) -> str:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value.lower()):
        raise AppModeError("accent must be a #rrggbb color")
    return value.lower()


def settings_from_dict(data: Any) -> AppSettings:
    if not isinstance(data, Mapping):
        raise AppModeError("settings must be an object")
    unknown = set(data) - set(_SETTINGS_FIELDS)
    if unknown:
        raise AppModeError(f"settings has unknown fields: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    defaults = AppSettings()
    for json_key, attribute in _SETTINGS_FIELDS.items():
        if json_key not in data:
            values[attribute] = getattr(defaults, attribute)
            continue
        value = data[json_key]
        expected = getattr(defaults, attribute)
        if isinstance(expected, tuple):
            if not isinstance(value, list) or len(value) > 400:
                raise AppModeError(f"settings.{json_key} must be a list of at most 400 entries")
            if not all(isinstance(item, str) and _FEATURE_KEY.fullmatch(item) for item in value):
                raise AppModeError(f"settings.{json_key} entries must look like 'site.css|feature'")
            values[attribute] = tuple(dict.fromkeys(value))
            continue
        if isinstance(expected, bool):
            if not isinstance(value, bool):
                raise AppModeError(f"settings.{json_key} must be a boolean")
        elif isinstance(expected, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise AppModeError(f"settings.{json_key} must be an integer")
        elif not isinstance(value, str):
            raise AppModeError(f"settings.{json_key} must be a string")
        values[attribute] = value
    settings = AppSettings(**values)
    if settings.appearance not in APPEARANCES:
        raise AppModeError(f"settings.appearance must be one of {', '.join(APPEARANCES)}")
    if settings.glass not in GLASS_SOURCES:
        raise AppModeError(f"settings.glass must be one of {', '.join(GLASS_SOURCES)}")
    if settings.palette not in PALETTES:
        raise AppModeError(f"settings.palette must be one of {', '.join(PALETTES)}")
    if settings.color_scheme not in COLOR_SCHEMES:
        raise AppModeError(f"settings.colorScheme must be one of {', '.join(COLOR_SCHEMES)}")
    if settings.window_chrome not in WINDOW_CHROMES:
        raise AppModeError(f"settings.windowChrome must be one of {', '.join(WINDOW_CHROMES)}")
    if not TINT_RANGE[0] <= settings.tint <= TINT_RANGE[1]:
        raise AppModeError(f"settings.tint must be between {TINT_RANGE[0]} and {TINT_RANGE[1]}")
    return settings


def settings_to_dict(settings: AppSettings) -> dict[str, Any]:
    data = {json_key: getattr(settings, attribute) for json_key, attribute in _SETTINGS_FIELDS.items()}
    data["disabledFeatures"] = list(settings.disabled_features)
    return data


def definition_from_dict(data: Any) -> AppDefinition:
    if not isinstance(data, Mapping):
        raise AppModeError("app definition must be a JSON object")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise AppModeError(f"app definition has unknown fields: {', '.join(sorted(unknown))}")
    for required in ("schema", "id", "name", "startUrl", "scope"):
        if required not in data:
            raise AppModeError(f"app definition is missing {required}")
    if data["schema"] != SCHEMA:
        raise AppModeError(f"unsupported app definition schema {data['schema']!r}")
    scope = data["scope"]
    if not isinstance(scope, Mapping) or set(scope) - _SCOPE_KEYS or "hosts" not in scope:
        raise AppModeError("scope must be an object with hosts and optional paths")
    scope_hosts = _host_tuple(scope["hosts"], "scope.hosts")
    if not scope_hosts:
        raise AppModeError("scope.hosts must name at least one host")
    scope_paths = _string_tuple(scope.get("paths", []), "scope.paths", _PATH_PREFIX)
    start_url = validate_start_url(data["startUrl"])
    start_host = urlsplit(start_url).hostname or ""
    if not host_matches_any(start_host, scope_hosts):
        raise AppModeError("startUrl host must be inside scope.hosts")
    icon = data.get("icon")
    if icon is not None and (not isinstance(icon, str) or not (_ICON_NAME.fullmatch(icon) or icon.startswith("/"))):
        raise AppModeError("icon must be an icon-theme name or an absolute path")
    preset = data.get("preset")
    if preset is not None and (not isinstance(preset, str) or not _THEME_ID.fullmatch(preset)):
        raise AppModeError("preset must be a preset key")
    onboarding = data.get("onboarding", {"completed": False, "version": ONBOARDING_VERSION})
    if not isinstance(onboarding, Mapping) or set(onboarding) - _ONBOARDING_KEYS:
        raise AppModeError("onboarding must be an object with completed and version")
    completed = onboarding.get("completed", False)
    version = onboarding.get("version", ONBOARDING_VERSION)
    if not isinstance(completed, bool) or isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise AppModeError("onboarding.completed must be a boolean and onboarding.version an integer")
    return AppDefinition(
        id=validate_app_id(data["id"]),
        name=validate_name(data["name"]),
        start_url=start_url,
        scope_hosts=scope_hosts,
        scope_paths=scope_paths,
        auth_hosts=_host_tuple(data.get("authHosts", []), "authHosts"),
        extras=_string_tuple(data.get("extras", []), "extras", _THEME_ID, limit=8),
        icon=icon,
        categories=_string_tuple(data.get("categories", []), "categories", _CATEGORY, limit=12),
        keywords=_string_tuple(
            data.get("keywords", []), "keywords", re.compile(r"^[^;\x00-\x1f\x7f]{1,40}$"), limit=16
        ),
        accent=validate_accent(data.get("accent", "#7b6cff")),
        preset=preset,
        settings=settings_from_dict(data.get("settings", {})),
        onboarding_completed=completed,
        onboarding_version=version,
    )


def definition_to_dict(definition: AppDefinition) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": SCHEMA,
        "id": definition.id,
        "name": definition.name,
        "preset": definition.preset,
        "startUrl": definition.start_url,
        "scope": {"hosts": list(definition.scope_hosts), "paths": list(definition.scope_paths)},
        "authHosts": list(definition.auth_hosts),
        "extras": list(definition.extras),
        "icon": definition.icon,
        "categories": list(definition.categories),
        "keywords": list(definition.keywords),
        "accent": definition.accent,
        "settings": settings_to_dict(definition.settings),
        "onboarding": {"completed": definition.onboarding_completed, "version": definition.onboarding_version},
    }
    return data


def host_matches(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == pattern


def host_matches_any(host: str, patterns: tuple[str, ...]) -> bool:
    return any(host_matches(host, pattern) for pattern in patterns)


def definition_from_preset(
    preset: Preset,
    *,
    app_id: str | None = None,
    name: str | None = None,
    appearance: str | None = None,
) -> AppDefinition:
    settings = AppSettings(
        appearance=appearance or "transparent",
        notifications=preset.notifications,
        media=preset.media,
        autoplay=preset.autoplay,
        drm=preset.drm,
    )
    definition = AppDefinition(
        id=validate_app_id(app_id or preset.key),
        name=validate_name(name or preset.name),
        start_url=validate_start_url(preset.start_url),
        scope_hosts=tuple(validate_host_pattern(h, "preset scope") for h in preset.scope_hosts),
        scope_paths=preset.scope_paths,
        auth_hosts=tuple(validate_host_pattern(h, "preset auth") for h in preset.auth_hosts),
        extras=preset.extras,
        icon=None,
        categories=preset.categories,
        keywords=preset.keywords,
        accent=validate_accent(preset.accent),
        preset=preset.key,
        settings=settings,
    )
    return definition_from_dict(definition_to_dict(definition))


# Leading host labels that name a front door rather than the product
# (app.slack.com → "Slack", web.whatsapp.com → "Whatsapp").
_GENERIC_HOST_LABELS = frozenset(
    {"www", "app", "apps", "web", "open", "m", "mobile", "my", "client", "portal", "go", "beta", "home", "secure"}
)


def _name_label(host: str) -> str:
    labels = [label for label in host.split(":")[0].split(".") if label]
    if len(labels) > 1:
        labels = labels[:-1]  # the TLD never names the app
    for label in labels:
        if label not in _GENERIC_HOST_LABELS:
            return label
    return labels[0] if labels else ""


def definition_from_url(
    url: str,
    *,
    app_id: str | None = None,
    name: str | None = None,
    appearance: str | None = None,
    has_translucent_style: bool = False,
) -> AppDefinition:
    start_url = validate_start_url(url)
    host = urlsplit(start_url).hostname or ""
    if app_id is None:
        base = host[4:] if host.startswith("www.") else host
        base = re.sub(r"[^a-z0-9]+", "-", base.split(":")[0]).strip("-")
        app_id = (base[:64].rstrip("-") or "web-app")
    if name is None:
        label = _name_label(host)
        name = " ".join(part[:1].upper() + part[1:] for part in re.split(r"[-_]+", label) if part) or "Web App"
    definition = AppDefinition(
        id=validate_app_id(app_id),
        name=validate_name(name[:80].strip()),
        start_url=start_url,
        scope_hosts=(host,),
        settings=AppSettings(appearance=appearance or ("transparent" if has_translucent_style else "opaque")),
    )
    return definition_from_dict(definition_to_dict(definition))


def with_settings(definition: AppDefinition, **changes: Any) -> AppDefinition:
    return replace(definition, settings=replace(definition.settings, **changes))
