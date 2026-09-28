import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zen_app_runtime.cli as cli
import zen_app_runtime.workflow as workflow
from zen_app_runtime.app_shell import APP_SHELL_ADAPTER_ID, REQUIRED_ADAPTER_CAPABILITIES
from zen_app_runtime.cli import main
from zen_app_runtime.profile import ProfileSafetyError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.store = Path(self.temporary.name) / "definitions"

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--store", str(self.store), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_end_to_end_cli_lifecycle(self):
        code, output, error = self.run_cli(
            "create", "calendar", "--name", "Calendar", "--url", "https://calendar.example.com",
            "--mode", "Transparent", "--with-stylus",
        )
        self.assertEqual((0, "", ""), (code, output, error))

        code, output, error = self.run_cli("list")
        self.assertEqual((0, "calendar\n", ""), (code, output, error))

        code, output, error = self.run_cli("show", "calendar")
        shown = json.loads(output)
        self.assertEqual(0, code)
        self.assertEqual("Transparent", shown["configuration"]["mode"])
        self.assertEqual(["zen-internet", "stylus"], [item["id"] for item in shown["extensions"]])
        self.assertEqual("", error)

        code, output, error = self.run_cli("remove", "calendar", "--yes")
        self.assertEqual((0, "", ""), (code, output, error))

    def test_plan_and_desktop_render_are_inert_and_deterministic(self):
        self.run_cli(
            "create", "demo", "--name", "Demo % App", "--url", "https://example.com/path?x=%f"
        )
        profile_root = Path(self.temporary.name) / "profiles-do-not-create"
        common = (
            "demo",
            "--runtime", "/opt/Zen Runtime/zen%stable",
            "--launcher", "/usr/local/bin/zen app%preview",
            "--profile-root", str(profile_root),
        )

        code, output, error = self.run_cli("plan", *common)
        self.assertEqual((0, ""), (code, error))
        plan = json.loads(output)
        self.assertEqual(1, plan["planVersion"])
        self.assertEqual(1, plan["manifestSchemaVersion"])
        self.assertEqual(str(profile_root / "demo"), plan["profilePath"])
        self.assertEqual("/opt/Zen Runtime/zen%stable", plan["argv"][0])
        self.assertFalse(profile_root.exists())
        self.assertTrue(output.endswith("\n"))

        code, desktop, error = self.run_cli("desktop", "render", *common)
        self.assertEqual((0, ""), (code, error))
        self.assertIn("Terminal=true\n", desktop)
        self.assertIn("Icon=application-x-executable\n", desktop)
        self.assertIn("X-Zen-App-Runtime-Plan-Only=true\n", desktop)
        self.assertIn('"plan" "demo"', desktop)
        self.assertIn("zen app%%preview", desktop)
        self.assertNotIn("StartupWMClass", desktop)
        self.assertNotIn("DBusActivatable", desktop)
        self.assertNotIn("MimeType", desktop)
        self.assertFalse(profile_root.exists())

    def test_render_commands_handle_invalid_paths_with_exit_code_two(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        code, output, error = self.run_cli(
            "plan", "demo", "--runtime", "relative", "--launcher", "/bin/zen-app",
            "--profile-root", "/profiles",
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("runtime path", error)

    def test_render_commands_do_not_create_a_missing_store_or_profile(self):
        missing_store = Path(self.temporary.name) / "missing" / "definitions"
        missing_profile = Path(self.temporary.name) / "missing" / "profiles"
        self.store = missing_store
        code, output, error = self.run_cli(
            "plan", "missing", "--runtime", "/opt/zen", "--launcher", "/bin/zen-app",
            "--profile-root", str(missing_profile),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("not found", error)
        self.assertFalse(missing_store.exists())
        self.assertFalse(missing_profile.exists())

    def test_remove_requires_explicit_confirmation(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        code, _, error = self.run_cli("remove", "demo")
        self.assertEqual(2, code)
        self.assertIn("--yes", error)
        self.assertTrue((self.store / "demo.json").exists())

    def test_duplicate_and_missing_operations_return_nonzero(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        code, _, error = self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        self.assertEqual(2, code)
        self.assertIn("already exists", error)

        code, _, error = self.run_cli("show", "missing")
        self.assertEqual(2, code)
        self.assertIn("not found", error)

    def _runtime_lock(self):
        import hashlib

        binary = Path(self.temporary.name) / "runtime" / "zen"
        binary.parent.mkdir(mode=0o700)
        binary.write_bytes(b"cli runtime\n")
        binary.chmod(0o700)
        lock = Path(self.temporary.name) / "runtime.lock.json"
        lock.write_text(
            json.dumps(
                {
                    "runtimeLockVersion": 1,
                    "runtime": {
                        "path": str(binary),
                        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        lock.chmod(0o600)
        return binary, lock

    def _app_shell_capability(self, binary, *, digest=None):
        import hashlib

        capability = Path(self.temporary.name) / "app-shell-capability.json"
        capability.write_text(
            json.dumps(
                {
                    "adapterCapabilityVersion": 2,
                    "adapterId": APP_SHELL_ADAPTER_ID,
                    "adapterProtocolVersion": 2,
                    "capabilities": list(REQUIRED_ADAPTER_CAPABILITIES),
                    "runtimeIdentity": {
                        "identityVersion": 1,
                        "path": str(binary),
                        "sha256": digest or hashlib.sha256(binary.read_bytes()).hexdigest(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        capability.chmod(0o600)
        return capability

    def test_runtime_verify_prints_only_deterministic_identity(self):
        binary, lock = self._runtime_lock()
        code, output, error = self.run_cli("runtime", "verify", "--lock", str(lock))
        self.assertEqual((0, ""), (code, error))
        identity = json.loads(output)
        self.assertEqual(1, identity["identityVersion"])
        self.assertEqual(str(binary), identity["path"])
        self.assertTrue(output.endswith("\n"))

    def test_security_paths_retain_raw_lexical_form_until_validation(self):
        _, lock = self._runtime_lock()
        non_normal_lock = str(lock.parent) + "/./runtime.lock.json"
        code, output, error = self.run_cli("runtime", "verify", "--lock", non_normal_lock)
        self.assertEqual((2, ""), (code, output))
        self.assertIn("normalized", error)

        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        non_normal_root = str(Path(self.temporary.name)) + "/./profiles"
        code, output, error = self.run_cli(
            "profile", "provision", "demo", "--profile-root", non_normal_root,
            "--runtime-lock", str(lock),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("normalized", error)

    def test_profile_provision_reports_created_then_existing(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "profiles"
        arguments = (
            "profile", "provision", "demo", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock),
        )
        code, output, error = self.run_cli(*arguments)
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(
            {"profilePath": str(profile_root / "demo"), "status": "created"},
            json.loads(output),
        )
        code, output, error = self.run_cli(*arguments)
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("existing", json.loads(output)["status"])
        self.assertTrue((profile_root / "demo" / ".zen-app-runtime-profile.json").is_file())

    def test_runtime_and_profile_failures_return_two_without_stdout(self):
        _, lock = self._runtime_lock()
        lock.write_text("{}\n", encoding="utf-8")
        code, output, error = self.run_cli("runtime", "verify", "--lock", str(lock))
        self.assertEqual((2, ""), (code, output))
        self.assertIn("error:", error)

        code, output, error = self.run_cli(
            "profile", "provision", "missing", "--profile-root",
            str(Path(self.temporary.name) / "profiles"), "--runtime-lock", str(lock),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("error:", error)
    def test_launch_inspect_and_confirmation_contract(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        binary, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "profiles"
        provision = (
            "profile", "provision", "demo", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock),
        )
        self.assertEqual(0, self.run_cli(*provision)[0])
        launch_arguments = (
            "demo", "--profile-root", str(profile_root), "--runtime-lock", str(lock),
        )
        code, output, error = self.run_cli("launch", "inspect", *launch_arguments)
        self.assertEqual((0, ""), (code, error))
        ticket = json.loads(output)
        self.assertEqual(1, ticket["launchTicketVersion"])
        self.assertEqual(str(binary), ticket["argv"][0])
        self.assertEqual(
            ["--no-remote", "--new-instance", "--profile"],
            ticket["argv"][1:4],
        )
        self.assertFalse((profile_root / "demo" / ".zen-app-runtime").exists())
        self.assertFalse((profile_root / "demo" / ".zen-app-runtime-launch.lock").exists())

        code, output, error = self.run_cli("launch", "execute", *launch_arguments)
        self.assertEqual((2, ""), (code, output))
        self.assertIn("--yes", error)
        self.assertFalse((profile_root / "demo" / ".zen-app-runtime").exists())

    def test_launch_display_backend_cli_emits_v2_and_rejects_unknown_values(self):
        self.run_cli("create", "demo", "--name", "Demo", "--url", "https://example.com")
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "profiles"
        self.assertEqual(
            0,
            self.run_cli(
                "profile",
                "provision",
                "demo",
                "--profile-root",
                str(profile_root),
                "--runtime-lock",
                str(lock),
            )[0],
        )
        arguments = (
            "demo",
            "--profile-root",
            str(profile_root),
            "--runtime-lock",
            str(lock),
        )
        session = {
            "DISPLAY": ":9",
            "WAYLAND_DISPLAY": "wayland-9",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
        with mock.patch.dict(os.environ, session, clear=False):
            code, output, error = self.run_cli(
                "launch",
                "inspect",
                *arguments,
                "--display-backend",
                "auto",
            )
            self.assertEqual((0, ""), (code, error))
            ticket = json.loads(output)
            self.assertEqual(2, ticket["launchTicketVersion"])
            self.assertEqual(
                {"requested": "auto", "resolved": "wayland"},
                ticket["displayBackend"],
            )

            code, output, error = self.run_cli(
                "launch",
                "inspect",
                *arguments,
                "--display-backend",
                "x11",
            )
            self.assertEqual((0, ""), (code, error))
            ticket = json.loads(output)
            self.assertEqual(
                {"requested": "x11", "resolved": "x11"},
                ticket["displayBackend"],
            )
            self.assertEqual(":9", ticket["environment"]["DISPLAY"])
            self.assertNotIn("WAYLAND_DISPLAY", ticket["environment"])

        code, output, error = self.run_cli(
            "launch",
            "inspect",
            *arguments,
            "--display-backend",
            "invalid",
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("invalid choice", error)
        self.assertFalse((profile_root / "demo" / ".zen-app-runtime").exists())

        with mock.patch.object(cli, "execute_launch", return_value=0) as executor:
            code, output, error = self.run_cli(
                "launch",
                "execute",
                *arguments,
                "--display-backend",
                "x11",
                "--yes",
            )
        self.assertEqual((0, "", ""), (code, output, error))
        self.assertEqual("x11", executor.call_args.kwargs["display_backend"])
        self.assertIs(True, executor.call_args.kwargs["confirmed"])

    def test_launch_execute_requires_yes_before_reading_a_missing_store(self):
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "profiles-do-not-create"
        code, output, error = self.run_cli(
            "launch", "execute", "missing", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("--yes", error)
        self.assertFalse(self.store.exists())
        self.assertFalse(profile_root.exists())
    def test_high_level_app_workflow_end_to_end_and_idempotent_install(self):
        binary, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "workflow-profiles"
        install_arguments = (
            "app", "install", "demo", "--name", "Demo", "--url", "https://example.com",
            "--profile-root", str(profile_root), "--runtime-lock", str(lock),
        )
        code, output, error = self.run_cli(*install_arguments)
        self.assertEqual((0, ""), (code, error))
        first = json.loads(output)
        self.assertEqual(1, first["appInstallResultVersion"])
        self.assertEqual(("created", "created"), (first["definitionStatus"], first["profile"]["status"]))

        code, output, error = self.run_cli(*install_arguments)
        self.assertEqual((0, ""), (code, error))
        second = json.loads(output)
        self.assertEqual(("existing", "existing"), (second["definitionStatus"], second["profile"]["status"]))

        common = (
            "demo", "--profile-root", str(profile_root), "--runtime-lock", str(lock),
        )
        code, output, error = self.run_cli("app", "status", *common)
        self.assertEqual((0, ""), (code, error))
        status = json.loads(output)
        self.assertEqual("ready", status["overallStatus"])
        self.assertEqual({"status": "legacy-v1"}, status["display"])

        app_inspect = self.run_cli("app", "inspect", *common)
        low_level_inspect = self.run_cli("launch", "inspect", *common)
        self.assertEqual(low_level_inspect, app_inspect)
        self.assertEqual(str(binary), json.loads(app_inspect[1])["argv"][0])

        with mock.patch.object(cli, "execute_launch", return_value=23) as executor:
            code, output, error = self.run_cli("app", "launch", *common, "--yes")
        self.assertEqual((23, "", ""), (code, output, error))
        self.assertIs(True, executor.call_args.kwargs["confirmed"])

        code, output, error = self.run_cli("app", "repair", *common)
        self.assertEqual((0, ""), (code, error))
        repair = json.loads(output)
        self.assertEqual(1, repair["appRepairResultVersion"])
        self.assertEqual("existing", repair["profile"]["status"])

    def test_high_level_install_rejects_mismatch_and_invalid_lock_before_mutation(self):
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "workflow-profiles"
        common = ("--profile-root", str(profile_root), "--runtime-lock", str(lock))
        code, output, error = self.run_cli(
            "app", "install", "demo", "--name", "Demo", "--url", "https://example.com", *common
        )
        self.assertEqual((0, ""), (code, error))

        definition_before = (self.store / "demo.json").read_bytes()
        code, output, error = self.run_cli(
            "app", "install", "demo", "--name", "Changed", "--url", "https://example.com", *common
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("different content", error)
        self.assertEqual(definition_before, (self.store / "demo.json").read_bytes())

        other_store = Path(self.temporary.name) / "invalid-lock-definitions"
        other_profile = Path(self.temporary.name) / "invalid-lock-profiles"
        self.store = other_store
        lock.write_text("{}\n", encoding="utf-8")
        code, output, error = self.run_cli(
            "app", "install", "other", "--name", "Other", "--url", "https://other.example.com",
            "--profile-root", str(other_profile), "--runtime-lock", str(lock),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("error:", error)
        self.assertFalse(other_store.exists())
        self.assertFalse(other_profile.exists())

    def test_high_level_partial_install_is_repairable(self):
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "repair-profiles"
        install_arguments = (
            "app", "install", "demo", "--name", "Demo", "--url", "https://example.com",
            "--profile-root", str(profile_root), "--runtime-lock", str(lock),
        )
        with mock.patch.object(
            workflow,
            "provision_profile",
            side_effect=ProfileSafetyError("injected profile failure"),
        ):
            code, output, error = self.run_cli(*install_arguments)
        self.assertEqual((2, ""), (code, output))
        self.assertIn("injected profile failure", error)
        self.assertTrue((self.store / "demo.json").is_file())
        self.assertFalse(profile_root.exists())

        code, output, error = self.run_cli(
            "app", "repair", "demo", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock),
        )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("created", json.loads(output)["profile"]["status"])

    def test_high_level_launch_requires_yes_before_store_access(self):
        _, lock = self._runtime_lock()
        profile_root = Path(self.temporary.name) / "workflow-profiles-do-not-create"
        code, output, error = self.run_cli(
            "app", "launch", "missing", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("--yes", error)
        self.assertFalse(self.store.exists())
        self.assertFalse(profile_root.exists())

    def test_doctor_emits_valid_ready_and_blocked_reports_with_exit_zero_or_one(self):
        platform_available = {"status": "available"}
        with mock.patch.object(workflow, "_proc_self_fd_status", return_value=platform_available), \
             mock.patch.object(workflow, "_sealed_snapshot_status", return_value=platform_available), \
             mock.patch.dict(os.environ, {"DISPLAY": ":8"}, clear=True):
            code, output, error = self.run_cli("doctor", "--display-backend", "x11")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("ready", json.loads(output)["overallStatus"])
        self.assertFalse(self.store.exists())

        with mock.patch.object(workflow, "_proc_self_fd_status", return_value=platform_available), \
             mock.patch.object(workflow, "_sealed_snapshot_status", return_value=platform_available), \
             mock.patch.dict(os.environ, {}, clear=True):
            code, output, error = self.run_cli("doctor", "--display-backend", "x11")
        self.assertEqual((1, ""), (code, error))
        blocked = json.loads(output)
        self.assertEqual("blocked", blocked["overallStatus"])
        self.assertEqual("unavailable", blocked["display"]["status"])
        self.assertFalse(self.store.exists())
    def test_app_shell_cli_install_status_inspect_and_launch_contract(self):
        binary, lock = self._runtime_lock()
        capability = self._app_shell_capability(binary)
        profile_root = Path(self.temporary.name) / "app-shell-profiles"
        shell_option = ("--app-shell-capability", str(capability))
        code, output, error = self.run_cli(
            "app", "install", "shell-app", "--name", "Shell App",
            "--url", "https://shell.example.com", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock), *shell_option,
        )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(2, json.loads(output)["appInstallResultVersion"])

        common = (
            "shell-app", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock), *shell_option,
        )
        code, output, error = self.run_cli("app", "status", *common)
        self.assertEqual((0, ""), (code, error))
        status = json.loads(output)
        self.assertEqual(2, status["workflowStatusVersion"])
        self.assertEqual("ready-for-startup-handshake", status["appShell"]["status"])

        code, output, error = self.run_cli("app", "inspect", *common)
        self.assertEqual((0, ""), (code, error))
        inspection = json.loads(output)
        self.assertEqual(1, inspection["appShellInspectionVersion"])
        self.assertEqual("hidden", inspection["policy"]["presentation"]["sidebar"])
        self.assertEqual(
            "show-origin-and-security-panel",
            inspection["policy"]["shortcuts"]["remapped"]["Ctrl+L"],
        )

        with mock.patch.object(cli, "execute_launch", return_value=0) as executor:
            code, output, error = self.run_cli("app", "launch", *common, "--yes")
        self.assertEqual((0, "", ""), (code, output, error))
        self.assertEqual(
            str(capability),
            executor.call_args.kwargs["app_shell_capability"],
        )

    def test_app_shell_cli_mismatch_fails_before_install_mutation(self):
        binary, lock = self._runtime_lock()
        capability = self._app_shell_capability(binary, digest="0" * 64)
        profile_root = Path(self.temporary.name) / "blocked-shell-profiles"
        code, output, error = self.run_cli(
            "app", "install", "blocked", "--name", "Blocked",
            "--url", "https://blocked.example.com", "--profile-root", str(profile_root),
            "--runtime-lock", str(lock), "--app-shell-capability", str(capability),
        )
        self.assertEqual((2, ""), (code, output))
        self.assertIn("different runtime identity", error)
        self.assertFalse(self.store.exists())
        self.assertFalse(profile_root.exists())


if __name__ == "__main__":
    unittest.main()
