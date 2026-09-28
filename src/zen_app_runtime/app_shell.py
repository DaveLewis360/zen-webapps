"""Versioned App Shell policy and runtime-bound adapter contracts.

The external path requires a runtime-bound privileged adapter capability and
startup handshake. The bundled path is enforced by the package's verified
Gecko application adapter, which validates a sealed policy descriptor and
ACKs readiness before site content can be requested.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .manifest import APP_ID_PATTERN, AppManifest, manifest_to_dict
from .runtime import (
    RuntimeIdentity,
    RuntimeSafetyError,
    _absolute_normalized_path,
    _load_json,
    _open_absolute_file,
    _read_bounded_utf8,
    _stat_identity,
    runtime_identity_from_dict,
    runtime_identity_to_dict,
)

APP_SHELL_POLICY_VERSION = 2
APP_SHELL_ADAPTER_CAPABILITY_VERSION = 2
APP_SHELL_ADAPTER_PROTOCOL_VERSION = 2
APP_SHELL_HANDSHAKE_VERSION = 2
APP_SHELL_ADAPTER_ID = "org.zen-app-runtime.app-shell"
MAX_ADAPTER_CAPABILITY_BYTES = 64 * 1024
MAX_HANDSHAKE_BYTES = 16 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_ADAPTER_CAPABILITIES = (
    "download-mediation-v2",
    "file-picker-boundary-v1",
    "minimal-security-chrome-v1",
    "navigation-policy-v1",
    "onboarding-v1",
    "permission-mediation-v2",
    "shortcut-policy-v2",
    "startup-handshake-v2",
    "wayland-screencast-portal-v1",
    "window-identity-v1",
)

_ALLOWED_SHORTCUTS = (
    "Alt+F4",
    "Ctrl++",
    "Ctrl+-",
    "Ctrl+0",
    "Ctrl+F",
    "Ctrl+K",
    "Ctrl+P",
    "Ctrl+R",
    "Ctrl+Shift+N",
    "Ctrl+Shift+R",
    "Escape",
)
_BLOCKED_SHORTCUTS = (
    "Ctrl+N",
    "Ctrl+Shift+I",
    "Ctrl+Shift+J",
    "Ctrl+Shift+P",
    "Ctrl+Shift+T",
    "Ctrl+T",
    "F11",
    "F12",
    "F6",
)
_REMAPPED_SHORTCUTS = {
    "Alt+Left": "same-origin-history-back",
    "Alt+Right": "same-origin-history-forward",
    "Ctrl+L": "show-origin-and-security-panel",
    "Ctrl+W": "close-app-window",
}


class AppShellError(ValueError):
    """An App Shell authority document violated its closed contract."""


@dataclass(frozen=True)
class AppShellPolicy:
    policy_version: int
    app_id: str
    start_url: str
    start_origin: str
    presentation_mode: str


@dataclass(frozen=True)
class AppShellAdapterCapability:
    capability_version: int
    adapter_id: str
    protocol_version: int
    runtime_identity: RuntimeIdentity
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class AppShellHandshake:
    handshake_version: int
    adapter_id: str
    protocol_version: int
    policy_sha256: str
    status: str


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise AppShellError(f"{label} fields: {', '.join(details)}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AppShellError(f"{label} must be an object with text keys")
    return value


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _canonical_origin(start_url: str) -> str:
    if (
        not isinstance(start_url, str)
        or any(ord(character) <= 32 or ord(character) == 127 for character in start_url)
        or "\\" in start_url
    ):
        raise AppShellError("app-shell start URL is invalid")
    parsed = urlsplit(start_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AppShellError("app-shell start URL must have a credential-free HTTPS origin")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise AppShellError(f"app-shell start origin is invalid: {error}") from error
    if not host:
        raise AppShellError("app-shell start origin host is empty")
    rendered_host = f"[{host}]" if ":" in host else host
    return f"https://{rendered_host}" + (f":{port}" if port not in (None, 443) else "")


def build_app_shell_policy(manifest: AppManifest) -> AppShellPolicy:
    """Derive the only supported App Shell policy from a strict manifest."""
    manifest_to_dict(manifest)
    return AppShellPolicy(
        policy_version=APP_SHELL_POLICY_VERSION,
        app_id=manifest.app_id,
        start_url=manifest.start_url,
        start_origin=_canonical_origin(manifest.start_url),
        presentation_mode=manifest.configuration.mode,
    )


def app_shell_policy_to_dict(policy: AppShellPolicy) -> dict[str, Any]:
    if policy.policy_version != APP_SHELL_POLICY_VERSION:
        raise AppShellError(f"appShellPolicyVersion must be {APP_SHELL_POLICY_VERSION}")
    if policy.start_origin != _canonical_origin(policy.start_url):
        raise AppShellError("app-shell start origin does not match the start URL")
    if policy.presentation_mode not in ("Opaque", "Transparent"):
        raise AppShellError("app-shell presentation mode is invalid")
    if not isinstance(policy.app_id, str) or not APP_ID_PATTERN.fullmatch(policy.app_id):
        raise AppShellError("app-shell app ID is invalid")
    return {
        "appId": policy.app_id,
        "appShellPolicyVersion": APP_SHELL_POLICY_VERSION,
        "downloads": {
            "defaultAction": "deny-unless-privileged-confirmation",
            "destination": "system-file-picker",
            "maxOutstandingRequests": 1,
            "openAfterDownload": False,
            "requestBinding": "exact-origin-document-navigation-epoch",
            "requestExpiryMs": 30_000,
            "silentDownloads": False,
        },
        "navigation": {
            "authenticationCallbacks": "deny-unless-explicitly-pinned",
            "customProtocols": "confirmation-required",
            "externalTarget": "system-default-browser",
            "internalNavigation": "same-origin",
            "popups": "external-by-default",
            "topLevelDecisionPoint": "final-commit-including-redirects",
            "userOverride": False,
        },
        "onboarding": {
            "defaultProfileImport": False,
            "requiredBeforeSiteContent": True,
            "telemetryPrompt": False,
            "version": 1,
        },
        "permissions": {
            "defaultAction": "deny-unless-privileged-prompt",
            "maxOutstandingRequests": 1,
            "requestBinding": "exact-origin-document-navigation-epoch",
            "requestExpiryMs": 30_000,
            "requestKinds": {
                "camera": "content-activation-and-session-prompt",
                "clipboard-read": "content-activation-and-upstream-paste-consent",
                "clipboard-write": "content-activation-only",
                "file-picker": "content-activation-and-system-picker",
                "microphone": "content-activation-and-session-prompt",
                "notifications": "content-activation-and-session-prompt",
                "screen-capture": "content-activation-shell-prompt-and-portal",
                "speaker-selection": "content-activation-and-session-prompt",
            },
            "revokePendingOn": [
                "navigation",
                "reload",
                "window-close",
                "process-exit",
            ],
            "sessionOnly": True,
            "silentGrants": False,
        },
        "portal": {
            "directSourceSelection": False,
            "persistence": "none",
            "rePromptAfterStop": True,
            "screenCast": "xdg-desktop-portal-required-on-wayland",
            "transientUserActivation": True,
        },
        "presentation": {
            "addressBar": "hidden",
            "mode": policy.presentation_mode,
            "originIndicator": "always-available",
            "permissionIndicator": "always-available",
            "securityChrome": "minimal",
            "sidebar": "hidden",
            "tabStrip": "hidden",
            "topBar": "minimal-security-chrome",
        },
        "shortcuts": {
            "allowed": list(_ALLOWED_SHORTCUTS),
            "blocked": list(_BLOCKED_SHORTCUTS),
            "developerTools": "disabled",
            "osReserved": "not-intercepted",
            "privilegedPriority": True,
            "remapped": dict(_REMAPPED_SHORTCUTS),
        },
        "startOrigin": policy.start_origin,
        "startUrl": policy.start_url,
        "window": {
            "additionalBrowserWindows": "blocked",
            "identity": "app-specific",
            "normalBrowserChrome": "blocked",
        },
    }


def render_app_shell_policy(policy: AppShellPolicy) -> str:
    return _canonical_json_bytes(app_shell_policy_to_dict(policy)).decode("utf-8")


def app_shell_policy_sha256(policy: AppShellPolicy) -> str:
    return hashlib.sha256(_canonical_json_bytes(app_shell_policy_to_dict(policy))).hexdigest()


def adapter_capability_from_dict(value: Any) -> AppShellAdapterCapability:
    data = _mapping(value, "app-shell adapter capability")
    _exact_keys(
        data,
        frozenset(
            {
                "adapterCapabilityVersion",
                "adapterId",
                "adapterProtocolVersion",
                "capabilities",
                "runtimeIdentity",
            }
        ),
        "app-shell adapter capability",
    )
    if type(data["adapterCapabilityVersion"]) is not int or data["adapterCapabilityVersion"] != APP_SHELL_ADAPTER_CAPABILITY_VERSION:
        raise AppShellError(
            f"adapterCapabilityVersion must be {APP_SHELL_ADAPTER_CAPABILITY_VERSION}"
        )
    if data["adapterId"] != APP_SHELL_ADAPTER_ID:
        raise AppShellError(f"adapterId must be {APP_SHELL_ADAPTER_ID}")
    if type(data["adapterProtocolVersion"]) is not int or data["adapterProtocolVersion"] != APP_SHELL_ADAPTER_PROTOCOL_VERSION:
        raise AppShellError(
            f"adapterProtocolVersion must be {APP_SHELL_ADAPTER_PROTOCOL_VERSION}"
        )
    raw_capabilities = data["capabilities"]
    if not isinstance(raw_capabilities, list) or not all(
        isinstance(item, str) for item in raw_capabilities
    ):
        raise AppShellError("adapter capabilities must be an array of strings")
    capabilities = tuple(raw_capabilities)
    if capabilities != REQUIRED_ADAPTER_CAPABILITIES:
        raise AppShellError("adapter capabilities must exactly match the required v2 set")
    try:
        runtime_identity = runtime_identity_from_dict(data["runtimeIdentity"])
    except RuntimeSafetyError as error:
        raise AppShellError(f"invalid adapter runtime identity: {error}") from error
    return AppShellAdapterCapability(
        capability_version=APP_SHELL_ADAPTER_CAPABILITY_VERSION,
        adapter_id=APP_SHELL_ADAPTER_ID,
        protocol_version=APP_SHELL_ADAPTER_PROTOCOL_VERSION,
        runtime_identity=runtime_identity,
        capabilities=capabilities,
    )


def adapter_capability_to_dict(capability: AppShellAdapterCapability) -> dict[str, Any]:
    data = {
        "adapterCapabilityVersion": capability.capability_version,
        "adapterId": capability.adapter_id,
        "adapterProtocolVersion": capability.protocol_version,
        "capabilities": list(capability.capabilities),
        "runtimeIdentity": runtime_identity_to_dict(capability.runtime_identity),
    }
    adapter_capability_from_dict(data)
    return data


def render_adapter_capability(capability: AppShellAdapterCapability) -> str:
    return _canonical_json_bytes(adapter_capability_to_dict(capability)).decode("utf-8")


def require_adapter_runtime_binding(
    capability: AppShellAdapterCapability,
    runtime_identity: RuntimeIdentity,
) -> None:
    adapter_capability_to_dict(capability)
    if capability.runtime_identity != runtime_identity:
        raise AppShellError("app-shell adapter capability is bound to a different runtime identity")


def load_adapter_capability(
    capability_path: os.PathLike[str] | str,
    runtime_identity: RuntimeIdentity,
) -> AppShellAdapterCapability:
    """Read a bounded capability authority and bind it to a verified runtime."""
    try:
        path = _absolute_normalized_path(capability_path, "app-shell adapter capability")
        with _open_absolute_file(path, "app-shell adapter capability") as (
            descriptor,
            initial_info,
        ):
            text = _read_bounded_utf8(
                descriptor,
                MAX_ADAPTER_CAPABILITY_BYTES,
                "app-shell adapter capability",
            )
            final_info = os.fstat(descriptor)
            if _stat_identity(initial_info) != _stat_identity(final_info):
                raise RuntimeSafetyError(
                    "app-shell adapter capability changed while reading"
                )
        value = _load_json(text, "app-shell adapter capability")
    except RuntimeSafetyError as error:
        raise AppShellError(str(error)) from error
    capability = adapter_capability_from_dict(value)
    require_adapter_runtime_binding(capability, runtime_identity)
    return capability


def app_shell_handshake_from_dict(
    value: Any,
    *,
    expected_policy_sha256: str,
) -> AppShellHandshake:
    data = _mapping(value, "app-shell startup handshake")
    _exact_keys(
        data,
        frozenset(
            {
                "adapterId",
                "adapterProtocolVersion",
                "handshakeVersion",
                "policySha256",
                "status",
            }
        ),
        "app-shell startup handshake",
    )
    if type(data["handshakeVersion"]) is not int or data["handshakeVersion"] != APP_SHELL_HANDSHAKE_VERSION:
        raise AppShellError(f"handshakeVersion must be {APP_SHELL_HANDSHAKE_VERSION}")
    if data["adapterId"] != APP_SHELL_ADAPTER_ID:
        raise AppShellError("startup handshake adapter ID does not match")
    if type(data["adapterProtocolVersion"]) is not int or data["adapterProtocolVersion"] != APP_SHELL_ADAPTER_PROTOCOL_VERSION:
        raise AppShellError("startup handshake protocol version does not match")
    digest = data["policySha256"]
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise AppShellError("startup handshake policySha256 must be lowercase hexadecimal")
    if digest != expected_policy_sha256:
        raise AppShellError("startup handshake policy digest does not match")
    if data["status"] != "ready-before-site-content":
        raise AppShellError("startup handshake did not confirm readiness before site content")
    return AppShellHandshake(
        handshake_version=APP_SHELL_HANDSHAKE_VERSION,
        adapter_id=APP_SHELL_ADAPTER_ID,
        protocol_version=APP_SHELL_ADAPTER_PROTOCOL_VERSION,
        policy_sha256=digest,
        status="ready-before-site-content",
    )



def parse_app_shell_handshake_payload(
    payload: bytes,
    *,
    expected_policy_sha256: str,
) -> AppShellHandshake:
    if not isinstance(payload, bytes):
        raise AppShellError("app-shell startup handshake payload must be bytes")
    if len(payload) > MAX_HANDSHAKE_BYTES:
        raise AppShellError("app-shell startup handshake exceeds the size limit")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = _load_json(text, "app-shell startup handshake")
    except (UnicodeDecodeError, RuntimeSafetyError) as error:
        raise AppShellError(f"invalid app-shell startup handshake: {error}") from error
    return app_shell_handshake_from_dict(
        value,
        expected_policy_sha256=expected_policy_sha256,
    )
