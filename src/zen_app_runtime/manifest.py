"""Versioned, strictly validated app-manifest model.

Extension entries are curated metadata selections. They intentionally contain no
URLs, package paths, extension identifiers, or executable third-party content.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

CURRENT_SCHEMA_VERSION = 1
APP_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MODES = frozenset({"Opaque", "Transparent"})


class ManifestError(ValueError):
    """Raised when an app manifest violates its schema or safety invariants."""


@dataclass(frozen=True)
class Configuration:
    mode: str


@dataclass(frozen=True)
class ExtensionSelection:
    id: str
    name: str
    provider: str
    selection: str
    code_included: bool = False


@dataclass(frozen=True)
class LinkPolicy:
    internal_navigation: str = "same-origin"
    default_action: str = "external"
    external_target: str = "system-default-browser"
    allow_user_override: bool = False


@dataclass(frozen=True)
class AppManifest:
    schema_version: int
    app_id: str
    name: str
    start_url: str
    configuration: Configuration
    extensions: tuple[ExtensionSelection, ...]
    link_policy: LinkPolicy


ZEN_INTERNET = ExtensionSelection(
    id="zen-internet",
    name="Zen Internet",
    provider="Zen Browser",
    selection="bundled",
)
STYLUS = ExtensionSelection(
    id="stylus",
    name="Stylus",
    provider="OpenStyles",
    selection="optional",
)
_KNOWN_EXTENSIONS = {item.id: item for item in (ZEN_INTERNET, STYLUS)}
_TOP_LEVEL_KEYS = frozenset(
    {"schemaVersion", "appId", "name", "startUrl", "configuration", "extensions", "linkPolicy"}
)
_CONFIGURATION_KEYS = frozenset({"mode"})
_EXTENSION_KEYS = frozenset({"id", "name", "provider", "selection", "codeIncluded"})
_LINK_POLICY_KEYS = frozenset(
    {"internalNavigation", "defaultAction", "externalTarget", "allowUserOverride"}
)


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], location: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ManifestError(f"{location} fields: {', '.join(details)}")


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{location} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ManifestError(f"{location} keys must be strings")
    return value


def _validate_app_id(app_id: Any) -> str:
    if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
        raise ManifestError("appId must be a lowercase ASCII slug of 1-64 characters")
    return app_id


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ManifestError("name must contain 1-80 characters")
    if name != name.strip() or any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ManifestError("name cannot have surrounding whitespace or control characters")
    return name


def _validate_start_url(start_url: Any) -> str:
    if not isinstance(start_url, str):
        raise ManifestError("startUrl must be a string")
    if any(ord(character) <= 32 or ord(character) == 127 for character in start_url) or "\\" in start_url:
        raise ManifestError("startUrl cannot contain whitespace, control characters, or backslashes")
    try:
        parsed = urlsplit(start_url)
        port = parsed.port
    except ValueError as error:
        raise ManifestError(f"startUrl is invalid: {error}") from error
    if parsed.scheme != "https" or not parsed.hostname:
        raise ManifestError("startUrl must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ManifestError("startUrl cannot contain credentials")
    if port is not None and not 1 <= port <= 65535:
        raise ManifestError("startUrl port is out of range")
    try:
        ascii_host = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ManifestError("startUrl hostname is invalid") from error
    host_without_dot = ascii_host[:-1] if ascii_host.endswith(".") else ascii_host
    labels = host_without_dot.split(".")
    if (
        not host_without_dot
        or len(ascii_host) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[A-Za-z0-9-]+", label)
            for label in labels
        )
    ):
        # Colons identify an IPv6 literal, whose syntax urlsplit already validates.
        if ":" not in ascii_host:
            raise ManifestError("startUrl hostname is invalid")
    return start_url


def _parse_extension(value: Any, index: int) -> ExtensionSelection:
    data = _require_mapping(value, f"extensions[{index}]")
    _require_exact_keys(data, _EXTENSION_KEYS, f"extensions[{index}]")
    extension_id = data["id"]
    expected = _KNOWN_EXTENSIONS.get(extension_id) if isinstance(extension_id, str) else None
    if expected is None:
        raise ManifestError(f"extensions[{index}] is not an approved metadata selection")
    if type(data["codeIncluded"]) is not bool:
        raise ManifestError(f"extensions[{index}].codeIncluded must be boolean")
    if not all(isinstance(data[field], str) for field in ("id", "name", "provider", "selection")):
        raise ManifestError(f"extensions[{index}] catalog fields must be strings")
    parsed = ExtensionSelection(
        id=extension_id,
        name=data["name"],
        provider=data["provider"],
        selection=data["selection"],
        code_included=data["codeIncluded"],
    )
    if parsed != expected:
        raise ManifestError(f"extensions[{index}] metadata does not match the approved catalog")
    return parsed


def create_manifest(
    app_id: str,
    name: str,
    start_url: str,
    mode: str = "Opaque",
    include_stylus: bool = False,
) -> AppManifest:
    """Create a schema-v1 manifest with fixed safe policy and curated metadata."""
    _validate_app_id(app_id)
    _validate_name(name)
    _validate_start_url(start_url)
    if not isinstance(mode, str) or mode not in MODES:
        raise ManifestError(f"configuration.mode must be one of {sorted(MODES)}")
    if not isinstance(include_stylus, bool):
        raise ManifestError("include_stylus must be boolean")
    extensions = (ZEN_INTERNET, STYLUS) if include_stylus else (ZEN_INTERNET,)
    return AppManifest(
        schema_version=CURRENT_SCHEMA_VERSION,
        app_id=app_id,
        name=name,
        start_url=start_url,
        configuration=Configuration(mode=mode),
        extensions=extensions,
        link_policy=LinkPolicy(),
    )


def manifest_from_dict(value: Any) -> AppManifest:
    """Parse untrusted data, rejecting unknown fields and policy weakening."""
    data = _require_mapping(value, "manifest")
    _require_exact_keys(data, _TOP_LEVEL_KEYS, "manifest")
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != CURRENT_SCHEMA_VERSION:
        raise ManifestError(f"schemaVersion must be {CURRENT_SCHEMA_VERSION}")

    configuration = _require_mapping(data["configuration"], "configuration")
    _require_exact_keys(configuration, _CONFIGURATION_KEYS, "configuration")
    mode = configuration["mode"]
    if not isinstance(mode, str) or mode not in MODES:
        raise ManifestError(f"configuration.mode must be one of {sorted(MODES)}")

    raw_extensions = data["extensions"]
    if not isinstance(raw_extensions, list):
        raise ManifestError("extensions must be an array")
    extensions = tuple(_parse_extension(item, index) for index, item in enumerate(raw_extensions))
    allowed_extension_sets = ((ZEN_INTERNET,), (ZEN_INTERNET, STYLUS))
    if extensions not in allowed_extension_sets:
        raise ManifestError("extensions must select bundled Zen Internet and optionally Stylus, in catalog order")

    raw_policy = _require_mapping(data["linkPolicy"], "linkPolicy")
    _require_exact_keys(raw_policy, _LINK_POLICY_KEYS, "linkPolicy")
    if type(raw_policy["allowUserOverride"]) is not bool:
        raise ManifestError("linkPolicy.allowUserOverride must be boolean")
    if not all(
        isinstance(raw_policy[field], str)
        for field in ("internalNavigation", "defaultAction", "externalTarget")
    ):
        raise ManifestError("linkPolicy action fields must be strings")
    link_policy = LinkPolicy(
        internal_navigation=raw_policy["internalNavigation"],
        default_action=raw_policy["defaultAction"],
        external_target=raw_policy["externalTarget"],
        allow_user_override=raw_policy["allowUserOverride"],
    )
    if link_policy != LinkPolicy():
        raise ManifestError("linkPolicy must retain the automatic safe defaults")

    return AppManifest(
        schema_version=CURRENT_SCHEMA_VERSION,
        app_id=_validate_app_id(data["appId"]),
        name=_validate_name(data["name"]),
        start_url=_validate_start_url(data["startUrl"]),
        configuration=Configuration(mode=mode),
        extensions=extensions,
        link_policy=link_policy,
    )


def manifest_to_dict(manifest: AppManifest) -> dict[str, Any]:
    """Serialize only after re-validating the complete manifest."""
    data: dict[str, Any] = {
        "schemaVersion": manifest.schema_version,
        "appId": manifest.app_id,
        "name": manifest.name,
        "startUrl": manifest.start_url,
        "configuration": {"mode": manifest.configuration.mode},
        "extensions": [
            {
                "id": item.id,
                "name": item.name,
                "provider": item.provider,
                "selection": item.selection,
                "codeIncluded": item.code_included,
            }
            for item in manifest.extensions
        ],
        "linkPolicy": {
            "internalNavigation": manifest.link_policy.internal_navigation,
            "defaultAction": manifest.link_policy.default_action,
            "externalTarget": manifest.link_policy.external_target,
            "allowUserOverride": manifest.link_policy.allow_user_override,
        },
    }
    manifest_from_dict(data)
    return data
