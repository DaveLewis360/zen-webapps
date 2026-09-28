"""Versioned, privacy-safe evidence for current-Zen App Shell optimization.

The native harness records only whole-scope aggregate memory/CPU/security
counts and fixed configuration booleans.  Process IDs, cgroup paths, command
lines, environments, URLs, account/message data, screenshots, and free-form
diagnostics are deliberately excluded from the persistable report.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .benchmark_runner import (
    BenchmarkRunnerError,
    CgroupSample,
    cgroup_sample_to_dict,
)

APP_SHELL_OPTIMIZATION_REPORT_VERSION = 1
APP_SHELL_OPTIMIZATION_EVALUATION_VERSION = 1
APP_SHELL_SCOPE_EVIDENCE_VERSION = 1
APP_SHELL_OPTIMIZATION_TRIAL_VERSION = 1
APP_SHELL_OPTIMIZATION_VARIANTS = ("baseline", "gc-empty-chunks-0")
APP_SHELL_OPTIMIZATION_STARTUP_KINDS = ("cold", "warm")
APP_SHELL_OPTIMIZATION_REPETITIONS = 3

GC_EMPTY_CHUNK_PREF = "javascript.options.mem.gc_min_empty_chunk_count"
GC_EMPTY_CHUNK_BASELINE = 1
GC_EMPTY_CHUNK_CANDIDATE = 0
GC_PREF_SOURCE = "gecko-154-omni-greprefs.js"
GC_PREF_SOURCE_SHA256 = "f5359324dc1d863c72b9804b24a48bda198342f0eda5f6162fad1f60f1656ac5"

ONBOARDING_PSS_LIMIT_KIB = 122_880
ACTIVE_PSS_LIMIT_KIB = 358_400
USABLE_TIME_LIMIT_MS = 3_000
IDLE_CPU_LIMIT_BASIS_POINTS = 100
MINIMUM_MEMORY_IMPROVEMENT_KIB = 4_096
MEMORY_NOISE_KIB = 2_048
STARTUP_NOISE_MS = 150
CPU_NOISE_BASIS_POINTS = 10

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DECISION_REASONS = (
    "active-pss-hard-gate",
    "candidate-preference-mismatch",
    "cold-usable-hard-gate",
    "cleanup-incomplete",
    "content-security-invariant",
    "idle-cpu-hard-gate",
    "idle-cpu-regression",
    "memory-regression",
    "memory-reduction-below-threshold",
    "onboarding-pss-hard-gate",
    "runtime-security-invariant",
    "startup-regression",
    "warm-usable-hard-gate",
)
_FORBIDDEN_PRIVACY_KEYS = (
    "account",
    "cgroup",
    "command",
    "diagnostic",
    "message",
    "path",
    "pid",
    "processname",
    "url",
)


class RuntimeOptimizationError(ValueError):
    """Optimization evidence was malformed, incomplete, or unsafe."""


@dataclass(frozen=True)
class AppShellScopeEvidence:
    evidence_version: int
    process_count: int
    pss_kib: int
    rss_kib: int
    private_kib: int
    swap_pss_kib: int
    memory_current_bytes: int
    memory_peak_bytes: int
    memory_swap_current_bytes: int
    seccomp_filtered_count: int
    no_new_privs_count: int


@dataclass(frozen=True)
class AppShellOptimizationTrial:
    trial_version: int
    variant: str
    pair_index: int
    startup_kind: str
    onboarding_milliseconds: int
    usable_milliseconds: int
    onboarding: AppShellScopeEvidence
    active: AppShellScopeEvidence
    idle: AppShellScopeEvidence
    idle_cpu_basis_points: int
    post_commit_new_secure_process_count: int
    preference_value: int
    fission_enabled: bool
    remote_content_enabled: bool
    content_sandbox_level: int
    socket_sandbox_level: int
    native_wayland: bool
    hardware_acceleration_disable_pref_absent: bool
    transparent_surface_configured: bool
    cleanup_complete: bool


@dataclass(frozen=True)
class IntegerMetricSummary:
    sample_count: int
    minimum: int
    median: int
    p95: int
    maximum: int


@dataclass(frozen=True)
class AppShellOptimizationSummary:
    cold_usable_milliseconds: IntegerMetricSummary
    warm_usable_milliseconds: IntegerMetricSummary
    onboarding_pss_kib: IntegerMetricSummary
    active_pss_kib: IntegerMetricSummary
    idle_pss_kib: IntegerMetricSummary
    idle_cpu_basis_points: IntegerMetricSummary


@dataclass(frozen=True)
class AppShellOptimizationEvaluation:
    evaluation_version: int
    status: str
    reasons: tuple[str, ...]
    baseline: AppShellOptimizationSummary
    candidate: AppShellOptimizationSummary


@dataclass(frozen=True)
class AppShellOptimizationReport:
    report_version: int
    runtime_sha256: str
    measured_adapter_tree_sha256: str
    app_shell_version: str
    preference_name: str
    preference_source: str
    preference_source_sha256: str
    trials: tuple[AppShellOptimizationTrial, ...]
    evaluation: AppShellOptimizationEvaluation


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise RuntimeOptimizationError(f"{label} must be an object with text keys")
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
        raise RuntimeOptimizationError(f"{label} fields: {', '.join(details)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RuntimeOptimizationError(f"{label} must be an integer at least {minimum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise RuntimeOptimizationError(f"{label} must be boolean")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise RuntimeOptimizationError(f"{label} must be a lowercase SHA-256 digest")
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
        raise RuntimeOptimizationError(f"report is not canonical JSON data: {error}") from error


def build_scope_evidence(sample: CgroupSample) -> AppShellScopeEvidence:
    """Drop PID/path data from one authoritative cgroup-v2 sample."""
    try:
        cgroup_sample_to_dict(sample)
    except BenchmarkRunnerError as error:
        raise RuntimeOptimizationError(f"invalid cgroup sample: {error}") from error
    return AppShellScopeEvidence(
        evidence_version=APP_SHELL_SCOPE_EVIDENCE_VERSION,
        process_count=len(sample.processes),
        pss_kib=sample.summed_pss_kib,
        rss_kib=sample.summed_rss_kib,
        private_kib=sample.summed_private_kib,
        swap_pss_kib=sample.summed_swap_pss_kib,
        memory_current_bytes=sample.memory_current_bytes,
        memory_peak_bytes=sample.memory_peak_bytes,
        memory_swap_current_bytes=sample.memory_swap_current_bytes,
        seccomp_filtered_count=sum(process.seccomp == 2 for process in sample.processes),
        no_new_privs_count=sum(process.no_new_privs == 1 for process in sample.processes),
    )


def _scope_to_dict(scope: AppShellScopeEvidence) -> dict[str, Any]:
    if not isinstance(scope, AppShellScopeEvidence):
        raise RuntimeOptimizationError("scope evidence authority was fabricated")
    values = {
        "memoryCurrentBytes": _integer(scope.memory_current_bytes, "memoryCurrentBytes"),
        "memoryPeakBytes": _integer(scope.memory_peak_bytes, "memoryPeakBytes"),
        "memorySwapCurrentBytes": _integer(
            scope.memory_swap_current_bytes, "memorySwapCurrentBytes"
        ),
        "noNewPrivsCount": _integer(scope.no_new_privs_count, "noNewPrivsCount"),
        "privateKiB": _integer(scope.private_kib, "privateKiB"),
        "processCount": _integer(scope.process_count, "processCount", minimum=1),
        "pssKiB": _integer(scope.pss_kib, "pssKiB"),
        "rssKiB": _integer(scope.rss_kib, "rssKiB"),
        "scopeEvidenceVersion": _integer(
            scope.evidence_version, "scopeEvidenceVersion", minimum=1
        ),
        "seccompFilteredCount": _integer(
            scope.seccomp_filtered_count, "seccompFilteredCount"
        ),
        "swapPssKiB": _integer(scope.swap_pss_kib, "swapPssKiB"),
    }
    if scope.evidence_version != APP_SHELL_SCOPE_EVIDENCE_VERSION:
        raise RuntimeOptimizationError(
            f"scopeEvidenceVersion must be {APP_SHELL_SCOPE_EVIDENCE_VERSION}"
        )
    if scope.seccomp_filtered_count > scope.process_count:
        raise RuntimeOptimizationError("seccomp count exceeds process count")
    if scope.no_new_privs_count > scope.process_count:
        raise RuntimeOptimizationError("NoNewPrivs count exceeds process count")
    return values


def _scope_from_dict(value: Any, label: str) -> AppShellScopeEvidence:
    data = _mapping(value, label)
    expected = frozenset(
        {
            "memoryCurrentBytes",
            "memoryPeakBytes",
            "memorySwapCurrentBytes",
            "noNewPrivsCount",
            "privateKiB",
            "processCount",
            "pssKiB",
            "rssKiB",
            "scopeEvidenceVersion",
            "seccompFilteredCount",
            "swapPssKiB",
        }
    )
    _exact_keys(data, expected, label)
    scope = AppShellScopeEvidence(
        evidence_version=_integer(data["scopeEvidenceVersion"], f"{label} version", minimum=1),
        process_count=_integer(data["processCount"], f"{label} processCount", minimum=1),
        pss_kib=_integer(data["pssKiB"], f"{label} pssKiB"),
        rss_kib=_integer(data["rssKiB"], f"{label} rssKiB"),
        private_kib=_integer(data["privateKiB"], f"{label} privateKiB"),
        swap_pss_kib=_integer(data["swapPssKiB"], f"{label} swapPssKiB"),
        memory_current_bytes=_integer(
            data["memoryCurrentBytes"], f"{label} memoryCurrentBytes"
        ),
        memory_peak_bytes=_integer(data["memoryPeakBytes"], f"{label} memoryPeakBytes"),
        memory_swap_current_bytes=_integer(
            data["memorySwapCurrentBytes"], f"{label} memorySwapCurrentBytes"
        ),
        seccomp_filtered_count=_integer(
            data["seccompFilteredCount"], f"{label} seccompFilteredCount"
        ),
        no_new_privs_count=_integer(
            data["noNewPrivsCount"], f"{label} noNewPrivsCount"
        ),
    )
    _scope_to_dict(scope)
    return scope


def _trial_to_dict(trial: AppShellOptimizationTrial) -> dict[str, Any]:
    if not isinstance(trial, AppShellOptimizationTrial):
        raise RuntimeOptimizationError("optimization trial authority was fabricated")
    if trial.trial_version != APP_SHELL_OPTIMIZATION_TRIAL_VERSION:
        raise RuntimeOptimizationError(
            f"optimizationTrialVersion must be {APP_SHELL_OPTIMIZATION_TRIAL_VERSION}"
        )
    if trial.variant not in APP_SHELL_OPTIMIZATION_VARIANTS:
        raise RuntimeOptimizationError("optimization trial variant is invalid")
    if trial.startup_kind not in APP_SHELL_OPTIMIZATION_STARTUP_KINDS:
        raise RuntimeOptimizationError("optimization startup kind is invalid")
    return {
        "active": _scope_to_dict(trial.active),
        "cleanupComplete": _boolean(trial.cleanup_complete, "cleanupComplete"),
        "configuration": {
            "contentSandboxLevel": _integer(
                trial.content_sandbox_level, "contentSandboxLevel"
            ),
            "fissionEnabled": _boolean(trial.fission_enabled, "fissionEnabled"),
            "hardwareAccelerationDisablePrefAbsent": _boolean(
                trial.hardware_acceleration_disable_pref_absent,
                "hardwareAccelerationDisablePrefAbsent",
            ),
            "nativeWayland": _boolean(trial.native_wayland, "nativeWayland"),
            "remoteContentEnabled": _boolean(
                trial.remote_content_enabled, "remoteContentEnabled"
            ),
            "socketSandboxLevel": _integer(
                trial.socket_sandbox_level, "socketSandboxLevel"
            ),
            "transparentSurfaceConfigured": _boolean(
                trial.transparent_surface_configured,
                "transparentSurfaceConfigured",
            ),
        },
        "idle": _scope_to_dict(trial.idle),
        "idleCpuBasisPoints": _integer(
            trial.idle_cpu_basis_points, "idleCpuBasisPoints"
        ),
        "onboarding": _scope_to_dict(trial.onboarding),
        "onboardingMilliseconds": _integer(
            trial.onboarding_milliseconds, "onboardingMilliseconds"
        ),
        "optimizationTrialVersion": trial.trial_version,
        "pairIndex": _integer(trial.pair_index, "pairIndex", minimum=1),
        "postCommitNewSecureProcessCount": _integer(
            trial.post_commit_new_secure_process_count,
            "postCommitNewSecureProcessCount",
        ),
        "preferenceValue": _integer(trial.preference_value, "preferenceValue"),
        "startupKind": trial.startup_kind,
        "usableMilliseconds": _integer(trial.usable_milliseconds, "usableMilliseconds"),
        "variant": trial.variant,
    }


def _trial_from_dict(value: Any, index: int) -> AppShellOptimizationTrial:
    label = f"optimization trial {index}"
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset(
            {
                "active",
                "cleanupComplete",
                "configuration",
                "idle",
                "idleCpuBasisPoints",
                "onboarding",
                "onboardingMilliseconds",
                "optimizationTrialVersion",
                "pairIndex",
                "postCommitNewSecureProcessCount",
                "preferenceValue",
                "startupKind",
                "usableMilliseconds",
                "variant",
            }
        ),
        label,
    )
    configuration = _mapping(data["configuration"], f"{label} configuration")
    _exact_keys(
        configuration,
        frozenset(
            {
                "contentSandboxLevel",
                "fissionEnabled",
                "hardwareAccelerationDisablePrefAbsent",
                "nativeWayland",
                "remoteContentEnabled",
                "socketSandboxLevel",
                "transparentSurfaceConfigured",
            }
        ),
        f"{label} configuration",
    )
    trial = AppShellOptimizationTrial(
        trial_version=_integer(
            data["optimizationTrialVersion"], f"{label} version", minimum=1
        ),
        variant=data["variant"],
        pair_index=_integer(data["pairIndex"], f"{label} pairIndex", minimum=1),
        startup_kind=data["startupKind"],
        onboarding_milliseconds=_integer(
            data["onboardingMilliseconds"], f"{label} onboardingMilliseconds"
        ),
        usable_milliseconds=_integer(
            data["usableMilliseconds"], f"{label} usableMilliseconds"
        ),
        onboarding=_scope_from_dict(data["onboarding"], f"{label} onboarding"),
        active=_scope_from_dict(data["active"], f"{label} active"),
        idle=_scope_from_dict(data["idle"], f"{label} idle"),
        idle_cpu_basis_points=_integer(
            data["idleCpuBasisPoints"], f"{label} idleCpuBasisPoints"
        ),
        post_commit_new_secure_process_count=_integer(
            data["postCommitNewSecureProcessCount"],
            f"{label} postCommitNewSecureProcessCount",
        ),
        preference_value=_integer(data["preferenceValue"], f"{label} preferenceValue"),
        fission_enabled=_boolean(
            configuration["fissionEnabled"], f"{label} fissionEnabled"
        ),
        remote_content_enabled=_boolean(
            configuration["remoteContentEnabled"], f"{label} remoteContentEnabled"
        ),
        content_sandbox_level=_integer(
            configuration["contentSandboxLevel"], f"{label} contentSandboxLevel"
        ),
        socket_sandbox_level=_integer(
            configuration["socketSandboxLevel"], f"{label} socketSandboxLevel"
        ),
        native_wayland=_boolean(
            configuration["nativeWayland"], f"{label} nativeWayland"
        ),
        hardware_acceleration_disable_pref_absent=_boolean(
            configuration["hardwareAccelerationDisablePrefAbsent"],
            f"{label} hardwareAccelerationDisablePrefAbsent",
        ),
        transparent_surface_configured=_boolean(
            configuration["transparentSurfaceConfigured"],
            f"{label} transparentSurfaceConfigured",
        ),
        cleanup_complete=_boolean(data["cleanupComplete"], f"{label} cleanupComplete"),
    )
    if _trial_to_dict(trial) != data:
        raise RuntimeOptimizationError(f"{label} changed during validation")
    return trial


def _integer_summary(values: Sequence[int]) -> IntegerMetricSummary:
    if not values:
        raise RuntimeOptimizationError("metric summary requires at least one value")
    normalized = sorted(_integer(value, "metric value") for value in values)
    middle = len(normalized) // 2
    median = (
        normalized[middle]
        if len(normalized) % 2
        else (normalized[middle - 1] + normalized[middle] + 1) // 2
    )
    p95_index = (95 * len(normalized) + 99) // 100 - 1
    return IntegerMetricSummary(
        sample_count=len(normalized),
        minimum=normalized[0],
        median=median,
        p95=normalized[p95_index],
        maximum=normalized[-1],
    )


def _validate_trial_matrix(trials: Sequence[AppShellOptimizationTrial]) -> None:
    expected = {
        (variant, pair_index, startup_kind)
        for variant in APP_SHELL_OPTIMIZATION_VARIANTS
        for pair_index in range(1, APP_SHELL_OPTIMIZATION_REPETITIONS + 1)
        for startup_kind in APP_SHELL_OPTIMIZATION_STARTUP_KINDS
    }
    actual = [(trial.variant, trial.pair_index, trial.startup_kind) for trial in trials]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise RuntimeOptimizationError(
            "optimization trials must contain the exact paired cold/warm matrix"
        )


def summarize_trials(
    trials: Sequence[AppShellOptimizationTrial],
    variant: str,
) -> AppShellOptimizationSummary:
    if variant not in APP_SHELL_OPTIMIZATION_VARIANTS:
        raise RuntimeOptimizationError("optimization summary variant is invalid")
    selected = [trial for trial in trials if trial.variant == variant]
    if not selected:
        raise RuntimeOptimizationError("optimization summary has no trials")
    cold = [trial.usable_milliseconds for trial in selected if trial.startup_kind == "cold"]
    warm = [trial.usable_milliseconds for trial in selected if trial.startup_kind == "warm"]
    return AppShellOptimizationSummary(
        cold_usable_milliseconds=_integer_summary(cold),
        warm_usable_milliseconds=_integer_summary(warm),
        onboarding_pss_kib=_integer_summary([trial.onboarding.pss_kib for trial in selected]),
        active_pss_kib=_integer_summary([trial.active.pss_kib for trial in selected]),
        idle_pss_kib=_integer_summary([trial.idle.pss_kib for trial in selected]),
        idle_cpu_basis_points=_integer_summary(
            [trial.idle_cpu_basis_points for trial in selected]
        ),
    )


def _trial_security_ok(trial: AppShellOptimizationTrial) -> bool:
    return (
        trial.fission_enabled
        and trial.remote_content_enabled
        and trial.content_sandbox_level == 6
        and trial.socket_sandbox_level == 2
        and trial.native_wayland
        and trial.hardware_acceleration_disable_pref_absent
        and trial.transparent_surface_configured
    )


def evaluate_app_shell_optimization(
    trials: Sequence[AppShellOptimizationTrial],
) -> AppShellOptimizationEvaluation:
    normalized = tuple(trials)
    for trial in normalized:
        _trial_to_dict(trial)
    _validate_trial_matrix(normalized)
    baseline = summarize_trials(normalized, "baseline")
    candidate = summarize_trials(normalized, "gc-empty-chunks-0")
    candidate_trials = [trial for trial in normalized if trial.variant == "gc-empty-chunks-0"]
    baseline_trials = [trial for trial in normalized if trial.variant == "baseline"]
    reasons: set[str] = set()

    if any(trial.preference_value != GC_EMPTY_CHUNK_BASELINE for trial in baseline_trials) or any(
        trial.preference_value != GC_EMPTY_CHUNK_CANDIDATE for trial in candidate_trials
    ):
        reasons.add("candidate-preference-mismatch")
    if any(not _trial_security_ok(trial) for trial in candidate_trials):
        reasons.add("runtime-security-invariant")
    if any(trial.post_commit_new_secure_process_count < 1 for trial in candidate_trials):
        reasons.add("content-security-invariant")
    if any(not trial.cleanup_complete for trial in normalized):
        reasons.add("cleanup-incomplete")
    if candidate.cold_usable_milliseconds.p95 > USABLE_TIME_LIMIT_MS:
        reasons.add("cold-usable-hard-gate")
    if candidate.warm_usable_milliseconds.p95 > USABLE_TIME_LIMIT_MS:
        reasons.add("warm-usable-hard-gate")
    if candidate.onboarding_pss_kib.p95 > ONBOARDING_PSS_LIMIT_KIB:
        reasons.add("onboarding-pss-hard-gate")
    if candidate.active_pss_kib.p95 > ACTIVE_PSS_LIMIT_KIB:
        reasons.add("active-pss-hard-gate")
    if candidate.idle_cpu_basis_points.p95 >= IDLE_CPU_LIMIT_BASIS_POINTS:
        reasons.add("idle-cpu-hard-gate")

    if (
        candidate.cold_usable_milliseconds.median
        > baseline.cold_usable_milliseconds.median + STARTUP_NOISE_MS
        or candidate.warm_usable_milliseconds.median
        > baseline.warm_usable_milliseconds.median + STARTUP_NOISE_MS
    ):
        reasons.add("startup-regression")
    if (
        candidate.idle_cpu_basis_points.median
        > baseline.idle_cpu_basis_points.median + CPU_NOISE_BASIS_POINTS
    ):
        reasons.add("idle-cpu-regression")

    memory_pairs = (
        (baseline.onboarding_pss_kib.median, candidate.onboarding_pss_kib.median),
        (baseline.active_pss_kib.median, candidate.active_pss_kib.median),
        (baseline.idle_pss_kib.median, candidate.idle_pss_kib.median),
    )
    if any(candidate_value > baseline_value + MEMORY_NOISE_KIB for baseline_value, candidate_value in memory_pairs):
        reasons.add("memory-regression")
    if max(baseline_value - candidate_value for baseline_value, candidate_value in memory_pairs) < MINIMUM_MEMORY_IMPROVEMENT_KIB:
        reasons.add("memory-reduction-below-threshold")

    ordered_reasons = tuple(reason for reason in _ALLOWED_DECISION_REASONS if reason in reasons)
    return AppShellOptimizationEvaluation(
        evaluation_version=APP_SHELL_OPTIMIZATION_EVALUATION_VERSION,
        status="accepted" if not ordered_reasons else "rejected",
        reasons=ordered_reasons,
        baseline=baseline,
        candidate=candidate,
    )


def _metric_summary_to_dict(summary: IntegerMetricSummary) -> dict[str, int]:
    return {
        "maximum": summary.maximum,
        "median": summary.median,
        "minimum": summary.minimum,
        "p95": summary.p95,
        "sampleCount": summary.sample_count,
    }


def _summary_to_dict(summary: AppShellOptimizationSummary) -> dict[str, Any]:
    return {
        "activePssKiB": _metric_summary_to_dict(summary.active_pss_kib),
        "coldUsableMilliseconds": _metric_summary_to_dict(
            summary.cold_usable_milliseconds
        ),
        "idleCpuBasisPoints": _metric_summary_to_dict(
            summary.idle_cpu_basis_points
        ),
        "idlePssKiB": _metric_summary_to_dict(summary.idle_pss_kib),
        "onboardingPssKiB": _metric_summary_to_dict(summary.onboarding_pss_kib),
        "warmUsableMilliseconds": _metric_summary_to_dict(
            summary.warm_usable_milliseconds
        ),
    }


def _evaluation_to_dict(evaluation: AppShellOptimizationEvaluation) -> dict[str, Any]:
    if evaluation.evaluation_version != APP_SHELL_OPTIMIZATION_EVALUATION_VERSION:
        raise RuntimeOptimizationError(
            "optimization evaluation version does not match"
        )
    if evaluation.status not in ("accepted", "rejected"):
        raise RuntimeOptimizationError("optimization evaluation status is invalid")
    if evaluation.reasons != tuple(
        reason for reason in _ALLOWED_DECISION_REASONS if reason in evaluation.reasons
    ):
        raise RuntimeOptimizationError("optimization reasons are unknown, duplicated, or unordered")
    if (evaluation.status == "accepted") != (not evaluation.reasons):
        raise RuntimeOptimizationError("optimization status does not match its reasons")
    return {
        "baseline": _summary_to_dict(evaluation.baseline),
        "candidate": _summary_to_dict(evaluation.candidate),
        "evaluationVersion": evaluation.evaluation_version,
        "reasons": list(evaluation.reasons),
        "status": evaluation.status,
    }


def build_app_shell_optimization_report(
    *,
    runtime_sha256: str,
    measured_adapter_tree_sha256: str,
    app_shell_version: str,
    trials: Sequence[AppShellOptimizationTrial],
) -> AppShellOptimizationReport:
    _digest(runtime_sha256, "runtimeSha256")
    _digest(measured_adapter_tree_sha256, "measuredAdapterTreeSha256")
    if not isinstance(app_shell_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", app_shell_version):
        raise RuntimeOptimizationError("appShellVersion must be semantic numeric text")
    normalized = tuple(trials)
    evaluation = evaluate_app_shell_optimization(normalized)
    return AppShellOptimizationReport(
        report_version=APP_SHELL_OPTIMIZATION_REPORT_VERSION,
        runtime_sha256=runtime_sha256,
        measured_adapter_tree_sha256=measured_adapter_tree_sha256,
        app_shell_version=app_shell_version,
        preference_name=GC_EMPTY_CHUNK_PREF,
        preference_source=GC_PREF_SOURCE,
        preference_source_sha256=GC_PREF_SOURCE_SHA256,
        trials=normalized,
        evaluation=evaluation,
    )


def app_shell_optimization_report_to_dict(
    report: AppShellOptimizationReport,
) -> dict[str, Any]:
    if not isinstance(report, AppShellOptimizationReport):
        raise RuntimeOptimizationError("optimization report authority was fabricated")
    if report.report_version != APP_SHELL_OPTIMIZATION_REPORT_VERSION:
        raise RuntimeOptimizationError(
            f"optimizationReportVersion must be {APP_SHELL_OPTIMIZATION_REPORT_VERSION}"
        )
    if report.preference_name != GC_EMPTY_CHUNK_PREF:
        raise RuntimeOptimizationError("optimization preference name is invalid")
    if report.preference_source != GC_PREF_SOURCE:
        raise RuntimeOptimizationError("optimization preference source is invalid")
    if report.preference_source_sha256 != GC_PREF_SOURCE_SHA256:
        raise RuntimeOptimizationError("optimization preference source digest is invalid")
    expected = build_app_shell_optimization_report(
        runtime_sha256=_digest(report.runtime_sha256, "runtimeSha256"),
        measured_adapter_tree_sha256=_digest(
            report.measured_adapter_tree_sha256, "measuredAdapterTreeSha256"
        ),
        app_shell_version=report.app_shell_version,
        trials=report.trials,
    )
    if report != expected:
        raise RuntimeOptimizationError("optimization report does not match its trials")
    data = {
        "appShellVersion": report.app_shell_version,
        "evaluation": _evaluation_to_dict(report.evaluation),
        "measuredAdapterTreeSha256": report.measured_adapter_tree_sha256,
        "optimizationReportVersion": report.report_version,
        "preference": {
            "baselineValue": GC_EMPTY_CHUNK_BASELINE,
            "candidateValue": GC_EMPTY_CHUNK_CANDIDATE,
            "name": report.preference_name,
            "source": report.preference_source,
            "sourceSha256": report.preference_source_sha256,
        },
        "runtimeSha256": report.runtime_sha256,
        "trials": [_trial_to_dict(trial) for trial in report.trials],
    }
    _assert_privacy_safe_keys(data)
    return data


def _assert_privacy_safe_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = key.lower()
            if any(forbidden in lowered for forbidden in _FORBIDDEN_PRIVACY_KEYS):
                raise RuntimeOptimizationError(
                    f"optimization report contains forbidden privacy key {key}"
                )
            _assert_privacy_safe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_privacy_safe_keys(child)


def render_app_shell_optimization_report(report: AppShellOptimizationReport) -> str:
    return _canonical_json(app_shell_optimization_report_to_dict(report))


def app_shell_optimization_report_sha256(report: AppShellOptimizationReport) -> str:
    return hashlib.sha256(render_app_shell_optimization_report(report).encode("ascii")).hexdigest()


def app_shell_optimization_report_from_dict(value: Any) -> AppShellOptimizationReport:
    data = _mapping(value, "optimization report")
    _exact_keys(
        data,
        frozenset(
            {
                "appShellVersion",
                "evaluation",
                "measuredAdapterTreeSha256",
                "optimizationReportVersion",
                "preference",
                "runtimeSha256",
                "trials",
            }
        ),
        "optimization report",
    )
    version = _integer(data["optimizationReportVersion"], "optimizationReportVersion", minimum=1)
    if version != APP_SHELL_OPTIMIZATION_REPORT_VERSION:
        raise RuntimeOptimizationError(
            f"optimizationReportVersion must be {APP_SHELL_OPTIMIZATION_REPORT_VERSION}"
        )
    preference = _mapping(data["preference"], "optimization preference")
    _exact_keys(
        preference,
        frozenset({"baselineValue", "candidateValue", "name", "source", "sourceSha256"}),
        "optimization preference",
    )
    if preference != {
        "baselineValue": GC_EMPTY_CHUNK_BASELINE,
        "candidateValue": GC_EMPTY_CHUNK_CANDIDATE,
        "name": GC_EMPTY_CHUNK_PREF,
        "source": GC_PREF_SOURCE,
        "sourceSha256": GC_PREF_SOURCE_SHA256,
    }:
        raise RuntimeOptimizationError("optimization preference authority does not match")
    raw_trials = data["trials"]
    if not isinstance(raw_trials, list):
        raise RuntimeOptimizationError("optimization trials must be an array")
    trials = tuple(_trial_from_dict(item, index) for index, item in enumerate(raw_trials))
    report = build_app_shell_optimization_report(
        runtime_sha256=_digest(data["runtimeSha256"], "runtimeSha256"),
        measured_adapter_tree_sha256=_digest(
            data["measuredAdapterTreeSha256"], "measuredAdapterTreeSha256"
        ),
        app_shell_version=data["appShellVersion"],
        trials=trials,
    )
    rendered = app_shell_optimization_report_to_dict(report)
    if data != rendered:
        raise RuntimeOptimizationError("optimization report evaluation or summary was tampered")
    return report
