"""Engine-neutral Discord-on-Linux web conformance contract.

The suite is expressed only as semantic actions and expected event IDs.  It
contains no browser-engine API, DOM selector, screenshot, account credential,
message text, URL history, or free-form diagnostic field.  Runtime Driver v2
implementations execute the actions and return a closed transcript; this module
validates and evaluates that transcript without launching a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .benchmark_contract import BenchmarkCheckResult

DISCORD_CONFORMANCE_SUITE_VERSION = 1
DISCORD_CONFORMANCE_TRANSCRIPT_VERSION = 1
DISCORD_CONFORMANCE_REPORT_VERSION = 1
DISCORD_FIXTURE_PROTOCOL_VERSION = 1
REQUIRED_DRIVER_PROTOCOL_VERSION = 2
DISCORD_CONFORMANCE_SUITE_ID = "discord-web-on-linux-v1"
DISCORD_LIVE_ORIGIN = "https://discord.com"

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CASE_STATUSES = frozenset({"passed", "failed", "not-run"})
_FAILURE_CODES = frozenset(
    {
        "assertion-failed",
        "driver-error",
        "fixture-error",
        "permission-denied",
        "target-changed",
        "timeout",
        "unsupported",
    }
)
_LAYER_STATUSES = frozenset({"pass", "fail", "incomplete"})

# case id, layer, area, execution, and action/event/timeout triples.  Synthetic
# cases precede live cases so a local platform qualification can be reported as
# incomplete without ever being confused with live Discord parity.
_CASE_DEFINITIONS = (
    (
        "synthetic-startup-boundary",
        "synthetic",
        "shell",
        "explicit-user-activation",
        (
            ("observe-onboarding", "onboarding-before-content", 15000),
            ("activate-onboarding", "content-activation-accepted", 10000),
            ("await-content-request", "content-request-after-activation", 10000),
            ("await-content-commit", "content-commit", 30000),
        ),
    ),
    (
        "synthetic-session-storage-persistence",
        "synthetic",
        "storage",
        "automatic",
        (
            ("write-cookie-localstorage-indexeddb", "storage-written", 10000),
            ("restart-with-retained-profile", "warm-profile-restarted", 30000),
            ("verify-cookie-localstorage-indexeddb", "storage-restored", 10000),
        ),
    ),
    (
        "synthetic-service-worker-cache",
        "synthetic",
        "service-worker",
        "automatic",
        (
            ("register-service-worker", "service-worker-registered", 10000),
            ("await-service-worker-active", "service-worker-active", 10000),
            ("request-controlled-resource", "service-worker-fetch-intercepted", 10000),
            ("verify-offline-cache-hit", "service-worker-cache-hit", 10000),
            ("unregister-service-worker", "service-worker-unregistered", 10000),
        ),
    ),
    (
        "synthetic-websocket-gateway-resume",
        "synthetic",
        "networking",
        "automatic",
        (
            ("connect-fixture-websocket", "websocket-connected", 10000),
            ("exchange-heartbeat", "websocket-heartbeat-ack", 10000),
            ("receive-sequenced-event", "websocket-sequence-received", 10000),
            ("drop-websocket-transport", "websocket-transport-dropped", 10000),
            ("resume-websocket-session", "websocket-session-resumed", 30000),
            ("verify-no-duplicate-event", "websocket-resume-deduplicated", 10000),
        ),
    ),
    (
        "synthetic-fetch-streaming",
        "synthetic",
        "networking",
        "automatic",
        (
            ("fetch-chunked-response", "fetch-stream-consumed", 10000),
            ("post-streaming-body", "fetch-upload-hash-match", 10000),
            ("fetch-range-response", "fetch-range-hash-match", 10000),
            ("abort-inflight-fetch", "fetch-abort-observed", 10000),
        ),
    ),
    (
        "synthetic-file-upload-download",
        "synthetic",
        "files",
        "explicit-user-activation",
        (
            ("activate-file-picker", "file-picker-activated", 15000),
            ("submit-selected-file", "selected-file-hash-match", 15000),
            ("activate-download", "download-activation-accepted", 10000),
            ("confirm-download-prompt", "download-prompt-confirmed", 30000),
            ("verify-downloaded-file", "downloaded-file-hash-match", 15000),
        ),
    ),
    (
        "synthetic-drag-drop-file-input",
        "synthetic",
        "files",
        "automatic",
        (
            ("inject-synthetic-file-drop", "file-drop-received", 10000),
            ("verify-drop-file-metadata", "file-drop-metadata-match", 10000),
            ("verify-drop-file-bytes", "file-drop-hash-match", 10000),
        ),
    ),
    (
        "synthetic-clipboard-roundtrip",
        "synthetic",
        "clipboard",
        "explicit-user-activation",
        (
            ("activate-clipboard-write", "clipboard-write-activated", 10000),
            ("write-synthetic-clipboard", "clipboard-write-complete", 10000),
            ("activate-clipboard-read", "clipboard-read-activated", 10000),
            ("resolve-clipboard-prompt", "clipboard-read-mediated", 30000),
            ("verify-clipboard-roundtrip", "clipboard-roundtrip-match", 10000),
        ),
    ),
    (
        "synthetic-notification-focus",
        "synthetic",
        "notifications",
        "explicit-user-activation",
        (
            ("request-notification-permission", "notification-permission-prompted", 10000),
            ("grant-notification-permission", "notification-permission-granted", 30000),
            ("show-synthetic-notification", "notification-shown", 10000),
            ("activate-notification", "notification-clicked", 30000),
            ("verify-window-focus", "notification-focus-restored", 10000),
        ),
    ),
    (
        "synthetic-audio-output",
        "synthetic",
        "audio",
        "explicit-user-activation",
        (
            ("activate-audio-context", "audio-context-running", 10000),
            ("decode-opus-fixture", "audio-fixture-decoded", 10000),
            ("play-audio-fixture", "audio-playback-progressed", 15000),
            ("await-audio-ended", "audio-playback-ended", 15000),
        ),
    ),
    (
        "synthetic-microphone-webrtc",
        "synthetic",
        "voice",
        "explicit-user-activation",
        (
            ("request-microphone", "microphone-permission-prompted", 10000),
            ("grant-microphone", "microphone-track-live", 30000),
            ("connect-audio-loopback", "microphone-loopback-packets", 30000),
            ("toggle-microphone-mute", "microphone-mute-roundtrip", 10000),
            ("stop-microphone", "microphone-track-ended", 10000),
        ),
    ),
    (
        "synthetic-camera-webrtc",
        "synthetic",
        "video",
        "explicit-user-activation",
        (
            ("request-camera", "camera-permission-prompted", 10000),
            ("grant-camera", "camera-track-live", 30000),
            ("render-camera-frames", "camera-frames-rendered", 30000),
            ("connect-video-loopback", "camera-loopback-frames", 30000),
            ("stop-camera", "camera-track-ended", 10000),
        ),
    ),
    (
        "synthetic-screen-share-portal",
        "synthetic",
        "screen-share",
        "explicit-user-activation",
        (
            ("activate-display-capture", "display-capture-activated", 10000),
            ("request-display-capture", "screen-cast-portal-presented", 30000),
            ("select-synthetic-portal-source", "display-track-live", 30000),
            ("render-display-frame", "display-frame-rendered", 30000),
            ("stop-display-capture", "display-track-ended", 10000),
            ("request-display-after-restart", "screen-cast-portal-presented-again", 30000),
        ),
    ),
    (
        "synthetic-webrtc-network-recovery",
        "synthetic",
        "webrtc",
        "automatic",
        (
            ("connect-webrtc-loopback", "webrtc-connected", 30000),
            ("verify-webrtc-media-flow", "webrtc-media-flowing", 15000),
            ("cut-fixture-network", "webrtc-network-lost", 10000),
            ("restart-ice", "webrtc-ice-restarted", 30000),
            ("restore-fixture-network", "webrtc-media-resumed", 30000),
        ),
    ),
    (
        "synthetic-media-codecs",
        "synthetic",
        "media",
        "automatic",
        (
            ("negotiate-opus", "opus-negotiated", 30000),
            ("decode-opus", "opus-decoded", 15000),
            ("negotiate-browser-video-codec", "video-codec-negotiated", 30000),
            ("decode-browser-video-codec", "video-codec-decoded", 30000),
        ),
    ),
    (
        "synthetic-rendering-emoji-fonts",
        "synthetic",
        "rendering",
        "automatic",
        (
            ("render-canvas-fixture", "canvas-pixel-hash-match", 10000),
            ("render-webgl-fixture", "webgl-pixel-hash-match", 15000),
            ("render-emoji-fixture", "emoji-layout-hash-match", 10000),
            ("load-webfont-fixture", "webfont-layout-hash-match", 10000),
        ),
    ),
    (
        "synthetic-navigation-auth-protocol",
        "synthetic",
        "navigation",
        "explicit-user-activation",
        (
            ("navigate-same-origin", "same-origin-navigation-internal", 10000),
            ("start-auth-callback-flow", "auth-origin-prompted", 15000),
            ("confirm-auth-origin", "auth-origin-committed", 15000),
            ("complete-auth-callback", "auth-return-committed", 15000),
            ("activate-external-link", "external-link-delegated", 15000),
            ("activate-custom-protocol", "custom-protocol-confirmed", 15000),
        ),
    ),
    (
        "synthetic-keyboard-accessibility",
        "synthetic",
        "accessibility",
        "explicit-user-activation",
        (
            ("traverse-focus-order", "focus-order-valid", 15000),
            ("verify-visible-focus", "focus-indicator-visible", 10000),
            ("inspect-accessibility-tree", "accessibility-names-valid", 15000),
            ("enable-reduced-motion", "reduced-motion-observed", 10000),
            ("dispatch-discord-shortcuts", "discord-shortcuts-delivered", 15000),
        ),
    ),
    (
        "synthetic-background-heartbeat",
        "synthetic",
        "lifecycle",
        "automatic",
        (
            ("connect-heartbeat-channel", "background-heartbeat-connected", 10000),
            ("background-app-window", "app-window-backgrounded", 10000),
            ("observe-background-heartbeats", "background-heartbeats-maintained", 120000),
            ("foreground-app-window", "app-window-foregrounded", 10000),
        ),
    ),
    (
        "synthetic-offline-recovery",
        "synthetic",
        "lifecycle",
        "automatic",
        (
            ("force-fixture-offline", "offline-event-observed", 10000),
            ("verify-offline-ui-signal", "offline-state-rendered", 10000),
            ("restore-fixture-online", "online-event-observed", 10000),
            ("verify-state-resync", "online-state-resynchronized", 30000),
        ),
    ),
    (
        "synthetic-secure-context-isolation",
        "synthetic",
        "security",
        "automatic",
        (
            ("verify-secure-context", "secure-context-confirmed", 10000),
            ("verify-cross-origin-isolation-policy", "origin-boundary-confirmed", 10000),
            ("attempt-forbidden-top-navigation", "forbidden-navigation-blocked", 10000),
            ("verify-release-debug-closed", "release-debug-authority-absent", 10000),
        ),
    ),
    (
        "live-auth-session-persistence",
        "live-discord",
        "authentication",
        "operator-assisted",
        (
            ("complete-live-discord-login", "live-login-complete", 180000),
            ("restart-live-discord-session", "live-session-restarted", 60000),
            ("verify-live-session-restored", "live-session-restored", 30000),
            ("complete-live-logout-login", "live-reauthentication-complete", 180000),
        ),
    ),
    (
        "live-server-channel-dm-navigation",
        "live-discord",
        "navigation",
        "operator-assisted",
        (
            ("open-test-server", "live-test-server-opened", 30000),
            ("switch-text-channels", "live-channel-switch-complete", 30000),
            ("open-test-direct-message", "live-direct-message-opened", 30000),
            ("return-to-test-channel", "live-channel-returned", 30000),
        ),
    ),
    (
        "live-message-lifecycle",
        "live-discord",
        "messaging",
        "operator-assisted",
        (
            ("send-unique-test-message", "live-message-sent", 30000),
            ("edit-test-message", "live-message-edited", 30000),
            ("react-to-test-message", "live-message-reaction-added", 30000),
            ("reply-to-test-message", "live-message-reply-sent", 30000),
            ("create-test-thread", "live-message-thread-created", 30000),
            ("delete-test-artifacts", "live-message-artifacts-deleted", 30000),
        ),
    ),
    (
        "live-rich-content-rendering",
        "live-discord",
        "messaging",
        "operator-assisted",
        (
            ("send-markdown-test-message", "live-markdown-rendered", 30000),
            ("open-emoji-picker", "live-emoji-picker-opened", 30000),
            ("send-test-emoji", "live-emoji-rendered", 30000),
            ("open-gif-picker", "live-gif-picker-opened", 30000),
            ("verify-link-embed", "live-link-embed-rendered", 30000),
            ("delete-rich-content-artifacts", "live-rich-artifacts-deleted", 30000),
        ),
    ),
    (
        "live-search-history-pagination",
        "live-discord",
        "search",
        "operator-assisted",
        (
            ("load-older-channel-history", "live-history-page-loaded", 30000),
            ("search-current-channel", "live-channel-search-complete", 30000),
            ("search-all-channels", "live-global-search-complete", 30000),
            ("jump-to-search-result", "live-search-result-opened", 30000),
        ),
    ),
    (
        "live-upload-download-preview",
        "live-discord",
        "files",
        "operator-assisted",
        (
            ("upload-synthetic-test-file", "live-file-uploaded", 60000),
            ("open-upload-preview", "live-upload-preview-opened", 30000),
            ("download-uploaded-file", "live-file-download-mediated", 60000),
            ("verify-live-file-hash", "live-file-hash-match", 30000),
            ("delete-upload-message", "live-upload-artifact-deleted", 30000),
        ),
    ),
    (
        "live-notification-focus",
        "live-discord",
        "notifications",
        "operator-assisted",
        (
            ("enable-live-browser-notification", "live-notification-enabled", 60000),
            ("trigger-test-account-notification", "live-notification-received", 60000),
            ("activate-live-notification", "live-notification-activated", 30000),
            ("verify-live-notification-target", "live-notification-target-opened", 30000),
        ),
    ),
    (
        "live-voice-channel",
        "live-discord",
        "voice",
        "operator-assisted",
        (
            ("join-test-voice-channel", "live-voice-connected", 60000),
            ("verify-microphone-send", "live-voice-microphone-sent", 60000),
            ("verify-remote-audio-receive", "live-voice-audio-received", 60000),
            ("toggle-live-mute-deafen", "live-voice-controls-roundtrip", 30000),
            ("leave-test-voice-channel", "live-voice-disconnected", 30000),
        ),
    ),
    (
        "live-video-call",
        "live-discord",
        "video",
        "operator-assisted",
        (
            ("start-test-video-call", "live-video-connected", 60000),
            ("verify-camera-send", "live-video-camera-sent", 60000),
            ("verify-remote-video-receive", "live-video-remote-rendered", 60000),
            ("open-video-popout", "live-video-popout-opened", 30000),
            ("return-video-to-main-window", "live-video-popout-returned", 30000),
            ("end-test-video-call", "live-video-disconnected", 30000),
        ),
    ),
    (
        "live-screen-share",
        "live-discord",
        "screen-share",
        "operator-assisted",
        (
            ("join-screen-share-call", "live-screen-call-connected", 60000),
            ("activate-live-screen-share", "live-screen-activation-accepted", 30000),
            ("select-portal-test-source", "live-screen-share-started", 60000),
            ("verify-remote-screen-frames", "live-screen-frames-received", 60000),
            ("stop-live-screen-share", "live-screen-share-stopped", 30000),
            ("request-live-screen-share-again", "live-screen-portal-reprompted", 60000),
            ("leave-screen-share-call", "live-screen-call-disconnected", 30000),
        ),
    ),
    (
        "live-shortcuts-keyboard-accessibility",
        "live-discord",
        "accessibility",
        "operator-assisted",
        (
            ("open-discord-shortcut-help", "live-shortcut-help-opened", 30000),
            ("invoke-discord-quick-switcher", "live-quick-switcher-opened", 30000),
            ("invoke-discord-create-join-server", "live-create-join-server-opened", 30000),
            ("invoke-discord-search-shortcuts", "live-search-shortcuts-delivered", 30000),
            ("invoke-discord-media-shortcuts", "live-media-shortcuts-delivered", 30000),
            ("traverse-live-ui-by-keyboard", "live-keyboard-navigation-complete", 60000),
            ("inspect-live-accessibility-tree", "live-accessibility-names-valid", 30000),
        ),
    ),
    (
        "live-gateway-reconnect-resume",
        "live-discord",
        "networking",
        "operator-assisted",
        (
            ("observe-live-gateway-ready", "live-gateway-connected", 30000),
            ("interrupt-live-network", "live-gateway-disconnected", 30000),
            ("restore-live-network", "live-gateway-resumed", 60000),
            ("verify-live-state-consistency", "live-gateway-state-consistent", 30000),
            ("verify-live-no-duplicate-events", "live-gateway-events-deduplicated", 30000),
        ),
    ),
    (
        "live-settings-persistence",
        "live-discord",
        "settings",
        "operator-assisted",
        (
            ("change-test-safe-setting", "live-setting-changed", 30000),
            ("restart-after-setting-change", "live-setting-session-restarted", 60000),
            ("verify-setting-persisted", "live-setting-restored", 30000),
            ("restore-original-setting", "live-setting-cleanup-complete", 30000),
        ),
    ),
    (
        "live-oauth-invite-external-navigation",
        "live-discord",
        "navigation",
        "operator-assisted",
        (
            ("complete-live-oauth-roundtrip", "live-oauth-returned", 180000),
            ("open-live-test-invite", "live-invite-opened", 60000),
            ("activate-live-external-link", "live-external-link-delegated", 30000),
            ("cancel-live-custom-protocol", "live-custom-protocol-cancelled", 30000),
        ),
    ),
    (
        "live-background-idle-resume",
        "live-discord",
        "lifecycle",
        "operator-assisted",
        (
            ("background-live-discord", "live-discord-backgrounded", 30000),
            ("hold-live-background-idle", "live-background-connection-maintained", 120000),
            ("foreground-live-discord", "live-discord-foregrounded", 30000),
            ("verify-live-state-after-idle", "live-idle-state-consistent", 30000),
        ),
    ),
)

_SCOPE_EXCLUSIONS = (
    ("screen-share-system-audio", "not-supported-by-upstream-linux-web"),
    ("custom-global-keybinds", "desktop-client-only"),
    ("game-overlay", "desktop-client-only"),
    ("screen-share-quality-control", "browser-client-does-not-offer-control"),
)

_SOURCE_REFERENCES = (
    (
        "discord-shortcuts",
        "https://support.discord.com/hc/en-us/articles/31232432266647-Discord-Commands-Shortcuts-and-Navigation-Guide",
    ),
    (
        "discord-screen-share",
        "https://support.discord.com/hc/en-us/articles/360040816151",
    ),
    (
        "discord-voice-video-streaming",
        "https://support.discord.com/hc/en-us/articles/33030151293079-Discord-Voice-Video-Streaming-Guide",
    ),
)


class DiscordConformanceError(ValueError):
    """A conformance suite, transcript, or report violated its closed schema."""


@dataclass(frozen=True)
class DiscordConformanceStep:
    action_id: str
    expected_event: str
    timeout_ms: int


@dataclass(frozen=True)
class DiscordConformanceCase:
    case_id: str
    layer: str
    area: str
    execution: str
    required: bool
    steps: tuple[DiscordConformanceStep, ...]


@dataclass(frozen=True)
class DiscordScopeExclusion:
    capability_id: str
    reason_code: str


@dataclass(frozen=True)
class DiscordSourceReference:
    reference_id: str
    url: str


@dataclass(frozen=True)
class DiscordConformanceSuite:
    suite_version: int
    suite_id: str
    required_driver_protocol_version: int
    fixture_protocol_version: int
    live_origin: str
    cases: tuple[DiscordConformanceCase, ...]
    scope_exclusions: tuple[DiscordScopeExclusion, ...]
    source_references: tuple[DiscordSourceReference, ...]


@dataclass(frozen=True)
class DiscordConformanceEvent:
    event_id: str
    elapsed_ms: int


@dataclass(frozen=True)
class DiscordConformanceCaseResult:
    case_id: str
    status: str
    failure_code: str | None
    events: tuple[DiscordConformanceEvent, ...]


@dataclass(frozen=True)
class DiscordConformanceTranscript:
    transcript_version: int
    suite_sha256: str
    fixture_protocol_version: int
    driver_id: str
    driver_protocol_version: int
    live_origin: str
    mode: str
    started_at_utc: str
    completed_at_utc: str
    cases: tuple[DiscordConformanceCaseResult, ...]


@dataclass(frozen=True)
class DiscordConformanceReport:
    report_version: int
    suite_sha256: str
    transcript_sha256: str
    synthetic_status: str
    live_status: str
    overall_status: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    not_run_cases: int


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise DiscordConformanceError(f"{label} must be an object with text keys")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise DiscordConformanceError(f"{label} must be an array")
    return value


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
        raise DiscordConformanceError(f"{label} fields: {', '.join(details)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise DiscordConformanceError(f"{label} must be an integer")
    if value < minimum:
        raise DiscordConformanceError(f"{label} must be at least {minimum}")
    return value


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not _IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise DiscordConformanceError(f"{label} must be a bounded lowercase identifier")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise DiscordConformanceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.fullmatch(value):
        raise DiscordConformanceError(f"{label} must be second-precision UTC RFC3339 text")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise DiscordConformanceError(f"{label} is not a valid UTC timestamp") from error
    return value, parsed


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise DiscordConformanceError(f"document is not canonical JSON data: {error}") from error


def _default_cases() -> tuple[DiscordConformanceCase, ...]:
    return tuple(
        DiscordConformanceCase(
            case_id=case_id,
            layer=layer,
            area=area,
            execution=execution,
            required=True,
            steps=tuple(
                DiscordConformanceStep(action_id, expected_event, timeout_ms)
                for action_id, expected_event, timeout_ms in steps
            ),
        )
        for case_id, layer, area, execution, steps in _CASE_DEFINITIONS
    )


def build_discord_conformance_suite() -> DiscordConformanceSuite:
    """Return the immutable Discord web-on-Linux conformance suite v1."""
    return DiscordConformanceSuite(
        suite_version=DISCORD_CONFORMANCE_SUITE_VERSION,
        suite_id=DISCORD_CONFORMANCE_SUITE_ID,
        required_driver_protocol_version=REQUIRED_DRIVER_PROTOCOL_VERSION,
        fixture_protocol_version=DISCORD_FIXTURE_PROTOCOL_VERSION,
        live_origin=DISCORD_LIVE_ORIGIN,
        cases=_default_cases(),
        scope_exclusions=tuple(
            DiscordScopeExclusion(capability_id, reason_code)
            for capability_id, reason_code in _SCOPE_EXCLUSIONS
        ),
        source_references=tuple(
            DiscordSourceReference(reference_id, url)
            for reference_id, url in _SOURCE_REFERENCES
        ),
    )


def _step_to_dict(step: DiscordConformanceStep) -> dict[str, Any]:
    return {
        "actionId": step.action_id,
        "expectedEvent": step.expected_event,
        "timeoutMs": step.timeout_ms,
    }


def _case_to_dict(case: DiscordConformanceCase) -> dict[str, Any]:
    return {
        "area": case.area,
        "caseId": case.case_id,
        "execution": case.execution,
        "layer": case.layer,
        "required": case.required,
        "steps": [_step_to_dict(step) for step in case.steps],
    }


def _suite_to_dict_unchecked(suite: DiscordConformanceSuite) -> dict[str, Any]:
    return {
        "cases": [_case_to_dict(case) for case in suite.cases],
        "discordConformanceSuiteVersion": suite.suite_version,
        "fixtureProtocolVersion": suite.fixture_protocol_version,
        "liveOrigin": suite.live_origin,
        "requiredDriverProtocolVersion": suite.required_driver_protocol_version,
        "scopeExclusions": [
            {
                "capabilityId": exclusion.capability_id,
                "reasonCode": exclusion.reason_code,
            }
            for exclusion in suite.scope_exclusions
        ],
        "sourceReferences": [
            {"referenceId": reference.reference_id, "url": reference.url}
            for reference in suite.source_references
        ],
        "suiteId": suite.suite_id,
    }


def _parse_step(value: Any, label: str) -> DiscordConformanceStep:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"actionId", "expectedEvent", "timeoutMs"}), label)
    timeout = _integer(data["timeoutMs"], f"{label} timeoutMs", minimum=1)
    if timeout > 300000:
        raise DiscordConformanceError(f"{label} timeoutMs exceeds five minutes")
    return DiscordConformanceStep(
        _identifier(data["actionId"], f"{label} actionId"),
        _identifier(data["expectedEvent"], f"{label} expectedEvent"),
        timeout,
    )


def _parse_case(value: Any, label: str) -> DiscordConformanceCase:
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset({"area", "caseId", "execution", "layer", "required", "steps"}),
        label,
    )
    if type(data["required"]) is not bool or data["required"] is not True:
        raise DiscordConformanceError(f"{label} required must be true")
    steps = tuple(
        _parse_step(item, f"{label} step {index}")
        for index, item in enumerate(_array(data["steps"], f"{label} steps"))
    )
    if not steps:
        raise DiscordConformanceError(f"{label} steps cannot be empty")
    layer = _identifier(data["layer"], f"{label} layer")
    if layer not in ("synthetic", "live-discord"):
        raise DiscordConformanceError(f"{label} layer is invalid")
    execution = _identifier(data["execution"], f"{label} execution")
    if execution not in ("automatic", "explicit-user-activation", "operator-assisted"):
        raise DiscordConformanceError(f"{label} execution is invalid")
    return DiscordConformanceCase(
        case_id=_identifier(data["caseId"], f"{label} caseId"),
        layer=layer,
        area=_identifier(data["area"], f"{label} area"),
        execution=execution,
        required=True,
        steps=steps,
    )


def _parse_exclusion(value: Any, label: str) -> DiscordScopeExclusion:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"capabilityId", "reasonCode"}), label)
    return DiscordScopeExclusion(
        _identifier(data["capabilityId"], f"{label} capabilityId"),
        _identifier(data["reasonCode"], f"{label} reasonCode"),
    )


def _parse_reference(value: Any, label: str) -> DiscordSourceReference:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"referenceId", "url"}), label)
    reference_id = _identifier(data["referenceId"], f"{label} referenceId")
    url = data["url"]
    if (
        not isinstance(url, str)
        or len(url) > 512
        or not url.startswith("https://support.discord.com/")
        or any(ord(character) < 32 or ord(character) == 127 for character in url)
    ):
        raise DiscordConformanceError(f"{label} URL must be a bounded Discord support URL")
    return DiscordSourceReference(reference_id, url)


def discord_conformance_suite_from_dict(value: Any) -> DiscordConformanceSuite:
    data = _mapping(value, "Discord conformance suite")
    _exact_keys(
        data,
        frozenset(
            {
                "cases",
                "discordConformanceSuiteVersion",
                "fixtureProtocolVersion",
                "liveOrigin",
                "requiredDriverProtocolVersion",
                "scopeExclusions",
                "sourceReferences",
                "suiteId",
            }
        ),
        "Discord conformance suite",
    )
    version = _integer(
        data["discordConformanceSuiteVersion"],
        "discordConformanceSuiteVersion",
        minimum=1,
    )
    if version != DISCORD_CONFORMANCE_SUITE_VERSION:
        raise DiscordConformanceError(
            f"discordConformanceSuiteVersion must be {DISCORD_CONFORMANCE_SUITE_VERSION}"
        )
    parsed = DiscordConformanceSuite(
        suite_version=version,
        suite_id=_identifier(data["suiteId"], "suiteId"),
        required_driver_protocol_version=_integer(
            data["requiredDriverProtocolVersion"],
            "requiredDriverProtocolVersion",
            minimum=1,
        ),
        fixture_protocol_version=_integer(
            data["fixtureProtocolVersion"],
            "fixtureProtocolVersion",
            minimum=1,
        ),
        live_origin=data["liveOrigin"] if isinstance(data["liveOrigin"], str) else "",
        cases=tuple(
            _parse_case(item, f"Discord conformance case {index}")
            for index, item in enumerate(_array(data["cases"], "conformance cases"))
        ),
        scope_exclusions=tuple(
            _parse_exclusion(item, f"scope exclusion {index}")
            for index, item in enumerate(
                _array(data["scopeExclusions"], "scopeExclusions")
            )
        ),
        source_references=tuple(
            _parse_reference(item, f"source reference {index}")
            for index, item in enumerate(
                _array(data["sourceReferences"], "sourceReferences")
            )
        ),
    )
    if parsed != build_discord_conformance_suite():
        raise DiscordConformanceError(
            "Discord conformance suite must exactly match immutable v1"
        )
    return parsed


def discord_conformance_suite_to_dict(
    suite: DiscordConformanceSuite,
) -> dict[str, Any]:
    data = _suite_to_dict_unchecked(suite)
    parsed = discord_conformance_suite_from_dict(data)
    if parsed != suite:
        raise DiscordConformanceError("Discord conformance suite changed during validation")
    return data


def render_discord_conformance_suite(suite: DiscordConformanceSuite) -> str:
    return _canonical_json(discord_conformance_suite_to_dict(suite))


def discord_conformance_suite_sha256(suite: DiscordConformanceSuite) -> str:
    return hashlib.sha256(
        render_discord_conformance_suite(suite).encode("utf-8")
    ).hexdigest()


def _parse_event(
    value: Any,
    expected_event: str,
    label: str,
    previous_elapsed: int,
) -> DiscordConformanceEvent:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"elapsedMs", "eventId"}), label)
    event_id = _identifier(data["eventId"], f"{label} eventId")
    if event_id != expected_event:
        raise DiscordConformanceError(
            f"{label} event must be {expected_event}, not {event_id}"
        )
    elapsed = _integer(data["elapsedMs"], f"{label} elapsedMs")
    if elapsed <= previous_elapsed:
        raise DiscordConformanceError("conformance event elapsedMs must be strictly increasing")
    return DiscordConformanceEvent(event_id, elapsed)


def _parse_case_result(
    value: Any,
    case: DiscordConformanceCase,
    previous_elapsed: int,
) -> tuple[DiscordConformanceCaseResult, int]:
    label = f"conformance result {case.case_id}"
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"caseId", "events", "failureCode", "status"}), label)
    case_id = _identifier(data["caseId"], f"{label} caseId")
    if case_id != case.case_id:
        raise DiscordConformanceError(
            f"conformance case order must match {case.case_id}"
        )
    status = _identifier(data["status"], f"{label} status")
    if status not in _CASE_STATUSES:
        raise DiscordConformanceError(f"{label} status is invalid")
    raw_failure = data["failureCode"]
    raw_events = _array(data["events"], f"{label} events")
    expected = tuple(step.expected_event for step in case.steps)
    if status == "passed":
        if raw_failure is not None:
            raise DiscordConformanceError(f"{label} passed result cannot have failureCode")
        if len(raw_events) != len(expected):
            raise DiscordConformanceError(
                f"{label} passed events must exactly match required steps"
            )
        failure_code = None
    elif status == "failed":
        if not isinstance(raw_failure, str) or raw_failure not in _FAILURE_CODES:
            raise DiscordConformanceError(f"{label} failed result needs a closed failureCode")
        if len(raw_events) > len(expected):
            raise DiscordConformanceError(f"{label} failed events cannot exceed required steps")
        failure_code = raw_failure
    else:
        if raw_failure is not None or raw_events:
            raise DiscordConformanceError(
                f"{label} not-run result cannot contain events or failureCode"
            )
        failure_code = None
    events: list[DiscordConformanceEvent] = []
    current_elapsed = previous_elapsed
    for index, item in enumerate(raw_events):
        event = _parse_event(
            item,
            expected[index],
            f"{label} event {index}",
            current_elapsed,
        )
        events.append(event)
        current_elapsed = event.elapsed_ms
    return (
        DiscordConformanceCaseResult(case_id, status, failure_code, tuple(events)),
        current_elapsed,
    )


def discord_conformance_transcript_from_dict(
    value: Any,
    *,
    suite: DiscordConformanceSuite,
) -> DiscordConformanceTranscript:
    discord_conformance_suite_to_dict(suite)
    data = _mapping(value, "Discord conformance transcript")
    _exact_keys(
        data,
        frozenset(
            {
                "cases",
                "completedAtUtc",
                "discordConformanceTranscriptVersion",
                "driver",
                "fixtureProtocolVersion",
                "liveOrigin",
                "mode",
                "startedAtUtc",
                "suiteSha256",
            }
        ),
        "Discord conformance transcript",
    )
    version = _integer(
        data["discordConformanceTranscriptVersion"],
        "discordConformanceTranscriptVersion",
        minimum=1,
    )
    if version != DISCORD_CONFORMANCE_TRANSCRIPT_VERSION:
        raise DiscordConformanceError(
            f"discordConformanceTranscriptVersion must be {DISCORD_CONFORMANCE_TRANSCRIPT_VERSION}"
        )
    suite_digest = _digest(data["suiteSha256"], "transcript suiteSha256")
    expected_digest = discord_conformance_suite_sha256(suite)
    if suite_digest != expected_digest:
        raise DiscordConformanceError("transcript is bound to a different conformance suite")
    fixture_version = _integer(
        data["fixtureProtocolVersion"],
        "transcript fixtureProtocolVersion",
        minimum=1,
    )
    if fixture_version != suite.fixture_protocol_version:
        raise DiscordConformanceError("transcript fixture protocol does not match suite")
    driver = _mapping(data["driver"], "conformance transcript driver")
    _exact_keys(driver, frozenset({"id", "protocolVersion"}), "conformance transcript driver")
    driver_id = _identifier(driver["id"], "conformance driver id")
    driver_protocol = _integer(
        driver["protocolVersion"], "conformance driver protocolVersion", minimum=1
    )
    if driver_protocol != suite.required_driver_protocol_version:
        raise DiscordConformanceError("conformance driver protocol does not match suite")
    if data["liveOrigin"] != suite.live_origin:
        raise DiscordConformanceError("transcript live origin does not match suite")
    if data["mode"] != "synthetic-and-live":
        raise DiscordConformanceError("transcript mode must be synthetic-and-live")
    started_text, started = _timestamp(data["startedAtUtc"], "startedAtUtc")
    completed_text, completed = _timestamp(data["completedAtUtc"], "completedAtUtc")
    if completed < started:
        raise DiscordConformanceError("completedAtUtc cannot precede startedAtUtc")

    raw_cases = _array(data["cases"], "transcript cases")
    if len(raw_cases) != len(suite.cases):
        raise DiscordConformanceError("transcript cases must exactly match the suite")
    cases: list[DiscordConformanceCaseResult] = []
    previous_elapsed = -1
    for item, case in zip(raw_cases, suite.cases, strict=True):
        result, previous_elapsed = _parse_case_result(item, case, previous_elapsed)
        cases.append(result)
    duration_ms = int((completed - started).total_seconds()) * 1000
    if previous_elapsed > duration_ms:
        raise DiscordConformanceError("event elapsedMs exceeds transcript duration")
    return DiscordConformanceTranscript(
        transcript_version=version,
        suite_sha256=suite_digest,
        fixture_protocol_version=fixture_version,
        driver_id=driver_id,
        driver_protocol_version=driver_protocol,
        live_origin=suite.live_origin,
        mode="synthetic-and-live",
        started_at_utc=started_text,
        completed_at_utc=completed_text,
        cases=tuple(cases),
    )


def _event_to_dict(event: DiscordConformanceEvent) -> dict[str, Any]:
    return {"elapsedMs": event.elapsed_ms, "eventId": event.event_id}


def _case_result_to_dict(result: DiscordConformanceCaseResult) -> dict[str, Any]:
    return {
        "caseId": result.case_id,
        "events": [_event_to_dict(event) for event in result.events],
        "failureCode": result.failure_code,
        "status": result.status,
    }


def _transcript_to_dict_unchecked(
    transcript: DiscordConformanceTranscript,
) -> dict[str, Any]:
    return {
        "cases": [_case_result_to_dict(result) for result in transcript.cases],
        "completedAtUtc": transcript.completed_at_utc,
        "discordConformanceTranscriptVersion": transcript.transcript_version,
        "driver": {
            "id": transcript.driver_id,
            "protocolVersion": transcript.driver_protocol_version,
        },
        "fixtureProtocolVersion": transcript.fixture_protocol_version,
        "liveOrigin": transcript.live_origin,
        "mode": transcript.mode,
        "startedAtUtc": transcript.started_at_utc,
        "suiteSha256": transcript.suite_sha256,
    }


def discord_conformance_transcript_to_dict(
    transcript: DiscordConformanceTranscript,
    *,
    suite: DiscordConformanceSuite,
) -> dict[str, Any]:
    data = _transcript_to_dict_unchecked(transcript)
    parsed = discord_conformance_transcript_from_dict(data, suite=suite)
    if parsed != transcript:
        raise DiscordConformanceError("Discord transcript changed during validation")
    return data


def render_discord_conformance_transcript(
    transcript: DiscordConformanceTranscript,
    *,
    suite: DiscordConformanceSuite,
) -> str:
    return _canonical_json(discord_conformance_transcript_to_dict(transcript, suite=suite))


def discord_conformance_transcript_sha256(
    transcript: DiscordConformanceTranscript,
    *,
    suite: DiscordConformanceSuite,
) -> str:
    return hashlib.sha256(
        render_discord_conformance_transcript(transcript, suite=suite).encode("utf-8")
    ).hexdigest()


def _layer_status(results: tuple[DiscordConformanceCaseResult, ...]) -> str:
    statuses = {result.status for result in results}
    if "failed" in statuses:
        return "fail"
    if "not-run" in statuses:
        return "incomplete"
    return "pass"


def evaluate_discord_conformance(
    suite: DiscordConformanceSuite,
    transcript: DiscordConformanceTranscript,
) -> DiscordConformanceReport:
    discord_conformance_suite_to_dict(suite)
    discord_conformance_transcript_to_dict(transcript, suite=suite)
    pairs = tuple(zip(suite.cases, transcript.cases, strict=True))
    synthetic_results = tuple(result for case, result in pairs if case.layer == "synthetic")
    live_results = tuple(result for case, result in pairs if case.layer == "live-discord")
    synthetic_status = _layer_status(synthetic_results)
    live_status = _layer_status(live_results)
    if "fail" in (synthetic_status, live_status):
        overall = "fail"
    elif "incomplete" in (synthetic_status, live_status):
        overall = "incomplete"
    else:
        overall = "pass"
    passed = sum(result.status == "passed" for result in transcript.cases)
    failed = sum(result.status == "failed" for result in transcript.cases)
    not_run = sum(result.status == "not-run" for result in transcript.cases)
    return DiscordConformanceReport(
        report_version=DISCORD_CONFORMANCE_REPORT_VERSION,
        suite_sha256=discord_conformance_suite_sha256(suite),
        transcript_sha256=discord_conformance_transcript_sha256(
            transcript, suite=suite
        ),
        synthetic_status=synthetic_status,
        live_status=live_status,
        overall_status=overall,
        total_cases=len(transcript.cases),
        passed_cases=passed,
        failed_cases=failed,
        not_run_cases=not_run,
    )


def discord_conformance_report_to_dict(
    report: DiscordConformanceReport,
) -> dict[str, Any]:
    if (
        type(report.report_version) is not int
        or report.report_version != DISCORD_CONFORMANCE_REPORT_VERSION
    ):
        raise DiscordConformanceError(
            f"discordConformanceReportVersion must be {DISCORD_CONFORMANCE_REPORT_VERSION}"
        )
    suite_digest = _digest(report.suite_sha256, "report suiteSha256")
    transcript_digest = _digest(report.transcript_sha256, "report transcriptSha256")
    for label, status in (
        ("syntheticStatus", report.synthetic_status),
        ("liveStatus", report.live_status),
        ("overallStatus", report.overall_status),
    ):
        if status not in _LAYER_STATUSES:
            raise DiscordConformanceError(f"report {label} is invalid")
    total = _integer(report.total_cases, "report totalCases", minimum=1)
    passed = _integer(report.passed_cases, "report passedCases")
    failed = _integer(report.failed_cases, "report failedCases")
    not_run = _integer(report.not_run_cases, "report notRunCases")
    if passed + failed + not_run != total:
        raise DiscordConformanceError("report case counts do not add up")
    expected_overall = (
        "fail"
        if "fail" in (report.synthetic_status, report.live_status)
        else "incomplete"
        if "incomplete" in (report.synthetic_status, report.live_status)
        else "pass"
    )
    if report.overall_status != expected_overall:
        raise DiscordConformanceError("report overallStatus does not match layer statuses")
    if report.overall_status == "pass" and (failed or not_run):
        raise DiscordConformanceError("passing report cannot contain failed or not-run cases")
    if report.overall_status == "fail" and failed == 0:
        raise DiscordConformanceError("failed report must contain a failed case")
    if report.overall_status == "incomplete" and (failed != 0 or not_run == 0):
        raise DiscordConformanceError(
            "incomplete report must contain only passed and not-run cases"
        )
    return {
        "discordConformanceReportVersion": report.report_version,
        "failedCases": failed,
        "liveStatus": report.live_status,
        "notRunCases": not_run,
        "overallStatus": report.overall_status,
        "passedCases": passed,
        "suiteSha256": suite_digest,
        "syntheticStatus": report.synthetic_status,
        "totalCases": total,
        "transcriptSha256": transcript_digest,
    }


def render_discord_conformance_report(report: DiscordConformanceReport) -> str:
    return _canonical_json(discord_conformance_report_to_dict(report))


def discord_web_parity_check(
    report: DiscordConformanceReport,
) -> BenchmarkCheckResult:
    """Bridge a validated report to BenchmarkResult's exact parity check."""
    discord_conformance_report_to_dict(report)
    status = {
        "pass": "passed",
        "fail": "failed",
        "incomplete": "not-run",
    }[report.overall_status]
    return BenchmarkCheckResult("discord-web-parity", status)
