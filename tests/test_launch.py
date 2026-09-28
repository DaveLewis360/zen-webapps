import fcntl
from dataclasses import replace
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from zen_app_runtime.app_shell import (
    APP_SHELL_ADAPTER_ID,
    REQUIRED_ADAPTER_CAPABILITIES,
    app_shell_policy_sha256,
    build_app_shell_policy,
)
import zen_app_runtime.launch as launch
from zen_app_runtime.launch import (
    LaunchBusyError,
    LaunchExecutionError,
    LaunchSafetyError,
    execute_launch,
    inspect_launch,
    launch_ticket_to_dict,
    render_launch_ticket,
)
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.profile import (
    manifest_sha256,
    provision_profile,
    verify_profile_binding,
)
from zen_app_runtime.runtime import runtime_identity_to_dict, verify_runtime
from zen_app_runtime.store import AppStore


FAKE_RUNTIME = """#!__PYTHON__
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

variant = __VARIANT__
profile = Path(sys.argv[sys.argv.index("--profile") + 1])
mode_file = profile / "fake-runtime-mode.txt"
mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else ""
app_shell_policy = None
if "--zen-app-runtime-app-shell-policy-fd" in sys.argv:
    policy_fd = int(sys.argv[sys.argv.index("--zen-app-runtime-app-shell-policy-fd") + 1])
    handshake_fd = int(sys.argv[sys.argv.index("--zen-app-runtime-app-shell-handshake-fd") + 1])
    with os.fdopen(policy_fd, "r", encoding="utf-8") as stream:
        app_shell_policy = json.load(stream)
    handshake = {
        "adapterId": "org.zen-app-runtime.app-shell",
        "adapterProtocolVersion": 2,
        "handshakeVersion": 2,
        "policySha256": hashlib.sha256(
            (json.dumps(app_shell_policy, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\\n").encode("utf-8")
        ).hexdigest(),
        "status": "ready-before-site-content",
    }
    if mode == "app-shell-bad-handshake":
        handshake["policySha256"] = "0" * 64
    if mode != "app-shell-no-handshake":
        os.write(handshake_fd, (json.dumps(handshake, sort_keys=True) + "\\n").encode("utf-8"))
    os.close(handshake_fd)
    url = app_shell_policy["startUrl"]
else:
    url = sys.argv[-1]
result = {
    "appShellPolicy": app_shell_policy,
    "argv": sys.argv,
    "environment": dict(os.environ),
    "pgid": os.getpgrp(),
    "pid": os.getpid(),
    "variant": variant,
}
(profile / "fake-runtime-result.json").write_text(
    json.dumps(result, sort_keys=True) + "\\n", encoding="utf-8"
)
if mode == "wait" or url.endswith("/wait"):
    def stop(signum, _frame):
        (profile / "fake-runtime-signal.txt").write_text(str(signum), encoding="utf-8")
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    for forwarded in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
        signal.signal(forwarded, stop)
    (profile / "fake-runtime-ready").write_text("ready", encoding="utf-8")
    while True:
        signal.pause()
if mode == "linger":
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (profile / "fake-runtime-linger-pid").write_text(str(os.getpid()), encoding="utf-8")
        while True:
            signal.pause()
    raise SystemExit(0)
if mode.startswith("exit:"):
    raise SystemExit(int(mode.split(":", 1)[1]))
if "/exit-" in url:
    raise SystemExit(int(url.rsplit("/exit-", 1)[1]))
"""


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.temporary.name)
        self.profile_root = self.base / "profiles"
        self.manifest = create_manifest("demo", "Demo", "https://demo.example.com/exit-0")
        self.runtime = self.base / "runtime" / "zen"
        self.runtime.parent.mkdir(mode=0o700)
        self._write_runtime(self.runtime, "ORIGINAL")
        self.lock = self.base / "runtime-lock.json"
        self._write_lock()
        provision_profile(self.profile_root, self.manifest, self.lock)
        self.profile = self.profile_root / "demo"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_runtime(path: Path, variant: str) -> None:
        path.write_text(
            FAKE_RUNTIME.replace("__PYTHON__", sys.executable).replace(
                "__VARIANT__", repr(variant)
            ),
            encoding="utf-8",
        )
        path.chmod(0o700)

    def _write_lock(self) -> None:
        self.lock.write_text(
            json.dumps(
                {
                    "runtimeLockVersion": 1,
                    "runtime": {
                        "path": str(self.runtime),
                        "sha256": hashlib.sha256(self.runtime.read_bytes()).hexdigest(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.lock.chmod(0o600)

    def _write_app_shell_capability(self, *, digest: str | None = None) -> Path:
        identity = verify_runtime(self.lock)
        capability = self.base / "app-shell-capability.json"
        runtime_identity = runtime_identity_to_dict(identity)
        if digest is not None:
            runtime_identity["sha256"] = digest
        capability.write_text(
            json.dumps(
                {
                    "adapterCapabilityVersion": 2,
                    "adapterId": APP_SHELL_ADAPTER_ID,
                    "adapterProtocolVersion": 2,
                    "capabilities": list(REQUIRED_ADAPTER_CAPABILITIES),
                    "runtimeIdentity": runtime_identity,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        capability.chmod(0o600)
        return capability

    def test_inspect_is_canonical_deterministic_and_side_effect_free(self):
        source_environment = {
            "DISPLAY": ":99",
            "LC_TIME": "C",
            "LD_PRELOAD": "/host/inject.so",
            "MOZ_ENABLE_WAYLAND": "1",
            "PATH": "/host/bin",
            "PYTHONPATH": "/host/python",
            "UNKNOWN_SECRET": "secret",
        }
        state = self.profile / ".zen-app-runtime"
        lock_file = self.profile / launch.PROFILE_LOCK_NAME
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")), \
             mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            first = inspect_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                environment=source_environment,
            )
            second = inspect_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                environment=source_environment,
            )
        expected_environment = {
            "DISPLAY": ":99",
            "HOME": str(state / "home"),
            "LC_TIME": "C",
            "PATH": launch.SAFE_PATH,
            "TMPDIR": str(state / "tmp"),
            "XDG_CACHE_HOME": str(state / "cache"),
            "XDG_CONFIG_HOME": str(state / "config"),
            "XDG_DATA_HOME": str(state / "data"),
            "XDG_STATE_HOME": str(state / "state"),
        }
        expected = {
            "appId": "demo",
            "argv": [
                str(self.runtime),
                "--no-remote",
                "--new-instance",
                "--profile",
                str(self.profile),
                self.manifest.start_url,
            ],
            "environment": expected_environment,
            "launchTicketVersion": 1,
            "manifest": {
                "schemaVersion": 1,
                "sha256": manifest_sha256(self.manifest),
            },
            "profilePath": str(self.profile),
            "runtimeIdentity": runtime_identity_to_dict(first.runtime_identity),
        }
        rendered = json.dumps(expected, indent=2, sort_keys=True) + "\n"
        self.assertEqual(expected, launch_ticket_to_dict(first))
        self.assertEqual(rendered, render_launch_ticket(first))
        self.assertEqual(rendered, render_launch_ticket(second))
        self.assertNotIn("/proc/self/fd/", rendered)
        self.assertFalse(state.exists())
        self.assertFalse(lock_file.exists())

    def test_display_ticket_v2_is_canonical_for_wayland_x11_and_auto(self):
        state = self.profile / ".zen-app-runtime"
        source_environment = {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "DISPLAY": ":99",
            "GDK_BACKEND": "host-value-is-ignored",
            "MOZ_ENABLE_WAYLAND": "host-value-is-ignored",
            "WAYLAND_DISPLAY": "wayland-9",
            "XAUTHORITY": "/tmp/test-xauthority",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        wayland = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment=source_environment,
            display_backend="wayland",
        )
        wayland_data = launch_ticket_to_dict(wayland)
        self.assertEqual(2, wayland_data["launchTicketVersion"])
        expected_wayland_environment = {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "GDK_BACKEND": "wayland",
            "HOME": str(state / "home"),
            "MOZ_ENABLE_WAYLAND": "1",
            "PATH": launch.SAFE_PATH,
            "TMPDIR": str(state / "tmp"),
            "WAYLAND_DISPLAY": "wayland-9",
            "XDG_CACHE_HOME": str(state / "cache"),
            "XDG_CONFIG_HOME": str(state / "config"),
            "XDG_DATA_HOME": str(state / "data"),
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "XDG_STATE_HOME": str(state / "state"),
        }
        self.assertEqual(
            {
                "appId": "demo",
                "argv": [
                    str(self.runtime),
                    "--no-remote",
                    "--new-instance",
                    "--profile",
                    str(self.profile),
                    self.manifest.start_url,
                ],
                "displayBackend": {"requested": "wayland", "resolved": "wayland"},
                "environment": expected_wayland_environment,
                "launchTicketVersion": 2,
                "manifest": {
                    "schemaVersion": 1,
                    "sha256": manifest_sha256(self.manifest),
                },
                "profilePath": str(self.profile),
                "runtimeIdentity": runtime_identity_to_dict(wayland.runtime_identity),
            },
            wayland_data,
        )
        self.assertEqual(
            {"requested": "wayland", "resolved": "wayland"},
            wayland_data["displayBackend"],
        )
        self.assertEqual("wayland-9", wayland_data["environment"]["WAYLAND_DISPLAY"])
        self.assertEqual("wayland", wayland_data["environment"]["GDK_BACKEND"])
        self.assertEqual("1", wayland_data["environment"]["MOZ_ENABLE_WAYLAND"])
        self.assertNotIn("DISPLAY", wayland_data["environment"])
        self.assertNotIn("XAUTHORITY", wayland_data["environment"])

        automatic = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment=source_environment,
            display_backend="auto",
        )
        automatic_data = launch_ticket_to_dict(automatic)
        self.assertEqual(
            {"requested": "auto", "resolved": "wayland"},
            automatic_data["displayBackend"],
        )
        self.assertEqual(
            wayland_data["environment"],
            automatic_data["environment"],
        )
        automatic_x11 = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment={"DISPLAY": ":88"},
            display_backend="auto",
        )
        self.assertEqual(
            {"requested": "auto", "resolved": "x11"},
            launch_ticket_to_dict(automatic_x11)["displayBackend"],
        )

        x11 = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment=source_environment,
            display_backend="x11",
        )
        x11_data = launch_ticket_to_dict(x11)
        self.assertEqual(
            {"requested": "x11", "resolved": "x11"},
            x11_data["displayBackend"],
        )
        self.assertEqual(":99", x11_data["environment"]["DISPLAY"])
        self.assertEqual("/tmp/test-xauthority", x11_data["environment"]["XAUTHORITY"])
        self.assertEqual("x11", x11_data["environment"]["GDK_BACKEND"])
        self.assertNotIn("WAYLAND_DISPLAY", x11_data["environment"])
        self.assertNotIn("MOZ_ENABLE_WAYLAND", x11_data["environment"])
        self.assertEqual(
            str(state / "home"),
            x11_data["environment"]["HOME"],
        )
        self.assertEqual(
            json.dumps(x11_data, indent=2, sort_keys=True) + "\n",
            render_launch_ticket(x11),
        )
        self.assertFalse(state.exists())
        self.assertFalse((self.profile / launch.PROFILE_LOCK_NAME).exists())

    def test_display_backend_validation_fails_before_execution_side_effects(self):
        cases = (
            ("bogus", {"DISPLAY": ":1"}, "one of"),
            ("auto", {}, "valid Wayland or X11"),
            ("wayland", {"DISPLAY": ":1"}, "WAYLAND_DISPLAY"),
            ("wayland", {"WAYLAND_DISPLAY": "wayland-1"}, "XDG_RUNTIME_DIR"),
            (
                "wayland",
                {"WAYLAND_DISPLAY": "nested/socket", "XDG_RUNTIME_DIR": "/run/user/1000"},
                "socket basename",
            ),
            ("x11", {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/run/user/1000"}, "DISPLAY"),
            ("x11", {"DISPLAY": ":1", "XAUTHORITY": "relative"}, "absolute"),
            (
                "auto",
                {"WAYLAND_DISPLAY": "", "DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1000"},
                "WAYLAND_DISPLAY",
            ),
        )
        for requested, environment, message in cases:
            with self.subTest(requested=requested, environment=environment), \
                 mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")):
                with self.assertRaisesRegex(LaunchSafetyError, message):
                    execute_launch(
                        self.profile_root,
                        self.manifest,
                        self.lock,
                        confirmed=True,
                        environment=environment,
                        display_backend=requested,
                    )
                self.assertFalse((self.profile / ".zen-app-runtime").exists())
                self.assertFalse((self.profile / launch.PROFILE_LOCK_NAME).exists())

    def test_display_backend_execute_uses_captured_sanitized_environment(self):
        source_environment = {
            "DISPLAY": ":77",
            "GDK_BACKEND": "host-injection",
            "MOZ_ENABLE_WAYLAND": "host-injection",
            "WAYLAND_DISPLAY": "wayland-7",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        original_prepare = launch._ensure_runtime_directories

        def mutate_source_after_ticket(profile_fd):
            source_environment["DISPLAY"] = ":attacker"
            source_environment["WAYLAND_DISPLAY"] = "attacker"
            original_prepare(profile_fd)

        with mock.patch.object(
            launch,
            "_ensure_runtime_directories",
            side_effect=mutate_source_after_ticket,
        ):
            status = execute_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                confirmed=True,
                environment=source_environment,
                display_backend="wayland",
            )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        child_environment = result["environment"]
        self.assertEqual("wayland-7", child_environment["WAYLAND_DISPLAY"])
        self.assertEqual("wayland", child_environment["GDK_BACKEND"])
        self.assertEqual("1", child_environment["MOZ_ENABLE_WAYLAND"])
        self.assertNotIn("DISPLAY", child_environment)

        status = execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment={
                "DISPLAY": ":78",
                "WAYLAND_DISPLAY": "wayland-8",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
            display_backend="x11",
        )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        child_environment = result["environment"]
        self.assertEqual(":78", child_environment["DISPLAY"])
        self.assertEqual("x11", child_environment["GDK_BACKEND"])
        self.assertNotIn("WAYLAND_DISPLAY", child_environment)
        self.assertNotIn("MOZ_ENABLE_WAYLAND", child_environment)

    def test_renderer_rejects_fabricated_url_and_environment_authority(self):
        ticket = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment={},
        )
        with self.assertRaisesRegex(LaunchSafetyError, "fixed runtime contract"):
            launch_ticket_to_dict(replace(ticket, start_url="https://other.example.com"))
        injected_environment = tuple(
            sorted((*ticket.environment, ("LD_PRELOAD", "/tmp/inject.so")))
        )
        with self.assertRaisesRegex(LaunchSafetyError, "not allowlisted"):
            launch_ticket_to_dict(replace(ticket, environment=injected_environment))

        display_ticket = inspect_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            environment={
                "WAYLAND_DISPLAY": "wayland-1",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
            display_backend="wayland",
        )
        with self.assertRaisesRegex(LaunchSafetyError, "resolution mismatch"):
            launch_ticket_to_dict(
                replace(display_ticket, resolved_display_backend="x11")
            )
        contradictory_environment = tuple(
            sorted((*display_ticket.environment, ("DISPLAY", ":1")))
        )
        with self.assertRaisesRegex(LaunchSafetyError, "environment policy mismatch"):
            launch_ticket_to_dict(
                replace(display_ticket, environment=contradictory_environment)
            )

    def test_profile_binding_is_read_only_and_allows_browser_files(self):
        browser_file = self.profile / "prefs.js"
        browser_file.write_text("// browser state\n", encoding="utf-8")
        before = sorted(item.name for item in self.profile.iterdir())
        binding = verify_profile_binding(self.profile_root, self.manifest, self.lock)
        self.assertEqual(self.profile, binding.path)
        self.assertEqual(before, sorted(item.name for item in self.profile.iterdir()))

    def test_execute_requires_confirmation_before_side_effects(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")):
            with self.assertRaisesRegex(LaunchSafetyError, "--yes"):
                execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=False,
                )
        self.assertFalse((self.profile / launch.PROFILE_LOCK_NAME).exists())
        self.assertFalse((self.profile / ".zen-app-runtime").exists())

    def test_execute_uses_fixed_argv_isolated_environment_and_propagates_exit(self):
        (self.profile / "fake-runtime-mode.txt").write_text("exit:7", encoding="utf-8")
        environment = {
            "DISPLAY": ":7",
            "WAYLAND_DISPLAY": "wayland-9",
            "LANG": "C.UTF-8",
            "LD_PRELOAD": "/host/inject.so",
            "MOZ_LOG": "all:5",
            "PATH": "/host/bin",
            "PYTHONPATH": "/host/python",
            "SECRET_TOKEN": "secret",
        }
        status = execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment=environment,
        )
        self.assertEqual(7, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        self.assertEqual(
            [
                result["argv"][0],
                "--no-remote",
                "--new-instance",
                "--profile",
                result["argv"][4],
                self.manifest.start_url,
            ],
            result["argv"],
        )
        self.assertRegex(result["argv"][0], r"^/proc/self/fd/\d+$")
        self.assertRegex(result["argv"][4], r"^/proc/self/fd/\d+$")
        child_environment = result["environment"]
        self.assertEqual(":7", child_environment["DISPLAY"])
        self.assertEqual("wayland-9", child_environment["WAYLAND_DISPLAY"])
        self.assertEqual(launch.SAFE_PATH, child_environment["PATH"])
        self.assertRegex(
            child_environment["HOME"],
            r"^/proc/self/fd/\d+/\.zen-app-runtime/home$",
        )
        for rejected in ("LD_PRELOAD", "MOZ_LOG", "PYTHONPATH", "SECRET_TOKEN"):
            self.assertNotIn(rejected, child_environment)
        for relative in ("home", "cache", "config", "data", "state", "tmp"):
            path = self.profile / ".zen-app-runtime" / relative
            self.assertTrue(path.is_dir())
            self.assertEqual(0o700, path.stat().st_mode & 0o777)

    def test_runtime_path_replacement_still_executes_the_verified_descriptor(self):
        original_prepare = launch._ensure_runtime_directories

        def replace_runtime(profile_fd):
            original_prepare(profile_fd)
            detached = self.runtime.with_name("verified-original")
            self.runtime.rename(detached)
            self._write_runtime(self.runtime, "REPLACEMENT")

        with mock.patch.object(launch, "_ensure_runtime_directories", side_effect=replace_runtime):
            status = execute_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                confirmed=True,
                environment={},
            )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        self.assertEqual("ORIGINAL", result["variant"])

    def test_popen_boundary_profile_rebind_uses_retained_profile_descriptor(self):
        original_profile_proc = launch._verified_proc_profile
        detached = self.profile.with_name("demo-retained")

        def rebind_profile(opened):
            proc_path = original_profile_proc(opened)
            self.profile.rename(detached)
            self.profile.mkdir(mode=0o700)
            return proc_path

        try:
            with mock.patch.object(
                launch,
                "_verified_proc_profile",
                side_effect=rebind_profile,
            ):
                status = execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=True,
                    environment={},
                )
            self.assertEqual(0, status)
            self.assertFalse((self.profile / "fake-runtime-result.json").exists())
            result = json.loads((detached / "fake-runtime-result.json").read_text())
            self.assertEqual("ORIGINAL", result["variant"])
            self.assertRegex(result["argv"][4], r"^/proc/self/fd/\d+$")
            self.assertRegex(
                result["environment"]["HOME"],
                r"^/proc/self/fd/\d+/\.zen-app-runtime/home$",
            )
        finally:
            if detached.exists():
                if self.profile.exists():
                    self.profile.rmdir()
                detached.rename(self.profile)

    def test_popen_boundary_mutation_executes_only_the_sealed_runtime_snapshot(self):
        original_popen = subprocess.Popen
        captured: dict[str, object] = {}

        def mutate_source_then_spawn(arguments, *positional, **keywords):
            captured["arguments"] = tuple(arguments)
            captured["executable"] = keywords.get("executable")
            captured["pass_fds"] = tuple(keywords.get("pass_fds", ()))
            with self.runtime.open("r+b") as runtime_file:
                runtime_file.seek(0)
                runtime_file.write(b"X")
                runtime_file.flush()
                os.fsync(runtime_file.fileno())
            return original_popen(arguments, *positional, **keywords)

        with mock.patch.object(subprocess, "Popen", side_effect=mutate_source_then_spawn):
            status = execute_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                confirmed=True,
                environment={},
            )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        self.assertEqual("ORIGINAL", result["variant"])
        arguments = captured["arguments"]
        self.assertEqual(str(self.runtime), arguments[0])
        self.assertRegex(arguments[4], r"^/proc/self/fd/\d+$")
        self.assertRegex(captured["executable"], r"^/proc/self/fd/\d+$")
        self.assertEqual(2, len(captured["pass_fds"]))

    def test_lock_entry_replacement_before_spawn_fails_closed(self):
        original_prepare = launch._ensure_runtime_directories

        def replace_lock(profile_fd):
            original_prepare(profile_fd)
            os.unlink(launch.PROFILE_LOCK_NAME, dir_fd=profile_fd)
            replacement = os.open(
                launch.PROFILE_LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=profile_fd,
            )
            os.close(replacement)

        with mock.patch.object(launch, "_ensure_runtime_directories", side_effect=replace_lock), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")):
            with self.assertRaisesRegex(
                LaunchSafetyError,
                "lock entry changed|exactly one hard link",
            ):
                execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=True,
                    environment={},
                )

    def test_lock_is_nonblocking_and_released_after_child_exit(self):
        execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment={},
        )
        lock_path = self.profile / launch.PROFILE_LOCK_NAME
        with lock_path.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaisesRegex(LaunchBusyError, "already running"):
                execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=True,
                    environment={},
                )
            self.assertLess(time.monotonic() - started, 1.0)
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        status = execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment={},
        )
        self.assertEqual(0, status)

    def test_unsafe_managed_state_and_spawn_failure_fail_closed(self):
        state = self.profile / ".zen-app-runtime"
        external = self.base / "external"
        external.mkdir(mode=0o700)
        state.symlink_to(external, target_is_directory=True)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")):
            with self.assertRaisesRegex(LaunchSafetyError, "symbolic link|cannot prepare"):
                execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=True,
                    environment={},
                )
        state.unlink()
        invalid_runtime = self.base / "runtime" / "invalid"
        invalid_runtime.write_bytes(b"not an executable format\n")
        invalid_runtime.chmod(0o700)
        self.runtime = invalid_runtime
        self._write_lock()
        invalid_manifest = create_manifest("invalid", "Invalid", "https://invalid.example.com")
        invalid_profile = self.profile_root / "invalid"
        provision_profile(self.profile_root, invalid_manifest, self.lock)
        with self.assertRaisesRegex(LaunchExecutionError, "spawn"):
            execute_launch(
                self.profile_root,
                invalid_manifest,
                self.lock,
                confirmed=True,
                environment={},
            )
        self.assertTrue((invalid_profile / launch.PROFILE_LOCK_NAME).is_file())

    def test_shell_metacharacters_are_never_evaluated(self):
        sentinel = self.base / "shell-was-run"
        metachar_runtime = self.base / f"runtime;touch${{IFS}}{sentinel.name}" / "zen"
        metachar_runtime.parent.mkdir(mode=0o700)
        self._write_runtime(metachar_runtime, "METACHAR")
        self.runtime = metachar_runtime
        self._write_lock()
        metachar_manifest = create_manifest("metachar", "Metachar", "https://meta.example.com")
        metachar_profile = self.profile_root / "metachar"
        provision_profile(self.profile_root, metachar_manifest, self.lock)
        status = execute_launch(
            self.profile_root,
            metachar_manifest,
            self.lock,
            confirmed=True,
            environment={},
        )
        self.assertEqual(0, status)
        result = json.loads((metachar_profile / "fake-runtime-result.json").read_text())
        self.assertEqual("METACHAR", result["variant"])
        self.assertFalse(sentinel.exists())

    def test_inspect_success_and_busy_paths_do_not_leak_descriptors(self):
        before = len(os.listdir("/proc/self/fd"))
        inspect_launch(self.profile_root, self.manifest, self.lock, environment={})
        execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment={},
        )
        lock_path = self.profile / launch.PROFILE_LOCK_NAME
        with lock_path.open("r+b") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy_baseline = len(os.listdir("/proc/self/fd"))
            with self.assertRaises(LaunchBusyError):
                execute_launch(
                    self.profile_root,
                    self.manifest,
                    self.lock,
                    confirmed=True,
                    environment={},
                )
            self.assertEqual(busy_baseline, len(os.listdir("/proc/self/fd")))
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_second_cli_launch_fails_immediately_with_exit_two(self):
        project_root = Path(__file__).resolve().parents[1]
        store_root = self.base / "busy-store"
        AppStore(store_root).create(self.manifest)
        ready = self.profile / "fake-runtime-ready"
        signal_file = self.profile / "fake-runtime-signal.txt"
        (self.profile / "fake-runtime-mode.txt").write_text("wait", encoding="utf-8")
        ready.unlink(missing_ok=True)
        signal_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "zen_app_runtime",
            "--store",
            str(store_root),
            "launch",
            "execute",
            "demo",
            "--profile-root",
            str(self.profile_root),
            "--runtime-lock",
            str(self.lock),
            "--yes",
        ]
        child_environment = {**os.environ, "PYTHONPATH": str(project_root / "src")}
        first = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(
                ready.exists(),
                first.stderr.read() if first.poll() is not None else "runtime did not become ready",
            )
            started = time.monotonic()
            second = subprocess.run(
                command,
                cwd=project_root,
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(2, second.returncode)
            self.assertEqual("", second.stdout)
            self.assertIn("error: app profile is already running", second.stderr)
            first.send_signal(signal.SIGTERM)
            first_stdout, first_stderr = first.communicate(timeout=10)
            self.assertEqual(143, first.returncode, first_stderr)
            self.assertEqual("", first_stdout)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_profile_lock_is_held_until_lingering_launch_session_is_drained(self):
        project_root = Path(__file__).resolve().parents[1]
        store_root = self.base / "linger-store"
        AppStore(store_root).create(self.manifest)
        mode_file = self.profile / "fake-runtime-mode.txt"
        linger_file = self.profile / "fake-runtime-linger-pid"
        mode_file.write_text("linger", encoding="utf-8")
        linger_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            "-m",
            "zen_app_runtime",
            "--store",
            str(store_root),
            "launch",
            "execute",
            "demo",
            "--profile-root",
            str(self.profile_root),
            "--runtime-lock",
            str(self.lock),
            "--yes",
        ]
        environment = {**os.environ, "PYTHONPATH": str(project_root / "src")}
        first = subprocess.Popen(
            command,
            cwd=project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_group: int | None = None
        try:
            deadline = time.monotonic() + 10
            while not linger_file.exists() and first.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(
                linger_file.exists(),
                first.stderr.read() if first.poll() is not None else "linger child did not start",
            )
            result = json.loads((self.profile / "fake-runtime-result.json").read_text())
            process_group = int(result["pgid"])
            linger_pid = int(linger_file.read_text())
            self.assertIsNone(first.poll(), "supervisor released before session drain")
            second = subprocess.run(
                command,
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(2, second.returncode)
            self.assertIn("app profile is already running", second.stderr)
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(0, first.returncode, stderr)
            self.assertEqual("", stdout)
            deadline = time.monotonic() + 5
            while Path(f"/proc/{linger_pid}/stat").exists() and time.monotonic() < deadline:
                try:
                    state = Path(f"/proc/{linger_pid}/stat").read_text().split(") ", 1)[1][0]
                except (OSError, IndexError):
                    break
                if state == "Z":
                    break
                time.sleep(0.02)
            if Path(f"/proc/{linger_pid}/stat").exists():
                state = Path(f"/proc/{linger_pid}/stat").read_text().split(") ", 1)[1][0]
                self.assertEqual("Z", state)
        finally:
            if process_group is not None:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_supervisor_forwards_session_signals_and_maps_status(self):
        project_root = Path(__file__).resolve().parents[1]
        for signum, expected in (
            (signal.SIGHUP, 129),
            (signal.SIGINT, 130),
            (signal.SIGQUIT, 131),
            (signal.SIGTERM, 143),
        ):
            with self.subTest(signal=signum):
                store_root = self.base / f"store-{signum}"
                AppStore(store_root).create(self.manifest)
                ready = self.profile / "fake-runtime-ready"
                result = self.profile / "fake-runtime-result.json"
                signal_file = self.profile / "fake-runtime-signal.txt"
                mode_file = self.profile / "fake-runtime-mode.txt"
                for path in (ready, result, signal_file):
                    path.unlink(missing_ok=True)
                mode_file.write_text("wait", encoding="utf-8")
                command = [
                    sys.executable,
                    "-m",
                    "zen_app_runtime",
                    "--store",
                    str(store_root),
                    "launch",
                    "execute",
                    "demo",
                    "--profile-root",
                    str(self.profile_root),
                    "--runtime-lock",
                    str(self.lock),
                    "--yes",
                ]
                process = subprocess.Popen(
                    command,
                    cwd=project_root,
                    env={**os.environ, "PYTHONPATH": str(project_root / "src")},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), process.stderr.read() if process.poll() is not None else "")
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(expected, process.returncode, stderr)
                self.assertEqual("", stdout)
                self.assertEqual(str(signum), signal_file.read_text())
    def test_app_shell_exec_requires_exact_runtime_bound_capability_before_side_effects(self):
        state = self.profile / launch.RUNTIME_STATE_DIRECTORY
        lock_file = self.profile / launch.PROFILE_LOCK_NAME
        capability = self._write_app_shell_capability(digest="0" * 64)
        with self.assertRaisesRegex(LaunchSafetyError, "different runtime identity"):
            execute_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                confirmed=True,
                app_shell_capability=capability,
            )
        self.assertFalse(state.exists())
        self.assertFalse(lock_file.exists())
        self.assertFalse((self.profile / "fake-runtime-result.json").exists())

    def test_app_shell_exec_passes_policy_without_site_url_and_requires_precontent_ack(self):
        capability = self._write_app_shell_capability()
        status = execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            app_shell_capability=capability,
        )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        self.assertNotIn(self.manifest.start_url, result["argv"])
        self.assertIn("--zen-app-runtime-app-shell-policy-fd", result["argv"])
        self.assertIn("--zen-app-runtime-app-shell-handshake-fd", result["argv"])
        self.assertEqual(self.manifest.start_url, result["appShellPolicy"]["startUrl"])
        self.assertEqual("hidden", result["appShellPolicy"]["presentation"]["sidebar"])
        self.assertEqual(
            "minimal-security-chrome",
            result["appShellPolicy"]["presentation"]["topBar"],
        )
        self.assertEqual(
            app_shell_policy_sha256(build_app_shell_policy(self.manifest)),
            hashlib.sha256(
                (
                    json.dumps(
                        result["appShellPolicy"],
                        allow_nan=False,
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_app_shell_bad_or_missing_handshake_fails_and_drains_runtime(self):
        capability = self._write_app_shell_capability()
        for mode, message in (
            ("app-shell-bad-handshake", "digest does not match"),
            ("app-shell-no-handshake", "without readiness|before its"),
        ):
            with self.subTest(mode=mode):
                result_file = self.profile / "fake-runtime-result.json"
                result_file.unlink(missing_ok=True)
                (self.profile / "fake-runtime-mode.txt").write_text(mode, encoding="utf-8")
                with self.assertRaisesRegex(LaunchExecutionError, message):
                    execute_launch(
                        self.profile_root,
                        self.manifest,
                        self.lock,
                        confirmed=True,
                        app_shell_capability=capability,
                    )
                (self.profile / "fake-runtime-mode.txt").unlink()
                self.assertEqual(
                    0,
                    execute_launch(
                        self.profile_root,
                        self.manifest,
                        self.lock,
                        confirmed=True,
                        app_shell_capability=capability,
                    ),
                )
    def test_bundled_app_shell_exec_adds_verified_application_fd_and_environment(self):
        status = execute_launch(
            self.profile_root,
            self.manifest,
            self.lock,
            confirmed=True,
            environment={},
            use_bundled_app_shell=True,
        )
        self.assertEqual(0, status)
        result = json.loads((self.profile / "fake-runtime-result.json").read_text())
        argv = result["argv"]
        self.assertNotIn(self.manifest.start_url, argv)
        self.assertIn("-app", argv)
        application = argv[argv.index("-app") + 1]
        self.assertRegex(application, r"^/proc/self/fd/\d+/application\.ini$")
        policy_index = argv.index("--zen-app-runtime-app-shell-policy-fd")
        handshake_index = argv.index("--zen-app-runtime-app-shell-handshake-fd")
        self.assertEqual(
            argv[policy_index + 1],
            result["environment"]["ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD"],
        )
        self.assertEqual(
            argv[handshake_index + 1],
            result["environment"]["ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD"],
        )
        self.assertEqual(self.manifest.start_url, result["appShellPolicy"]["startUrl"])

    def test_external_and_bundled_app_shell_authorities_are_mutually_exclusive(self):
        capability = self._write_app_shell_capability()
        with self.assertRaisesRegex(LaunchSafetyError, "mutually exclusive"):
            execute_launch(
                self.profile_root,
                self.manifest,
                self.lock,
                confirmed=True,
                app_shell_capability=capability,
                use_bundled_app_shell=True,
            )


if __name__ == "__main__":
    unittest.main()
