import json
import tempfile
import unittest
from pathlib import Path

from zen_app_runtime.launch_plan import (
    LaunchPlanError,
    build_launch_plan,
    launch_plan_to_dict,
    render_launch_plan,
)
from zen_app_runtime.manifest import create_manifest


class LaunchPlanTests(unittest.TestCase):
    def setUp(self):
        self.manifest = create_manifest(
            "example-mail",
            "Example Mail",
            "https://mail.example.com/inbox?view=all%20mail",
            mode="Transparent",
            include_stylus=True,
        )

    def test_plan_is_complete_versioned_and_canonical(self):
        plan = build_launch_plan(
            self.manifest,
            runtime_path="/opt/zen/zen",
            launcher_path="/usr/local/bin/zen-app",
            profile_root="/var/lib/zen-app-runtime/profiles",
        )

        expected = {
            "appId": "example-mail",
            "argv": [
                "/opt/zen/zen",
                "--profile",
                "/var/lib/zen-app-runtime/profiles/example-mail",
                "https://mail.example.com/inbox?view=all%20mail",
            ],
            "capabilities": [
                {
                    "name": "presentation-mode",
                    "requested": "Transparent",
                    "status": "requested-not-enforced",
                },
                {
                    "name": "profile-isolation",
                    "requested": "/var/lib/zen-app-runtime/profiles/example-mail",
                    "status": "requested-not-enforced",
                },
                {
                    "name": "link-policy",
                    "requested": {
                        "allowUserOverride": False,
                        "defaultAction": "external",
                        "externalTarget": "system-default-browser",
                        "internalNavigation": "same-origin",
                    },
                    "status": "requested-not-enforced",
                },
                {
                    "name": "extension:zen-internet",
                    "requested": "bundled",
                    "status": "unsupported",
                },
                {
                    "name": "extension:stylus",
                    "requested": "optional",
                    "status": "unsupported",
                },
            ],
            "launcherPath": "/usr/local/bin/zen-app",
            "manifestSchemaVersion": 1,
            "name": "Example Mail",
            "planVersion": 1,
            "profilePath": "/var/lib/zen-app-runtime/profiles/example-mail",
            "profileRoot": "/var/lib/zen-app-runtime/profiles",
            "runtimePath": "/opt/zen/zen",
            "startUrl": "https://mail.example.com/inbox?view=all%20mail",
        }
        self.assertEqual(expected, launch_plan_to_dict(plan))
        rendered = render_launch_plan(plan)
        self.assertEqual(json.dumps(expected, indent=2, sort_keys=True) + "\n", rendered)
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(rendered.endswith("\n\n"))

    def test_profile_path_is_derived_but_never_created(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            profile_root = Path(temporary) / "profiles-do-not-create"
            plan = build_launch_plan(
                self.manifest,
                runtime_path="/opt/zen/zen",
                launcher_path="/usr/local/bin/zen-app",
                profile_root=profile_root,
            )
            self.assertEqual(profile_root / "example-mail", Path(plan.profile_path))
            self.assertFalse(profile_root.exists())

    def test_runtime_launcher_and_profile_root_must_be_explicit_absolute_paths(self):
        valid = {
            "runtime_path": "/opt/zen/zen",
            "launcher_path": "/usr/local/bin/zen-app",
            "profile_root": "/profiles",
        }
        for field in valid:
            with self.subTest(field=field):
                values = dict(valid)
                values[field] = "relative/path"
                with self.assertRaisesRegex(LaunchPlanError, field.replace("_", " ")):
                    build_launch_plan(self.manifest, **values)

    def test_paths_reject_controls_without_touching_the_filesystem(self):
        with self.assertRaises(LaunchPlanError):
            build_launch_plan(
                self.manifest,
                runtime_path="/opt/zen\nlauncher",
                launcher_path="/usr/local/bin/zen-app",
                profile_root="/profiles",
            )

    def test_argv_is_an_ordered_token_sequence_not_a_shell_command(self):
        plan = build_launch_plan(
            self.manifest,
            runtime_path="/opt/Zen Runtime/zen;$HOME",
            launcher_path="/usr/local/bin/zen app",
            profile_root="/profiles with spaces",
        )
        self.assertIsInstance(plan.argv, tuple)
        self.assertEqual("/opt/Zen Runtime/zen;$HOME", plan.argv[0])
        self.assertEqual("--profile", plan.argv[1])
        self.assertNotIn("'", plan.argv[0])
    def test_render_rejects_incomplete_or_mislabeled_capabilities(self):
        from dataclasses import replace

        plan = build_launch_plan(
            self.manifest,
            runtime_path="/opt/zen/zen",
            launcher_path="/usr/local/bin/zen-app",
            profile_root="/profiles",
        )
        with self.assertRaisesRegex(LaunchPlanError, "capabilities"):
            launch_plan_to_dict(replace(plan, capabilities=plan.capabilities[:-1]))
        wrong_status = replace(plan.capabilities[0], status="unsupported")
        with self.assertRaisesRegex(LaunchPlanError, "capabilities"):
            launch_plan_to_dict(replace(plan, capabilities=(wrong_status, *plan.capabilities[1:])))

        escaped_profile = "/profiles/../escape"
        escaped_profile_capability = replace(plan.capabilities[1], requested=escaped_profile)
        forged = replace(
            plan,
            app_id="../escape",
            profile_path=escaped_profile,
            argv=(plan.runtime_path, "--profile", escaped_profile, plan.start_url),
            capabilities=(
                plan.capabilities[0],
                escaped_profile_capability,
                *plan.capabilities[2:],
            ),
        )
        with self.assertRaisesRegex(LaunchPlanError, "appId"):
            launch_plan_to_dict(forged)

        with self.assertRaisesRegex(LaunchPlanError, "schema-v1"):
            launch_plan_to_dict(replace(plan, name="forged\nname"))

        forged_url = "file:///etc/passwd"
        with self.assertRaisesRegex(LaunchPlanError, "schema-v1"):
            launch_plan_to_dict(
                replace(
                    plan,
                    start_url=forged_url,
                    argv=(*plan.argv[:-1], forged_url),
                )
            )


if __name__ == "__main__":
    unittest.main()
