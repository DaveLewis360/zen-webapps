"""Opt-in direct smoke for an assembled, retained Firefox candidate tree."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from zen_app_runtime.app_shell import (
    app_shell_policy_sha256,
    build_app_shell_policy,
    parse_app_shell_handshake_payload,
    render_app_shell_policy,
)
from zen_app_runtime.benchmark_runner import (
    BenchmarkRunnerError,
    build_systemd_scope_plan,
    launch_systemd_scope,
)
from zen_app_runtime.browser_adapter import (
    browser_adapter_application_path,
    open_bundled_browser_adapter,
)
from zen_app_runtime.gecko_runtime_candidate import (
    gecko_runtime_candidate_executable_path,
    open_verified_gecko_runtime_candidate,
)
from zen_app_runtime.launch import _create_app_shell_policy_descriptor
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.native_app_shell_benchmark import (
    NativeAppShellBenchmarkError,
    _activate_onboarding,
    _benchmark_environment,
    _BrowserOutputConsumer,
    _create_stop_file,
    _new_secure_process_count,
    _prepare_profile,
    _read_handshake as _benchmark_read_handshake,
    _terminate_browser,
    _verify_profile_preference,
    _verify_static_runtime_invariants,
    _wait_cgroup_removed,
    _wait_worker_status,
    _WorkerStatus,
)
from tests.test_real_app_shell import (
    _OutputCollector,
    _read_handshake,
    _terminate,
    _wait_session_empty,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_CANDIDATE = os.environ.get("ZEN_REAL_GECKO_CANDIDATE")
REAL_WAYLAND = os.environ.get("ZEN_REAL_GECKO_CANDIDATE_WAYLAND") == "1"


def _candidate_worker(
    *,
    candidate_path: Path,
    profile: Path,
    status_path: Path,
    stop_path: Path,
) -> int:
    """Run direct retained candidate authority inside the controller's scope."""
    if status_path.exists() or stop_path.exists():
        raise NativeAppShellBenchmarkError("candidate worker control file already exists")
    if status_path.parent != profile.parent or stop_path.parent != profile.parent:
        raise NativeAppShellBenchmarkError("candidate worker files must share one private root")
    _prepare_profile(profile, "baseline")
    manifest = create_manifest(
        "real-gecko-candidate-wayland",
        "Real Gecko Candidate Wayland",
        "https://example.com/",
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
        with open_verified_gecko_runtime_candidate(candidate_path) as runtime, \
             open_bundled_browser_adapter() as adapter:
            executable = gecko_runtime_candidate_executable_path(runtime)
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
                executable,
                "-app",
                browser_adapter_application_path(adapter),
                "--profile",
                f"/proc/self/fd/{profile_descriptor}",
                "--no-remote",
            )
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                executable=executable,
                close_fds=True,
                env=environment,
                pass_fds=(
                    runtime.runtime_descriptor,
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
                handshake = _benchmark_read_handshake(handshake_read, 20)
                parse_app_shell_handshake_payload(
                    handshake,
                    expected_policy_sha256=app_shell_policy_sha256(policy),
                )
            except BaseException:
                failure_code = "handshake-failed"
                raise
            status.update(handshakeReady=True, state="handshake")

            deadline = time.monotonic() + 90
            while not stop_path.exists():
                if process.poll() is not None:
                    failure_code = "browser-exited"
                    raise NativeAppShellBenchmarkError(
                        "candidate browser exited before controller stop"
                    )
                if time.monotonic() >= deadline:
                    failure_code = "stop-timeout"
                    raise NativeAppShellBenchmarkError(
                        "candidate worker stop timed out"
                    )
                time.sleep(0.05)
            return_code = _terminate_browser(process)
            if return_code not in (0, -signal.SIGTERM):
                raise NativeAppShellBenchmarkError(
                    f"candidate browser returned unexpected status {return_code}"
                )
            consumer.join()
            if not _verify_profile_preference(profile, "baseline"):
                raise NativeAppShellBenchmarkError(
                    "candidate baseline preference verification failed"
                )
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
            f"real Gecko candidate worker failed: {type(error).__name__}: {error}",
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


@unittest.skipUnless(
    REAL_CANDIDATE,
    "set ZEN_REAL_GECKO_CANDIDATE to an assembled Firefox candidate",
)
class RealGeckoCandidateTests(unittest.TestCase):
    def test_headless_retained_candidate_app_shell_handshake(self) -> None:
        candidate_path = Path(REAL_CANDIDATE or "")
        self.assertTrue(candidate_path.is_absolute())
        manifest = create_manifest(
            "real-gecko-candidate-headless",
            "Real Gecko Candidate Headless",
            "https://example.com/",
        )
        policy = build_app_shell_policy(manifest)
        policy_payload = render_app_shell_policy(policy).encode("utf-8")
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary, \
             open_verified_gecko_runtime_candidate(candidate_path) as runtime, \
             open_bundled_browser_adapter() as adapter:
            profile = Path(temporary) / "profile"
            profile.mkdir(mode=0o700)
            executable = gecko_runtime_candidate_executable_path(runtime)
            policy_read = _create_app_shell_policy_descriptor(policy_payload)
            handshake_read, handshake_write = os.pipe2(os.O_CLOEXEC)
            process: subprocess.Popen[str] | None = None
            collector: _OutputCollector | None = None
            stdout = ""
            stderr = ""
            try:
                environment = {
                    **os.environ,
                    "ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD": str(policy_read),
                    "ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD": str(handshake_write),
                    "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTART": "1",
                    "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTOP": "1",
                    "ZEN_APP_RUNTIME_APP_SHELL_TEST_MEDIATION": "1",
                }
                command = (
                    executable,
                    "-app",
                    browser_adapter_application_path(adapter),
                    "--profile",
                    str(profile),
                    "--headless",
                    "--no-remote",
                )
                process = subprocess.Popen(
                    command,
                    executable=executable,
                    close_fds=True,
                    env=environment,
                    pass_fds=(
                        runtime.runtime_descriptor,
                        adapter.descriptor,
                        policy_read,
                        handshake_write,
                    ),
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                collector = _OutputCollector(process)
                os.close(policy_read)
                policy_read = -1
                os.close(handshake_write)
                handshake_write = -1
                before_ack = collector.wait_output(
                    "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY permission download xdg-desktop-portal",
                    20,
                )
                self.assertIn(
                    "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_SELFTEST "
                    "permission-no-activation-denied permission-malformed-denied "
                    "download-cancelled",
                    f"{before_ack[0]}\n{before_ack[1]}",
                )
                handshake = _read_handshake(handshake_read, 20)
                parsed = parse_app_shell_handshake_payload(
                    handshake,
                    expected_policy_sha256=app_shell_policy_sha256(policy),
                )
                self.assertEqual("ready-before-site-content", parsed.status)
                process.wait(timeout=35)
                stdout, stderr = collector.join()
            except BaseException:
                if process is not None:
                    _terminate(process)
                    if collector is not None:
                        stdout, stderr = collector.join()
                raise
            finally:
                for descriptor in (policy_read, handshake_read, handshake_write):
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
            assert process is not None
            self.assertEqual(0, process.returncode, stderr)
            combined = f"{stdout}\n{stderr}"
            self.assertNotIn("ZEN_APP_RUNTIME_APP_SHELL_FATAL", combined)
            markers = (
                "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY",
                "ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY",
                "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED",
                "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED",
            )
            for marker in markers:
                self.assertIn(marker, combined, combined)
            self.assertEqual(list(markers), sorted(markers, key=combined.index))
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_RESTORED download",
                combined,
            )
            self.assertEqual({}, _wait_session_empty(process.pid, 8))

    @unittest.skipUnless(
        REAL_WAYLAND,
        "set ZEN_REAL_GECKO_CANDIDATE_WAYLAND=1 for native Wayland cgroup smoke",
    )
    def test_wayland_cgroup_app_shell_sandbox_smoke(self) -> None:
        candidate_path = Path(REAL_CANDIDATE or "")
        self.assertTrue(candidate_path.is_absolute())
        _verify_static_runtime_invariants()
        with tempfile.TemporaryDirectory(
            prefix=".real-gecko-candidate-", dir=PROJECT_ROOT
        ) as temporary:
            private_root = Path(temporary)
            private_root.chmod(0o700)
            profile = private_root / "profile"
            status_path = private_root / "status.json"
            stop_path = private_root / "stop"
            environment_root = private_root / "environment"
            environment_root.mkdir(mode=0o700)
            environment = _benchmark_environment(environment_root)
            executable = Path(sys.executable)
            self.assertTrue(executable.is_absolute())
            command = (
                str(executable),
                "-m",
                "tests.test_real_gecko_candidate",
                "--candidate-worker",
                "--candidate",
                str(candidate_path),
                "--profile",
                str(profile),
                "--status",
                str(status_path),
                "--stop",
                str(stop_path),
            )
            scope_name = f"zen-app-benchmark-gecko-{os.getpid() % 100000}"
            plan = build_systemd_scope_plan(
                command,
                scope_name=scope_name,
                working_directory=PROJECT_ROOT,
            )
            scope = None
            control_group = ""
            try:
                scope = launch_systemd_scope(
                    plan, environment, discovery_timeout=8.0
                )
                control_group = scope.control_group
                onboarding = _wait_worker_status(
                    status_path,
                    lambda value: (
                        value["handshakeReady"]
                        and value["onboardingMilliseconds"] is not None
                    ),
                    timeout=40,
                    scope=scope,
                    stage="candidate-onboarding",
                )
                self.assertFalse(onboarding["contentCommitted"])
                onboarding_sample = scope.sample(max_attempts=10)
                self.assertGreater(onboarding_sample.summed_pss_kib, 0)
                _activate_onboarding(int(onboarding["browserPid"]))
                active = _wait_worker_status(
                    status_path,
                    lambda value: (
                        value["contentCommitted"]
                        and value["usableMilliseconds"] is not None
                    ),
                    timeout=50,
                    scope=scope,
                    stage="candidate-content",
                )
                self.assertTrue(active["contentCommitted"])
                time.sleep(2)
                active_sample = scope.sample(max_attempts=10)
                self.assertGreater(
                    len(active_sample.processes),
                    len(onboarding_sample.processes),
                )
                self.assertGreaterEqual(
                    _new_secure_process_count(onboarding_sample, active_sample),
                    1,
                    "no new Seccomp=2 + NoNewPrivs renderer/socket process appeared",
                )
                _create_stop_file(stop_path)
                finished = _wait_worker_status(
                    status_path,
                    lambda value: value["state"] == "finished",
                    timeout=35,
                    scope=scope,
                    stage="candidate-cleanup",
                )
                self.assertTrue(finished["preferenceVerified"])
                self.assertEqual(0, scope.wait(timeout=20))
                scope.close()
                scope = None
                self.assertTrue(
                    _wait_cgroup_removed(plan.cgroup_root, control_group, 10),
                    "candidate cgroup survived cleanup",
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


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--candidate-worker", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("--profile")
    parser.add_argument("--status")
    parser.add_argument("--stop")
    return parser


def _maybe_run_worker(argv: list[str]) -> int | None:
    if "--candidate-worker" not in argv:
        return None
    arguments = _worker_parser().parse_args(argv)
    required = (arguments.candidate, arguments.profile, arguments.status, arguments.stop)
    if not all(required):
        return 2
    return _candidate_worker(
        candidate_path=Path(arguments.candidate),
        profile=Path(arguments.profile),
        status_path=Path(arguments.status),
        stop_path=Path(arguments.stop),
    )


if __name__ == "__main__":
    worker_result = _maybe_run_worker(sys.argv[1:])
    if worker_result is not None:
        raise SystemExit(worker_result)
    unittest.main()
