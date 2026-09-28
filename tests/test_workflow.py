import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zen_app_runtime.workflow as workflow
from zen_app_runtime.app_shell import (
    APP_SHELL_ADAPTER_ID,
    AppShellError,
    REQUIRED_ADAPTER_CAPABILITIES,
)
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.profile import ProfileSafetyError, provision_profile
from zen_app_runtime.runtime import (
    RuntimeSafetyError,
    runtime_identity_to_dict,
    verify_runtime,
)
from zen_app_runtime.store import AppStore
from zen_app_runtime.workflow import (
    WorkflowError,
    doctor_report,
    inspect_app_shell,
    install_app,
    render_doctor_report,
    repair_app,
    report_exit_code,
    workflow_status,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.temporary.name)
        self.store_root = self.base / "definitions"
        self.profile_root = self.base / "profiles"
        self.manifest = create_manifest("demo", "Demo", "https://demo.example.com")
        self.runtime = self.base / "runtime" / "zen"
        self.runtime.parent.mkdir(mode=0o700)
        self.runtime.write_bytes(b"workflow runtime\n")
        self.runtime.chmod(0o700)
        self.runtime_lock = self.base / "runtime-lock.json"
        self._write_lock()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_lock(self, *, digest=None):
        if digest is None:
            digest = hashlib.sha256(self.runtime.read_bytes()).hexdigest()
        self.runtime_lock.write_text(
            json.dumps(
                {
                    "runtimeLockVersion": 1,
                    "runtime": {"path": str(self.runtime), "sha256": digest},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.runtime_lock.chmod(0o600)

    def _write_capability(self, *, digest=None):
        identity = runtime_identity_to_dict(verify_runtime(self.runtime_lock))
        if digest is not None:
            identity["sha256"] = digest
        capability = self.base / "app-shell-capability.json"
        capability.write_text(
            json.dumps(
                {
                    "adapterCapabilityVersion": 2,
                    "adapterId": APP_SHELL_ADAPTER_ID,
                    "adapterProtocolVersion": 2,
                    "capabilities": list(REQUIRED_ADAPTER_CAPABILITIES),
                    "runtimeIdentity": identity,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        capability.chmod(0o600)
        return capability

    def test_doctor_ready_v1_output_is_exact_and_canonical(self):
        expected = {
            "display": {
                "requested": "x11",
                "resolved": "x11",
                "status": "available",
            },
            "doctorReportVersion": 1,
            "overallStatus": "ready",
            "platform": {
                "procSelfFd": {"status": "available"},
                "sealedRuntimeSnapshots": {"status": "available"},
            },
            "runtime": {"status": "not-requested"},
        }
        with mock.patch.object(
            workflow, "_proc_self_fd_status", return_value={"status": "available"}
        ), mock.patch.object(
            workflow, "_sealed_snapshot_status", return_value={"status": "available"}
        ):
            report = doctor_report(display_backend="x11", environment={"DISPLAY": ":7"})
        self.assertEqual(expected, report)
        self.assertEqual(json.dumps(expected, indent=2, sort_keys=True) + "\n", render_doctor_report(report))
        self.assertEqual(0, report_exit_code(report))

    def test_doctor_reports_independent_platform_display_and_runtime_blocks(self):
        with mock.patch.object(
            workflow,
            "_proc_self_fd_status",
            return_value={"error": "proc unavailable", "status": "unavailable"},
        ), mock.patch.object(
            workflow,
            "_sealed_snapshot_status",
            return_value={"error": "seals unavailable", "status": "unavailable"},
        ):
            report = doctor_report(
                runtime_lock=self.base / "missing-lock.json",
                display_backend="wayland",
                environment={},
            )
        self.assertEqual(1, report["doctorReportVersion"])
        self.assertEqual("blocked", report["overallStatus"])
        self.assertEqual("unavailable", report["platform"]["procSelfFd"]["status"])
        self.assertEqual("unavailable", report["platform"]["sealedRuntimeSnapshots"]["status"])
        self.assertEqual("unavailable", report["display"]["status"])
        self.assertEqual("invalid", report["runtime"]["status"])
        self.assertEqual(1, report_exit_code(report))

    def test_doctor_runtime_authority_and_platform_flag_failure_are_reported(self):
        with mock.patch.object(
            workflow, "_proc_self_fd_status", return_value={"status": "available"}
        ), mock.patch.object(
            workflow, "_sealed_snapshot_status", return_value={"status": "available"}
        ):
            report = doctor_report(
                runtime_lock=self.runtime_lock,
                display_backend="x11",
                environment={"DISPLAY": ":9"},
            )
        self.assertEqual("verified", report["runtime"]["status"])
        self.assertEqual("sealed-snapshot", report["runtime"]["executionAuthority"])
        self.assertEqual(str(self.runtime), report["runtime"]["identity"]["path"])

        with mock.patch.object(workflow.os, "O_DIRECTORY", None):
            platform = workflow._proc_self_fd_status()
        self.assertEqual("unavailable", platform["status"])

    def test_status_ready_legacy_and_explicit_display_contracts(self):
        provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        status = workflow_status(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual(1, status["workflowStatusVersion"])
        self.assertEqual("ready", status["overallStatus"])
        self.assertEqual("present", status["definition"]["status"])
        self.assertEqual("verified", status["runtime"]["status"])
        self.assertEqual("ready", status["profile"]["status"])
        self.assertEqual({"status": "legacy-v1"}, status["display"])
        self.assertEqual(0, report_exit_code(status))

        explicit = workflow_status(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            display_backend="auto",
            environment={
                "DISPLAY": ":7",
                "WAYLAND_DISPLAY": "wayland-7",
                "XDG_RUNTIME_DIR": "/run/user/1000",
            },
        )
        self.assertEqual(
            {"requested": "auto", "resolved": "wayland", "status": "available"},
            explicit["display"],
        )
        self.assertEqual("ready", explicit["overallStatus"])

    def test_status_blocked_profile_display_and_invalid_runtime_are_distinct(self):
        missing_profile = workflow_status(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            display_backend="x11",
            environment={},
        )
        self.assertEqual("blocked", missing_profile["overallStatus"])
        self.assertEqual("verified", missing_profile["runtime"]["status"])
        self.assertEqual("blocked", missing_profile["profile"]["status"])
        self.assertEqual("unavailable", missing_profile["display"]["status"])
        self.assertEqual(1, report_exit_code(missing_profile))

        provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.runtime.write_bytes(b"changed runtime\n")
        invalid_runtime = workflow_status(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual("blocked", invalid_runtime["overallStatus"])
        self.assertEqual("invalid", invalid_runtime["runtime"]["status"])
        self.assertEqual("unchecked", invalid_runtime["profile"]["status"])
        self.assertEqual({"status": "legacy-v1"}, invalid_runtime["display"])

    def test_doctor_and_status_do_not_create_store_or_profile_state(self):
        missing_store = self.base / "missing" / "definitions"
        missing_profile = self.base / "missing" / "profiles"
        doctor_report(display_backend="x11", environment={"DISPLAY": ":1"})
        status = workflow_status(missing_profile, self.manifest, self.runtime_lock)
        self.assertEqual("blocked", status["overallStatus"])
        self.assertFalse(missing_store.exists())
        self.assertFalse(missing_profile.exists())

    def test_install_is_exact_idempotent_and_invalid_runtime_precedes_store_mutation(self):
        store = AppStore(self.store_root)
        self._write_lock(digest="0" * 64)
        with self.assertRaisesRegex(RuntimeSafetyError, "digest"):
            install_app(store, self.profile_root, self.manifest, self.runtime_lock)
        self.assertFalse(self.store_root.exists())
        self.assertFalse(self.profile_root.exists())

        self._write_lock()
        first = install_app(store, self.profile_root, self.manifest, self.runtime_lock)
        second = install_app(store, self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual(("created", "created"), (first["definitionStatus"], first["profile"]["status"]))
        self.assertEqual(("existing", "existing"), (second["definitionStatus"], second["profile"]["status"]))

        changed = create_manifest("demo", "Changed", "https://demo.example.com")
        with self.assertRaisesRegex(WorkflowError, "different content"):
            install_app(store, self.profile_root, changed, self.runtime_lock)
        self.assertEqual(self.manifest, store.get_read_only("demo"))

    def test_partial_install_keeps_definition_and_repair_completes_profile(self):
        store = AppStore(self.store_root)
        with mock.patch.object(
            workflow,
            "provision_profile",
            side_effect=ProfileSafetyError("injected profile failure"),
        ):
            with self.assertRaisesRegex(ProfileSafetyError, "injected profile failure"):
                install_app(store, self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual(self.manifest, store.get_read_only("demo"))
        self.assertFalse(self.profile_root.exists())

        repaired = repair_app(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual("ready", repaired["overallStatus"])
        self.assertEqual("created", repaired["profile"]["status"])
        self.assertTrue((self.profile_root / "demo" / ".zen-app-runtime-profile.json").is_file())
    def test_app_shell_status_and_inspection_are_read_only_and_runtime_bound(self):
        provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        capability = self._write_capability()
        marker_before = (
            self.profile_root / "demo" / ".zen-app-runtime-profile.json"
        ).read_bytes()
        status = workflow_status(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            app_shell_capability=capability,
        )
        self.assertEqual(2, status["workflowStatusVersion"])
        self.assertEqual("ready", status["overallStatus"])
        self.assertEqual(
            "ready-for-startup-handshake",
            status["appShell"]["status"],
        )
        inspection = inspect_app_shell(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            capability,
        )
        self.assertEqual(1, inspection["appShellInspectionVersion"])
        self.assertEqual("hidden", inspection["policy"]["presentation"]["sidebar"])
        self.assertEqual(
            "minimal-security-chrome",
            inspection["policy"]["presentation"]["topBar"],
        )
        self.assertEqual(1, inspection["launchTicket"]["launchTicketVersion"])
        self.assertEqual(
            marker_before,
            (self.profile_root / "demo" / ".zen-app-runtime-profile.json").read_bytes(),
        )
        self.assertFalse((self.profile_root / "demo" / ".zen-app-runtime").exists())

    def test_app_shell_mismatch_blocks_status_and_precedes_install_mutation(self):
        capability = self._write_capability(digest="0" * 64)
        status = workflow_status(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            app_shell_capability=capability,
        )
        self.assertEqual("blocked", status["overallStatus"])
        self.assertEqual("blocked", status["appShell"]["status"])

        store = AppStore(self.store_root)
        with self.assertRaisesRegex(AppShellError, "different runtime identity"):
            install_app(
                store,
                self.profile_root,
                self.manifest,
                self.runtime_lock,
                app_shell_capability=capability,
            )
        self.assertFalse(self.store_root.exists())
        self.assertFalse(self.profile_root.exists())

    def test_app_shell_install_and_repair_report_v2_without_changing_legacy_v1(self):
        capability = self._write_capability()
        store = AppStore(self.store_root)
        installed = install_app(
            store,
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            app_shell_capability=capability,
        )
        self.assertEqual(2, installed["appInstallResultVersion"])
        self.assertEqual(
            "ready-for-startup-handshake",
            installed["appShell"]["status"],
        )
        repaired = repair_app(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            app_shell_capability=capability,
        )
        self.assertEqual(2, repaired["appRepairResultVersion"])
        self.assertEqual("existing", repaired["profile"]["status"])

        legacy = install_app(
            store,
            self.profile_root,
            self.manifest,
            self.runtime_lock,
        )
        self.assertEqual(1, legacy["appInstallResultVersion"])
        self.assertNotIn("appShell", legacy)
    def test_bundled_app_shell_reports_verified_bundle_and_in_memory_runtime_binding(self):
        store = AppStore(self.store_root)
        installed = install_app(
            store,
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            use_bundled_app_shell=True,
        )
        self.assertEqual(3, installed["appInstallResultVersion"])
        self.assertEqual("package-bundled", installed["appShell"]["browserAdapter"]["source"])
        self.assertEqual(
            runtime_identity_to_dict(verify_runtime(self.runtime_lock)),
            installed["appShell"]["adapterCapability"]["runtimeIdentity"],
        )

        status = workflow_status(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            use_bundled_app_shell=True,
        )
        self.assertEqual(3, status["workflowStatusVersion"])
        self.assertEqual("ready", status["overallStatus"])
        self.assertEqual(
            "ready-for-startup-handshake",
            status["appShell"]["status"],
        )

        inspection = inspect_app_shell(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            use_bundled_app_shell=True,
        )
        self.assertEqual(2, inspection["appShellInspectionVersion"])
        self.assertEqual("package-bundled", inspection["browserAdapter"]["source"])
        self.assertNotIn("/proc/self/fd/", json.dumps(inspection))

        repaired = repair_app(
            self.profile_root,
            self.manifest,
            self.runtime_lock,
            use_bundled_app_shell=True,
        )
        self.assertEqual(3, repaired["appRepairResultVersion"])

        capability = self._write_capability()
        with self.assertRaisesRegex(WorkflowError, "mutually exclusive"):
            install_app(
                store,
                self.profile_root,
                self.manifest,
                self.runtime_lock,
                app_shell_capability=capability,
                use_bundled_app_shell=True,
            )


if __name__ == "__main__":
    unittest.main()
