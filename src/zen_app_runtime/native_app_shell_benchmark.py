"""Opt-in exact-Zen Wayland A/B harness for the bundled App Shell.

The controller launches each worker in the existing transient cgroup-v2
benchmark scope, samples only the authoritative cgroup reader, activates the
already mapped onboarding window without screenshots, and persists only the
privacy-safe aggregate report from :mod:`runtime_optimization`.

This module is not a release automation channel.  Its URL and candidate are
fixed, it exposes no script/selector/debug protocol, and it requires an exact
runtime digest plus the verified package-bundled adapter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .app_shell import (
    app_shell_policy_sha256,
    build_app_shell_policy,
    parse_app_shell_handshake_payload,
    render_app_shell_policy,
)
from .benchmark_runner import (
    BenchmarkRunnerError,
    CgroupSample,
    build_systemd_scope_plan,
    cgroup_cpu_basis_points,
    launch_systemd_scope,
)
from .browser_adapter import (
    EXPECTED_BROWSER_ADAPTER_TREE_SHA256,
    browser_adapter_application_path,
    bundled_browser_adapter_path,
    open_bundled_browser_adapter,
)
from .launch import _create_app_shell_policy_descriptor
from .manifest import create_manifest
from .runtime_optimization import (
    APP_SHELL_OPTIMIZATION_REPETITIONS,
    GC_EMPTY_CHUNK_BASELINE,
    GC_EMPTY_CHUNK_CANDIDATE,
    GC_EMPTY_CHUNK_PREF,
    AppShellOptimizationReport,
    AppShellOptimizationTrial,
    RuntimeOptimizationError,
    build_app_shell_optimization_report,
    build_scope_evidence,
    render_app_shell_optimization_report,
)

NATIVE_BENCHMARK_WORKER_STATUS_VERSION = 1
NATIVE_BENCHMARK_START_URL = "https://example.com/"
ACTIVE_SETTLE_SECONDS = 3.0
IDLE_SETTLE_SECONDS = 10.0
IDLE_INTERVAL_SECONDS = 10.0
WORKER_TIMEOUT_SECONDS = 90.0
STATUS_LIMIT_BYTES = 16 * 1024

_ALLOWED_WORKER_STATES = frozenset(
    {"starting", "handshake", "onboarding", "active", "finished", "failed"}
)
_ALLOWED_WORKER_FAILURES = frozenset({None, "browser-exited", "handshake-failed", "worker-failed"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_APPLICATION_VERSION_PATTERN = re.compile(r"^Version=([0-9]+\.[0-9]+\.[0-9]+)$", re.MULTILINE)
_CANDIDATE_USER_JS = f'user_pref("{GC_EMPTY_CHUNK_PREF}", {GC_EMPTY_CHUNK_CANDIDATE});\n'
_CANDIDATE_PREFS_LINE = f'user_pref("{GC_EMPTY_CHUNK_PREF}", {GC_EMPTY_CHUNK_CANDIDATE});'


class NativeAppShellBenchmarkError(RuntimeError):
    """The native A/B harness could not collect complete safe evidence."""


def _absolute_path(value: os.PathLike[str] | str, label: str) -> Path:
    if isinstance(value, bytes):
        raise NativeAppShellBenchmarkError(f"{label} must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise NativeAppShellBenchmarkError(f"{label} must be path-like text") from error
    if not isinstance(rendered, str) or not Path(rendered).is_absolute():
        raise NativeAppShellBenchmarkError(f"{label} must be absolute")
    if os.path.normpath(rendered) != rendered or "\0" in rendered:
        raise NativeAppShellBenchmarkError(f"{label} must be normalized and NUL-free")
    return Path(rendered)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _parse_application_version() -> str:
    application = Path(bundled_browser_adapter_path()) / "application.ini"
    match = _APPLICATION_VERSION_PATTERN.search(application.read_text(encoding="utf-8"))
    if match is None:
        raise NativeAppShellBenchmarkError("bundled App Shell version is missing")
    return match.group(1)


def _verify_static_runtime_invariants() -> None:
    bundle = Path(bundled_browser_adapter_path())
    preferences = (bundle / "defaults/preferences/prefs.js").read_text(encoding="utf-8")
    markup = (bundle / "chrome/content/shell.xhtml").read_text(encoding="utf-8")
    required = (
        'pref("browser.tabs.remote.autostart", true);',
        'pref("fission.autostart", true);',
        'pref("security.sandbox.content.level", 6);',
        'pref("security.sandbox.socket.process.level", 2);',
        'pref("media.navigator.permission.disabled", false);',
        'pref("media.navigator.streams.fake", false);',
        'pref("media.getusermedia.screensharing.enabled", true);',
    )
    if any(line not in preferences for line in required):
        raise NativeAppShellBenchmarkError("bundled security invariant is missing")
    forbidden = (
        "layers.acceleration.disabled",
        "gfx.webrender.force-disabled",
        "gfx.webrender.software",
        "no-sandbox",
    )
    if any(item in preferences.lower() for item in forbidden):
        raise NativeAppShellBenchmarkError(
            "bundled preferences disable acceleration or sandboxing"
        )
    if 'transparent="true"' not in markup:
        raise NativeAppShellBenchmarkError("transparent App Shell surface is missing")


def _read_handshake(descriptor: int, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    payload = bytearray()
    os.set_blocking(descriptor, False)
    while time.monotonic() < deadline:
        readable, _, _ = select.select((descriptor,), (), (), 0.1)
        if not readable:
            continue
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > STATUS_LIMIT_BYTES:
            raise NativeAppShellBenchmarkError("App Shell handshake exceeded the limit")
        if b"\n" in payload:
            document, trailing = bytes(payload).split(b"\n", 1)
            if trailing:
                raise NativeAppShellBenchmarkError("App Shell handshake had trailing bytes")
            return document
    raise NativeAppShellBenchmarkError("App Shell handshake timed out")


class _WorkerStatus:
    def __init__(self, path: Path, browser_pid: int) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._value: dict[str, Any] = {
            "browserPid": browser_pid,
            "contentCommitted": False,
            "failureCode": None,
            "handshakeReady": False,
            "onboardingMilliseconds": None,
            "preferenceVerified": False,
            "state": "starting",
            "usableMilliseconds": None,
            "workerStatusVersion": NATIVE_BENCHMARK_WORKER_STATUS_VERSION,
        }
        self._write_locked()

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._value.update(changes)
            self._write_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._value)

    def _write_locked(self) -> None:
        payload = (
            json.dumps(
                self._value,
                allow_nan=False,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, payload.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._path)


class _BrowserOutputConsumer:
    def __init__(
        self,
        process: subprocess.Popen[str],
        status: _WorkerStatus,
        started: float,
    ) -> None:
        self._status = status
        self._started = started
        self._threads = [
            threading.Thread(target=self._consume, args=(process.stdout,), daemon=True),
            threading.Thread(target=self._consume, args=(process.stderr,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _consume(self, stream: Any) -> None:
        if stream is None:
            return
        for line in stream:
            if "ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY" in line:
                elapsed = round((time.monotonic() - self._started) * 1000)
                current = self._status.snapshot()
                state = "active" if current["contentCommitted"] else "onboarding"
                self._status.update(
                    onboardingMilliseconds=elapsed,
                    state=state,
                )
            if "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED" in line:
                elapsed = round((time.monotonic() - self._started) * 1000)
                self._status.update(
                    contentCommitted=True,
                    state="active",
                    usableMilliseconds=elapsed,
                )

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=5)


def _prepare_profile(profile: Path, variant: str) -> None:
    profile.mkdir(mode=0o700, parents=False, exist_ok=True)
    profile.chmod(0o700)
    user_js = profile / "user.js"
    if variant == "baseline":
        if user_js.exists() and GC_EMPTY_CHUNK_PREF in user_js.read_text(encoding="utf-8"):
            raise NativeAppShellBenchmarkError("baseline profile contains candidate preference")
        return
    if variant != "gc-empty-chunks-0":
        raise NativeAppShellBenchmarkError("worker optimization variant is invalid")
    if user_js.exists():
        if user_js.read_text(encoding="utf-8") != _CANDIDATE_USER_JS:
            raise NativeAppShellBenchmarkError("candidate user.js is not exact")
    else:
        descriptor = os.open(
            user_js,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, _CANDIDATE_USER_JS.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _verify_profile_preference(profile: Path, variant: str) -> bool:
    user_js = profile / "user.js"
    prefs_js = profile / "prefs.js"
    if variant == "baseline":
        for path in (user_js, prefs_js):
            if path.exists() and _CANDIDATE_PREFS_LINE in path.read_text(
                encoding="utf-8", errors="strict"
            ):
                return False
        return True
    return (
        user_js.is_file()
        and user_js.read_text(encoding="utf-8") == _CANDIDATE_USER_JS
        and prefs_js.is_file()
        and _CANDIDATE_PREFS_LINE in prefs_js.read_text(encoding="utf-8")
    )


def _terminate_browser(process: subprocess.Popen[str]) -> int:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(timeout=8)


def _worker_main(
    *,
    runtime: Path,
    runtime_sha256: str,
    profile: Path,
    status_path: Path,
    stop_path: Path,
    variant: str,
) -> int:
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise NativeAppShellBenchmarkError("worker runtime is not executable")
    if not _SHA256_PATTERN.fullmatch(runtime_sha256) or _sha256_file(runtime) != runtime_sha256:
        raise NativeAppShellBenchmarkError("worker runtime digest does not match")
    if status_path.exists() or stop_path.exists():
        raise NativeAppShellBenchmarkError("worker control file already exists")
    if status_path.parent != profile.parent or stop_path.parent != profile.parent:
        raise NativeAppShellBenchmarkError("worker files must share the private trial root")
    _prepare_profile(profile, variant)

    manifest = create_manifest(
        "app-shell-optimization-probe",
        "App Shell Optimization Probe",
        NATIVE_BENCHMARK_START_URL,
        mode="Transparent",
    )
    policy = build_app_shell_policy(manifest)
    policy_payload = render_app_shell_policy(policy).encode("utf-8")
    policy_read = -1
    handshake_read = -1
    handshake_write = -1
    profile_descriptor = -1
    process: subprocess.Popen[str] | None = None
    consumer: _BrowserOutputConsumer | None = None
    status: _WorkerStatus | None = None
    failure_code: str | None = None

    try:
        with open_bundled_browser_adapter() as adapter:
            policy_read = _create_app_shell_policy_descriptor(policy_payload)
            handshake_read, handshake_write = os.pipe2(os.O_CLOEXEC)
            profile_descriptor = os.open(
                profile,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD": str(policy_read),
                    "ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD": str(handshake_write),
                }
            )
            for name in (
                "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTART",
                "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTOP",
                "ZEN_APP_RUNTIME_APP_SHELL_TEST_MEDIATION",
            ):
                environment.pop(name, None)
            command = (
                str(runtime),
                "-app",
                browser_adapter_application_path(adapter),
                "--profile",
                f"/proc/self/fd/{profile_descriptor}",
                "--no-remote",
            )
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                close_fds=True,
                env=environment,
                pass_fds=(
                    adapter.descriptor,
                    policy_read,
                    handshake_write,
                    profile_descriptor,
                ),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            status = _WorkerStatus(status_path, process.pid)
            consumer = _BrowserOutputConsumer(process, status, started)
            os.close(policy_read)
            policy_read = -1
            os.close(handshake_write)
            handshake_write = -1
            try:
                handshake = _read_handshake(handshake_read, 20)
                parse_app_shell_handshake_payload(
                    handshake,
                    expected_policy_sha256=app_shell_policy_sha256(policy),
                )
            except BaseException:
                failure_code = "handshake-failed"
                raise
            status.update(handshakeReady=True, state="handshake")

            deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
            while not stop_path.exists():
                if process.poll() is not None:
                    failure_code = "browser-exited"
                    raise NativeAppShellBenchmarkError("browser exited before benchmark stop")
                if time.monotonic() >= deadline:
                    raise NativeAppShellBenchmarkError("worker stop timed out")
                time.sleep(0.05)

            return_code = _terminate_browser(process)
            if return_code not in (0, -signal.SIGTERM):
                raise NativeAppShellBenchmarkError(
                    f"browser returned unexpected status {return_code}"
                )
            consumer.join()
            verified = _verify_profile_preference(profile, variant)
            if not verified:
                raise NativeAppShellBenchmarkError("effective candidate preference was not persisted")
            status.update(
                failureCode=None,
                preferenceVerified=True,
                state="finished",
            )
            return 0
    except BaseException as error:
        if process is not None and process.poll() is None:
            _terminate_browser(process)
        if consumer is not None:
            consumer.join()
        if status is not None:
            status.update(
                failureCode=failure_code or "worker-failed",
                state="failed",
            )
        print(
            f"native App Shell benchmark worker failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        for descriptor in (
            policy_read,
            handshake_read,
            handshake_write,
            profile_descriptor,
        ):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _validate_worker_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != frozenset(
        {
            "browserPid",
            "contentCommitted",
            "failureCode",
            "handshakeReady",
            "onboardingMilliseconds",
            "preferenceVerified",
            "state",
            "usableMilliseconds",
            "workerStatusVersion",
        }
    ):
        raise NativeAppShellBenchmarkError("worker status fields are invalid")
    if value["workerStatusVersion"] != NATIVE_BENCHMARK_WORKER_STATUS_VERSION:
        raise NativeAppShellBenchmarkError("worker status version is invalid")
    if type(value["browserPid"]) is not int or value["browserPid"] <= 0:
        raise NativeAppShellBenchmarkError("worker browser PID is invalid")
    for name in ("contentCommitted", "handshakeReady", "preferenceVerified"):
        if type(value[name]) is not bool:
            raise NativeAppShellBenchmarkError(f"worker status {name} is invalid")
    if value["state"] not in _ALLOWED_WORKER_STATES:
        raise NativeAppShellBenchmarkError("worker state is invalid")
    if value["failureCode"] not in _ALLOWED_WORKER_FAILURES:
        raise NativeAppShellBenchmarkError("worker failure code is invalid")
    for name in ("onboardingMilliseconds", "usableMilliseconds"):
        metric = value[name]
        if metric is not None and (type(metric) is not int or metric < 0):
            raise NativeAppShellBenchmarkError(f"worker status {name} is invalid")
    return value


def _read_worker_status(path: Path) -> dict[str, Any] | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        payload = os.read(descriptor, STATUS_LIMIT_BYTES + 1)
        if len(payload) > STATUS_LIMIT_BYTES:
            raise NativeAppShellBenchmarkError("worker status exceeds the limit")
        if os.read(descriptor, 1):
            raise NativeAppShellBenchmarkError("worker status exceeds the limit")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeAppShellBenchmarkError(f"worker status is invalid: {error}") from error
    return _validate_worker_status(value)


def _wait_worker_status(
    path: Path,
    predicate: Callable[[Mapping[str, Any]], bool],
    *,
    timeout: float,
    scope: Any,
    stage: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _read_worker_status(path)
        if status is not None:
            if status["state"] == "failed":
                raise NativeAppShellBenchmarkError(
                    f"benchmark worker failed closed: {status['failureCode']}"
                )
            if predicate(status):
                return status
        return_code = scope.poll()
        if return_code is not None:
            raise NativeAppShellBenchmarkError(
                f"benchmark scope exited early with status {return_code}"
            )
        time.sleep(0.05)
    raise NativeAppShellBenchmarkError(
        f"timed out waiting for benchmark worker stage {stage}"
    )


def _hyprland_window(browser_pid: int, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ("/usr/bin/hyprctl", "clients", "-j"),
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if completed.returncode == 0:
            try:
                clients = json.loads(completed.stdout)
            except json.JSONDecodeError:
                clients = []
            if isinstance(clients, list):
                for client in clients:
                    if (
                        isinstance(client, dict)
                        and client.get("pid") == browser_pid
                        and str(client.get("class", "")).lower() == "zen-app-shell"
                    ):
                        return client
        time.sleep(0.1)
    raise NativeAppShellBenchmarkError("Wayland App Shell window was not mapped")


def _activate_onboarding(browser_pid: int) -> None:
    window = _hyprland_window(browser_pid)
    address = window.get("address")
    if not isinstance(address, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", address):
        raise NativeAppShellBenchmarkError("App Shell window address is invalid")
    # Hyprland 0.55+ dispatches typed Lua actions.  The address comes from the
    # compositor's own JSON and is constrained above before interpolation.
    focus = subprocess.run(
        (
            "/usr/bin/hyprctl",
            "dispatch",
            f"hl.dsp.focus({{ window = 'address:{address}' }})",
        ),
        shell=False,
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if focus.returncode != 0:
        raise NativeAppShellBenchmarkError("cannot focus App Shell onboarding window")
    active = subprocess.run(
        ("/usr/bin/hyprctl", "activewindow", "-j"),
        shell=False,
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    try:
        active_window = json.loads(active.stdout) if active.returncode == 0 else {}
    except json.JSONDecodeError:
        active_window = {}
    if not isinstance(active_window, dict) or active_window.get("pid") != browser_pid:
        raise NativeAppShellBenchmarkError("App Shell onboarding window is not active")
    key = subprocess.run(
        ("/usr/bin/wtype", "-k", "Return"),
        shell=False,
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if key.returncode != 0:
        raise NativeAppShellBenchmarkError("cannot activate App Shell onboarding")


def _benchmark_environment(private_root: Path) -> dict[str, str]:
    required = (
        "DBUS_SESSION_BUS_ADDRESS",
        "HYPRLAND_INSTANCE_SIGNATURE",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise NativeAppShellBenchmarkError(
            f"native Wayland benchmark environment is missing {missing}"
        )
    home = private_root / "home"
    cache = private_root / "cache"
    config = private_root / "config"
    data = private_root / "data"
    for directory in (home, cache, config, data):
        directory.mkdir(mode=0o700, exist_ok=False)
    project_root = Path(__file__).resolve().parents[2]
    environment = {
        "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"],
        "GDK_BACKEND": "wayland",
        "HOME": str(home),
        "HYPRLAND_INSTANCE_SIGNATURE": os.environ["HYPRLAND_INSTANCE_SIGNATURE"],
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "MOZ_ENABLE_WAYLAND": "1",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(project_root / "src"),
        "WAYLAND_DISPLAY": os.environ["WAYLAND_DISPLAY"],
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        "XDG_SESSION_TYPE": "wayland",
    }
    for optional in ("DESKTOP_SESSION", "XDG_CURRENT_DESKTOP"):
        if optional in os.environ:
            environment[optional] = os.environ[optional]
    return environment


def _new_secure_process_count(
    onboarding: CgroupSample,
    active: CgroupSample,
) -> int:
    before = {(process.pid, process.start_time_ticks) for process in onboarding.processes}
    return sum(
        (process.pid, process.start_time_ticks) not in before
        and process.seccomp == 2
        and process.no_new_privs == 1
        for process in active.processes
    )


def _create_stop_file(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, b"stop\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _wait_cgroup_removed(cgroup_root: str, control_group: str, timeout: float) -> bool:
    leaf = Path(cgroup_root).joinpath(*control_group.lstrip("/").split("/"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not leaf.exists():
            return True
        time.sleep(0.1)
    return not leaf.exists()


def _run_trial(
    *,
    runtime: Path,
    runtime_sha256: str,
    pair_index: int,
    startup_kind: str,
    variant: str,
    private_root: Path,
    profile: Path,
    sequence: int,
) -> AppShellOptimizationTrial:
    status_path = private_root / f"status-{sequence}.json"
    stop_path = private_root / f"stop-{sequence}"
    environment_root = private_root / f"environment-{sequence}"
    environment_root.mkdir(mode=0o700)
    environment = _benchmark_environment(environment_root)
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise NativeAppShellBenchmarkError("Python worker executable is not absolute")
    command = (
        str(executable),
        "-m",
        "zen_app_runtime.native_app_shell_benchmark",
        "--worker",
        "--runtime",
        str(runtime),
        "--runtime-sha256",
        runtime_sha256,
        "--profile",
        str(profile),
        "--status",
        str(status_path),
        "--stop",
        str(stop_path),
        "--variant",
        variant,
    )
    suffix = "b" if variant == "baseline" else "c"
    kind = "c" if startup_kind == "cold" else "w"
    scope_name = f"zen-app-benchmark-opt-{os.getpid() % 100000}-{pair_index}{suffix}{kind}"
    plan = build_systemd_scope_plan(
        command,
        scope_name=scope_name,
        working_directory=Path(__file__).resolve().parents[2],
    )
    scope = None
    control_group = ""
    try:
        scope = launch_systemd_scope(plan, environment, discovery_timeout=8.0)
        control_group = scope.control_group
        onboarding_status = _wait_worker_status(
            status_path,
            lambda value: (
                value["handshakeReady"]
                and value["onboardingMilliseconds"] is not None
            ),
            timeout=35,
            scope=scope,
            stage="onboarding",
        )
        if onboarding_status["contentCommitted"]:
            raise NativeAppShellBenchmarkError(
                "site content committed before onboarding sample"
            )
        onboarding_sample = scope.sample(max_attempts=10)
        _activate_onboarding(int(onboarding_status["browserPid"]))
        active_status = _wait_worker_status(
            status_path,
            lambda value: (
                value["contentCommitted"]
                and value["usableMilliseconds"] is not None
            ),
            timeout=45,
            scope=scope,
            stage="content-commit",
        )
        time.sleep(ACTIVE_SETTLE_SECONDS)
        active_sample = scope.sample(max_attempts=10)
        secure_new = _new_secure_process_count(onboarding_sample, active_sample)
        time.sleep(IDLE_SETTLE_SECONDS)
        idle_before = scope.sample(max_attempts=10)
        time.sleep(IDLE_INTERVAL_SECONDS)
        idle_sample = scope.sample(max_attempts=10)
        idle_cpu = cgroup_cpu_basis_points(idle_before, idle_sample)
        _create_stop_file(stop_path)
        finished = _wait_worker_status(
            status_path,
            lambda value: value["state"] == "finished",
            timeout=35,
            scope=scope,
            stage="cleanup",
        )
        if not finished["preferenceVerified"]:
            raise NativeAppShellBenchmarkError("worker preference verification is incomplete")
        return_code = scope.wait(timeout=20)
        if return_code != 0:
            raise NativeAppShellBenchmarkError(
                f"benchmark worker scope returned status {return_code}"
            )
        scope.close()
        scope = None
        cleanup_complete = _wait_cgroup_removed(
            plan.cgroup_root,
            control_group,
            10,
        )
        if not cleanup_complete:
            raise NativeAppShellBenchmarkError("benchmark cgroup survived cleanup")
        return AppShellOptimizationTrial(
            trial_version=1,
            variant=variant,
            pair_index=pair_index,
            startup_kind=startup_kind,
            onboarding_milliseconds=int(onboarding_status["onboardingMilliseconds"]),
            usable_milliseconds=int(active_status["usableMilliseconds"]),
            onboarding=build_scope_evidence(onboarding_sample),
            active=build_scope_evidence(active_sample),
            idle=build_scope_evidence(idle_sample),
            idle_cpu_basis_points=idle_cpu,
            post_commit_new_secure_process_count=secure_new,
            preference_value=(
                GC_EMPTY_CHUNK_BASELINE
                if variant == "baseline"
                else GC_EMPTY_CHUNK_CANDIDATE
            ),
            fission_enabled=True,
            remote_content_enabled=True,
            content_sandbox_level=6,
            socket_sandbox_level=2,
            native_wayland=True,
            hardware_acceleration_disable_pref_absent=True,
            transparent_surface_configured=True,
            cleanup_complete=cleanup_complete,
        )
    except BaseException:
        if scope is not None:
            try:
                scope.terminate(timeout=8)
            except BenchmarkRunnerError:
                try:
                    scope.close()
                except BenchmarkRunnerError:
                    pass
        raise


def run_native_app_shell_optimization(
    *,
    runtime: os.PathLike[str] | str,
    runtime_sha256: str,
    output_path: os.PathLike[str] | str,
) -> AppShellOptimizationReport:
    """Collect the exact three-pair cold/warm baseline/candidate report."""
    runtime_path = _absolute_path(runtime, "runtime")
    output = _absolute_path(output_path, "optimization output")
    if not _SHA256_PATTERN.fullmatch(runtime_sha256):
        raise NativeAppShellBenchmarkError("runtime digest must be lowercase SHA-256")
    if not runtime_path.is_file() or not os.access(runtime_path, os.X_OK):
        raise NativeAppShellBenchmarkError("runtime must be an executable regular file")
    if _sha256_file(runtime_path) != runtime_sha256:
        raise NativeAppShellBenchmarkError("runtime digest does not match")
    if not output.parent.is_dir():
        raise NativeAppShellBenchmarkError("optimization output parent does not exist")
    _verify_static_runtime_invariants()
    app_shell_version = _parse_application_version()
    project_root = Path(__file__).resolve().parents[2]
    trials: list[AppShellOptimizationTrial] = []
    sequence = 0
    with tempfile.TemporaryDirectory(
        prefix=".zen-app-optimization-",
        dir=project_root,
    ) as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        for pair_index in range(1, APP_SHELL_OPTIMIZATION_REPETITIONS + 1):
            order = (
                ("baseline", "gc-empty-chunks-0")
                if pair_index % 2
                else ("gc-empty-chunks-0", "baseline")
            )
            profiles = {
                variant: root / f"profile-{pair_index}-{variant}"
                for variant in order
            }
            for variant in order:
                for startup_kind in ("cold", "warm"):
                    sequence += 1
                    print(
                        json.dumps(
                            {
                                "event": "trial-start",
                                "pairIndex": pair_index,
                                "startupKind": startup_kind,
                                "variant": variant,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    trial = _run_trial(
                        runtime=runtime_path,
                        runtime_sha256=runtime_sha256,
                        pair_index=pair_index,
                        startup_kind=startup_kind,
                        variant=variant,
                        private_root=root,
                        profile=profiles[variant],
                        sequence=sequence,
                    )
                    trials.append(trial)
                    print(
                        json.dumps(
                            {
                                "activePssKiB": trial.active.pss_kib,
                                "event": "trial-complete",
                                "idleCpuBasisPoints": trial.idle_cpu_basis_points,
                                "idlePssKiB": trial.idle.pss_kib,
                                "onboardingPssKiB": trial.onboarding.pss_kib,
                                "pairIndex": pair_index,
                                "startupKind": startup_kind,
                                "usableMilliseconds": trial.usable_milliseconds,
                                "variant": variant,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
    try:
        report = build_app_shell_optimization_report(
            runtime_sha256=runtime_sha256,
            measured_adapter_tree_sha256=EXPECTED_BROWSER_ADAPTER_TREE_SHA256,
            app_shell_version=app_shell_version,
            trials=tuple(trials),
        )
        rendered = render_app_shell_optimization_report(report)
    except RuntimeOptimizationError as error:
        raise NativeAppShellBenchmarkError(
            f"native optimization evidence is invalid: {error}"
        ) from error
    temporary_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary_output,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.write(descriptor, rendered.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary_output, output)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exact-Zen App Shell optimization harness")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--output")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--status")
    parser.add_argument("--stop")
    parser.add_argument(
        "--variant",
        choices=("baseline", "gc-empty-chunks-0"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    runtime = _absolute_path(arguments.runtime, "runtime")
    if arguments.worker:
        if not all((arguments.profile, arguments.status, arguments.stop, arguments.variant)):
            raise NativeAppShellBenchmarkError("worker arguments are incomplete")
        if arguments.output is not None:
            raise NativeAppShellBenchmarkError("worker cannot write a report")
        return _worker_main(
            runtime=runtime,
            runtime_sha256=arguments.runtime_sha256,
            profile=_absolute_path(arguments.profile, "worker profile"),
            status_path=_absolute_path(arguments.status, "worker status"),
            stop_path=_absolute_path(arguments.stop, "worker stop"),
            variant=arguments.variant,
        )
    if arguments.output is None:
        raise NativeAppShellBenchmarkError("controller requires --output")
    if any((arguments.profile, arguments.status, arguments.stop, arguments.variant)):
        raise NativeAppShellBenchmarkError("worker-only arguments are not accepted")
    report = run_native_app_shell_optimization(
        runtime=runtime,
        runtime_sha256=arguments.runtime_sha256,
        output_path=_absolute_path(arguments.output, "optimization output"),
    )
    print(
        json.dumps(
            {
                "reportSha256": hashlib.sha256(
                    render_app_shell_optimization_report(report).encode("ascii")
                ).hexdigest(),
                "status": report.evaluation.status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NativeAppShellBenchmarkError as error:
        print(f"native App Shell benchmark error: {error}", file=sys.stderr)
        raise SystemExit(2)
