import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zen_app_runtime.cli import main
from zen_app_runtime.onboarding import (
    OnboardingError,
    build_web_app_onboarding_plan,
    canonicalize_web_app_url,
    default_web_app_id,
    default_web_app_name,
    render_web_app_onboarding_plan,
    web_app_onboarding_plan_to_dict,
)
from zen_app_runtime.runtime import RuntimeSafetyError
from zen_app_runtime.store import AppStore
from zen_app_runtime.workflow import WorkflowError, complete_web_app_onboarding


FAKE_RUNTIME = """#!__PYTHON__
import hashlib
import json
import os
from pathlib import Path
import sys

profile = Path(sys.argv[sys.argv.index("--profile") + 1])
policy = None
if "--zen-app-runtime-app-shell-policy-fd" in sys.argv:
    policy_fd = int(sys.argv[sys.argv.index("--zen-app-runtime-app-shell-policy-fd") + 1])
    handshake_fd = int(sys.argv[sys.argv.index("--zen-app-runtime-app-shell-handshake-fd") + 1])
    with os.fdopen(policy_fd, "r", encoding="utf-8") as stream:
        policy = json.load(stream)
    canonical = (json.dumps(policy, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\\n").encode("utf-8")
    handshake = {
        "adapterId": "org.zen-app-runtime.app-shell",
        "adapterProtocolVersion": 2,
        "handshakeVersion": 2,
        "policySha256": hashlib.sha256(canonical).hexdigest(),
        "status": "ready-before-site-content",
    }
    os.write(handshake_fd, (json.dumps(handshake, sort_keys=True) + "\\n").encode("utf-8"))
    os.close(handshake_fd)
(profile / "onboarding-launch.json").write_text(
    json.dumps({"argv": sys.argv, "environment": dict(os.environ), "policy": policy}, sort_keys=True) + "\\n",
    encoding="utf-8",
)
"""


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.temporary.name)
        self.store_root = self.base / "definitions"
        self.profile_root = self.base / "profiles"
        self.runtime = self.base / "runtime" / "zen"
        self.runtime.parent.mkdir(mode=0o700)
        self.runtime.write_text(
            FAKE_RUNTIME.replace("__PYTHON__", sys.executable),
            encoding="utf-8",
        )
        self.runtime.chmod(0o700)
        self.runtime_lock = self.base / "runtime-lock.json"
        self._write_lock()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_lock(self, digest=None):
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

    def run_cli(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--store", str(self.store_root), *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_url_is_canonical_and_defaults_are_stable_without_network_metadata(self):
        canonical = canonicalize_web_app_url(
            "HTTPS://WWW.Example.COM.:443/mail/inbox?view=all#latest"
        )
        self.assertEqual(
            "https://www.example.com/mail/inbox?view=all#latest",
            canonical,
        )
        self.assertEqual("Example", default_web_app_name(canonical))
        self.assertEqual("example-com-6a87d7e230", default_web_app_id(canonical))
        different_path = canonicalize_web_app_url("https://www.example.com/calendar")
        self.assertNotEqual(default_web_app_id(canonical), default_web_app_id(different_path))

    def test_onboarding_rejects_unsafe_urls_paths_and_fabricated_plans(self):
        for unsafe in (
            "http://example.com",
            "https://user:secret@example.com",
            "https://example.com/bad path",
            "https://example.com\\evil",
            "file:///etc/passwd",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(OnboardingError):
                canonicalize_web_app_url(unsafe)

        with self.assertRaises(OnboardingError):
            build_web_app_onboarding_plan(
                "https://example.com",
                "relative/profiles",
                self.runtime_lock,
            )
        plan = build_web_app_onboarding_plan(
            "https://example.com",
            self.profile_root,
            self.runtime_lock,
        )
        with self.assertRaises(OnboardingError):
            web_app_onboarding_plan_to_dict(replace(plan, plan_version=999))
        with self.assertRaises(OnboardingError):
            web_app_onboarding_plan_to_dict(replace(plan, launch_requested=1))

    def test_preview_is_canonical_confirmation_required_and_side_effect_free(self):
        missing_store = self.base / "missing" / "definitions"
        self.store_root = missing_store
        missing_profiles = self.base / "missing" / "profiles"
        missing_lock = self.base / "missing-runtime-lock.json"
        code, output, error = self.run_cli(
            "app",
            "onboard",
            "https://calendar.example.com/team",
            "--profile-root",
            str(missing_profiles),
            "--runtime-lock",
            str(missing_lock),
            "--launch",
        )
        self.assertEqual((0, ""), (code, error))
        preview = json.loads(output)
        self.assertEqual(1, preview["webAppOnboardingPlanVersion"])
        self.assertEqual("required", preview["confirmation"])
        self.assertTrue(preview["actions"]["launchAfterInstall"])
        self.assertFalse(preview["browserExperience"]["inBrowserAppShell"])
        self.assertEqual("legacy-isolated-browser", preview["browserExperience"]["mode"])
        self.assertFalse(missing_store.exists())
        self.assertFalse(missing_profiles.exists())
        self.assertFalse(missing_lock.exists())
        self.assertEqual(output, render_web_app_onboarding_plan(
            build_web_app_onboarding_plan(
                "https://calendar.example.com/team",
                missing_profiles,
                missing_lock,
                launch_requested=True,
            )
        ))

    def test_confirmed_onboarding_is_idempotent_and_invalid_runtime_precedes_mutation(self):
        plan = build_web_app_onboarding_plan(
            "https://mail.example.com/inbox",
            self.profile_root,
            self.runtime_lock,
            name="Mail",
        )
        first = complete_web_app_onboarding(AppStore(self.store_root), plan)
        second = complete_web_app_onboarding(AppStore(self.store_root), plan)
        self.assertEqual("completed", first["onboardingStatus"])
        self.assertEqual("created", first["installation"]["definitionStatus"])
        self.assertEqual("existing", second["installation"]["definitionStatus"])
        self.assertEqual("not-requested", second["launch"]["status"])
        self.assertFalse(second["browserExperience"]["inBrowserAppShell"])

        collision = build_web_app_onboarding_plan(
            "https://mail.example.com/another-account",
            self.profile_root,
            self.runtime_lock,
            app_id=plan.manifest.app_id,
            name="Mail",
        )
        with self.assertRaises(WorkflowError):
            complete_web_app_onboarding(AppStore(self.store_root), collision)
        self.assertEqual(
            plan.manifest,
            AppStore(self.store_root).get_read_only(plan.manifest.app_id),
        )

        bad_store = self.base / "bad-definitions"
        bad_profiles = self.base / "bad-profiles"
        self._write_lock("0" * 64)
        bad_plan = build_web_app_onboarding_plan(
            "https://bad.example.com",
            bad_profiles,
            self.runtime_lock,
        )
        with self.assertRaises(RuntimeSafetyError):
            complete_web_app_onboarding(AppStore(bad_store), bad_plan)
        self.assertFalse(bad_store.exists())
        self.assertFalse(bad_profiles.exists())

    def test_one_confirmed_command_installs_and_crosses_real_launch_boundary(self):
        xdg_data_home = self.base / "xdg-data"
        xdg_data_home.mkdir(mode=0o700)
        self.profile_root = xdg_data_home / "zen-app-runtime"
        with mock.patch.dict(
            os.environ,
            {"DISPLAY": ":77", "XDG_DATA_HOME": str(xdg_data_home)},
            clear=False,
        ):
            code, output, error = self.run_cli(
                "app",
                "onboard",
                "https://chat.example.com/room?team=core",
                "--name",
                "Team Chat",
                "--runtime-lock",
                str(self.runtime_lock),
                "--display-backend",
                "x11",
                "--launch",
                "--yes",
            )
        self.assertEqual((0, ""), (code, error))
        result = json.loads(output)
        self.assertEqual(1, result["webAppOnboardingResultVersion"])
        self.assertEqual("completed", result["onboardingStatus"])
        self.assertEqual("starting", result["launch"]["status"])
        app_id = result["appId"]
        manifest = AppStore(self.store_root).get_read_only(app_id)
        self.assertEqual("Team Chat", manifest.name)
        self.assertEqual("https://chat.example.com/room?team=core", manifest.start_url)
        launch_result = json.loads(
            (self.profile_root / app_id / "onboarding-launch.json").read_text(encoding="utf-8")
        )
        argv = launch_result["argv"]
        self.assertIn("--no-remote", argv)
        self.assertIn("--new-instance", argv)
        self.assertEqual(manifest.start_url, argv[-1])

        code, status_output, error = self.run_cli(
            "app",
            "status",
            app_id,
            "--profile-root",
            str(self.profile_root),
            "--runtime-lock",
            str(self.runtime_lock),
        )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("ready", json.loads(status_output)["overallStatus"])
        code, ticket_output, error = self.run_cli(
            "app",
            "inspect",
            app_id,
            "--profile-root",
            str(self.profile_root),
            "--runtime-lock",
            str(self.runtime_lock),
        )
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(manifest.start_url, json.loads(ticket_output)["argv"][-1])
    def test_bundled_app_shell_onboarding_v2_previews_then_launches_from_one_link(self):
        missing_store = self.base / "preview-definitions"
        self.store_root = missing_store
        missing_profiles = self.base / "preview-profiles"
        missing_lock = self.base / "preview-lock.json"
        code, output, error = self.run_cli(
            "app",
            "onboard",
            "https://music.example.com/player",
            "--profile-root",
            str(missing_profiles),
            "--runtime-lock",
            str(missing_lock),
            "--bundled-app-shell",
            "--launch",
        )
        self.assertEqual((0, ""), (code, error))
        preview = json.loads(output)
        self.assertEqual(2, preview["webAppOnboardingPlanVersion"])
        self.assertTrue(preview["browserExperience"]["inBrowserAppShell"])
        self.assertEqual(
            "policy-fd-and-pre-content-handshake",
            preview["browserExperience"]["startupAuthority"],
        )
        self.assertFalse(missing_store.exists())
        self.assertFalse(missing_profiles.exists())

        self.store_root = self.base / "bundled-definitions"
        with mock.patch.dict(os.environ, {"DISPLAY": ":81"}, clear=False):
            code, output, error = self.run_cli(
                "app",
                "onboard",
                "https://music.example.com/player",
                "--profile-root",
                str(self.profile_root),
                "--runtime-lock",
                str(self.runtime_lock),
                "--display-backend",
                "x11",
                "--bundled-app-shell",
                "--launch",
                "--yes",
            )
        self.assertEqual((0, ""), (code, error))
        result = json.loads(output)
        self.assertEqual(2, result["webAppOnboardingResultVersion"])
        self.assertTrue(result["browserExperience"]["inBrowserAppShell"])
        self.assertEqual(3, result["installation"]["appInstallResultVersion"])
        self.assertEqual(
            "package-bundled",
            result["installation"]["appShell"]["browserAdapter"]["source"],
        )
        launch_result = json.loads(
            (
                self.profile_root
                / result["appId"]
                / "onboarding-launch.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn("-app", launch_result["argv"])
        self.assertNotIn(result["startUrl"], launch_result["argv"])
        self.assertEqual(result["startUrl"], launch_result["policy"]["startUrl"])
        self.assertIn(
            "ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD",
            launch_result["environment"],
        )


if __name__ == "__main__":
    unittest.main()
