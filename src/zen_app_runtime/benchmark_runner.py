"""Cgroup-v2 workload containment and privacy-safe benchmark sampling.

The collector treats summed process PSS from ``smaps_rollup`` as the primary
memory metric and records cgroup-v2 memory/CPU/task counters as whole-workload
secondary evidence.  It never reads process command lines, environment, maps,
or URLs.  The optional launcher uses an explicit token array to create a
transient systemd user scope; it never invokes a shell.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import re
import stat
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from .benchmark_contract import ConfidenceInterval, MetricObservation

CGROUP_SAMPLE_VERSION = 1
SYSTEMD_SCOPE_PLAN_VERSION = 1
_MAX_CONTROL_FILE_BYTES = 256 * 1024
_MAX_PROC_FILE_BYTES = 4 * 1024 * 1024
_REQUIRED_CONTROLLERS = frozenset({"cpu", "memory", "pids"})
_REQUIRED_MEMORY_STAT = frozenset({"anon", "file", "kernel"})
_REQUIRED_CPU_STAT = frozenset({"usage_usec", "user_usec", "system_usec"})
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.]+$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SCOPE_PATTERN = re.compile(r"^zen-app-benchmark-[a-z0-9][a-z0-9-]{0,47}$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BenchmarkRunnerError(RuntimeError):
    """Cgroup containment or benchmark evidence was incomplete or unsafe."""


class _TransientSampleError(Exception):
    """A process set changed while one bounded sample was being collected."""


@dataclass(frozen=True)
class ProcessMemorySample:
    pid: int
    start_time_ticks: int
    state: str
    rss_kib: int
    pss_kib: int
    private_kib: int
    swap_pss_kib: int
    seccomp: int
    no_new_privs: int


@dataclass(frozen=True)
class CgroupSample:
    sample_version: int
    control_group: str
    started_monotonic_ns: int
    completed_monotonic_ns: int
    memory_current_bytes: int
    memory_peak_bytes: int
    memory_swap_current_bytes: int
    memory_stat: tuple[tuple[str, int], ...]
    cpu_stat: tuple[tuple[str, int], ...]
    pids_current: int
    processes: tuple[ProcessMemorySample, ...]

    @property
    def summed_rss_kib(self) -> int:
        return sum(process.rss_kib for process in self.processes)

    @property
    def summed_pss_kib(self) -> int:
        return sum(process.pss_kib for process in self.processes)

    @property
    def summed_private_kib(self) -> int:
        return sum(process.private_kib for process in self.processes)

    @property
    def summed_swap_pss_kib(self) -> int:
        return sum(process.swap_pss_kib for process in self.processes)


@dataclass(frozen=True)
class SystemdScopePlan:
    plan_version: int
    scope_name: str
    command: tuple[str, ...]
    working_directory: str
    cgroup_root: str
    proc_root: str
    systemd_run_path: str
    systemctl_path: str

    @property
    def unit_id(self) -> str:
        return f"{self.scope_name}.scope"


class CgroupV2Reader:
    """Retained-descriptor reader for one existing cgroup-v2 leaf."""

    def __init__(
        self,
        *,
        cgroup_root: str,
        control_group: str,
        leaf_fd: int,
        proc_root: str,
        proc_fd: int,
        monotonic_ns: Callable[[], int],
    ) -> None:
        self.cgroup_root = cgroup_root
        self.control_group = control_group
        self.proc_root = proc_root
        self._leaf_fd = leaf_fd
        self._proc_fd = proc_fd
        self._monotonic_ns = monotonic_ns
        self._closed = False

    @classmethod
    def open(
        cls,
        cgroup_root: os.PathLike[str] | str,
        control_group: str,
        *,
        proc_root: os.PathLike[str] | str = "/proc",
        monotonic_ns: Callable[[], int] | None = None,
    ) -> "CgroupV2Reader":
        root_path = _absolute_normalized_path(cgroup_root, "cgroup root")
        proc_path = _absolute_normalized_path(proc_root, "proc root")
        segments = _control_group_segments(control_group)
        root_fd = proc_fd = leaf_fd = -1
        try:
            root_fd = _open_absolute_directory(root_path, "cgroup root")
            controllers = frozenset(
                _read_text_at(
                    root_fd,
                    "cgroup.controllers",
                    _MAX_CONTROL_FILE_BYTES,
                    "cgroup controllers",
                ).split()
            )
            if not _REQUIRED_CONTROLLERS.issubset(controllers):
                missing = sorted(_REQUIRED_CONTROLLERS - controllers)
                raise BenchmarkRunnerError(
                    f"cgroup v2 root is missing required controllers {missing}"
                )
            leaf_fd = _open_descendant_directory(root_fd, segments, "control group")
            proc_fd = _open_absolute_directory(proc_path, "proc root")
            # Probe every required leaf file before returning retained authority.
            for name in (
                "cgroup.procs",
                "cpu.stat",
                "memory.current",
                "memory.peak",
                "memory.stat",
                "memory.swap.current",
                "pids.current",
            ):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=leaf_fd,
                )
                os.close(descriptor)
            reader = cls(
                cgroup_root=root_path,
                control_group=control_group,
                leaf_fd=leaf_fd,
                proc_root=proc_path,
                proc_fd=proc_fd,
                monotonic_ns=time.monotonic_ns if monotonic_ns is None else monotonic_ns,
            )
            # Ownership moves to ``reader`` only after every probe succeeds.
            leaf_fd = proc_fd = -1
            return reader
        except BenchmarkRunnerError:
            raise
        except OSError as error:
            raise BenchmarkRunnerError(
                f"cannot open cgroup-v2 benchmark authority: {error}"
            ) from error
        finally:
            for descriptor in (root_fd, leaf_fd, proc_fd):
                if descriptor >= 0:
                    os.close(descriptor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        leaf_fd, proc_fd = self._leaf_fd, self._proc_fd
        self._leaf_fd = self._proc_fd = -1
        errors: list[OSError] = []
        for descriptor in (leaf_fd, proc_fd):
            try:
                os.close(descriptor)
            except OSError as error:
                errors.append(error)
        if errors:
            raise BenchmarkRunnerError(f"cannot close benchmark reader: {errors[0]}")

    def __enter__(self) -> "CgroupV2Reader":
        if self._closed:
            raise BenchmarkRunnerError("cgroup reader is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise BenchmarkRunnerError("cgroup reader is closed")

    def _read_cgroup_file(self, name: str, maximum: int) -> str:
        self._require_open()
        try:
            return _read_text_at(self._leaf_fd, name, maximum, f"cgroup {name}")
        except OSError as error:
            raise BenchmarkRunnerError(f"cannot read cgroup {name}: {error}") from error

    def _read_process_file(self, pid: int, name: str, maximum: int) -> str:
        self._require_open()
        process_fd = -1
        try:
            process_fd = os.open(
                str(pid),
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._proc_fd,
            )
            return _read_text_at(process_fd, name, maximum, f"process {pid} {name}")
        except FileNotFoundError as error:
            raise _TransientSampleError(f"process {pid} disappeared") from error
        except ProcessLookupError as error:
            raise _TransientSampleError(f"process {pid} disappeared") from error
        except PermissionError as error:
            raise BenchmarkRunnerError(
                f"process {pid} {name} is not readable; PSS evidence is incomplete"
            ) from error
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ESRCH):
                raise _TransientSampleError(f"process {pid} disappeared") from error
            raise BenchmarkRunnerError(f"cannot read process {pid} {name}: {error}") from error
        finally:
            if process_fd >= 0:
                os.close(process_fd)

    def _read_process(self, pid: int) -> ProcessMemorySample:
        first_stat = self._read_process_file(pid, "stat", _MAX_PROC_FILE_BYTES)
        first_state, first_start = _parse_proc_stat(first_stat, pid)
        if first_state == "Z":
            raise _TransientSampleError(f"process {pid} became a zombie")
        status = _parse_proc_status(
            self._read_process_file(pid, "status", _MAX_PROC_FILE_BYTES),
            pid,
        )
        memory = _parse_smaps_rollup(
            self._read_process_file(pid, "smaps_rollup", _MAX_PROC_FILE_BYTES),
            pid,
        )
        final_stat = self._read_process_file(pid, "stat", _MAX_PROC_FILE_BYTES)
        final_state, final_start = _parse_proc_stat(final_stat, pid)
        if first_start != final_start:
            raise BenchmarkRunnerError(f"process {pid} identity changed while sampling")
        if final_state == "Z":
            raise _TransientSampleError(f"process {pid} became a zombie")
        return ProcessMemorySample(
            pid=pid,
            start_time_ticks=first_start,
            state=final_state,
            rss_kib=memory["Rss"],
            pss_kib=memory["Pss"],
            private_kib=(
                memory["Private_Clean"]
                + memory["Private_Dirty"]
                + memory["Private_Hugetlb"]
            ),
            swap_pss_kib=memory["SwapPss"],
            seccomp=status["Seccomp"],
            no_new_privs=status["NoNewPrivs"],
        )

    def sample(self, *, max_attempts: int = 3) -> CgroupSample:
        """Collect one stable, complete process/cgroup snapshot."""
        self._require_open()
        if type(max_attempts) is not int or max_attempts < 1 or max_attempts > 100:
            raise BenchmarkRunnerError("max_attempts must be an integer from 1 through 100")
        last_transient: str | None = None
        for _attempt in range(max_attempts):
            try:
                started = _nonnegative_integer(
                    self._monotonic_ns(), "sample monotonic start"
                )
                before = _parse_pid_list(
                    self._read_cgroup_file("cgroup.procs", _MAX_CONTROL_FILE_BYTES)
                )
                memory_current = _parse_scalar(
                    self._read_cgroup_file("memory.current", _MAX_CONTROL_FILE_BYTES),
                    "memory.current",
                )
                memory_peak = _parse_scalar(
                    self._read_cgroup_file("memory.peak", _MAX_CONTROL_FILE_BYTES),
                    "memory.peak",
                )
                memory_swap = _parse_scalar(
                    self._read_cgroup_file(
                        "memory.swap.current", _MAX_CONTROL_FILE_BYTES
                    ),
                    "memory.swap.current",
                )
                memory_stat = _parse_key_value_stat(
                    self._read_cgroup_file("memory.stat", _MAX_CONTROL_FILE_BYTES),
                    "memory.stat",
                    _REQUIRED_MEMORY_STAT,
                )
                cpu_stat = _parse_key_value_stat(
                    self._read_cgroup_file("cpu.stat", _MAX_CONTROL_FILE_BYTES),
                    "cpu.stat",
                    _REQUIRED_CPU_STAT,
                )
                pids_current = _parse_scalar(
                    self._read_cgroup_file("pids.current", _MAX_CONTROL_FILE_BYTES),
                    "pids.current",
                )
                processes = tuple(self._read_process(pid) for pid in before)
                after = _parse_pid_list(
                    self._read_cgroup_file("cgroup.procs", _MAX_CONTROL_FILE_BYTES)
                )
                completed = _nonnegative_integer(
                    self._monotonic_ns(), "sample monotonic completion"
                )
                if before != after:
                    raise _TransientSampleError("cgroup process set changed")
                if completed < started:
                    raise BenchmarkRunnerError("monotonic clock regressed while sampling")
                sample = CgroupSample(
                    sample_version=CGROUP_SAMPLE_VERSION,
                    control_group=self.control_group,
                    started_monotonic_ns=started,
                    completed_monotonic_ns=completed,
                    memory_current_bytes=memory_current,
                    memory_peak_bytes=memory_peak,
                    memory_swap_current_bytes=memory_swap,
                    memory_stat=tuple(sorted(memory_stat.items())),
                    cpu_stat=tuple(sorted(cpu_stat.items())),
                    pids_current=pids_current,
                    processes=processes,
                )
                _validate_cgroup_sample(sample)
                return sample
            except _TransientSampleError as error:
                last_transient = str(error)
                continue
        detail = last_transient or "unknown process race"
        raise BenchmarkRunnerError(
            f"cannot obtain a stable complete cgroup sample after {max_attempts} attempts: {detail}"
        )


class RunningSystemdScope:
    """Own one launched transient scope and its retained cgroup reader."""

    def __init__(
        self,
        plan: SystemdScopePlan,
        environment: Mapping[str, str],
        process: Any,
        reader: CgroupV2Reader,
        scope_signaler: Callable[[SystemdScopePlan, Mapping[str, str], str], None],
    ) -> None:
        self.plan = plan
        self.control_group = reader.control_group
        self._environment = dict(environment)
        self._process = process
        self._reader = reader
        self._scope_signaler = scope_signaler
        self._closed = False

    def sample(self, *, max_attempts: int = 3) -> CgroupSample:
        if self._closed:
            raise BenchmarkRunnerError("running benchmark scope is closed")
        return self._reader.sample(max_attempts=max_attempts)

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise BenchmarkRunnerError("benchmark scope did not exit before timeout") from error

    def terminate(self, *, timeout: float = 5.0) -> int:
        if self._closed:
            raise BenchmarkRunnerError("running benchmark scope is closed")
        if self._process.poll() is None:
            try:
                self._scope_signaler(self.plan, self._environment, "TERM")
            except BenchmarkRunnerError:
                self._process.terminate()
            try:
                return_code = self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    self._scope_signaler(self.plan, self._environment, "KILL")
                except BenchmarkRunnerError:
                    self._process.kill()
                try:
                    return_code = self._process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    raise BenchmarkRunnerError(
                        "benchmark scope survived TERM and KILL"
                    ) from error
        else:
            return_code = self._process.poll()
        self.close()
        if return_code is None:
            raise BenchmarkRunnerError("benchmark scope has no terminal status")
        return int(return_code)

    def close(self) -> None:
        if self._closed:
            return
        if self._process.poll() is None:
            self.terminate()
            return
        self._closed = True
        self._reader.close()
        self._environment.clear()

    def __enter__(self) -> "RunningSystemdScope":
        if self._closed:
            raise BenchmarkRunnerError("running benchmark scope is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _absolute_normalized_path(
    value: os.PathLike[str] | str,
    label: str,
) -> str:
    if isinstance(value, bytes):
        raise BenchmarkRunnerError(f"{label} must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise BenchmarkRunnerError(f"{label} must be path-like text") from error
    if not isinstance(rendered, str):
        raise BenchmarkRunnerError(f"{label} must be text")
    if not Path(rendered).is_absolute():
        raise BenchmarkRunnerError(f"{label} must be absolute")
    if os.path.normpath(rendered) != rendered:
        raise BenchmarkRunnerError(f"{label} must be normalized")
    if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
        raise BenchmarkRunnerError(f"{label} cannot contain control characters")
    return rendered


def _control_group_segments(control_group: Any) -> tuple[str, ...]:
    if not isinstance(control_group, str) or not control_group.startswith("/"):
        raise BenchmarkRunnerError("control group must be an absolute cgroup path")
    if len(control_group) > 4096 or control_group == "/":
        raise BenchmarkRunnerError("control group must name one bounded leaf")
    if any(ord(character) < 32 or ord(character) == 127 for character in control_group):
        raise BenchmarkRunnerError("control group cannot contain control characters")
    segments = tuple(control_group[1:].split("/"))
    if any(not segment or segment in (".", "..") for segment in segments):
        raise BenchmarkRunnerError("control group contains an unsafe segment")
    return segments


def _open_absolute_directory(path: str, label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current = os.open("/", flags)
    try:
        for segment in Path(path).parts[1:]:
            following = os.open(segment, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except OSError as error:
        os.close(current)
        raise BenchmarkRunnerError(f"cannot open {label} without symlinks: {error}") from error


def _open_descendant_directory(root_fd: int, segments: Sequence[str], label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current = os.dup(root_fd)
    try:
        for segment in segments:
            following = os.open(segment, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except OSError as error:
        os.close(current)
        raise BenchmarkRunnerError(f"cannot open {label} without symlinks: {error}") from error


def _read_text_at(directory_fd: int, name: str, maximum: int, label: str) -> str:
    if "/" in name or name in ("", ".", ".."):
        raise BenchmarkRunnerError(f"{label} filename is invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        dir_fd=directory_fd,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BenchmarkRunnerError(f"{label} must be a regular kernel file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise BenchmarkRunnerError(f"{label} exceeds the size limit")
        try:
            return b"".join(chunks).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise BenchmarkRunnerError(f"{label} is not UTF-8") from error
    finally:
        os.close(descriptor)


def _nonnegative_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise BenchmarkRunnerError(f"{label} must be a non-negative integer")
    return value


def _parse_scalar(text: str, label: str) -> int:
    stripped = text.strip()
    if not stripped or not stripped.isascii() or not stripped.isdigit():
        raise BenchmarkRunnerError(f"{label} must contain one non-negative integer")
    return int(stripped)


def _parse_pid_list(text: str) -> tuple[int, ...]:
    pids: list[int] = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped or not stripped.isascii() or not stripped.isdigit():
            raise BenchmarkRunnerError(f"cgroup.procs line {index + 1} is invalid")
        pid = int(stripped)
        if pid <= 0:
            raise BenchmarkRunnerError("cgroup.procs contains a non-positive PID")
        pids.append(pid)
    if len(pids) != len(set(pids)):
        raise BenchmarkRunnerError("cgroup.procs contains a duplicate PID")
    return tuple(sorted(pids))


def _parse_key_value_stat(
    text: str,
    label: str,
    required: frozenset[str],
) -> dict[str, int]:
    values: dict[str, int] = {}
    for index, line in enumerate(text.splitlines()):
        fields = line.split()
        if len(fields) != 2 or not _KEY_PATTERN.fullmatch(fields[0]):
            raise BenchmarkRunnerError(f"{label} line {index + 1} is invalid")
        key, raw_value = fields
        if key in values:
            raise BenchmarkRunnerError(f"{label} contains duplicate key {key}")
        if not raw_value.isascii() or not raw_value.isdigit():
            raise BenchmarkRunnerError(f"{label} value for {key} is invalid")
        values[key] = int(raw_value)
    missing = sorted(required - values.keys())
    if missing:
        raise BenchmarkRunnerError(f"{label} is missing required keys {missing}")
    return values


def _parse_proc_stat(text: str, pid: int) -> tuple[str, int]:
    closing = text.rfind(")")
    opening = text.find("(")
    if opening <= 0 or closing <= opening:
        raise BenchmarkRunnerError(f"process {pid} stat is malformed")
    prefix = text[:opening].strip()
    if prefix != str(pid):
        raise BenchmarkRunnerError(f"process {pid} stat PID does not match")
    fields = text[closing + 1 :].strip().split()
    if len(fields) <= 19 or len(fields[0]) != 1:
        raise BenchmarkRunnerError(f"process {pid} stat is incomplete")
    state = fields[0]
    try:
        start_time = int(fields[19])
    except ValueError as error:
        raise BenchmarkRunnerError(f"process {pid} start time is invalid") from error
    if start_time < 0:
        raise BenchmarkRunnerError(f"process {pid} start time is invalid")
    return state, start_time


def _parse_proc_status(text: str, pid: int) -> dict[str, int]:
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            raise BenchmarkRunnerError(f"process {pid} status line is malformed")
        key, value = line.split(":", 1)
        if key in raw:
            raise BenchmarkRunnerError(f"process {pid} status contains duplicate {key}")
        raw[key] = value.strip()
    result: dict[str, int] = {}
    for key in ("Seccomp", "NoNewPrivs"):
        if key not in raw or not raw[key].isascii() or not raw[key].isdigit():
            raise BenchmarkRunnerError(f"process {pid} status is missing valid {key}")
        result[key] = int(raw[key])
    if result["NoNewPrivs"] not in (0, 1):
        raise BenchmarkRunnerError(f"process {pid} NoNewPrivs is invalid")
    if result["Seccomp"] not in (0, 1, 2):
        raise BenchmarkRunnerError(f"process {pid} Seccomp is invalid")
    return result


def _parse_smaps_rollup(text: str, pid: int) -> dict[str, int]:
    required = {
        "Rss",
        "Pss",
        "Private_Clean",
        "Private_Dirty",
        "Private_Hugetlb",
        "SwapPss",
    }
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        if key not in required:
            continue
        if key in values:
            raise BenchmarkRunnerError(
                f"process {pid} smaps_rollup contains duplicate {key}"
            )
        fields = raw.split()
        if len(fields) != 2 or fields[1] != "kB" or not fields[0].isdigit():
            raise BenchmarkRunnerError(
                f"process {pid} smaps_rollup {key} is invalid"
            )
        values[key] = int(fields[0])
    missing = sorted(required - values.keys())
    if missing:
        raise BenchmarkRunnerError(
            f"process {pid} smaps_rollup is missing required fields {missing}"
        )
    return values


def _validate_stat_items(
    items: tuple[tuple[str, int], ...],
    label: str,
    required: frozenset[str],
) -> None:
    if not isinstance(items, tuple):
        raise BenchmarkRunnerError(f"{label} must be an immutable tuple")
    keys: list[str] = []
    for item in items:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not _KEY_PATTERN.fullmatch(item[0])
        ):
            raise BenchmarkRunnerError(f"{label} contains an invalid entry")
        _nonnegative_integer(item[1], f"{label} {item[0]}")
        keys.append(item[0])
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise BenchmarkRunnerError(f"{label} keys must be unique and sorted")
    missing = sorted(required - set(keys))
    if missing:
        raise BenchmarkRunnerError(f"{label} is missing required keys {missing}")


def _validate_process_sample(process: ProcessMemorySample) -> None:
    if type(process.pid) is not int or process.pid <= 0:
        raise BenchmarkRunnerError("process sample PID must be positive")
    _nonnegative_integer(process.start_time_ticks, "process startTimeTicks")
    if not isinstance(process.state, str) or len(process.state) != 1:
        raise BenchmarkRunnerError("process sample state is invalid")
    for label, value in (
        ("rssKiB", process.rss_kib),
        ("pssKiB", process.pss_kib),
        ("privateKiB", process.private_kib),
        ("swapPssKiB", process.swap_pss_kib),
    ):
        _nonnegative_integer(value, f"process {label}")
    if type(process.seccomp) is not int or process.seccomp not in (0, 1, 2):
        raise BenchmarkRunnerError("process sample seccomp is invalid")
    if type(process.no_new_privs) is not int or process.no_new_privs not in (0, 1):
        raise BenchmarkRunnerError("process sample noNewPrivs is invalid")


def _validate_cgroup_sample(sample: CgroupSample) -> None:
    if type(sample.sample_version) is not int or sample.sample_version != CGROUP_SAMPLE_VERSION:
        raise BenchmarkRunnerError(f"cgroupSampleVersion must be {CGROUP_SAMPLE_VERSION}")
    _control_group_segments(sample.control_group)
    started = _nonnegative_integer(sample.started_monotonic_ns, "startedMonotonicNs")
    completed = _nonnegative_integer(sample.completed_monotonic_ns, "completedMonotonicNs")
    if completed < started:
        raise BenchmarkRunnerError("sample completion cannot precede start")
    for label, value in (
        ("memoryCurrentBytes", sample.memory_current_bytes),
        ("memoryPeakBytes", sample.memory_peak_bytes),
        ("memorySwapCurrentBytes", sample.memory_swap_current_bytes),
        ("pidsCurrent", sample.pids_current),
    ):
        _nonnegative_integer(value, label)
    _validate_stat_items(sample.memory_stat, "memoryStat", _REQUIRED_MEMORY_STAT)
    _validate_stat_items(sample.cpu_stat, "cpuStat", _REQUIRED_CPU_STAT)
    pids: list[int] = []
    for process in sample.processes:
        _validate_process_sample(process)
        pids.append(process.pid)
    if pids != sorted(pids) or len(pids) != len(set(pids)):
        raise BenchmarkRunnerError("process samples must be PID-sorted and unique")


def cgroup_sample_to_dict(sample: CgroupSample) -> dict[str, Any]:
    _validate_cgroup_sample(sample)
    return {
        "cgroup": {
            "cpuStat": dict(sample.cpu_stat),
            "memoryCurrentBytes": sample.memory_current_bytes,
            "memoryPeakBytes": sample.memory_peak_bytes,
            "memoryStat": dict(sample.memory_stat),
            "memorySwapCurrentBytes": sample.memory_swap_current_bytes,
            "pidsCurrent": sample.pids_current,
        },
        "cgroupSampleVersion": sample.sample_version,
        "completedMonotonicNs": sample.completed_monotonic_ns,
        "controlGroup": sample.control_group,
        "processes": [
            {
                "noNewPrivs": process.no_new_privs,
                "pid": process.pid,
                "privateKiB": process.private_kib,
                "pssKiB": process.pss_kib,
                "rssKiB": process.rss_kib,
                "seccomp": process.seccomp,
                "startTimeTicks": process.start_time_ticks,
                "state": process.state,
                "swapPssKiB": process.swap_pss_kib,
            }
            for process in sample.processes
        ],
        "startedMonotonicNs": sample.started_monotonic_ns,
        "summedProcessMemory": {
            "privateKiB": sample.summed_private_kib,
            "pssKiB": sample.summed_pss_kib,
            "rssKiB": sample.summed_rss_kib,
            "swapPssKiB": sample.summed_swap_pss_kib,
        },
    }


def render_cgroup_sample(sample: CgroupSample) -> str:
    return json.dumps(
        cgroup_sample_to_dict(sample),
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _sample_midpoint_ns(sample: CgroupSample) -> int:
    return sample.started_monotonic_ns + (
        sample.completed_monotonic_ns - sample.started_monotonic_ns
    ) // 2


def cgroup_cpu_basis_points(before: CgroupSample, after: CgroupSample) -> int:
    """Return total-workload CPU where 100 basis points equal one CPU percent."""
    _validate_cgroup_sample(before)
    _validate_cgroup_sample(after)
    if before.control_group != after.control_group:
        raise BenchmarkRunnerError("CPU samples must come from the same control group")
    before_usage = dict(before.cpu_stat)["usage_usec"]
    after_usage = dict(after.cpu_stat)["usage_usec"]
    if after_usage < before_usage:
        raise BenchmarkRunnerError("cgroup CPU usage counter regressed")
    elapsed_ns = _sample_midpoint_ns(after) - _sample_midpoint_ns(before)
    if elapsed_ns <= 0:
        raise BenchmarkRunnerError("CPU sample clock did not advance")
    # usage_usec * 1000 converts to ns; multiplying by 10_000 yields basis
    # points of CPU percentage.  Round to nearest integer without floats.
    numerator = (after_usage - before_usage) * 10_000_000
    return (numerator + elapsed_ns // 2) // elapsed_ns


def _statistic(values: Sequence[int], aggregation: str) -> int:
    ordered = sorted(values)
    if aggregation == "median":
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle] + 1) // 2
    if aggregation == "p95":
        index = (95 * len(ordered) + 99) // 100 - 1
        return ordered[index]
    raise BenchmarkRunnerError("aggregation must be median or p95")


def _nearest_rank(values: Sequence[int], numerator: int, denominator: int) -> int:
    ordered = sorted(values)
    index = (numerator * len(ordered) + denominator - 1) // denominator - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def aggregate_integer_observation(
    metric_id: str,
    values: Sequence[int],
    *,
    aggregation: str,
) -> MetricObservation:
    """Create a deterministic non-parametric bootstrap observation.

    The seed is derived from the metric, aggregation, and complete integer
    sample vector.  This makes canonical evidence reproducible while keeping
    the resampling independent of process-global random state.
    """
    if not isinstance(metric_id, str) or not _IDENTIFIER_PATTERN.fullmatch(metric_id):
        raise BenchmarkRunnerError("metric_id must be a lowercase identifier")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
        raise BenchmarkRunnerError("observation values must be a non-empty sequence")
    normalized = tuple(_nonnegative_integer(value, "observation value") for value in values)
    point = _statistic(normalized, aggregation)
    if len(normalized) == 1:
        lower = upper = point
    else:
        seed_payload = json.dumps(
            {
                "aggregation": aggregation,
                "metricId": metric_id,
                "values": normalized,
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        seed = int.from_bytes(hashlib.sha256(seed_payload).digest(), "big")
        generator = random.Random(seed)
        bootstrap: list[int] = []
        resamples = 4096
        for _ in range(resamples):
            draw = tuple(
                normalized[generator.randrange(len(normalized))]
                for _item in normalized
            )
            bootstrap.append(_statistic(draw, aggregation))
        lower = min(point, _nearest_rank(bootstrap, 25, 1000))
        upper = max(point, _nearest_rank(bootstrap, 975, 1000))
    return MetricObservation(
        metric_id=metric_id,
        value=point,
        sample_count=len(normalized),
        confidence95=ConfidenceInterval(lower, upper),
    )


def _argument(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BenchmarkRunnerError(f"{label} must be non-empty text")
    if "\0" in value:
        raise BenchmarkRunnerError(f"{label} cannot contain NUL")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BenchmarkRunnerError(f"{label} cannot contain control characters")
    return value


def build_systemd_scope_plan(
    command: Sequence[str],
    *,
    scope_name: str,
    working_directory: os.PathLike[str] | str,
    cgroup_root: os.PathLike[str] | str = "/sys/fs/cgroup",
    proc_root: os.PathLike[str] | str = "/proc",
    systemd_run_path: os.PathLike[str] | str = "/usr/bin/systemd-run",
    systemctl_path: os.PathLike[str] | str = "/usr/bin/systemctl",
) -> SystemdScopePlan:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise BenchmarkRunnerError("scope command must be a non-empty token sequence")
    tokens = tuple(_argument(item, f"scope command token {index}") for index, item in enumerate(command))
    if not Path(tokens[0]).is_absolute():
        raise BenchmarkRunnerError("scope command executable must be absolute")
    if not isinstance(scope_name, str) or not _SCOPE_PATTERN.fullmatch(scope_name):
        raise BenchmarkRunnerError(
            "scope_name must use the reserved zen-app-benchmark-* grammar"
        )
    return SystemdScopePlan(
        plan_version=SYSTEMD_SCOPE_PLAN_VERSION,
        scope_name=scope_name,
        command=tokens,
        working_directory=_absolute_normalized_path(
            working_directory, "scope working directory"
        ),
        cgroup_root=_absolute_normalized_path(cgroup_root, "scope cgroup root"),
        proc_root=_absolute_normalized_path(proc_root, "scope proc root"),
        systemd_run_path=_absolute_normalized_path(
            systemd_run_path, "systemd-run executable"
        ),
        systemctl_path=_absolute_normalized_path(
            systemctl_path, "systemctl executable"
        ),
    )


def _validate_scope_plan(plan: SystemdScopePlan) -> None:
    if type(plan.plan_version) is not int or plan.plan_version != SYSTEMD_SCOPE_PLAN_VERSION:
        raise BenchmarkRunnerError(
            f"systemd scope plan version must be {SYSTEMD_SCOPE_PLAN_VERSION}"
        )
    expected = build_systemd_scope_plan(
        plan.command,
        scope_name=plan.scope_name,
        working_directory=plan.working_directory,
        cgroup_root=plan.cgroup_root,
        proc_root=plan.proc_root,
        systemd_run_path=plan.systemd_run_path,
        systemctl_path=plan.systemctl_path,
    )
    if plan != expected:
        raise BenchmarkRunnerError("systemd scope plan is not canonical")


def systemd_scope_argv(plan: SystemdScopePlan) -> tuple[str, ...]:
    _validate_scope_plan(plan)
    return (
        plan.systemd_run_path,
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--no-ask-password",
        "--expand-environment=no",
        "--slice=app.slice",
        f"--unit={plan.unit_id}",
        "--property=MemoryAccounting=yes",
        "--property=TasksAccounting=yes",
        "--same-dir",
        "--",
        *plan.command,
    )


def _validated_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise BenchmarkRunnerError("scope environment must be an explicit mapping")
    result: dict[str, str] = {}
    for name, value in environment.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise BenchmarkRunnerError("scope environment contains an invalid name")
        if not isinstance(value, str) or "\0" in value:
            raise BenchmarkRunnerError(
                f"scope environment value for {name} must be NUL-free text"
            )
        result[name] = value
    return result


def _default_control_group_resolver(
    plan: SystemdScopePlan,
    environment: Mapping[str, str],
) -> str | None:
    try:
        completed = subprocess.run(
            (
                plan.systemctl_path,
                "--user",
                "show",
                "--property=ControlGroup",
                "--value",
                "--no-pager",
                plan.unit_id,
            ),
            cwd=plan.working_directory,
            env=dict(environment),
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkRunnerError(f"cannot query benchmark scope cgroup: {error}") from error
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _default_scope_signaler(
    plan: SystemdScopePlan,
    environment: Mapping[str, str],
    signal_name: str,
) -> None:
    if signal_name not in ("TERM", "KILL"):
        raise BenchmarkRunnerError("benchmark scope signal is invalid")
    try:
        completed = subprocess.run(
            (
                plan.systemctl_path,
                "--user",
                "kill",
                "--kill-whom=all",
                f"--signal={signal_name}",
                plan.unit_id,
            ),
            cwd=plan.working_directory,
            env=dict(environment),
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkRunnerError(f"cannot signal benchmark scope: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise BenchmarkRunnerError(f"cannot signal benchmark scope: {detail}")


def _cleanup_failed_launch(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        pass


def launch_systemd_scope(
    plan: SystemdScopePlan,
    environment: Mapping[str, str],
    *,
    discovery_timeout: float = 5.0,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    control_group_resolver: Callable[
        [SystemdScopePlan, Mapping[str, str]], str | None
    ] = _default_control_group_resolver,
    scope_signaler: Callable[
        [SystemdScopePlan, Mapping[str, str], str], None
    ] = _default_scope_signaler,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> RunningSystemdScope:
    """Launch a command in an isolated transient user scope and retain its reader."""
    _validate_scope_plan(plan)
    if not isinstance(discovery_timeout, (int, float)) or isinstance(discovery_timeout, bool) or discovery_timeout <= 0:
        raise BenchmarkRunnerError("discovery_timeout must be positive")
    env = _validated_environment(environment)
    try:
        process = popen_factory(
            systemd_scope_argv(plan),
            cwd=plan.working_directory,
            env=env,
            shell=False,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkRunnerError(f"cannot launch benchmark scope: {error}") from error

    deadline = monotonic() + float(discovery_timeout)
    try:
        control_group: str | None = None
        while monotonic() < deadline:
            control_group = control_group_resolver(plan, env)
            if control_group:
                break
            return_code = process.poll()
            if return_code is not None:
                raise BenchmarkRunnerError(
                    f"benchmark scope exited before cgroup discovery with status {return_code}"
                )
            sleeper(0.05)
        if not control_group:
            raise BenchmarkRunnerError("timed out discovering benchmark scope cgroup")
        segments = _control_group_segments(control_group)
        if segments[-1] != plan.unit_id:
            raise BenchmarkRunnerError(
                "resolved control group does not match the launched benchmark scope"
            )
        if process.poll() is not None:
            raise BenchmarkRunnerError("benchmark scope exited during cgroup discovery")
        reader = CgroupV2Reader.open(
            plan.cgroup_root,
            control_group,
            proc_root=plan.proc_root,
        )
        return RunningSystemdScope(
            plan,
            env,
            process,
            reader,
            scope_signaler,
        )
    except BaseException:
        _cleanup_failed_launch(process)
        raise
