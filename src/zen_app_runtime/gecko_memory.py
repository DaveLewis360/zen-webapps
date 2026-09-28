"""Privacy-safe coarse Gecko process-role memory attribution.

Whole-scope summed PSS from :mod:`benchmark_runner` remains authoritative.  A
closed role document binds every sampled PID incarnation to one coarse role;
it cannot omit a process or alter memory.  The persistable attribution contains
only role aggregates and cryptographic bindings, never process IDs, names,
command lines, environments, maps, origins, URLs, or content-derived labels.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from .benchmark_runner import (
    BenchmarkRunnerError,
    CgroupSample,
    ProcessMemorySample,
    cgroup_sample_to_dict,
    render_cgroup_sample,
)
from .discord_conformance import (
    DiscordConformanceSuite,
    discord_conformance_suite_to_dict,
)
from .runtime_driver import (
    RuntimeDriverDescriptor,
    RuntimeDriverError,
    runtime_driver_descriptor_sha256,
    runtime_driver_descriptor_to_dict,
)

GECKO_PROCESS_ROLE_EVIDENCE_VERSION = 1
GECKO_MEMORY_ATTRIBUTION_VERSION = 1
GECKO_PROCESS_ROLE_SOURCE = "runtime-driver-privileged-process-info-v1"
GECKO_MEMORY_ROLES = (
    "runtime-supervisor",
    "browser-parent",
    "web-content",
    "gpu",
    "network",
    "media",
    "utility",
    "extension",
    "preallocated",
    "sandbox-broker",
    "unknown",
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROLE_SET = frozenset(GECKO_MEMORY_ROLES)
_ATTRIBUTION_STATUSES = frozenset({"complete", "partial"})


class GeckoMemoryError(ValueError):
    """Gecko role evidence or aggregate memory attribution failed closed."""


@dataclass(frozen=True)
class GeckoProcessRole:
    pid: int
    start_time_ticks: int
    role: str


@dataclass(frozen=True)
class GeckoProcessRoleEvidence:
    evidence_version: int
    cgroup_sample_sha256: str
    driver_descriptor_sha256: str
    source: str
    processes: tuple[GeckoProcessRole, ...]


@dataclass(frozen=True)
class GeckoRoleMemory:
    role: str
    process_count: int
    rss_kib: int
    pss_kib: int
    private_kib: int
    swap_pss_kib: int
    seccomp_filtered_count: int
    no_new_privs_count: int


@dataclass(frozen=True)
class GeckoMemoryAttribution:
    attribution_version: int
    cgroup_sample_sha256: str
    role_evidence_sha256: str
    driver_descriptor_sha256: str
    status: str
    unknown_process_count: int
    memory_current_bytes: int
    memory_peak_bytes: int
    memory_swap_current_bytes: int
    pids_current: int
    process_count: int
    rss_kib: int
    pss_kib: int
    private_kib: int
    swap_pss_kib: int
    seccomp_filtered_count: int
    no_new_privs_count: int
    roles: tuple[GeckoRoleMemory, ...]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise GeckoMemoryError(f"{label} must be an object with text keys")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise GeckoMemoryError(f"{label} must be an array")
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
        raise GeckoMemoryError(f"{label} fields: {', '.join(details)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise GeckoMemoryError(f"{label} must be an integer")
    if value < minimum:
        raise GeckoMemoryError(f"{label} must be at least {minimum}")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise GeckoMemoryError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _role(value: Any, label: str) -> str:
    if not isinstance(value, str) or value not in _ROLE_SET:
        raise GeckoMemoryError(f"{label} has an unknown role")
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
        raise GeckoMemoryError(f"document is not canonical JSON data: {error}") from error


def _validate_sample(sample: CgroupSample) -> None:
    try:
        cgroup_sample_to_dict(sample)
    except BenchmarkRunnerError as error:
        raise GeckoMemoryError(f"invalid cgroup sample: {error}") from error


def cgroup_sample_sha256(sample: CgroupSample) -> str:
    """Hash the complete canonical cgroup sample used as attribution input."""
    try:
        rendered = render_cgroup_sample(sample)
    except BenchmarkRunnerError as error:
        raise GeckoMemoryError(f"invalid cgroup sample: {error}") from error
    return hashlib.sha256(rendered.encode("ascii")).hexdigest()


def _driver_sha256(
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> str:
    try:
        discord_conformance_suite_to_dict(suite)
        runtime_driver_descriptor_to_dict(driver, suite=suite)
        if driver.engine_family != "gecko":
            raise GeckoMemoryError("memory attribution requires a Gecko driver")
        return runtime_driver_descriptor_sha256(driver, suite=suite)
    except RuntimeDriverError as error:
        raise GeckoMemoryError(f"invalid Runtime Driver descriptor: {error}") from error


def _validate_role_mapping(
    sample: CgroupSample,
    roles: Mapping[int, str],
) -> tuple[GeckoProcessRole, ...]:
    if not isinstance(roles, Mapping):
        raise GeckoMemoryError("Gecko process roles must be an explicit mapping")
    if not all(type(pid) is int for pid in roles):
        raise GeckoMemoryError("Gecko process role keys must be integer PIDs")
    expected_pids = {process.pid for process in sample.processes}
    if set(roles) != expected_pids:
        raise GeckoMemoryError("Gecko process role PIDs must exactly match the cgroup sample")
    processes = tuple(
        GeckoProcessRole(
            pid=process.pid,
            start_time_ticks=process.start_time_ticks,
            role=_role(roles[process.pid], f"Gecko process {process.pid}"),
        )
        for process in sample.processes
    )
    if sum(item.role == "browser-parent" for item in processes) != 1:
        raise GeckoMemoryError("Gecko role evidence requires exactly one browser-parent")
    return processes


def build_gecko_process_role_evidence(
    sample: CgroupSample,
    roles: Mapping[int, str],
    *,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> GeckoProcessRoleEvidence:
    """Bind coarse privileged process roles to one stable cgroup sample."""
    _validate_sample(sample)
    evidence = GeckoProcessRoleEvidence(
        evidence_version=GECKO_PROCESS_ROLE_EVIDENCE_VERSION,
        cgroup_sample_sha256=cgroup_sample_sha256(sample),
        driver_descriptor_sha256=_driver_sha256(driver, suite),
        source=GECKO_PROCESS_ROLE_SOURCE,
        processes=_validate_role_mapping(sample, roles),
    )
    gecko_process_role_evidence_to_dict(
        evidence,
        sample=sample,
        driver=driver,
        suite=suite,
    )
    return evidence


def _process_role_to_dict(process: GeckoProcessRole) -> dict[str, Any]:
    return {
        "pid": process.pid,
        "role": process.role,
        "startTimeTicks": process.start_time_ticks,
    }


def _evidence_to_dict_unchecked(
    evidence: GeckoProcessRoleEvidence,
) -> dict[str, Any]:
    return {
        "cgroupSampleSha256": evidence.cgroup_sample_sha256,
        "driverDescriptorSha256": evidence.driver_descriptor_sha256,
        "geckoProcessRoleEvidenceVersion": evidence.evidence_version,
        "processes": [_process_role_to_dict(process) for process in evidence.processes],
        "source": evidence.source,
    }


def gecko_process_role_evidence_from_dict(
    value: Any,
    *,
    sample: CgroupSample,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> GeckoProcessRoleEvidence:
    _validate_sample(sample)
    data = _mapping(value, "Gecko process role evidence")
    _exact_keys(
        data,
        frozenset(
            {
                "cgroupSampleSha256",
                "driverDescriptorSha256",
                "geckoProcessRoleEvidenceVersion",
                "processes",
                "source",
            }
        ),
        "Gecko process role evidence",
    )
    version = _integer(
        data["geckoProcessRoleEvidenceVersion"],
        "geckoProcessRoleEvidenceVersion",
        minimum=1,
    )
    if version != GECKO_PROCESS_ROLE_EVIDENCE_VERSION:
        raise GeckoMemoryError(
            f"geckoProcessRoleEvidenceVersion must be {GECKO_PROCESS_ROLE_EVIDENCE_VERSION}"
        )
    sample_digest = _digest(data["cgroupSampleSha256"], "evidence cgroupSampleSha256")
    if sample_digest != cgroup_sample_sha256(sample):
        raise GeckoMemoryError("role evidence is bound to a different cgroup sample")
    driver_digest = _digest(
        data["driverDescriptorSha256"], "evidence driverDescriptorSha256"
    )
    if driver_digest != _driver_sha256(driver, suite):
        raise GeckoMemoryError("role evidence is bound to a different driver")
    if data["source"] != GECKO_PROCESS_ROLE_SOURCE:
        raise GeckoMemoryError("Gecko process role evidence source is invalid")
    parsed_processes: list[GeckoProcessRole] = []
    for index, item in enumerate(_array(data["processes"], "evidence processes")):
        process_data = _mapping(item, f"evidence process {index}")
        _exact_keys(
            process_data,
            frozenset({"pid", "role", "startTimeTicks"}),
            f"evidence process {index}",
        )
        parsed_processes.append(
            GeckoProcessRole(
                pid=_integer(process_data["pid"], f"evidence process {index} pid", minimum=1),
                start_time_ticks=_integer(
                    process_data["startTimeTicks"],
                    f"evidence process {index} startTimeTicks",
                    minimum=1,
                ),
                role=_role(process_data["role"], f"evidence process {index}"),
            )
        )
    pids = [process.pid for process in parsed_processes]
    if pids != sorted(pids) or len(pids) != len(set(pids)):
        raise GeckoMemoryError("evidence process PIDs must be sorted and unique")
    expected = tuple(
        (process.pid, process.start_time_ticks) for process in sample.processes
    )
    actual = tuple(
        (process.pid, process.start_time_ticks) for process in parsed_processes
    )
    if actual != expected:
        raise GeckoMemoryError(
            "evidence process incarnation set must exactly match the cgroup sample"
        )
    if sum(process.role == "browser-parent" for process in parsed_processes) != 1:
        raise GeckoMemoryError("Gecko role evidence requires exactly one browser-parent")
    return GeckoProcessRoleEvidence(
        evidence_version=version,
        cgroup_sample_sha256=sample_digest,
        driver_descriptor_sha256=driver_digest,
        source=GECKO_PROCESS_ROLE_SOURCE,
        processes=tuple(parsed_processes),
    )


def gecko_process_role_evidence_to_dict(
    evidence: GeckoProcessRoleEvidence,
    *,
    sample: CgroupSample,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> dict[str, Any]:
    if not isinstance(evidence, GeckoProcessRoleEvidence):
        raise GeckoMemoryError("Gecko process role evidence authority was fabricated")
    data = _evidence_to_dict_unchecked(evidence)
    parsed = gecko_process_role_evidence_from_dict(
        data,
        sample=sample,
        driver=driver,
        suite=suite,
    )
    if parsed != evidence:
        raise GeckoMemoryError("Gecko process role evidence changed during validation")
    return data


def render_gecko_process_role_evidence(
    evidence: GeckoProcessRoleEvidence,
    *,
    sample: CgroupSample,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> str:
    return _canonical_json(
        gecko_process_role_evidence_to_dict(
            evidence,
            sample=sample,
            driver=driver,
            suite=suite,
        )
    )


def gecko_process_role_evidence_sha256(
    evidence: GeckoProcessRoleEvidence,
    *,
    sample: CgroupSample,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> str:
    return hashlib.sha256(
        render_gecko_process_role_evidence(
            evidence,
            sample=sample,
            driver=driver,
            suite=suite,
        ).encode("ascii")
    ).hexdigest()


def _role_memory(
    role: str,
    processes: tuple[ProcessMemorySample, ...],
) -> GeckoRoleMemory:
    return GeckoRoleMemory(
        role=role,
        process_count=len(processes),
        rss_kib=sum(process.rss_kib for process in processes),
        pss_kib=sum(process.pss_kib for process in processes),
        private_kib=sum(process.private_kib for process in processes),
        swap_pss_kib=sum(process.swap_pss_kib for process in processes),
        seccomp_filtered_count=sum(process.seccomp == 2 for process in processes),
        no_new_privs_count=sum(process.no_new_privs == 1 for process in processes),
    )


def _compute_attribution(
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    *,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> GeckoMemoryAttribution:
    gecko_process_role_evidence_to_dict(
        evidence,
        sample=sample,
        driver=driver,
        suite=suite,
    )
    role_by_pid = {process.pid: process.role for process in evidence.processes}
    grouped: dict[str, list[ProcessMemorySample]] = {
        role: [] for role in GECKO_MEMORY_ROLES
    }
    for process in sample.processes:
        grouped[role_by_pid[process.pid]].append(process)
    roles = tuple(
        _role_memory(role, tuple(grouped[role])) for role in GECKO_MEMORY_ROLES
    )
    unknown_count = next(
        item.process_count for item in roles if item.role == "unknown"
    )
    attribution = GeckoMemoryAttribution(
        attribution_version=GECKO_MEMORY_ATTRIBUTION_VERSION,
        cgroup_sample_sha256=cgroup_sample_sha256(sample),
        role_evidence_sha256=gecko_process_role_evidence_sha256(
            evidence,
            sample=sample,
            driver=driver,
            suite=suite,
        ),
        driver_descriptor_sha256=_driver_sha256(driver, suite),
        status="partial" if unknown_count else "complete",
        unknown_process_count=unknown_count,
        memory_current_bytes=sample.memory_current_bytes,
        memory_peak_bytes=sample.memory_peak_bytes,
        memory_swap_current_bytes=sample.memory_swap_current_bytes,
        pids_current=sample.pids_current,
        process_count=len(sample.processes),
        rss_kib=sample.summed_rss_kib,
        pss_kib=sample.summed_pss_kib,
        private_kib=sample.summed_private_kib,
        swap_pss_kib=sample.summed_swap_pss_kib,
        seccomp_filtered_count=sum(process.seccomp == 2 for process in sample.processes),
        no_new_privs_count=sum(
            process.no_new_privs == 1 for process in sample.processes
        ),
        roles=roles,
    )
    if sum(item.process_count for item in roles) != attribution.process_count:
        raise GeckoMemoryError("role process counts do not reconcile with the cgroup sample")
    for field in ("rss_kib", "pss_kib", "private_kib", "swap_pss_kib"):
        if sum(getattr(item, field) for item in roles) != getattr(attribution, field):
            raise GeckoMemoryError(
                "role memory does not reconcile with authoritative summed process memory"
            )
    return attribution


def attribute_gecko_memory(
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    *,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> GeckoMemoryAttribution:
    """Aggregate one exact sample; this never changes authoritative total PSS."""
    return _compute_attribution(
        sample,
        evidence,
        driver=driver,
        suite=suite,
    )


def _role_memory_to_dict(role: GeckoRoleMemory) -> dict[str, Any]:
    return {
        "noNewPrivsCount": role.no_new_privs_count,
        "privateKiB": role.private_kib,
        "processCount": role.process_count,
        "pssKiB": role.pss_kib,
        "role": role.role,
        "rssKiB": role.rss_kib,
        "seccompFilteredCount": role.seccomp_filtered_count,
        "swapPssKiB": role.swap_pss_kib,
    }


def _attribution_to_dict_unchecked(
    attribution: GeckoMemoryAttribution,
) -> dict[str, Any]:
    return {
        "bindings": {
            "cgroupSampleSha256": attribution.cgroup_sample_sha256,
            "driverDescriptorSha256": attribution.driver_descriptor_sha256,
            "roleEvidenceSha256": attribution.role_evidence_sha256,
        },
        "geckoMemoryAttributionVersion": attribution.attribution_version,
        "roles": [_role_memory_to_dict(role) for role in attribution.roles],
        "status": attribution.status,
        "summedProcessMemory": {
            "noNewPrivsCount": attribution.no_new_privs_count,
            "privateKiB": attribution.private_kib,
            "processCount": attribution.process_count,
            "pssKiB": attribution.pss_kib,
            "rssKiB": attribution.rss_kib,
            "seccompFilteredCount": attribution.seccomp_filtered_count,
            "swapPssKiB": attribution.swap_pss_kib,
        },
        "unknownProcessCount": attribution.unknown_process_count,
        "wholeCgroup": {
            "memoryCurrentBytes": attribution.memory_current_bytes,
            "memoryPeakBytes": attribution.memory_peak_bytes,
            "memorySwapCurrentBytes": attribution.memory_swap_current_bytes,
            "pidsCurrent": attribution.pids_current,
        },
    }


def gecko_memory_attribution_from_dict(
    value: Any,
    *,
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> GeckoMemoryAttribution:
    data = _mapping(value, "Gecko memory attribution")
    _exact_keys(
        data,
        frozenset(
            {
                "bindings",
                "geckoMemoryAttributionVersion",
                "roles",
                "status",
                "summedProcessMemory",
                "unknownProcessCount",
                "wholeCgroup",
            }
        ),
        "Gecko memory attribution",
    )
    version = _integer(
        data["geckoMemoryAttributionVersion"],
        "geckoMemoryAttributionVersion",
        minimum=1,
    )
    if version != GECKO_MEMORY_ATTRIBUTION_VERSION:
        raise GeckoMemoryError(
            f"geckoMemoryAttributionVersion must be {GECKO_MEMORY_ATTRIBUTION_VERSION}"
        )
    status = data["status"]
    if status not in _ATTRIBUTION_STATUSES:
        raise GeckoMemoryError("Gecko memory attribution status is invalid")
    raw_roles = _array(data["roles"], "Gecko memory attribution roles")
    role_order: list[str] = []
    for index, item in enumerate(raw_roles):
        role_data = _mapping(item, f"Gecko memory role {index}")
        _exact_keys(
            role_data,
            frozenset(
                {
                    "noNewPrivsCount",
                    "privateKiB",
                    "processCount",
                    "pssKiB",
                    "role",
                    "rssKiB",
                    "seccompFilteredCount",
                    "swapPssKiB",
                }
            ),
            f"Gecko memory role {index}",
        )
        role_order.append(_role(role_data["role"], f"Gecko memory role {index}"))
    if tuple(role_order) != GECKO_MEMORY_ROLES:
        raise GeckoMemoryError("Gecko memory attribution must use the exact role order")
    expected = _compute_attribution(
        sample,
        evidence,
        driver=driver,
        suite=suite,
    )
    expected_data = _attribution_to_dict_unchecked(expected)
    if data != expected_data:
        raise GeckoMemoryError(
            "Gecko memory attribution does not match its bound evidence"
        )
    return expected


def gecko_memory_attribution_to_dict(
    attribution: GeckoMemoryAttribution,
    *,
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> dict[str, Any]:
    if not isinstance(attribution, GeckoMemoryAttribution):
        raise GeckoMemoryError("Gecko memory attribution authority was fabricated")
    expected = _compute_attribution(
        sample,
        evidence,
        driver=driver,
        suite=suite,
    )
    if attribution != expected:
        raise GeckoMemoryError(
            "Gecko memory attribution does not match its bound evidence"
        )
    return _attribution_to_dict_unchecked(attribution)


def render_gecko_memory_attribution(
    attribution: GeckoMemoryAttribution,
    *,
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> str:
    return _canonical_json(
        gecko_memory_attribution_to_dict(
            attribution,
            sample=sample,
            evidence=evidence,
            driver=driver,
            suite=suite,
        )
    )


def gecko_memory_attribution_sha256(
    attribution: GeckoMemoryAttribution,
    *,
    sample: CgroupSample,
    evidence: GeckoProcessRoleEvidence,
    driver: RuntimeDriverDescriptor,
    suite: DiscordConformanceSuite,
) -> str:
    return hashlib.sha256(
        render_gecko_memory_attribution(
            attribution,
            sample=sample,
            evidence=evidence,
            driver=driver,
            suite=suite,
        ).encode("ascii")
    ).hexdigest()
