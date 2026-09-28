"""Opt-in exact-Zen smoke for the bundled privileged App Shell adapter."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from zen_app_runtime.app_shell import (
    app_shell_policy_sha256,
    build_app_shell_policy,
    parse_app_shell_handshake_payload,
    render_app_shell_policy,
)
from zen_app_runtime.browser_adapter import (
    browser_adapter_application_path,
    open_bundled_browser_adapter,
)
from zen_app_runtime.launch import _create_app_shell_policy_descriptor
from zen_app_runtime.manifest import create_manifest
from tests.test_real_zen import (
    _descendants,
    _live_session_members,
    _process_environment,
    _process_table,
    _terminate_session,
    _x11_window_pids,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_RUNTIME = os.environ.get("ZEN_REAL_RUNTIME")
REAL_GUI = os.environ.get("ZEN_REAL_APP_SHELL_GUI") == "1"
REAL_BACKENDS = tuple(
    item.strip()
    for item in os.environ.get("ZEN_REAL_BACKENDS", "wayland,x11").split(",")
    if item.strip()
)


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
        if b"\n" in payload:
            document, trailing = bytes(payload).split(b"\n", 1)
            if trailing:
                raise AssertionError("handshake contained trailing bytes")
            return document
    raise AssertionError(f"handshake was not received: {bytes(payload)!r}")


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


class _OutputCollector:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._lock = threading.Lock()
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._streams = (process.stdout, process.stderr)
        self._threads = [
            threading.Thread(
                target=self._consume,
                args=(process.stdout, self._stdout),
                daemon=True,
            ),
            threading.Thread(
                target=self._consume,
                args=(process.stderr, self._stderr),
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def _consume(self, stream, destination: list[str]) -> None:
        if stream is None:
            return
        while True:
            chunk = stream.readline()
            if not chunk:
                return
            with self._lock:
                destination.append(chunk)

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            return "".join(self._stdout), "".join(self._stderr)

    def wait_output(self, marker: str, timeout: float) -> tuple[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stdout, stderr = self.snapshot()
            if marker in stdout or marker in stderr:
                return stdout, stderr
            time.sleep(0.05)
        stdout, stderr = self.snapshot()
        combined = f"{stdout}\n{stderr}"
        raise AssertionError(f"missing marker {marker!r}: {combined[-4000:]}")

    def join(self) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout=5)
        for stream in self._streams:
            if stream is not None and not stream.closed:
                stream.close()
        return self.snapshot()


def _hypr_window(pids: set[int], timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_clients: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["hyprctl", "clients", "-j"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            last_clients = json.loads(result.stdout)
            for client in last_clients:
                if (
                    int(client.get("pid", -1)) in pids
                    and str(client.get("class", "")).lower() == "zen-app-shell"
                ):
                    return client
        time.sleep(0.1)
    raise AssertionError(f"zen-app-shell window not mapped for {sorted(pids)}: {last_clients}")


def _wait_session_empty(session_id: int, timeout: float) -> dict[int, dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = _live_session_members(session_id)
        if not remaining:
            return {}
        time.sleep(0.1)
    return _live_session_members(session_id)


@unittest.skipUnless(REAL_RUNTIME, "set ZEN_REAL_RUNTIME for the exact-Zen App Shell smoke")
class RealAppShellTests(unittest.TestCase):
    def test_headless_precontent_handshake_then_site_commit(self) -> None:
        runtime = Path(REAL_RUNTIME or "")
        self.assertTrue(runtime.is_absolute())
        self.assertTrue(runtime.is_file())
        self.assertTrue(os.access(runtime, os.X_OK))
        manifest = create_manifest(
            "real-app-shell-smoke",
            "Real App Shell Smoke",
            "https://example.com/",
        )
        policy = build_app_shell_policy(manifest)
        policy_payload = render_app_shell_policy(policy).encode("utf-8")

        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary, \
             open_bundled_browser_adapter() as adapter:
            profile = Path(temporary) / "profile"
            profile.mkdir(mode=0o700)
            policy_read = _create_app_shell_policy_descriptor(policy_payload)
            policy_write = -1
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
                command = [
                    str(runtime),
                    "-app",
                    browser_adapter_application_path(adapter),
                    "--profile",
                    str(profile),
                    "--headless",
                    "--no-remote",
                ]
                process = subprocess.Popen(
                    command,
                    close_fds=True,
                    env=environment,
                    pass_fds=(adapter.descriptor, policy_read, handshake_write),
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

                # The privileged mediation/factory boundary must own its
                # contracts before the parent consumes the readiness ACK.
                pre_ack_stdout, pre_ack_stderr = collector.wait_output(
                    "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY permission download xdg-desktop-portal",
                    15,
                )
                pre_ack = f"{pre_ack_stdout}\n{pre_ack_stderr}"
                self.assertIn(
                    "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_SELFTEST "
                    "permission-no-activation-denied permission-malformed-denied "
                    "download-cancelled",
                    pre_ack,
                )
                handshake = _read_handshake(handshake_read, 15)
                parsed = parse_app_shell_handshake_payload(
                    handshake,
                    expected_policy_sha256=app_shell_policy_sha256(policy),
                )
                self.assertEqual("ready-before-site-content", parsed.status)
                process.wait(timeout=30)
                stdout, stderr = collector.join()
            except BaseException:
                if process is not None:
                    _terminate(process)
                    if collector is not None:
                        stdout, stderr = collector.join()
                    else:
                        stdout, stderr = process.communicate()
                    print(f"App Shell probe return code: {process.returncode}")
                    if stdout:
                        print(stdout)
                    if stderr:
                        print(stderr)
                raise
            finally:
                for descriptor in (
                    policy_write,
                    policy_read,
                    handshake_write,
                    handshake_read,
                ):
                    if descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
            assert process is not None
            self.assertEqual(0, process.returncode, stderr)
            combined = f"{stdout}\n{stderr}"
            self.assertNotIn("ZEN_APP_RUNTIME_APP_SHELL_FATAL", combined, combined)
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY "
                "permission download xdg-desktop-portal",
                combined,
                combined,
            )
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_RESTORED download",
                combined,
                combined,
            )
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY",
                combined,
                combined,
            )
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED",
                combined,
                combined,
            )
            self.assertIn(
                "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED",
                combined,
                combined,
            )
            self.assertLess(
                combined.index("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_SELFTEST"),
                combined.index("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY"),
            )
            mediation_ready = combined.index(
                "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY"
            )
            onboarding = combined.index("ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY")
            requested = combined.index("ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED")
            committed = combined.index("ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED")
            self.assertLess(mediation_ready, onboarding)
            self.assertLess(onboarding, requested)
            self.assertLess(requested, committed)
            self.assertEqual({}, _wait_session_empty(process.pid, 8))
    @unittest.skipUnless(
        REAL_GUI,
        "set ZEN_REAL_APP_SHELL_GUI=1 for Wayland/X11 App Shell GUI smoke",
    )
    def test_supervised_wayland_and_x11_onboarding_window_and_sandbox(self) -> None:
        runtime = Path(REAL_RUNTIME or "")
        self.assertTrue(set(REAL_BACKENDS) <= {"wayland", "x11"})
        for backend in REAL_BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(
                dir=PROJECT_ROOT
            ) as temporary:
                root = Path(temporary)
                runtime_lock = root / "runtime-lock.json"
                runtime_lock.write_text(
                    json.dumps(
                        {
                            "runtimeLockVersion": 1,
                            "runtime": {
                                "path": str(runtime),
                                "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                runtime_lock.chmod(0o600)
                command = [
                    sys.executable,
                    "-m",
                    "zen_app_runtime",
                    "--store",
                    str(root / "definitions"),
                    "app",
                    "onboard",
                    "https://example.com/",
                    "--profile-root",
                    str(root / "profiles"),
                    "--runtime-lock",
                    str(runtime_lock),
                    "--display-backend",
                    backend,
                    "--bundled-app-shell",
                    "--launch",
                    "--yes",
                ]
                environment = {
                    **os.environ,
                    "PYTHONPATH": str(PROJECT_ROOT / "src"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                    start_new_session=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                collector = _OutputCollector(process)
                runtime_session: int | None = None
                try:
                    initial_stdout, initial_stderr = collector.wait_output(
                        "ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY",
                        20,
                    )
                    self.assertNotIn(
                        "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED",
                        f"{initial_stdout}\n{initial_stderr}",
                        "site content started before onboarding confirmation",
                    )
                    table = _process_table()
                    descendants = _descendants(process.pid, table)
                    leaders = [
                        (pid, table[pid])
                        for pid in descendants
                        if " -app /proc/self/fd/" in str(table[pid]["command"])
                        and "application.ini" in str(table[pid]["command"])
                    ]
                    self.assertEqual(1, len(leaders), leaders)
                    leader_pid, leader = leaders[0]
                    runtime_session = int(leader["session"])
                    window = _hypr_window(descendants, 15)
                    self.assertIn("Zen App Shell", str(window.get("title", "")))
                    address = str(window.get("address", ""))
                    active_result = subprocess.run(
                        ["hyprctl", "activewindow", "-j"],
                        capture_output=True,
                        check=False,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(0, active_result.returncode, active_result.stderr)
                    active = json.loads(active_result.stdout)
                    self.assertEqual(
                        address,
                        str(active.get("address", "")),
                        {"expected": window, "active": active},
                    )
                    if backend == "x11":
                        search = subprocess.run(
                            [
                                "xdotool",
                                "search",
                                "--pid",
                                str(leader_pid),
                                "--class",
                                "zen-app-shell",
                            ],
                            capture_output=True,
                            check=False,
                            text=True,
                            timeout=5,
                        )
                        self.assertEqual(0, search.returncode, search.stderr)
                        window_ids = search.stdout.split()
                        self.assertTrue(window_ids, search.stdout)
                        key = subprocess.run(
                            ["ydotool", "key", "28:1", "28:0"],
                            capture_output=True,
                            check=False,
                            text=True,
                            timeout=5,
                        )
                    else:
                        key = subprocess.run(
                            ["wtype", "-k", "Return"],
                            capture_output=True,
                            check=False,
                            text=True,
                            timeout=5,
                        )
                    self.assertEqual(0, key.returncode, key.stderr)
                    collector.wait_output(
                        "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED",
                        10,
                    )
                    collector.wait_output(
                        "ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED",
                        30,
                    )
                    time.sleep(1)
                    table = _process_table()
                    descendants = _descendants(process.pid, table)
                    content = [
                        record
                        for pid, record in table.items()
                        if pid in descendants
                        and "-contentproc" in str(record["command"])
                        and "-isForBrowser" in str(record["command"])
                    ]
                    self.assertTrue(content, "no remote web content process was observed")
                    self.assertTrue(
                        any(
                            record["seccomp"] == 2
                            and record["no_new_privs"] == 1
                            and "-sandboxReporter" in str(record["command"])
                            and "-chrootClient" in str(record["command"])
                            for record in content
                        ),
                        content,
                    )
                    leader_environment = _process_environment(leader_pid)
                    self.assertIn(
                        "ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD",
                        leader_environment,
                    )
                    self.assertIn(
                        "ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD",
                        leader_environment,
                    )
                    self.assertNotIn(
                        "https://example.com/",
                        str(leader["command"]),
                    )
                    self.assertIn("/proc/self/fd/", str(leader["command"]))
                    if backend == "wayland":
                        self.assertEqual(
                            "wayland",
                            leader_environment.get("GDK_BACKEND"),
                        )
                    else:
                        self.assertEqual("x11", leader_environment.get("GDK_BACKEND"))
                        self.assertIn(leader_pid, _x11_window_pids())

                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=20)
                    stdout, stderr = collector.join()
                    self.assertEqual(143, process.returncode, stderr[-4000:])
                    result, _end = json.JSONDecoder().raw_decode(stdout)
                    self.assertEqual(2, result["webAppOnboardingResultVersion"])
                    self.assertTrue(result["browserExperience"]["inBrowserAppShell"])
                    self.assertNotIn(
                        "ZEN_APP_RUNTIME_APP_SHELL_FATAL",
                        f"{stdout}\n{stderr}",
                    )
                    self.assertEqual(
                        {},
                        _wait_session_empty(runtime_session, 8),
                    )
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            _terminate(process)
                    collector.join()
                    if runtime_session is not None:
                        remaining = _wait_session_empty(runtime_session, 1)
                        if remaining:
                            _terminate_session(runtime_session)


if __name__ == "__main__":
    unittest.main()
