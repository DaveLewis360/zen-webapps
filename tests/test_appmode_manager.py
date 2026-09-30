"""Webappok, the app manager: its profile, launcher, icon and `zen-app studio`."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import xml.dom.minidom

from zen_app_runtime.appmode import commands, icons, manager, render
from zen_app_runtime.appmode.paths import AppModePaths
from zen_app_runtime.appmode.zen import ZenRuntime


def fake_zen(root: Path) -> ZenRuntime:
    binary = root / "zen" / "zen-bin"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return ZenRuntime(binary=binary, version="9.9b", build_id="1", gecko="156.0")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = AppModePaths.under(self.root / "am")
        self.zen = fake_zen(self.root)
        self.patches = [
            mock.patch.object(render, "glass_sources", return_value={}),
            mock.patch.object(render, "inherited_render_prefs", return_value=({}, None)),
            mock.patch.object(icons, "_theme_chain", return_value=["hicolor"]),
            mock.patch.object(icons, "_all_installed_themes", return_value=[]),
            mock.patch.object(icons, "_find_in_theme", return_value=None),
            mock.patch.dict(os.environ, {"LANG": "hu_HU.UTF-8"}),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def test_manager_profile_is_an_app_profile_with_the_manager_role(self):
        prepared = manager.prepare_manager(self.paths, self.zen)
        profile = self.paths.manager_profile()
        self.assertEqual(profile, prepared.render.profile)
        config = json.loads((profile / "zen-app" / "app.json").read_text())
        self.assertEqual(("manager", "zen-apps", "zen-apps"), (config["role"], config["appId"], config["desktopId"]))
        self.assertEqual("Webappok", config["definition"]["name"])
        settings = config["definition"]["settings"]
        self.assertEqual((False, False, False, False), (settings["adblock"], settings["zenInternet"], settings["customCss"], settings["media"]))
        self.assertTrue(config["definition"]["onboarding"]["completed"])
        self.assertEqual(str(self.paths.manager_definition()), config["definitionPath"])
        self.assertEqual("zen-apps", json.loads(self.paths.manager_definition().read_text())["id"])
        self.assertTrue((profile / f"extensions/{render.ADDON_ID}.xpi").is_file())
        self.assertEqual(0o700, profile.parent.stat().st_mode & 0o777)
        # It is not one of the apps.
        self.assertEqual([], commands.Library(self.paths.library).list_ids())

    def test_launcher_and_icon(self):
        manager.prepare_manager(self.paths, self.zen)
        entry = self.paths.manager_desktop_path().read_text()
        self.assertIn("Name=Web Apps\nName[hu]=Webappok\n", entry)
        self.assertIn(f'Exec="{self.paths.shim_path()}" "studio"\n', entry)
        self.assertIn(f'Exec="{self.paths.shim_path()}" "studio" "--new"\n', entry)
        self.assertIn("StartupWMClass=zen-apps\n", entry)
        self.assertIn("Icon=zen-apps\n", entry)
        self.assertNotIn("zen-browser", entry)
        document = xml.dom.minidom.parseString(self.paths.manager_icon_path().read_text())
        self.assertEqual("svg", document.documentElement.tagName)

    def test_studio_starts_then_forwards_new_to_the_running_manager(self):
        calls = []

        class Process:
            pid = 4343

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return Process()

        outcome = manager.cmd_studio(new=True, paths=self.paths, zen=self.zen, popen=popen)
        self.assertEqual(("started", 4343), (outcome.action, outcome.pid))
        argv, kwargs = calls[0]
        link = self.paths.manager_dir() / "zen-apps"
        self.assertEqual(str(self.zen.binary), os.readlink(link))
        self.assertEqual(
            [str(link), "--name", "zen-apps", "--class", "zen-apps", "--profile", str(self.paths.manager_profile()), "about:blank"],
            argv,
        )
        self.assertEqual("zen-apps", kwargs["env"]["MOZ_APP_REMOTINGNAME"])
        pending = json.loads((self.paths.manager_profile() / "zen-app" / "pending.json").read_text())
        self.assertEqual("new", pending["action"])
        # While it runs, a second call forwards the action instead.
        with mock.patch.object(manager, "manager_running", return_value=99), mock.patch(
            "zen_app_runtime.appmode.launcher.running_pid", return_value=99
        ), mock.patch("zen_app_runtime.appmode.launcher._hyprctl_focus", return_value=True):
            outcome = manager.cmd_studio(new=True, paths=self.paths, zen=self.zen, popen=popen)
        self.assertEqual("forwarded", outcome.action)
        self.assertEqual(["--zen-app-action", "new"], calls[-1][0][-2:])

    def test_refresh_all_includes_the_manager(self):
        with mock.patch.dict(os.environ, {"ZEN_APP_MODE_ROOT": str(self.root / "am")}):
            out = __import__("io").StringIO()
            commands.cmd_refresh(None, as_json=True, out=out, paths=self.paths, zen=self.zen)
        result = json.loads(out.getvalue())
        self.assertEqual("zen-apps", result["apps"][0]["id"])
        self.assertTrue(result["apps"][0]["manager"])
        self.assertTrue(self.paths.manager_desktop_path().is_file())


if __name__ == "__main__":
    unittest.main()
