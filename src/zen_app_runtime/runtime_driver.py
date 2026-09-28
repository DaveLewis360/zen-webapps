"""Closed Runtime Driver v2 protocol and current Zen App Shell wrapper.

The protocol carries only suite-defined semantic action and event identifiers.
It has no script, selector, screenshot, credential, message-content, URL-history,
or generic diagnostic payload.  Automation authority is an explicitly injected
local test channel; release plans carry no driver/debug authority.  A live
session owns the same transient cgroup-v2 scope used for benchmark evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .app_shell import APP_SHELL_ADAPTER_ID, APP_SHELL_ADAPTER_PROTOCOL_VERSION
from .benchmark_runner import (
    BenchmarkRunnerError,
    CgroupSample,
    RunningSystemdScope,
    SystemdScopePlan,
    build_systemd_scope_plan,
    launch_systemd_scope,
    systemd_scope_argv,
)
from .discord_conformance import (
    DISCORD_CONFORMANCE_TRANSCRIPT_VERSION,
    DiscordConformanceError,
    DiscordConformanceSuite,
    DiscordConformanceTranscript,
    discord_conformance_suite_sha256,
    discord_conformance_suite_to_dict,
    discord_conformance_transcript_from_dict,
)

RUNTIME_DRIVER_DESCRIPTOR_VERSION = 1
RUNTIME_DRIVER_PROTOCOL_VERSION = 2
RUNTIME_DRIVER_SESSION_PLAN_VERSION = 1
RUNTIME_DRIVER_HANDSHAKE_TIMEOUT_MS = 5_000
MAX_RUNTIME_DRIVER_MESSAGE_BYTES = 16 * 1024
ZEN_RUNTIME_DRIVER_ID = "zen-app-shell-runtime-driver"
ZEN_RUNTIME_ENGINE_FAMILY = "gecko"
TEST_AUTOMATION_AUTHORITY = "test-only-semantic-channel"
TEST_AUTOMATION_DECLARATION = "explicit-local-semantic-channel"
RELEASE_DEBUG_AUTHORITY = "none"
RUNTIME_DRIVER_TRANSPORT = "injected-local-request-response"

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EXECUTION_MODES = frozenset({"synthetic-only", "synthetic-and-live"})
_ACTION_STATUSES = frozenset({"passed", "failed"})
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
_REQUIRED_DRIVER_CAPABILITIES = (
    "cgroup-v2-lifecycle-v1",
    "closed-semantic-actions-v1",
    "host-monotonic-event-time-v1",
    "privacy-bounded-transcript-v1",
    "test-only-automation-v1",
)
_REMOTE_DEBUG_ARGUMENTS = frozenset(
    {
        "--allow-remote-origins",
        "--devtools",
        "--jsconsole",
        "--marionette",
        "--remote-debugging-address",
        "--remote-debugging-pipe",
        "--remote-debugging-port",
        "--start-debugger-server",
        "-devtools",
        "-jsdebugger",
        "-marionette",
        "-start-debugger-server",
    }
)
_FORBIDDEN_SECURITY_ARGUMENTS = frozenset(
    {
        "--disable-site-isolation-trials",
        "--disable-web-security",
        "--no-sandbox",
    }
)
_FORBIDDEN_DEBUG_ENVIRONMENT = frozenset(
    {
        "MOZ_MARIONETTE",
        "MOZ_MARIONETTE_PREF_STATE_ACROSS_RESTARTS",
        "MOZ_REMOTE_SETTINGS_DEVTOOLS",
        "REMOTE_DEBUGGING_PORT",
    }
)
_FORBIDDEN_SECURITY_ENVIRONMENT = frozenset(
    {
        "MOZ_DISABLE_CONTENT_SANDBOX",
        "MOZ_DISABLE_GMP_SANDBOX",
        "MOZ_DISABLE_RDD_SANDBOX",
        "MOZ_DISABLE_SOCKET_PROCESS_SANDBOX",
    }
)


class RuntimeDriverError(RuntimeError):
    """A Runtime Driver authority, message, or lifecycle failed closed."""


@dataclass(frozen=True)
class RuntimeDriverDescriptor:
    descriptor_version: int
    protocol_version: int
    driver_id: str
    engine_family: str
    adapter_id: str
    adapter_protocol_version: int
    suite_sha256: str
    fixture_protocol_version: int
    semantic_vocabulary_sha256: str
    action_count: int
    event_count: int
    capabilities: tuple[str, ...]
    transport: str
    test_automation_authority: str
    release_debug_authority: str


@dataclass(frozen=True)
class RuntimeDriverSessionStart:
    protocol_version: int
    driver_id: str
    suite_sha256: str
    fixture_protocol_version: int
    execution_mode: str
    authority: str


@dataclass(frozen=True)
class RuntimeDriverSessionReady:
    protocol_version: int
    driver_id: str
    suite_sha256: str
    fixture_protocol_version: int
    execution_mode: str
    authority: str


@dataclass(frozen=True)
class RuntimeDriverActionRequest:
    protocol_version: int
    sequence: int
    case_id: str
    action_id: str


@dataclass(frozen=True)
class RuntimeDriverActionResult:
    protocol_version: int
    sequence: int
    case_id: str
    action_id: str
    status: str
    event_id: str | None
    failure_code: str | None


RuntimeDriverMessage = (
    RuntimeDriverSessionStart
    | RuntimeDriverSessionReady
    | RuntimeDriverActionRequest
    | RuntimeDriverActionResult
)


class RuntimeDriverSemanticChannel(Protocol):
    """An injected local request/response capability, never a release endpoint."""

    def exchange(self, payload: bytes, *, timeout_ms: int) -> bytes:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class RuntimeDriverSessionPlan:
    plan_version: int
    driver: RuntimeDriverDescriptor
    suite: DiscordConformanceSuite
    scope_plan: SystemdScopePlan
    environment: tuple[tuple[str, str], ...]
    execution_mode: str
    automation_authority: str


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise RuntimeDriverError(f"{label} must be an object with text keys")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeDriverError(f"{label} must be an array")
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
        raise RuntimeDriverError(f"{label} fields: {', '.join(details)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise RuntimeDriverError(f"{label} must be an integer")
    if value < minimum:
        raise RuntimeDriverError(f"{label} must be at least {minimum}")
    return value


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not _IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise RuntimeDriverError(f"{label} must be a bounded lowercase identifier")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise RuntimeDriverError(f"{label} must be a lowercase SHA-256 digest")
    return value


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
        raise RuntimeDriverError(f"document is not canonical JSON data: {error}") from error


def _semantic_vocabulary(suite: DiscordConformanceSuite) -> tuple[str, int, int]:
    discord_conformance_suite_to_dict(suite)
    actions = sorted({step.action_id for case in suite.cases for step in case.steps})
    events = sorted({step.expected_event for case in suite.cases for step in case.steps})
    payload = _canonical_json({"actions": actions, "events": events}).encode("ascii")
    return hashlib.sha256(payload).hexdigest(), len(actions), len(events)


def build_zen_runtime_driver_descriptor(
    suite: DiscordConformanceSuite,
) -> RuntimeDriverDescriptor:
    """Bind the engine-neutral v2 vocabulary to the current Zen App Shell."""
    vocabulary_digest, action_count, event_count = _semantic_vocabulary(suite)
    return RuntimeDriverDescriptor(
        descriptor_version=RUNTIME_DRIVER_DESCRIPTOR_VERSION,
        protocol_version=RUNTIME_DRIVER_PROTOCOL_VERSION,
        driver_id=ZEN_RUNTIME_DRIVER_ID,
        engine_family=ZEN_RUNTIME_ENGINE_FAMILY,
        adapter_id=APP_SHELL_ADAPTER_ID,
        adapter_protocol_version=APP_SHELL_ADAPTER_PROTOCOL_VERSION,
        suite_sha256=discord_conformance_suite_sha256(suite),
        fixture_protocol_version=suite.fixture_protocol_version,
        semantic_vocabulary_sha256=vocabulary_digest,
        action_count=action_count,
        event_count=event_count,
        capabilities=_REQUIRED_DRIVER_CAPABILITIES,
        transport=RUNTIME_DRIVER_TRANSPORT,
        test_automation_authority=TEST_AUTOMATION_DECLARATION,
        release_debug_authority=RELEASE_DEBUG_AUTHORITY,
    )


def _descriptor_to_dict_unchecked(
    descriptor: RuntimeDriverDescriptor,
) -> dict[str, Any]:
    return {
        "adapter": {
            "id": descriptor.adapter_id,
            "protocolVersion": descriptor.adapter_protocol_version,
        },
        "capabilities": list(descriptor.capabilities),
        "driverId": descriptor.driver_id,
        "engineFamily": descriptor.engine_family,
        "releaseDebugAuthority": descriptor.release_debug_authority,
        "runtimeDriverDescriptorVersion": descriptor.descriptor_version,
        "runtimeDriverProtocolVersion": descriptor.protocol_version,
        "semanticVocabulary": {
            "actionCount": descriptor.action_count,
            "eventCount": descriptor.event_count,
            "sha256": descriptor.semantic_vocabulary_sha256,
        },
        "suite": {
            "fixtureProtocolVersion": descriptor.fixture_protocol_version,
            "sha256": descriptor.suite_sha256,
        },
        "testAutomationAuthority": descriptor.test_automation_authority,
        "transport": descriptor.transport,
    }


def runtime_driver_descriptor_from_dict(
    value: Any,
    *,
    suite: DiscordConformanceSuite,
) -> RuntimeDriverDescriptor:
    discord_conformance_suite_to_dict(suite)
    data = _mapping(value, "Runtime Driver descriptor")
    _exact_keys(
        data,
        frozenset(
            {
                "adapter",
                "capabilities",
                "driverId",
                "engineFamily",
                "releaseDebugAuthority",
                "runtimeDriverDescriptorVersion",
                "runtimeDriverProtocolVersion",
                "semanticVocabulary",
                "suite",
                "testAutomationAuthority",
                "transport",
            }
        ),
        "Runtime Driver descriptor",
    )
    descriptor_version = _integer(
        data["runtimeDriverDescriptorVersion"],
        "runtimeDriverDescriptorVersion",
        minimum=1,
    )
    if descriptor_version != RUNTIME_DRIVER_DESCRIPTOR_VERSION:
        raise RuntimeDriverError(
            f"runtimeDriverDescriptorVersion must be {RUNTIME_DRIVER_DESCRIPTOR_VERSION}"
        )
    protocol_version = _integer(
        data["runtimeDriverProtocolVersion"],
        "runtimeDriverProtocolVersion",
        minimum=1,
    )
    if protocol_version != RUNTIME_DRIVER_PROTOCOL_VERSION:
        raise RuntimeDriverError(
            f"runtimeDriverProtocolVersion must be {RUNTIME_DRIVER_PROTOCOL_VERSION}"
        )
    adapter = _mapping(data["adapter"], "Runtime Driver adapter")
    _exact_keys(adapter, frozenset({"id", "protocolVersion"}), "Runtime Driver adapter")
    suite_data = _mapping(data["suite"], "Runtime Driver suite")
    _exact_keys(
        suite_data,
        frozenset({"fixtureProtocolVersion", "sha256"}),
        "Runtime Driver suite",
    )
    vocabulary_data = _mapping(
        data["semanticVocabulary"], "Runtime Driver semantic vocabulary"
    )
    _exact_keys(
        vocabulary_data,
        frozenset({"actionCount", "eventCount", "sha256"}),
        "Runtime Driver semantic vocabulary",
    )
    capabilities = tuple(
        _identifier(item, f"Runtime Driver capability {index}")
        for index, item in enumerate(_array(data["capabilities"], "Runtime Driver capabilities"))
    )
    if capabilities != _REQUIRED_DRIVER_CAPABILITIES:
        raise RuntimeDriverError("Runtime Driver capabilities must exactly match v2")
    suite_digest = _digest(suite_data["sha256"], "Runtime Driver suite sha256")
    expected_suite_digest = discord_conformance_suite_sha256(suite)
    if suite_digest != expected_suite_digest:
        raise RuntimeDriverError("Runtime Driver descriptor is bound to another suite")
    fixture_protocol = _integer(
        suite_data["fixtureProtocolVersion"],
        "Runtime Driver fixtureProtocolVersion",
        minimum=1,
    )
    if fixture_protocol != suite.fixture_protocol_version:
        raise RuntimeDriverError("Runtime Driver fixture protocol does not match suite")
    expected_vocabulary, expected_actions, expected_events = _semantic_vocabulary(suite)
    vocabulary_digest = _digest(
        vocabulary_data["sha256"], "Runtime Driver semantic vocabulary sha256"
    )
    action_count = _integer(
        vocabulary_data["actionCount"], "Runtime Driver actionCount", minimum=1
    )
    event_count = _integer(
        vocabulary_data["eventCount"], "Runtime Driver eventCount", minimum=1
    )
    if (
        vocabulary_digest != expected_vocabulary
        or action_count != expected_actions
        or event_count != expected_events
    ):
        raise RuntimeDriverError("Runtime Driver semantic vocabulary does not match suite")
    transport = data["transport"]
    if transport != RUNTIME_DRIVER_TRANSPORT:
        raise RuntimeDriverError("Runtime Driver transport is not the closed local transport")
    test_authority = data["testAutomationAuthority"]
    if test_authority != TEST_AUTOMATION_DECLARATION:
        raise RuntimeDriverError("Runtime Driver test automation authority is invalid")
    release_authority = data["releaseDebugAuthority"]
    if release_authority != RELEASE_DEBUG_AUTHORITY:
        raise RuntimeDriverError("Runtime Driver release debug authority must be none")
    return RuntimeDriverDescriptor(
        descriptor_version=descriptor_version,
        protocol_version=protocol_version,
        driver_id=_identifier(data["driverId"], "Runtime Driver driverId"),
        engine_family=_identifier(data["engineFamily"], "Runtime Driver engineFamily"),
        adapter_id=_identifier(adapter["id"], "Runtime Driver adapter id"),
        adapter_protocol_version=_integer(
            adapter["protocolVersion"], "Runtime Driver adapter protocolVersion", minimum=1
        ),
        suite_sha256=suite_digest,
        fixture_protocol_version=fixture_protocol,
        semantic_vocabulary_sha256=vocabulary_digest,
        action_count=action_count,
        event_count=event_count,
        capabilities=capabilities,
        transport=transport,
        test_automation_authority=test_authority,
        release_debug_authority=release_authority,
    )


def runtime_driver_descriptor_to_dict(
    descriptor: RuntimeDriverDescriptor,
    *,
    suite: DiscordConformanceSuite,
) -> dict[str, Any]:
    if not isinstance(descriptor, RuntimeDriverDescriptor):
        raise RuntimeDriverError("Runtime Driver descriptor authority was fabricated")
    data = _descriptor_to_dict_unchecked(descriptor)
    parsed = runtime_driver_descriptor_from_dict(data, suite=suite)
    if parsed != descriptor:
        raise RuntimeDriverError("Runtime Driver descriptor changed during validation")
    return data


def render_runtime_driver_descriptor(
    descriptor: RuntimeDriverDescriptor,
    *,
    suite: DiscordConformanceSuite,
) -> str:
    return _canonical_json(runtime_driver_descriptor_to_dict(descriptor, suite=suite))


def runtime_driver_descriptor_sha256(
    descriptor: RuntimeDriverDescriptor,
    *,
    suite: DiscordConformanceSuite,
) -> str:
    return hashlib.sha256(
        render_runtime_driver_descriptor(descriptor, suite=suite).encode("ascii")
    ).hexdigest()


def _session_message_to_dict(
    message: RuntimeDriverSessionStart | RuntimeDriverSessionReady,
    kind: str,
) -> dict[str, Any]:
    return {
        "authority": message.authority,
        "driverId": message.driver_id,
        "executionMode": message.execution_mode,
        "fixtureProtocolVersion": message.fixture_protocol_version,
        "kind": kind,
        "runtimeDriverProtocolVersion": message.protocol_version,
        "suiteSha256": message.suite_sha256,
    }


def _message_to_dict_unchecked(message: RuntimeDriverMessage) -> dict[str, Any]:
    if type(message) is RuntimeDriverSessionStart:
        return _session_message_to_dict(message, "session-start")
    if type(message) is RuntimeDriverSessionReady:
        return _session_message_to_dict(message, "session-ready")
    if type(message) is RuntimeDriverActionRequest:
        return {
            "actionId": message.action_id,
            "caseId": message.case_id,
            "kind": "action",
            "runtimeDriverProtocolVersion": message.protocol_version,
            "sequence": message.sequence,
        }
    if type(message) is RuntimeDriverActionResult:
        return {
            "actionId": message.action_id,
            "caseId": message.case_id,
            "eventId": message.event_id,
            "failureCode": message.failure_code,
            "kind": "action-result",
            "runtimeDriverProtocolVersion": message.protocol_version,
            "sequence": message.sequence,
            "status": message.status,
        }
    raise RuntimeDriverError("unsupported Runtime Driver message model")


def _parse_protocol(value: Any) -> int:
    protocol = _integer(value, "runtimeDriverProtocolVersion", minimum=1)
    if protocol != RUNTIME_DRIVER_PROTOCOL_VERSION:
        raise RuntimeDriverError(
            f"runtimeDriverProtocolVersion must be {RUNTIME_DRIVER_PROTOCOL_VERSION}"
        )
    return protocol


def _parse_session_message(
    data: Mapping[str, Any],
    *,
    ready: bool,
) -> RuntimeDriverSessionStart | RuntimeDriverSessionReady:
    label = "Runtime Driver session-ready" if ready else "Runtime Driver session-start"
    _exact_keys(
        data,
        frozenset(
            {
                "authority",
                "driverId",
                "executionMode",
                "fixtureProtocolVersion",
                "kind",
                "runtimeDriverProtocolVersion",
                "suiteSha256",
            }
        ),
        label,
    )
    mode = data["executionMode"]
    if mode not in _EXECUTION_MODES:
        raise RuntimeDriverError("Runtime Driver executionMode is invalid")
    authority = data["authority"]
    if authority != TEST_AUTOMATION_AUTHORITY:
        raise RuntimeDriverError("Runtime Driver wire authority must be test-only")
    values = (
        _parse_protocol(data["runtimeDriverProtocolVersion"]),
        _identifier(data["driverId"], "Runtime Driver wire driverId"),
        _digest(data["suiteSha256"], "Runtime Driver wire suiteSha256"),
        _integer(
            data["fixtureProtocolVersion"],
            "Runtime Driver wire fixtureProtocolVersion",
            minimum=1,
        ),
        mode,
        authority,
    )
    if ready:
        return RuntimeDriverSessionReady(*values)
    return RuntimeDriverSessionStart(*values)


def _message_from_data(value: Any) -> RuntimeDriverMessage:
    data = _mapping(value, "Runtime Driver message")
    kind = data.get("kind")
    if kind == "session-start":
        return _parse_session_message(data, ready=False)
    if kind == "session-ready":
        return _parse_session_message(data, ready=True)
    if kind == "action":
        _exact_keys(
            data,
            frozenset(
                {
                    "actionId",
                    "caseId",
                    "kind",
                    "runtimeDriverProtocolVersion",
                    "sequence",
                }
            ),
            "Runtime Driver action",
        )
        return RuntimeDriverActionRequest(
            protocol_version=_parse_protocol(data["runtimeDriverProtocolVersion"]),
            sequence=_integer(data["sequence"], "Runtime Driver action sequence", minimum=1),
            case_id=_identifier(data["caseId"], "Runtime Driver action caseId"),
            action_id=_identifier(data["actionId"], "Runtime Driver action actionId"),
        )
    if kind == "action-result":
        _exact_keys(
            data,
            frozenset(
                {
                    "actionId",
                    "caseId",
                    "eventId",
                    "failureCode",
                    "kind",
                    "runtimeDriverProtocolVersion",
                    "sequence",
                    "status",
                }
            ),
            "Runtime Driver action-result",
        )
        status = data["status"]
        if status not in _ACTION_STATUSES:
            raise RuntimeDriverError("Runtime Driver action-result status is invalid")
        raw_event = data["eventId"]
        raw_failure = data["failureCode"]
        if status == "passed":
            if raw_event is None:
                raise RuntimeDriverError("passed Runtime Driver result requires an eventId")
            event_id = _identifier(raw_event, "Runtime Driver action-result eventId")
            if raw_failure is not None:
                raise RuntimeDriverError("passed Runtime Driver result cannot have failureCode")
            failure_code = None
        else:
            if raw_event is not None:
                raise RuntimeDriverError("failed Runtime Driver result cannot have eventId")
            if not isinstance(raw_failure, str) or raw_failure not in _FAILURE_CODES:
                raise RuntimeDriverError(
                    "failed Runtime Driver result requires a closed failureCode"
                )
            event_id = None
            failure_code = raw_failure
        return RuntimeDriverActionResult(
            protocol_version=_parse_protocol(data["runtimeDriverProtocolVersion"]),
            sequence=_integer(
                data["sequence"], "Runtime Driver action-result sequence", minimum=1
            ),
            case_id=_identifier(data["caseId"], "Runtime Driver action-result caseId"),
            action_id=_identifier(data["actionId"], "Runtime Driver action-result actionId"),
            status=status,
            event_id=event_id,
            failure_code=failure_code,
        )
    raise RuntimeDriverError("Runtime Driver message kind is invalid")


def runtime_driver_message_to_dict(message: RuntimeDriverMessage) -> dict[str, Any]:
    data = _message_to_dict_unchecked(message)
    parsed = _message_from_data(data)
    if parsed != message:
        raise RuntimeDriverError("Runtime Driver message changed during validation")
    return data


def encode_runtime_driver_message(message: RuntimeDriverMessage) -> bytes:
    payload = _canonical_json(runtime_driver_message_to_dict(message)).encode("ascii")
    if len(payload) > MAX_RUNTIME_DRIVER_MESSAGE_BYTES:
        raise RuntimeDriverError("Runtime Driver message exceeds the size limit")
    return payload


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeDriverError(f"Runtime Driver message has duplicate field {key}")
        result[key] = value
    return result


def decode_runtime_driver_message(payload: bytes) -> RuntimeDriverMessage:
    if not isinstance(payload, bytes):
        raise RuntimeDriverError("Runtime Driver wire payload must be bytes")
    if not payload or len(payload) > MAX_RUNTIME_DRIVER_MESSAGE_BYTES:
        raise RuntimeDriverError("Runtime Driver wire payload size is invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(text, object_pairs_hook=_duplicate_free_object)
    except RuntimeDriverError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeDriverError(f"Runtime Driver wire payload is invalid JSON: {error}") from error
    message = _message_from_data(value)
    canonical = _canonical_json(_message_to_dict_unchecked(message)).encode("ascii")
    if payload != canonical:
        raise RuntimeDriverError("Runtime Driver wire payload must be exact canonical JSON")
    return message


def _validated_environment(environment: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    if not isinstance(environment, Mapping):
        raise RuntimeDriverError("Runtime Driver environment must be an explicit mapping")
    result: list[tuple[str, str]] = []
    for name, value in environment.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise RuntimeDriverError("Runtime Driver environment contains an invalid name")
        if not isinstance(value, str) or "\0" in value:
            raise RuntimeDriverError(
                f"Runtime Driver environment value for {name} must be NUL-free text"
            )
        if name in _FORBIDDEN_DEBUG_ENVIRONMENT:
            raise RuntimeDriverError("Runtime Driver environment requests debug authority")
        if name in _FORBIDDEN_SECURITY_ENVIRONMENT:
            raise RuntimeDriverError("Runtime Driver environment weakens sandbox authority")
        result.append((name, value))
    return tuple(sorted(result))


def _validate_zen_supervisor_command(command: Sequence[str]) -> None:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
        raise RuntimeDriverError("Zen Runtime Driver command must be a token sequence")
    tokens = tuple(command)
    if tokens.count("--bundled-app-shell") != 1:
        raise RuntimeDriverError("Zen Runtime Driver requires the bundled App Shell")
    if tokens.count("--yes") != 1:
        raise RuntimeDriverError("Zen Runtime Driver requires explicit launch confirmation")
    if "--app-shell-capability" in tokens:
        raise RuntimeDriverError("Zen Runtime Driver cannot use an external App Shell claim")
    for token in tokens:
        if not isinstance(token, str):
            raise RuntimeDriverError("Zen Runtime Driver command tokens must be text")
        option = token.lower().split("=", 1)[0]
        if option in _REMOTE_DEBUG_ARGUMENTS:
            raise RuntimeDriverError("Zen Runtime Driver command requests remote debugging")
        if option in _FORBIDDEN_SECURITY_ARGUMENTS:
            raise RuntimeDriverError("Zen Runtime Driver command weakens browser security")


def build_zen_runtime_driver_plan(
    suite: DiscordConformanceSuite,
    command: Sequence[str],
    *,
    scope_name: str,
    working_directory: os.PathLike[str] | str,
    environment: Mapping[str, str],
    execution_mode: str = "synthetic-only",
    test_automation: bool = False,
    cgroup_root: os.PathLike[str] | str = "/sys/fs/cgroup",
    proc_root: os.PathLike[str] | str = "/proc",
    systemd_run_path: os.PathLike[str] | str = "/usr/bin/systemd-run",
    systemctl_path: os.PathLike[str] | str = "/usr/bin/systemctl",
) -> RuntimeDriverSessionPlan:
    """Wrap the existing confirmed bundled-App-Shell supervisor in one scope."""
    discord_conformance_suite_to_dict(suite)
    if execution_mode not in _EXECUTION_MODES:
        raise RuntimeDriverError("Runtime Driver execution mode is invalid")
    if type(test_automation) is not bool:
        raise RuntimeDriverError("test_automation must be boolean")
    _validate_zen_supervisor_command(command)
    explicit_environment = _validated_environment(environment)
    try:
        scope_plan = build_systemd_scope_plan(
            command,
            scope_name=scope_name,
            working_directory=working_directory,
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            systemd_run_path=systemd_run_path,
            systemctl_path=systemctl_path,
        )
    except BenchmarkRunnerError as error:
        raise RuntimeDriverError(str(error)) from error
    return RuntimeDriverSessionPlan(
        plan_version=RUNTIME_DRIVER_SESSION_PLAN_VERSION,
        driver=build_zen_runtime_driver_descriptor(suite),
        suite=suite,
        scope_plan=scope_plan,
        environment=explicit_environment,
        execution_mode=execution_mode,
        automation_authority=(
            TEST_AUTOMATION_AUTHORITY if test_automation else RELEASE_DEBUG_AUTHORITY
        ),
    )


def _validate_plan(plan: RuntimeDriverSessionPlan) -> None:
    if not isinstance(plan, RuntimeDriverSessionPlan):
        raise RuntimeDriverError("Runtime Driver session plan authority was fabricated")
    if plan.plan_version != RUNTIME_DRIVER_SESSION_PLAN_VERSION:
        raise RuntimeDriverError(
            f"Runtime Driver session plan version must be {RUNTIME_DRIVER_SESSION_PLAN_VERSION}"
        )
    runtime_driver_descriptor_to_dict(plan.driver, suite=plan.suite)
    if plan.driver != build_zen_runtime_driver_descriptor(plan.suite):
        raise RuntimeDriverError("Runtime Driver plan does not use the current Zen adapter")
    if plan.execution_mode not in _EXECUTION_MODES:
        raise RuntimeDriverError("Runtime Driver plan execution mode is invalid")
    if plan.automation_authority not in (
        TEST_AUTOMATION_AUTHORITY,
        RELEASE_DEBUG_AUTHORITY,
    ):
        raise RuntimeDriverError("Runtime Driver plan automation authority is invalid")
    _validate_zen_supervisor_command(plan.scope_plan.command)
    try:
        systemd_scope_argv(plan.scope_plan)
    except BenchmarkRunnerError as error:
        raise RuntimeDriverError(str(error)) from error
    if _validated_environment(dict(plan.environment)) != plan.environment:
        raise RuntimeDriverError("Runtime Driver plan environment is not canonical")


class ZenRuntimeDriverAdapter:
    """Validate a test channel as the current Zen semantic adapter wrapper."""

    def __init__(
        self,
        plan: RuntimeDriverSessionPlan,
        channel: RuntimeDriverSemanticChannel,
    ) -> None:
        self.plan = plan
        self._channel = channel
        self._sequence = 0
        self._closed = False

    @classmethod
    def open(
        cls,
        plan: RuntimeDriverSessionPlan,
        channel: RuntimeDriverSemanticChannel,
    ) -> "ZenRuntimeDriverAdapter":
        start = RuntimeDriverSessionStart(
            protocol_version=plan.driver.protocol_version,
            driver_id=plan.driver.driver_id,
            suite_sha256=plan.driver.suite_sha256,
            fixture_protocol_version=plan.driver.fixture_protocol_version,
            execution_mode=plan.execution_mode,
            authority=TEST_AUTOMATION_AUTHORITY,
        )
        try:
            payload = channel.exchange(
                encode_runtime_driver_message(start),
                timeout_ms=RUNTIME_DRIVER_HANDSHAKE_TIMEOUT_MS,
            )
        except TimeoutError as error:
            raise RuntimeDriverError("Runtime Driver test handshake timed out") from error
        except RuntimeDriverError:
            raise
        except Exception as error:
            raise RuntimeDriverError("Runtime Driver test handshake failed") from error
        ready = decode_runtime_driver_message(payload)
        expected = RuntimeDriverSessionReady(
            protocol_version=start.protocol_version,
            driver_id=start.driver_id,
            suite_sha256=start.suite_sha256,
            fixture_protocol_version=start.fixture_protocol_version,
            execution_mode=start.execution_mode,
            authority=start.authority,
        )
        if ready != expected:
            raise RuntimeDriverError("Runtime Driver session-ready does not match its authority")
        return cls(plan, channel)

    def execute(
        self,
        case_id: str,
        action_id: str,
        *,
        timeout_ms: int,
    ) -> RuntimeDriverActionResult:
        if self._closed:
            raise RuntimeDriverError("Runtime Driver adapter is closed")
        self._sequence += 1
        request = RuntimeDriverActionRequest(
            protocol_version=self.plan.driver.protocol_version,
            sequence=self._sequence,
            case_id=case_id,
            action_id=action_id,
        )
        payload = self._channel.exchange(
            encode_runtime_driver_message(request),
            timeout_ms=timeout_ms,
        )
        result = decode_runtime_driver_message(payload)
        if not isinstance(result, RuntimeDriverActionResult):
            raise RuntimeDriverError("Runtime Driver action did not return an action-result")
        if (
            result.protocol_version != request.protocol_version
            or result.sequence != request.sequence
            or result.case_id != request.case_id
            or result.action_id != request.action_id
        ):
            raise RuntimeDriverError("Runtime Driver action-result was rebound or reordered")
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._channel.close()


def _utc_value(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeDriverError(f"{label} must be a timezone-aware datetime")
    rendered = value.astimezone(timezone.utc)
    if rendered.utcoffset() != timedelta(0):  # pragma: no cover - astimezone contract.
        raise RuntimeDriverError(f"{label} must resolve to UTC")
    return rendered


def _floor_utc_second(value: datetime, label: str) -> datetime:
    return _utc_value(value, label).replace(microsecond=0)


def _ceil_utc_second(value: datetime, label: str) -> datetime:
    rendered = _utc_value(value, label)
    if rendered.microsecond:
        rendered = rendered.replace(microsecond=0) + timedelta(seconds=1)
    return rendered


def _render_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope_cleanup(scope: Any) -> None:
    if scope.poll() is None:
        scope.terminate()
    else:
        scope.close()


class RuntimeDriverSession:
    """Own one semantic adapter and its complete measured cgroup lifecycle."""

    def __init__(
        self,
        plan: RuntimeDriverSessionPlan,
        scope: RunningSystemdScope,
        adapter: ZenRuntimeDriverAdapter,
        *,
        started_at: datetime,
        started_monotonic_ns: int,
        utc_now: Callable[[], datetime],
        monotonic_ns: Callable[[], int],
    ) -> None:
        self.plan = plan
        self._scope = scope
        self._adapter = adapter
        self._started_at = started_at
        self._started_monotonic_ns = started_monotonic_ns
        self._utc_now = utc_now
        self._monotonic_ns = monotonic_ns
        self._closed = False
        self._ran = False

    def sample(self, *, max_attempts: int = 3) -> CgroupSample:
        if self._closed:
            raise RuntimeDriverError("Runtime Driver session is closed")
        try:
            return self._scope.sample(max_attempts=max_attempts)
        except BenchmarkRunnerError as error:
            raise RuntimeDriverError(str(error)) from error

    def _event_elapsed_ms(self, observed_ns: int, previous_ms: int) -> int:
        if observed_ns < self._started_monotonic_ns:
            raise RuntimeDriverError("Runtime Driver monotonic clock regressed")
        measured = (observed_ns - self._started_monotonic_ns) // 1_000_000
        return max(previous_ms + 1, measured)

    def run_conformance(self) -> DiscordConformanceTranscript:
        if self._closed:
            raise RuntimeDriverError("Runtime Driver session is closed")
        if self._ran:
            raise RuntimeDriverError("Runtime Driver session already produced a transcript")
        self._ran = True
        case_documents: list[dict[str, Any]] = []
        stop = False
        previous_elapsed_ms = -1
        latest_monotonic_ns = self._started_monotonic_ns
        try:
            for case in self.plan.suite.cases:
                if stop or (
                    self.plan.execution_mode == "synthetic-only"
                    and case.layer == "live-discord"
                ):
                    case_documents.append(
                        {
                            "caseId": case.case_id,
                            "events": [],
                            "failureCode": None,
                            "status": "not-run",
                        }
                    )
                    continue
                events: list[dict[str, Any]] = []
                failure_code: str | None = None
                for step in case.steps:
                    if self._scope.poll() is not None:
                        failure_code = "driver-error"
                        break
                    before_ns = self._monotonic_ns()
                    try:
                        result = self._adapter.execute(
                            case.case_id,
                            step.action_id,
                            timeout_ms=step.timeout_ms,
                        )
                    except TimeoutError:
                        failure_code = "timeout"
                        latest_monotonic_ns = self._monotonic_ns()
                        break
                    except Exception:
                        failure_code = "driver-error"
                        latest_monotonic_ns = self._monotonic_ns()
                        break
                    after_ns = self._monotonic_ns()
                    latest_monotonic_ns = after_ns
                    if after_ns < before_ns:
                        failure_code = "driver-error"
                        break
                    action_elapsed_ms = (after_ns - before_ns + 999_999) // 1_000_000
                    if action_elapsed_ms > step.timeout_ms:
                        failure_code = "timeout"
                        break
                    if self._scope.poll() is not None:
                        failure_code = "driver-error"
                        break
                    if result.status == "failed":
                        failure_code = result.failure_code
                        break
                    if result.event_id != step.expected_event:
                        failure_code = "target-changed"
                        break
                    previous_elapsed_ms = self._event_elapsed_ms(
                        after_ns, previous_elapsed_ms
                    )
                    events.append(
                        {
                            "elapsedMs": previous_elapsed_ms,
                            "eventId": result.event_id,
                        }
                    )
                if failure_code is None:
                    case_documents.append(
                        {
                            "caseId": case.case_id,
                            "events": events,
                            "failureCode": None,
                            "status": "passed",
                        }
                    )
                else:
                    case_documents.append(
                        {
                            "caseId": case.case_id,
                            "events": events,
                            "failureCode": failure_code,
                            "status": "failed",
                        }
                    )
                    stop = True
            ended_monotonic_ns = self._monotonic_ns()
            if ended_monotonic_ns < latest_monotonic_ns:
                raise RuntimeDriverError("Runtime Driver monotonic clock regressed")
            elapsed_ns = ended_monotonic_ns - self._started_monotonic_ns
            required_seconds = (elapsed_ns + 999_999_999) // 1_000_000_000
            completed_at = max(
                _ceil_utc_second(self._utc_now(), "Runtime Driver completion time"),
                self._started_at + timedelta(seconds=required_seconds),
            )
            document = {
                "cases": case_documents,
                "completedAtUtc": _render_utc(completed_at),
                "discordConformanceTranscriptVersion": DISCORD_CONFORMANCE_TRANSCRIPT_VERSION,
                "driver": {
                    "id": self.plan.driver.driver_id,
                    "protocolVersion": self.plan.driver.protocol_version,
                },
                "fixtureProtocolVersion": self.plan.driver.fixture_protocol_version,
                "liveOrigin": self.plan.suite.live_origin,
                "mode": "synthetic-and-live",
                "startedAtUtc": _render_utc(self._started_at),
                "suiteSha256": self.plan.driver.suite_sha256,
            }
            try:
                return discord_conformance_transcript_from_dict(
                    document,
                    suite=self.plan.suite,
                )
            except DiscordConformanceError as error:
                raise RuntimeDriverError(
                    f"Runtime Driver produced an invalid closed transcript: {error}"
                ) from error
        except BaseException:
            try:
                self.close()
            except RuntimeDriverError:
                pass
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        try:
            self._adapter.close()
        except Exception as error:
            errors.append(error)
        try:
            _scope_cleanup(self._scope)
        except Exception as error:
            errors.append(error)
        if errors:
            raise RuntimeDriverError("Runtime Driver session cleanup failed") from errors[0]

    def __enter__(self) -> "RuntimeDriverSession":
        if self._closed:
            raise RuntimeDriverError("Runtime Driver session is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def start_runtime_driver_session(
    plan: RuntimeDriverSessionPlan,
    channel: RuntimeDriverSemanticChannel,
    *,
    launch_scope: Callable[
        [SystemdScopePlan, Mapping[str, str]], RunningSystemdScope
    ] = launch_systemd_scope,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
) -> RuntimeDriverSession:
    """Launch a measured Zen test session; release plans cannot enter this API."""
    _validate_plan(plan)
    if plan.automation_authority != TEST_AUTOMATION_AUTHORITY:
        raise RuntimeDriverError(
            "Runtime Driver automation is test-only; release authority is closed"
        )
    scope: Any | None = None
    adapter: ZenRuntimeDriverAdapter | None = None
    try:
        try:
            scope = launch_scope(plan.scope_plan, dict(plan.environment))
        except BenchmarkRunnerError as error:
            raise RuntimeDriverError(str(error)) from error
        if scope.poll() is not None:
            raise RuntimeDriverError("Runtime Driver scope exited before adapter handshake")
        adapter = ZenRuntimeDriverAdapter.open(plan, channel)
        if scope.poll() is not None:
            raise RuntimeDriverError("Runtime Driver scope exited during adapter handshake")
        started_at = _floor_utc_second(
            utc_now(), "Runtime Driver start time"
        )
        started_monotonic_ns = monotonic_ns()
        return RuntimeDriverSession(
            plan,
            scope,
            adapter,
            started_at=started_at,
            started_monotonic_ns=started_monotonic_ns,
            utc_now=utc_now,
            monotonic_ns=monotonic_ns,
        )
    except BaseException:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
        else:
            try:
                channel.close()
            except Exception:
                pass
        if scope is not None:
            try:
                _scope_cleanup(scope)
            except Exception:
                pass
        raise
