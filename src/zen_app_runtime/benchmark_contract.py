"""Pure, versioned benchmark and acceptance contracts.

The contract deliberately performs no measurement, process launch, filesystem
access, or network access.  It defines the evidence a benchmark runner must
produce and evaluates that evidence conservatively against immutable v1 gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from .runtime import (
    RuntimeIdentity,
    RuntimeSafetyError,
    runtime_identity_from_dict,
    runtime_identity_to_dict,
)

BENCHMARK_SCENARIO_VERSION = 1
BENCHMARK_RESULT_VERSION = 1
BENCHMARK_ENVIRONMENT_VERSION = 1
BENCHMARK_ACCEPTANCE_VERSION = 1
DEFAULT_SCENARIO_ID = "discord-app-shell-acceptance-v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PHASE_STATUSES = frozenset({"measured", "not-run", "failed"})
_CHECK_STATUSES = frozenset({"passed", "not-run", "failed"})
_GATE_STATUSES = frozenset({"pass", "not-measured", "fail"})

_REQUIRED_CHECKS = (
    "discord-web-parity",
    "general-linux-wayland",
    "fission-enabled",
    "content-sandbox-enforced",
    "no-new-privileges",
    "native-wayland",
    "alpha-surface",
    "permission-mediated",
    "screen-share-transient-activation",
    "release-debug-authority-disabled",
)

# phase id, cache state, profile state, repetitions, duration, measurement
# offset, and (metric id, unit, aggregation) tuples.  Fixed-point integer units
# avoid non-canonical float/NaN behavior in signed benchmark evidence.
_PHASE_DEFINITIONS = (
    (
        "cold-start",
        "cold",
        "existing-authenticated",
        10,
        30,
        0,
        (("usable-time", "milliseconds", "p95"),),
    ),
    (
        "warm-start",
        "warm",
        "existing-authenticated",
        10,
        30,
        0,
        (("usable-time", "milliseconds", "p95"),),
    ),
    (
        "onboarding",
        "cold",
        "fresh",
        5,
        60,
        30,
        (("summed-pss", "kibibytes", "median"),),
    ),
    (
        "active",
        "retained",
        "existing-authenticated",
        5,
        600,
        600,
        (
            ("summed-pss", "kibibytes", "median"),
            ("cpu", "basis-points", "p95"),
        ),
    ),
    (
        "idle",
        "retained",
        "existing-authenticated",
        5,
        600,
        300,
        (
            ("summed-pss", "kibibytes", "median"),
            ("cpu", "basis-points", "p95"),
        ),
    ),
    (
        "voice",
        "retained",
        "existing-authenticated",
        3,
        600,
        300,
        (
            ("summed-pss", "kibibytes", "median"),
            ("cpu", "basis-points", "p95"),
        ),
    ),
    (
        "video",
        "retained",
        "existing-authenticated",
        3,
        600,
        300,
        (
            ("summed-pss", "kibibytes", "median"),
            ("cpu", "basis-points", "p95"),
        ),
    ),
    (
        "screen-share",
        "retained",
        "existing-authenticated",
        3,
        600,
        300,
        (
            ("summed-pss", "kibibytes", "median"),
            ("cpu", "basis-points", "p95"),
        ),
    ),
    (
        "soak",
        "retained",
        "existing-authenticated",
        1,
        24 * 60 * 60,
        0,
        (
            ("summed-pss-start", "kibibytes", "median"),
            ("summed-pss-end", "kibibytes", "median"),
        ),
    ),
)


class BenchmarkContractError(ValueError):
    """A benchmark authority or evidence document violated its closed schema."""


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str
    unit: str
    aggregation: str


@dataclass(frozen=True)
class BenchmarkPhase:
    phase_id: str
    cache_state: str
    profile_state: str
    repetitions: int
    duration_seconds: int
    measurement_offset_seconds: int
    required_metrics: tuple[MetricSpec, ...]


@dataclass(frozen=True)
class HardGate:
    gate_id: str
    source_kind: str
    phase_id: str | None
    metric_id: str | None
    baseline_metric_id: str | None
    check_id: str | None
    statistic: str | None
    comparison: str
    unit: str | None
    limit: int | str


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_version: int
    scenario_id: str
    phases: tuple[BenchmarkPhase, ...]
    required_checks: tuple[str, ...]
    hard_gates: tuple[HardGate, ...]


@dataclass(frozen=True)
class HardwareIdentity:
    architecture: str
    cpu_model: str
    logical_cpu_count: int
    physical_memory_bytes: int


@dataclass(frozen=True)
class KernelIdentity:
    release: str


@dataclass(frozen=True)
class OperatingSystemIdentity:
    os_id: str
    version_id: str


@dataclass(frozen=True)
class CompositorIdentity:
    name: str
    version: str


@dataclass(frozen=True)
class PortalIdentity:
    desktop: str
    implementation: str
    version: str


@dataclass(frozen=True)
class SessionIdentity:
    session_type: str
    compositor: CompositorIdentity
    portal: PortalIdentity


@dataclass(frozen=True)
class BenchmarkRuntimeIdentity:
    engine: str
    engine_version: str
    driver_id: str
    driver_protocol_version: int
    app_shell_version: str
    app_shell_build_id: str
    adapter_tree_sha256: str
    executable: RuntimeIdentity


@dataclass(frozen=True)
class BenchmarkEnvironment:
    environment_version: int
    hardware: HardwareIdentity
    kernel: KernelIdentity
    operating_system: OperatingSystemIdentity
    session: SessionIdentity
    runtime: BenchmarkRuntimeIdentity


@dataclass(frozen=True)
class ConfidenceInterval:
    lower: int
    upper: int


@dataclass(frozen=True)
class MetricObservation:
    metric_id: str
    value: int
    sample_count: int
    confidence95: ConfidenceInterval


@dataclass(frozen=True)
class BenchmarkPhaseResult:
    phase_id: str
    status: str
    observations: tuple[MetricObservation, ...]


@dataclass(frozen=True)
class BenchmarkCheckResult:
    check_id: str
    status: str


@dataclass(frozen=True)
class BenchmarkResult:
    result_version: int
    scenario_sha256: str
    started_at_utc: str
    completed_at_utc: str
    environment: BenchmarkEnvironment
    phases: tuple[BenchmarkPhaseResult, ...]
    checks: tuple[BenchmarkCheckResult, ...]


@dataclass(frozen=True)
class HardGateEvaluation:
    gate_id: str
    status: str
    observed: Any
    threshold: Mapping[str, Any]


@dataclass(frozen=True)
class BenchmarkAcceptance:
    acceptance_version: int
    scenario_sha256: str
    result_sha256: str
    overall_status: str
    gates: tuple[HardGateEvaluation, ...]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise BenchmarkContractError(f"{label} must be an object with text keys")
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
        raise BenchmarkContractError(f"{label} fields: {', '.join(details)}")


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise BenchmarkContractError(f"{label} must be an array")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise BenchmarkContractError(f"{label} must be an integer")
    if value < minimum:
        raise BenchmarkContractError(f"{label} must be at least {minimum}")
    return value


def _text(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise BenchmarkContractError(f"{label} must be non-empty bounded text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BenchmarkContractError(f"{label} cannot contain control characters")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, maximum=128)
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise BenchmarkContractError(f"{label} must be a lowercase identifier")
    return text


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise BenchmarkContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str) or not _TIMESTAMP_PATTERN.fullmatch(value):
        raise BenchmarkContractError(f"{label} must be second-precision UTC RFC3339 text")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise BenchmarkContractError(f"{label} is not a valid UTC timestamp") from error
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
        raise BenchmarkContractError(f"document is not canonical JSON data: {error}") from error


def _metric_specs(raw: tuple[tuple[str, str, str], ...]) -> tuple[MetricSpec, ...]:
    return tuple(MetricSpec(metric_id, unit, aggregation) for metric_id, unit, aggregation in raw)


def _default_phases() -> tuple[BenchmarkPhase, ...]:
    return tuple(
        BenchmarkPhase(
            phase_id=phase_id,
            cache_state=cache_state,
            profile_state=profile_state,
            repetitions=repetitions,
            duration_seconds=duration_seconds,
            measurement_offset_seconds=measurement_offset_seconds,
            required_metrics=_metric_specs(metrics),
        )
        for (
            phase_id,
            cache_state,
            profile_state,
            repetitions,
            duration_seconds,
            measurement_offset_seconds,
            metrics,
        ) in _PHASE_DEFINITIONS
    )


def _metric_gate(
    gate_id: str,
    phase_id: str,
    metric_id: str,
    comparison: str,
    unit: str,
    limit: int,
) -> HardGate:
    return HardGate(
        gate_id=gate_id,
        source_kind="metric",
        phase_id=phase_id,
        metric_id=metric_id,
        baseline_metric_id=None,
        check_id=None,
        statistic="upper95",
        comparison=comparison,
        unit=unit,
        limit=limit,
    )


def _default_gates() -> tuple[HardGate, ...]:
    quantitative = (
        _metric_gate(
            "cold-start-usable-time",
            "cold-start",
            "usable-time",
            "less-than-or-equal",
            "milliseconds",
            3000,
        ),
        _metric_gate(
            "warm-start-usable-time",
            "warm-start",
            "usable-time",
            "less-than-or-equal",
            "milliseconds",
            3000,
        ),
        _metric_gate(
            "onboarding-pss",
            "onboarding",
            "summed-pss",
            "less-than-or-equal",
            "kibibytes",
            120 * 1024,
        ),
        _metric_gate(
            "active-pss",
            "active",
            "summed-pss",
            "less-than-or-equal",
            "kibibytes",
            350 * 1024,
        ),
        _metric_gate(
            "idle-cpu",
            "idle",
            "cpu",
            "less-than",
            "basis-points",
            100,
        ),
        HardGate(
            gate_id="soak-pss-growth",
            source_kind="ratio",
            phase_id="soak",
            metric_id="summed-pss-end",
            baseline_metric_id="summed-pss-start",
            check_id=None,
            statistic="worst-case-95",
            comparison="less-than",
            unit="percent",
            limit=10,
        ),
    )
    checks = tuple(
        HardGate(
            gate_id=f"check-{check_id}",
            source_kind="check",
            phase_id=None,
            metric_id=None,
            baseline_metric_id=None,
            check_id=check_id,
            statistic=None,
            comparison="equals",
            unit=None,
            limit="passed",
        )
        for check_id in _REQUIRED_CHECKS
    )
    return (*quantitative, *checks)


def build_benchmark_scenario() -> BenchmarkScenario:
    """Return the immutable Discord App Shell acceptance scenario v1."""
    return BenchmarkScenario(
        scenario_version=BENCHMARK_SCENARIO_VERSION,
        scenario_id=DEFAULT_SCENARIO_ID,
        phases=_default_phases(),
        required_checks=_REQUIRED_CHECKS,
        hard_gates=_default_gates(),
    )


def _metric_spec_to_dict(metric: MetricSpec) -> dict[str, Any]:
    return {
        "aggregation": metric.aggregation,
        "metricId": metric.metric_id,
        "unit": metric.unit,
    }


def _phase_to_dict(phase: BenchmarkPhase) -> dict[str, Any]:
    return {
        "cacheState": phase.cache_state,
        "durationSeconds": phase.duration_seconds,
        "measurementOffsetSeconds": phase.measurement_offset_seconds,
        "phaseId": phase.phase_id,
        "profileState": phase.profile_state,
        "repetitions": phase.repetitions,
        "requiredMetrics": [_metric_spec_to_dict(metric) for metric in phase.required_metrics],
    }


def _gate_source_to_dict(gate: HardGate) -> dict[str, Any]:
    if gate.source_kind == "metric":
        return {
            "metricId": gate.metric_id,
            "phaseId": gate.phase_id,
            "statistic": gate.statistic,
        }
    if gate.source_kind == "ratio":
        return {
            "baselineMetricId": gate.baseline_metric_id,
            "metricId": gate.metric_id,
            "phaseId": gate.phase_id,
            "statistic": gate.statistic,
        }
    if gate.source_kind == "check":
        return {"checkId": gate.check_id}
    raise BenchmarkContractError("hard gate source kind is invalid")


def _gate_threshold_to_dict(gate: HardGate) -> dict[str, Any]:
    if gate.source_kind == "check":
        return {"comparison": gate.comparison, "value": gate.limit}
    return {
        "comparison": gate.comparison,
        "unit": gate.unit,
        "value": gate.limit,
    }


def _gate_to_dict(gate: HardGate) -> dict[str, Any]:
    return {
        "gateId": gate.gate_id,
        "source": _gate_source_to_dict(gate),
        "threshold": _gate_threshold_to_dict(gate),
    }


def _scenario_to_dict_unchecked(scenario: BenchmarkScenario) -> dict[str, Any]:
    return {
        "benchmarkScenarioVersion": scenario.scenario_version,
        "hardGates": [_gate_to_dict(gate) for gate in scenario.hard_gates],
        "phases": [_phase_to_dict(phase) for phase in scenario.phases],
        "requiredChecks": list(scenario.required_checks),
        "scenarioId": scenario.scenario_id,
    }


def _parse_metric_spec(value: Any, label: str) -> MetricSpec:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"aggregation", "metricId", "unit"}), label)
    return MetricSpec(
        metric_id=_identifier(data["metricId"], f"{label} metricId"),
        unit=_identifier(data["unit"], f"{label} unit"),
        aggregation=_identifier(data["aggregation"], f"{label} aggregation"),
    )


def _parse_phase(value: Any, label: str) -> BenchmarkPhase:
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset(
            {
                "cacheState",
                "durationSeconds",
                "measurementOffsetSeconds",
                "phaseId",
                "profileState",
                "repetitions",
                "requiredMetrics",
            }
        ),
        label,
    )
    metrics = tuple(
        _parse_metric_spec(item, f"{label} required metric {index}")
        for index, item in enumerate(_array(data["requiredMetrics"], f"{label} requiredMetrics"))
    )
    if not metrics:
        raise BenchmarkContractError(f"{label} requiredMetrics cannot be empty")
    return BenchmarkPhase(
        phase_id=_identifier(data["phaseId"], f"{label} phaseId"),
        cache_state=_identifier(data["cacheState"], f"{label} cacheState"),
        profile_state=_identifier(data["profileState"], f"{label} profileState"),
        repetitions=_integer(data["repetitions"], f"{label} repetitions", minimum=1),
        duration_seconds=_integer(data["durationSeconds"], f"{label} durationSeconds"),
        measurement_offset_seconds=_integer(
            data["measurementOffsetSeconds"],
            f"{label} measurementOffsetSeconds",
        ),
        required_metrics=metrics,
    )


def _parse_gate(value: Any, label: str) -> HardGate:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"gateId", "source", "threshold"}), label)
    gate_id = _identifier(data["gateId"], f"{label} gateId")
    source = _mapping(data["source"], f"{label} source")
    threshold = _mapping(data["threshold"], f"{label} threshold")

    if frozenset(source) == frozenset({"checkId"}):
        _exact_keys(threshold, frozenset({"comparison", "value"}), f"{label} threshold")
        comparison = _identifier(threshold["comparison"], f"{label} comparison")
        limit = _identifier(threshold["value"], f"{label} threshold value")
        return HardGate(
            gate_id,
            "check",
            None,
            None,
            None,
            _identifier(source["checkId"], f"{label} checkId"),
            None,
            comparison,
            None,
            limit,
        )

    metric_keys = frozenset({"metricId", "phaseId", "statistic"})
    ratio_keys = frozenset(
        {"baselineMetricId", "metricId", "phaseId", "statistic"}
    )
    if frozenset(source) not in (metric_keys, ratio_keys):
        expected = sorted(metric_keys)
        raise BenchmarkContractError(
            f"{label} source fields are unknown; expected metric, ratio, or check source {expected}"
        )
    _exact_keys(
        threshold,
        frozenset({"comparison", "unit", "value"}),
        f"{label} threshold",
    )
    source_kind = "ratio" if "baselineMetricId" in source else "metric"
    return HardGate(
        gate_id=gate_id,
        source_kind=source_kind,
        phase_id=_identifier(source["phaseId"], f"{label} phaseId"),
        metric_id=_identifier(source["metricId"], f"{label} metricId"),
        baseline_metric_id=(
            _identifier(source["baselineMetricId"], f"{label} baselineMetricId")
            if source_kind == "ratio"
            else None
        ),
        check_id=None,
        statistic=_identifier(source["statistic"], f"{label} statistic"),
        comparison=_identifier(threshold["comparison"], f"{label} comparison"),
        unit=_identifier(threshold["unit"], f"{label} unit"),
        limit=_integer(threshold["value"], f"{label} threshold value"),
    )


def benchmark_scenario_from_dict(value: Any) -> BenchmarkScenario:
    data = _mapping(value, "benchmark scenario")
    _exact_keys(
        data,
        frozenset(
            {
                "benchmarkScenarioVersion",
                "hardGates",
                "phases",
                "requiredChecks",
                "scenarioId",
            }
        ),
        "benchmark scenario",
    )
    version = _integer(
        data["benchmarkScenarioVersion"],
        "benchmarkScenarioVersion",
        minimum=1,
    )
    if version != BENCHMARK_SCENARIO_VERSION:
        raise BenchmarkContractError(
            f"benchmarkScenarioVersion must be {BENCHMARK_SCENARIO_VERSION}"
        )
    scenario_id = _identifier(data["scenarioId"], "scenarioId")
    phases = tuple(
        _parse_phase(item, f"benchmark phase {index}")
        for index, item in enumerate(_array(data["phases"], "benchmark phases"))
    )
    expected_phase_ids = tuple(item[0] for item in _PHASE_DEFINITIONS)
    actual_phase_ids = tuple(phase.phase_id for phase in phases)
    if actual_phase_ids != expected_phase_ids:
        raise BenchmarkContractError(
            f"benchmark phase order must be exactly {list(expected_phase_ids)}"
        )
    raw_checks = _array(data["requiredChecks"], "requiredChecks")
    checks = tuple(
        _identifier(item, f"required check {index}") for index, item in enumerate(raw_checks)
    )
    gates = tuple(
        _parse_gate(item, f"hard gate {index}")
        for index, item in enumerate(_array(data["hardGates"], "hardGates"))
    )
    parsed = BenchmarkScenario(version, scenario_id, phases, checks, gates)
    expected = build_benchmark_scenario()
    if parsed != expected:
        raise BenchmarkContractError(
            "benchmark scenario must exactly match the immutable Discord acceptance v1 contract"
        )
    return parsed


def benchmark_scenario_to_dict(scenario: BenchmarkScenario) -> dict[str, Any]:
    data = _scenario_to_dict_unchecked(scenario)
    parsed = benchmark_scenario_from_dict(data)
    if parsed != scenario:
        raise BenchmarkContractError("benchmark scenario changed during validation")
    return data


def render_benchmark_scenario(scenario: BenchmarkScenario) -> str:
    return _canonical_json(benchmark_scenario_to_dict(scenario))


def benchmark_scenario_sha256(scenario: BenchmarkScenario) -> str:
    return hashlib.sha256(render_benchmark_scenario(scenario).encode("utf-8")).hexdigest()


def _parse_environment(value: Any) -> BenchmarkEnvironment:
    data = _mapping(value, "benchmark environment")
    _exact_keys(
        data,
        frozenset(
            {
                "environmentVersion",
                "hardware",
                "kernel",
                "operatingSystem",
                "runtime",
                "session",
            }
        ),
        "benchmark environment",
    )
    version = _integer(data["environmentVersion"], "environmentVersion", minimum=1)
    if version != BENCHMARK_ENVIRONMENT_VERSION:
        raise BenchmarkContractError(
            f"environmentVersion must be {BENCHMARK_ENVIRONMENT_VERSION}"
        )

    hardware_data = _mapping(data["hardware"], "benchmark hardware")
    _exact_keys(
        hardware_data,
        frozenset(
            {"architecture", "cpuModel", "logicalCpuCount", "physicalMemoryBytes"}
        ),
        "benchmark hardware",
    )
    hardware = HardwareIdentity(
        architecture=_identifier(hardware_data["architecture"], "hardware architecture"),
        cpu_model=_text(hardware_data["cpuModel"], "hardware cpuModel"),
        logical_cpu_count=_integer(
            hardware_data["logicalCpuCount"],
            "hardware logicalCpuCount",
            minimum=1,
        ),
        physical_memory_bytes=_integer(
            hardware_data["physicalMemoryBytes"],
            "hardware physicalMemoryBytes",
            minimum=1,
        ),
    )

    kernel_data = _mapping(data["kernel"], "benchmark kernel")
    _exact_keys(kernel_data, frozenset({"release"}), "benchmark kernel")
    kernel = KernelIdentity(_text(kernel_data["release"], "kernel release"))

    os_data = _mapping(data["operatingSystem"], "benchmark operating system")
    _exact_keys(os_data, frozenset({"id", "versionId"}), "benchmark operating system")
    operating_system = OperatingSystemIdentity(
        os_id=_identifier(os_data["id"], "operating system id"),
        version_id=_text(os_data["versionId"], "operating system versionId"),
    )

    session_data = _mapping(data["session"], "benchmark session")
    _exact_keys(
        session_data,
        frozenset({"compositor", "portal", "type"}),
        "benchmark session",
    )
    session_type = _identifier(session_data["type"], "session type")
    if session_type not in ("wayland", "x11", "headless"):
        raise BenchmarkContractError("session type must be wayland, x11, or headless")
    compositor_data = _mapping(session_data["compositor"], "benchmark compositor")
    _exact_keys(
        compositor_data,
        frozenset({"name", "version"}),
        "benchmark compositor",
    )
    compositor = CompositorIdentity(
        _text(compositor_data["name"], "compositor name"),
        _text(compositor_data["version"], "compositor version"),
    )
    portal_data = _mapping(session_data["portal"], "benchmark portal")
    _exact_keys(
        portal_data,
        frozenset({"desktop", "implementation", "version"}),
        "benchmark portal",
    )
    portal = PortalIdentity(
        _text(portal_data["desktop"], "portal desktop"),
        _text(portal_data["implementation"], "portal implementation"),
        _text(portal_data["version"], "portal version"),
    )
    session = SessionIdentity(session_type, compositor, portal)

    runtime_data = _mapping(data["runtime"], "benchmark runtime")
    _exact_keys(
        runtime_data,
        frozenset(
            {
                "adapterTreeSha256",
                "appShellBuildId",
                "appShellVersion",
                "driverId",
                "driverProtocolVersion",
                "engine",
                "engineVersion",
                "identity",
            }
        ),
        "benchmark runtime",
    )
    try:
        executable = runtime_identity_from_dict(runtime_data["identity"])
    except RuntimeSafetyError as error:
        raise BenchmarkContractError(f"invalid benchmark runtime identity: {error}") from error
    runtime = BenchmarkRuntimeIdentity(
        engine=_identifier(runtime_data["engine"], "runtime engine"),
        engine_version=_text(runtime_data["engineVersion"], "runtime engineVersion"),
        driver_id=_identifier(runtime_data["driverId"], "runtime driverId"),
        driver_protocol_version=_integer(
            runtime_data["driverProtocolVersion"],
            "runtime driverProtocolVersion",
            minimum=1,
        ),
        app_shell_version=_text(
            runtime_data["appShellVersion"],
            "runtime appShellVersion",
        ),
        app_shell_build_id=_text(
            runtime_data["appShellBuildId"],
            "runtime appShellBuildId",
        ),
        adapter_tree_sha256=_digest(
            runtime_data["adapterTreeSha256"],
            "runtime adapterTreeSha256",
        ),
        executable=executable,
    )
    return BenchmarkEnvironment(
        version,
        hardware,
        kernel,
        operating_system,
        session,
        runtime,
    )


def _environment_to_dict(environment: BenchmarkEnvironment) -> dict[str, Any]:
    return {
        "environmentVersion": environment.environment_version,
        "hardware": {
            "architecture": environment.hardware.architecture,
            "cpuModel": environment.hardware.cpu_model,
            "logicalCpuCount": environment.hardware.logical_cpu_count,
            "physicalMemoryBytes": environment.hardware.physical_memory_bytes,
        },
        "kernel": {"release": environment.kernel.release},
        "operatingSystem": {
            "id": environment.operating_system.os_id,
            "versionId": environment.operating_system.version_id,
        },
        "runtime": {
            "adapterTreeSha256": environment.runtime.adapter_tree_sha256,
            "appShellBuildId": environment.runtime.app_shell_build_id,
            "appShellVersion": environment.runtime.app_shell_version,
            "driverId": environment.runtime.driver_id,
            "driverProtocolVersion": environment.runtime.driver_protocol_version,
            "engine": environment.runtime.engine,
            "engineVersion": environment.runtime.engine_version,
            "identity": runtime_identity_to_dict(environment.runtime.executable),
        },
        "session": {
            "compositor": {
                "name": environment.session.compositor.name,
                "version": environment.session.compositor.version,
            },
            "portal": {
                "desktop": environment.session.portal.desktop,
                "implementation": environment.session.portal.implementation,
                "version": environment.session.portal.version,
            },
            "type": environment.session.session_type,
        },
    }


def _parse_observation(value: Any, phase: BenchmarkPhase, metric: MetricSpec) -> MetricObservation:
    label = f"phase {phase.phase_id} observation {metric.metric_id}"
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset({"confidence95", "metricId", "sampleCount", "value"}),
        label,
    )
    metric_id = _identifier(data["metricId"], f"{label} metricId")
    if metric_id != metric.metric_id:
        raise BenchmarkContractError(
            f"{label} metricId must be {metric.metric_id} in contract order"
        )
    observed_value = _integer(data["value"], f"{label} value")
    sample_count = _integer(data["sampleCount"], f"{label} sampleCount", minimum=1)
    if sample_count < phase.repetitions:
        raise BenchmarkContractError(
            f"{label} sampleCount must cover all {phase.repetitions} repetitions"
        )
    confidence_data = _mapping(data["confidence95"], f"{label} confidence95")
    _exact_keys(
        confidence_data,
        frozenset({"lower", "upper"}),
        f"{label} confidence95",
    )
    lower = _integer(confidence_data["lower"], f"{label} confidence lower")
    upper = _integer(confidence_data["upper"], f"{label} confidence upper")
    if not lower <= observed_value <= upper:
        raise BenchmarkContractError(
            f"{label} confidence interval must contain the observed value"
        )
    if metric.metric_id == "summed-pss-start" and lower == 0:
        raise BenchmarkContractError("soak baseline confidence lower bound must be positive")
    return MetricObservation(
        metric_id,
        observed_value,
        sample_count,
        ConfidenceInterval(lower, upper),
    )


def _parse_phase_result(value: Any, phase: BenchmarkPhase) -> BenchmarkPhaseResult:
    label = f"benchmark phase result {phase.phase_id}"
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"observations", "phaseId", "status"}), label)
    phase_id = _identifier(data["phaseId"], f"{label} phaseId")
    if phase_id != phase.phase_id:
        raise BenchmarkContractError(
            f"benchmark phase result order must match phase {phase.phase_id}"
        )
    status = _identifier(data["status"], f"{label} status")
    if status not in _PHASE_STATUSES:
        raise BenchmarkContractError(f"{label} status is invalid")
    raw_observations = _array(data["observations"], f"{label} observations")
    if status == "measured":
        if len(raw_observations) != len(phase.required_metrics):
            raise BenchmarkContractError(
                f"{label} observations must exactly match the required metrics"
            )
        observations = tuple(
            _parse_observation(item, phase, metric)
            for item, metric in zip(raw_observations, phase.required_metrics, strict=True)
        )
    else:
        if raw_observations:
            raise BenchmarkContractError(
                f"{label} observations must be empty when status is {status}"
            )
        observations = ()
    return BenchmarkPhaseResult(phase_id, status, observations)


def _parse_check_result(value: Any, expected_check_id: str) -> BenchmarkCheckResult:
    label = f"benchmark check result {expected_check_id}"
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"checkId", "status"}), label)
    check_id = _identifier(data["checkId"], f"{label} checkId")
    if check_id != expected_check_id:
        raise BenchmarkContractError(
            f"benchmark check result order must match check {expected_check_id}"
        )
    status = _identifier(data["status"], f"{label} status")
    if status not in _CHECK_STATUSES:
        raise BenchmarkContractError(f"{label} status is invalid")
    return BenchmarkCheckResult(check_id, status)


def benchmark_result_from_dict(
    value: Any,
    *,
    scenario: BenchmarkScenario,
) -> BenchmarkResult:
    scenario_data = benchmark_scenario_to_dict(scenario)
    del scenario_data  # validation is the purpose; the digest below binds the evidence.
    data = _mapping(value, "benchmark result")
    _exact_keys(
        data,
        frozenset(
            {
                "benchmarkResultVersion",
                "checks",
                "completedAtUtc",
                "environment",
                "phases",
                "scenarioSha256",
                "startedAtUtc",
            }
        ),
        "benchmark result",
    )
    version = _integer(data["benchmarkResultVersion"], "benchmarkResultVersion", minimum=1)
    if version != BENCHMARK_RESULT_VERSION:
        raise BenchmarkContractError(
            f"benchmarkResultVersion must be {BENCHMARK_RESULT_VERSION}"
        )
    scenario_digest = _digest(data["scenarioSha256"], "scenarioSha256")
    expected_digest = benchmark_scenario_sha256(scenario)
    if scenario_digest != expected_digest:
        raise BenchmarkContractError("benchmark result is bound to a different scenario")
    started_at, started = _timestamp(data["startedAtUtc"], "startedAtUtc")
    completed_at, completed = _timestamp(data["completedAtUtc"], "completedAtUtc")
    if completed < started:
        raise BenchmarkContractError("completedAtUtc cannot precede startedAtUtc")
    environment = _parse_environment(data["environment"])

    raw_phases = _array(data["phases"], "benchmark result phases")
    if len(raw_phases) != len(scenario.phases):
        raise BenchmarkContractError("benchmark result phases must exactly match the scenario")
    phases = tuple(
        _parse_phase_result(item, phase)
        for item, phase in zip(raw_phases, scenario.phases, strict=True)
    )

    raw_checks = _array(data["checks"], "benchmark result checks")
    if len(raw_checks) != len(scenario.required_checks):
        raise BenchmarkContractError("benchmark result checks must exactly match the scenario")
    checks = tuple(
        _parse_check_result(item, check_id)
        for item, check_id in zip(raw_checks, scenario.required_checks, strict=True)
    )
    return BenchmarkResult(
        version,
        scenario_digest,
        started_at,
        completed_at,
        environment,
        phases,
        checks,
    )


def _observation_to_dict(observation: MetricObservation) -> dict[str, Any]:
    return {
        "confidence95": {
            "lower": observation.confidence95.lower,
            "upper": observation.confidence95.upper,
        },
        "metricId": observation.metric_id,
        "sampleCount": observation.sample_count,
        "value": observation.value,
    }


def _phase_result_to_dict(phase: BenchmarkPhaseResult) -> dict[str, Any]:
    return {
        "observations": [_observation_to_dict(item) for item in phase.observations],
        "phaseId": phase.phase_id,
        "status": phase.status,
    }


def _result_to_dict_unchecked(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "benchmarkResultVersion": result.result_version,
        "checks": [
            {"checkId": check.check_id, "status": check.status}
            for check in result.checks
        ],
        "completedAtUtc": result.completed_at_utc,
        "environment": _environment_to_dict(result.environment),
        "phases": [_phase_result_to_dict(phase) for phase in result.phases],
        "scenarioSha256": result.scenario_sha256,
        "startedAtUtc": result.started_at_utc,
    }


def benchmark_result_to_dict(
    result: BenchmarkResult,
    *,
    scenario: BenchmarkScenario,
) -> dict[str, Any]:
    data = _result_to_dict_unchecked(result)
    parsed = benchmark_result_from_dict(data, scenario=scenario)
    if parsed != result:
        raise BenchmarkContractError("benchmark result changed during validation")
    return data


def render_benchmark_result(
    result: BenchmarkResult,
    *,
    scenario: BenchmarkScenario,
) -> str:
    return _canonical_json(benchmark_result_to_dict(result, scenario=scenario))


def benchmark_result_sha256(
    result: BenchmarkResult,
    *,
    scenario: BenchmarkScenario,
) -> str:
    return hashlib.sha256(
        render_benchmark_result(result, scenario=scenario).encode("utf-8")
    ).hexdigest()


def _observation_index(phase: BenchmarkPhaseResult) -> dict[str, MetricObservation]:
    return {observation.metric_id: observation for observation in phase.observations}


def _comparison_passes(observed: int, comparison: str, limit: int) -> bool:
    if comparison == "less-than-or-equal":
        return observed <= limit
    if comparison == "less-than":
        return observed < limit
    raise BenchmarkContractError(f"unsupported numeric hard gate comparison {comparison}")


def _evaluate_gate(
    gate: HardGate,
    phases: Mapping[str, BenchmarkPhaseResult],
    checks: Mapping[str, BenchmarkCheckResult],
) -> HardGateEvaluation:
    threshold = _gate_threshold_to_dict(gate)
    if gate.source_kind == "check":
        check = checks[gate.check_id or ""]
        status = {
            "passed": "pass",
            "failed": "fail",
            "not-run": "not-measured",
        }[check.status]
        return HardGateEvaluation(gate.gate_id, status, check.status, threshold)

    phase = phases[gate.phase_id or ""]
    if phase.status != "measured":
        status = "fail" if phase.status == "failed" else "not-measured"
        return HardGateEvaluation(gate.gate_id, status, None, threshold)
    observations = _observation_index(phase)

    if gate.source_kind == "metric":
        observation = observations[gate.metric_id or ""]
        observed = observation.confidence95.upper
        if type(gate.limit) is not int:
            raise BenchmarkContractError("numeric hard gate limit is invalid")
        status = (
            "pass"
            if _comparison_passes(observed, gate.comparison, gate.limit)
            else "fail"
        )
        return HardGateEvaluation(gate.gate_id, status, observed, threshold)

    if gate.source_kind == "ratio":
        baseline = observations[gate.baseline_metric_id or ""].confidence95.lower
        end = observations[gate.metric_id or ""].confidence95.upper
        if baseline <= 0 or type(gate.limit) is not int or gate.comparison != "less-than":
            raise BenchmarkContractError("ratio hard gate definition is invalid")
        # Compare ((end - baseline) / baseline) * 100 < limit without float
        # rounding.  Negative growth naturally passes this upper-bound gate.
        passes = (end - baseline) * 100 < baseline * gate.limit
        observed = {
            "baselineLower95": baseline,
            "endUpper95": end,
            "unit": "kibibytes",
        }
        return HardGateEvaluation(
            gate.gate_id,
            "pass" if passes else "fail",
            observed,
            threshold,
        )

    raise BenchmarkContractError("hard gate source kind is invalid")


def evaluate_benchmark_result(
    scenario: BenchmarkScenario,
    result: BenchmarkResult,
) -> BenchmarkAcceptance:
    """Evaluate all immutable gates; missing evidence is never reported as pass."""
    benchmark_scenario_to_dict(scenario)
    benchmark_result_to_dict(result, scenario=scenario)
    phases = {phase.phase_id: phase for phase in result.phases}
    checks = {check.check_id: check for check in result.checks}
    gates = tuple(_evaluate_gate(gate, phases, checks) for gate in scenario.hard_gates)
    statuses = {gate.status for gate in gates}
    if "fail" in statuses:
        overall = "fail"
    elif "not-measured" in statuses:
        overall = "incomplete"
    else:
        overall = "pass"
    return BenchmarkAcceptance(
        acceptance_version=BENCHMARK_ACCEPTANCE_VERSION,
        scenario_sha256=benchmark_scenario_sha256(scenario),
        result_sha256=benchmark_result_sha256(result, scenario=scenario),
        overall_status=overall,
        gates=gates,
    )


def _validate_json_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, str) or type(value) is int or type(value) is bool:
        return
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise BenchmarkContractError(f"{label} object keys must be text")
        for key, item in value.items():
            _validate_json_value(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]")
        return
    raise BenchmarkContractError(f"{label} contains a non-canonical JSON value")


def benchmark_acceptance_to_dict(acceptance: BenchmarkAcceptance) -> dict[str, Any]:
    if (
        type(acceptance.acceptance_version) is not int
        or acceptance.acceptance_version != BENCHMARK_ACCEPTANCE_VERSION
    ):
        raise BenchmarkContractError(
            f"benchmarkAcceptanceVersion must be {BENCHMARK_ACCEPTANCE_VERSION}"
        )
    scenario_digest = _digest(acceptance.scenario_sha256, "acceptance scenarioSha256")
    result_digest = _digest(acceptance.result_sha256, "acceptance resultSha256")
    if acceptance.overall_status not in ("pass", "fail", "incomplete"):
        raise BenchmarkContractError("acceptance overallStatus is invalid")
    if not acceptance.gates:
        raise BenchmarkContractError("acceptance gates cannot be empty")
    gate_ids: set[str] = set()
    gate_data: list[dict[str, Any]] = []
    statuses: set[str] = set()
    for index, gate in enumerate(acceptance.gates):
        gate_id = _identifier(gate.gate_id, f"acceptance gate {index} gateId")
        if gate_id in gate_ids:
            raise BenchmarkContractError("acceptance gate IDs must be unique")
        gate_ids.add(gate_id)
        if gate.status not in _GATE_STATUSES:
            raise BenchmarkContractError(f"acceptance gate {gate_id} status is invalid")
        threshold = dict(_mapping(gate.threshold, f"acceptance gate {gate_id} threshold"))
        _validate_json_value(gate.observed, f"acceptance gate {gate_id} observed")
        _validate_json_value(threshold, f"acceptance gate {gate_id} threshold")
        statuses.add(gate.status)
        gate_data.append(
            {
                "gateId": gate_id,
                "observed": gate.observed,
                "status": gate.status,
                "threshold": threshold,
            }
        )
    expected_overall = (
        "fail"
        if "fail" in statuses
        else "incomplete"
        if "not-measured" in statuses
        else "pass"
    )
    if acceptance.overall_status != expected_overall:
        raise BenchmarkContractError("acceptance overallStatus does not match gate statuses")
    return {
        "benchmarkAcceptanceVersion": acceptance.acceptance_version,
        "gates": gate_data,
        "overallStatus": acceptance.overall_status,
        "resultSha256": result_digest,
        "scenarioSha256": scenario_digest,
    }


def render_benchmark_acceptance(acceptance: BenchmarkAcceptance) -> str:
    return _canonical_json(benchmark_acceptance_to_dict(acceptance))
