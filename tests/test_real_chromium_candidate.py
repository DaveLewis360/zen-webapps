"""Opt-in, screenshot-free native Wayland smoke for the assembled CEF candidate."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from zen_app_runtime.benchmark_runner import (
    BenchmarkRunnerError,
    build_systemd_scope_plan,
    launch_systemd_scope,
)
from zen_app_runtime.chromium_runtime_candidate import (
    chromium_runtime_candidate_environment,
    chromium_runtime_candidate_executable_path,
    create_chromium_shell_config_descriptor,
    open_verified_chromium_runtime_candidate,
    render_chromium_shell_config,
)
from zen_app_runtime.native_app_shell_benchmark import (
    NativeAppShellBenchmarkError,
    _benchmark_environment,
    _create_stop_file,
    _new_secure_process_count,
    _terminate_browser,
    _wait_cgroup_removed,
    _wait_worker_status,
    _WorkerStatus,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_CANDIDATE = os.environ.get("ZEN_REAL_CHROMIUM_CANDIDATE")
REAL_WAYLAND = os.environ.get("ZEN_REAL_CHROMIUM_CANDIDATE_WAYLAND") == "1"
_MARKERS = (
    "ZEN_CEF_ONBOARDING_READY",
    "ZEN_CEF_CONTENT_REQUESTED",
    "ZEN_CEF_BROWSER_CREATED",
    "ZEN_CEF_CONTENT_COMMITTED",
)
_MAX_CMDLINE_BYTES = 64 * 1024


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        if self.path.split("?", 1)[0] != "/fixture":
            self.send_error(404)
            return
        payload = (
            b"<!doctype html><meta charset=utf-8>"
            b"<title>Zen local CEF fixture</title>"
            b"<main id=ready>local fixture ready</main>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        del args


class _CefOutputConsumer:
    def __init__(
        self,
        process: subprocess.Popen[str],
        status: _WorkerStatus,
        marker_path: Path,
        started: float,
    ) -> None:
        self._status = status
        self._marker_path = marker_path
        self._started = started
        self._lock = threading.Lock()
        self._markers: list[str] = []
        self._threads = [
            threading.Thread(target=self._consume, args=(process.stdout,), daemon=True),
            threading.Thread(target=self._consume, args=(process.stderr,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _consume(self, stream: object) -> None:
        if stream is None:
            return
        for line in stream:  # type: ignore[union-attr]
            if "ZEN_CEF_FATAL" in line:
                self._status.update(failureCode="browser-exited", state="failed")
            for marker in _MARKERS:
                if marker not in line:
                    continue
                elapsed = round((time.monotonic() - self._started) * 1000)
                with self._lock:
                    self._markers.append(marker)
                    self._write_markers_locked()
                if marker == "ZEN_CEF_ONBOARDING_READY":
                    self._status.update(
                        handshakeReady=True,
                        onboardingMilliseconds=elapsed,
                        state="onboarding",
                    )
                elif marker == "ZEN_CEF_CONTENT_COMMITTED":
                    self._status.update(
                        contentCommitted=True,
                        state="active",
                        usableMilliseconds=elapsed,
                    )

    def _write_markers_locked(self) -> None:
        payload = ("\n".join(self._markers) + "\n").encode("ascii")
        temporary = self._marker_path.with_name(
            f".{self._marker_path.name}.{os.getpid()}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._marker_path)

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=5)


def _candidate_worker(
    *,
    candidate_path: Path,
    profile: Path,
    status_path: Path,
    marker_path: Path,
    stop_path: Path,
) -> int:
    if any(path.exists() for path in (profile, status_path, marker_path, stop_path)):
        raise NativeAppShellBenchmarkError("CEF worker path already exists")
    if len({path.parent for path in (profile, status_path, marker_path, stop_path)}) != 1:
        raise NativeAppShellBenchmarkError("CEF worker files must share one private root")
    profile.mkdir(mode=0o700)
    profile.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = int(server.server_address[1])
    start_url = f"http://127.0.0.1:{port}/fixture"
    config_descriptor = -1
    process: subprocess.Popen[str] | None = None
    consumer: _CefOutputConsumer | None = None
    status: _WorkerStatus | None = None
    failure_code: str | None = None
    try:
        config_payload = render_chromium_shell_config(
            profile_root=profile,
            start_url=start_url,
            display_backend="wayland",
            fixture_mode=True,
            test_auto_activate=False,
        )
        config_descriptor = create_chromium_shell_config_descriptor(config_payload)
        with open_verified_chromium_runtime_candidate(candidate_path) as runtime:
            executable = chromium_runtime_candidate_executable_path(runtime)
            environment = dict(os.environ)
            environment.update(chromium_runtime_candidate_environment(runtime))
            environment.pop("DISPLAY", None)
            command = (executable, f"--zen-config-fd={config_descriptor}")
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                executable=executable,
                close_fds=True,
                env=environment,
                pass_fds=(runtime.runtime_descriptor, config_descriptor),
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            status = _WorkerStatus(status_path, process.pid)
            consumer = _CefOutputConsumer(process, status, marker_path, started)
            os.close(config_descriptor)
            config_descriptor = -1
            deadline = time.monotonic() + 120
            while not stop_path.exists():
                return_code = process.poll()
                if return_code is not None:
                    failure_code = "browser-exited"
                    raise NativeAppShellBenchmarkError(
                        f"CEF exited before controller stop with {return_code}"
                    )
                if time.monotonic() >= deadline:
                    failure_code = "worker-failed"
                    raise NativeAppShellBenchmarkError("CEF worker stop timed out")
                time.sleep(0.05)
            return_code = _terminate_browser(process)
            if return_code not in (0, -signal.SIGTERM):
                raise NativeAppShellBenchmarkError(
                    f"CEF returned unexpected status {return_code}"
                )
            consumer.join()
            status.update(failureCode=None, preferenceVerified=True, state="finished")
            return 0
    except BaseException as error:
        if process is not None and process.poll() is None:
            _terminate_browser(process)
        if consumer is not None:
            consumer.join()
        if status is not None:
            status.update(failureCode=failure_code or "worker-failed", state="failed")
        print(
            f"real Chromium candidate worker failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    finally:
        if config_descriptor >= 0:
            os.close(config_descriptor)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _cef_window(browser_pid: int, timeout: float = 12.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ("/usr/bin/hyprctl", "clients", "-j"),
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            try:
                clients = json.loads(result.stdout)
            except json.JSONDecodeError:
                clients = []
            if isinstance(clients, list):
                for client in clients:
                    if (
                        isinstance(client, dict)
                        and client.get("pid") == browser_pid
                        and client.get("title") == "Zen CEF Candidate"
                    ):
                        return client
        time.sleep(0.1)
    raise NativeAppShellBenchmarkError("CEF Wayland window was not mapped")


def _activate_cef_onboarding(browser_pid: int) -> None:
    window = _cef_window(browser_pid)
    address = window.get("address")
    if not isinstance(address, str) or re.fullmatch(r"0x[0-9a-fA-F]+", address) is None:
        raise NativeAppShellBenchmarkError("CEF window address is invalid")
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
        raise NativeAppShellBenchmarkError("cannot focus CEF onboarding window")
    key = subprocess.run(
        ("/usr/bin/wtype", "-k", "Tab", "-k", "Return"),
        shell=False,
        close_fds=True,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if key.returncode != 0:
        raise NativeAppShellBenchmarkError("cannot activate CEF onboarding")


def _wait_cef_window_gone(browser_pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ("/usr/bin/hyprctl", "clients", "-j"),
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            try:
                clients = json.loads(result.stdout)
            except json.JSONDecodeError:
                clients = []
            if isinstance(clients, list) and not any(
                isinstance(client, dict) and client.get("pid") == browser_pid
                for client in clients
            ):
                return True
        time.sleep(0.1)
    return False


def _cef_process_types(sample: object, profile: Path) -> dict[str, object]:
    process_types: dict[str, object] = {}
    profile_bytes = os.fsencode(profile)
    forbidden_prefixes = (
        b"--disable-gpu",
        b"--disable-setuid-sandbox",
        b"--no-sandbox",
        b"--remote-debugging-port",
    )
    for process in sample.processes:  # type: ignore[attr-defined]
        try:
            executable = os.readlink(f"/proc/{process.pid}/exe")
        except (FileNotFoundError, PermissionError):
            continue
        if Path(executable).name != "zen-cef-shell":
            continue
        payload = Path(f"/proc/{process.pid}/cmdline").read_bytes()
        if len(payload) > _MAX_CMDLINE_BYTES:
            raise NativeAppShellBenchmarkError("CEF child command line exceeded limit")
        tokens = tuple(token for token in payload.split(b"\0") if token)
        process_type = "browser"
        type_match = re.search(rb"(?:^|[ \x00])--type=([a-z0-9-]+)", payload)
        if type_match is not None:
            process_type = type_match.group(1).decode("ascii", errors="strict")
        leaks: list[str] = []
        for needle, label in (
            (profile_bytes, "profile"),
            (b"http://127.0.0.1:", "url"),
        ):
            offset = payload.find(needle)
            if offset < 0:
                continue
            switches = list(re.finditer(rb"--([a-z0-9-]+)=", payload[:offset]))
            switch = (
                switches[-1].group(1).decode("ascii", errors="strict")
                if switches
                else "positional"
            )
            leaks.append(f"{label}:--{switch}")
        if leaks:
            raise NativeAppShellBenchmarkError(
                f"CEF {process_type} command line leaked sealed config via {sorted(leaks)}"
            )
        if any(token.startswith(forbidden_prefixes) for token in tokens):
            raise NativeAppShellBenchmarkError("CEF child command line weakened sandbox/GPU policy")
        process_types.setdefault(process_type, process)
    return process_types


@unittest.skipUnless(
    REAL_CANDIDATE and REAL_WAYLAND,
    "set ZEN_REAL_CHROMIUM_CANDIDATE and ZEN_REAL_CHROMIUM_CANDIDATE_WAYLAND=1",
)
class RealChromiumCandidateTests(unittest.TestCase):
    def test_wayland_cgroup_fixture_sandbox_and_cleanup(self) -> None:
        candidate_path = Path(REAL_CANDIDATE or "")
        self.assertTrue(candidate_path.is_absolute())
        with tempfile.TemporaryDirectory(
            prefix=".real-chromium-candidate-", dir=PROJECT_ROOT
        ) as temporary:
            private_root = Path(temporary)
            private_root.chmod(0o700)
            profile = private_root / "profile"
            status_path = private_root / "status.json"
            marker_path = private_root / "markers"
            stop_path = private_root / "stop"
            environment_root = private_root / "environment"
            environment_root.mkdir(mode=0o700)
            environment = _benchmark_environment(environment_root)
            executable = Path(sys.executable)
            self.assertTrue(executable.is_absolute())
            command = (
                str(executable),
                "-m",
                "tests.test_real_chromium_candidate",
                "--candidate-worker",
                "--candidate",
                str(candidate_path),
                "--profile",
                str(profile),
                "--status",
                str(status_path),
                "--markers",
                str(marker_path),
                "--stop",
                str(stop_path),
            )
            scope_name = f"zen-app-benchmark-cef-{os.getpid() % 100000}"
            plan = build_systemd_scope_plan(
                command,
                scope_name=scope_name,
                working_directory=PROJECT_ROOT,
            )
            scope = None
            control_group = ""
            browser_pid = -1
            try:
                scope = launch_systemd_scope(plan, environment, discovery_timeout=8.0)
                control_group = scope.control_group
                onboarding = _wait_worker_status(
                    status_path,
                    lambda value: (
                        value["handshakeReady"]
                        and value["onboardingMilliseconds"] is not None
                    ),
                    timeout=45,
                    scope=scope,
                    stage="cef-onboarding",
                )
                browser_pid = int(onboarding["browserPid"])
                self.assertFalse(onboarding["contentCommitted"])
                onboarding_sample = scope.sample(max_attempts=10)
                self.assertGreater(onboarding_sample.summed_pss_kib, 0)
                _activate_cef_onboarding(browser_pid)
                active = _wait_worker_status(
                    status_path,
                    lambda value: (
                        value["contentCommitted"]
                        and value["usableMilliseconds"] is not None
                    ),
                    timeout=60,
                    scope=scope,
                    stage="cef-content",
                )
                self.assertTrue(active["contentCommitted"])
                time.sleep(2)
                active_sample = scope.sample(max_attempts=10)
                self.assertGreater(active_sample.summed_pss_kib, 0)
                self.assertGreater(
                    len(active_sample.processes), len(onboarding_sample.processes)
                )
                self.assertGreaterEqual(
                    _new_secure_process_count(onboarding_sample, active_sample), 1
                )
                process_types = _cef_process_types(active_sample, profile)
                for required_type in ("gpu-process", "renderer"):
                    self.assertIn(required_type, process_types)
                renderer = process_types["renderer"]
                self.assertEqual(2, renderer.seccomp)
                self.assertEqual(1, renderer.no_new_privs)
                gpu = process_types["gpu-process"]
                self.assertEqual(1, gpu.no_new_privs)
                markers = marker_path.read_text(encoding="ascii").splitlines()
                for marker in _MARKERS:
                    self.assertIn(marker, markers)
                self.assertEqual(
                    list(_MARKERS), sorted(_MARKERS, key=markers.index)
                )
                _create_stop_file(stop_path)
                finished = _wait_worker_status(
                    status_path,
                    lambda value: value["state"] == "finished",
                    timeout=35,
                    scope=scope,
                    stage="cef-cleanup",
                )
                self.assertTrue(finished["preferenceVerified"])
                self.assertEqual(0, scope.wait(timeout=20))
                scope.close()
                scope = None
                self.assertTrue(
                    _wait_cgroup_removed(plan.cgroup_root, control_group, 10),
                    "CEF candidate cgroup survived cleanup",
                )
                self.assertFalse(Path(f"/proc/{browser_pid}").exists())
                self.assertTrue(
                    _wait_cef_window_gone(browser_pid, 10),
                    "CEF candidate window survived cleanup",
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
                if control_group:
                    _wait_cgroup_removed(plan.cgroup_root, control_group, 10)
                if browser_pid > 0:
                    _wait_cef_window_gone(browser_pid, 10)
                raise


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--candidate-worker", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("--profile")
    parser.add_argument("--status")
    parser.add_argument("--markers")
    parser.add_argument("--stop")
    return parser


def _maybe_run_worker(argv: list[str]) -> int | None:
    if "--candidate-worker" not in argv:
        return None
    arguments = _worker_parser().parse_args(argv)
    required = (
        arguments.candidate,
        arguments.profile,
        arguments.status,
        arguments.markers,
        arguments.stop,
    )
    if not all(required):
        return 2
    return _candidate_worker(
        candidate_path=Path(arguments.candidate),
        profile=Path(arguments.profile),
        status_path=Path(arguments.status),
        marker_path=Path(arguments.markers),
        stop_path=Path(arguments.stop),
    )


if __name__ == "__main__":
    worker_result = _maybe_run_worker(sys.argv[1:])
    if worker_result is not None:
        raise SystemExit(worker_result)
    unittest.main()
