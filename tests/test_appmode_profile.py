import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from zen_app_runtime.appmode import commands, icons, launcher, render
from zen_app_runtime.appmode.definition import definition_from_preset, with_settings
from zen_app_runtime.appmode.desktop_entry import render_desktop_entry
from zen_app_runtime.appmode.icons import IconChoice
from zen_app_runtime.appmode.library import Library
from zen_app_runtime.appmode.paths import AppModePaths
from zen_app_runtime.appmode.presets import PRESETS
from zen_app_runtime.appmode.zen import ZenRuntime
from zen_app_runtime.cli import main


def fake_zen(root: Path) -> ZenRuntime:
    binary = root / "zen" / "zen-bin"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    (binary.parent / "application.ini").write_text("[App]\nVersion=9.9b\nBuildID=1\n")
    (binary.parent / "platform.ini").write_text("[Build]\nMilestone=156.0\n")
    return ZenRuntime(binary=binary, version="9.9b", build_id="1", gecko="156.0")


class AppModeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = AppModePaths.under(self.root / "am")
        self.library = Library(self.paths.library)
        self.zen = fake_zen(self.root)
        self.patches = [
            mock.patch.object(render, "glass_sources", return_value={"ghostty": {"color": "#000000", "alpha": 0.4, "label": "Ghostty", "path": None}}),
            mock.patch.object(render, "inherited_render_prefs", return_value=({"widget.dmabuf.force-enabled": True}, Path("/main/profile"))),
            mock.patch.object(icons, "_theme_chain", return_value=["hicolor"]),
            mock.patch.object(icons, "_all_installed_themes", return_value=[]),
            mock.patch.object(icons, "_find_in_theme", return_value=None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def prepare(self, key="discord"):
        definition = definition_from_preset(PRESETS[key])
        self.library.create(definition)
        return definition, commands.prepare(definition, self.paths, self.library, self.zen)

    def test_rendered_profile_has_prefs_chrome_addon_and_config(self):
        definition, prepared = self.prepare()
        profile = prepared.render.profile
        user_js = (profile / "user.js").read_text()
        self.assertIn('user_pref("xpinstall.signatures.required", false);', user_js)
        self.assertIn('user_pref("widget.gtk.native-context-menus", false);', user_js)
        self.assertIn('user_pref("widget.dmabuf.force-enabled", true);', user_js)
        self.assertLess(user_js.index("widget.dmabuf.force-enabled"), user_js.index("xpinstall.signatures.required"))
        self.assertIn(render.SETTINGS_BLOCK_BEGIN, user_js)
        self.assertIn('user_pref("zen.widget.linux.transparency", true);', user_js)
        self.assertIn('user_pref("permissions.default.desktop-notification", 1);', user_js)
        chrome = (profile / "chrome" / "userChrome.css").read_text()
        self.assertIn("--zenapp-tint: 40;", chrome)
        self.assertIn("--zenapp-tint-color: #000000;", chrome)
        self.assertIn("#navigator-toolbox", chrome)
        config = json.loads((profile / "zen-app" / "app.json").read_text())
        self.assertEqual("zen-app-discord", config["desktopId"])
        self.assertEqual("ghostty", config["glass"]["resolved"])
        self.assertEqual("bundled", config["zenInternet"]["origin"])
        self.assertTrue(Path(config["zenInternet"]["path"]).is_file())
        with zipfile.ZipFile(profile / "extensions" / f"{render.ADDON_ID}.xpi") as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
        self.assertIn("modules/ZenApp.sys.mjs", names)
        self.assertIn("modules/Styles.sys.mjs", names)
        self.assertIn("content/studio.html", names)
        self.assertEqual(prepared.render.addon_version, manifest["version"])
        self.assertNotIn("@VERSION@", json.dumps(manifest))

    def test_rendering_is_idempotent_and_addon_is_deterministic(self):
        definition, first = self.prepare()
        self.assertIn("user.js", first.render.changed)
        second = commands.prepare(definition, self.paths, self.library, self.zen)
        self.assertEqual([], second.render.changed)
        self.assertEqual(render.build_addon(), render.build_addon())

    def test_settings_change_rewrites_only_the_settings_block(self):
        definition, prepared = self.prepare()
        opaque = with_settings(definition, appearance="opaque")
        self.library.save(opaque)
        rendered = commands.prepare(opaque, self.paths, self.library, self.zen).render
        self.assertIn("user.js", rendered.changed)
        self.assertIn('user_pref("zen.widget.linux.transparency", false);', (rendered.profile / "user.js").read_text())

    def test_desktop_entry_is_native_and_never_names_zen_browser(self):
        definition, prepared = self.prepare("spotify")
        entry = prepared.desktop_entry.read_text()
        self.assertIn("StartupWMClass=zen-app-spotify", entry)
        self.assertIn("Categories=AudioVideo;Audio;Music;Player;", entry)
        self.assertIn('Exec="%s" "run" "spotify" %%U' % self.paths.shim_path(), entry)
        self.assertIn("[Desktop Action settings]", entry)
        self.assertNotIn("zen-browser", entry)
        self.assertEqual("generated", prepared.icon.source)
        self.assertIn("<svg", prepared.icon.file.read_text())
        shim = self.paths.shim_path().read_text()
        self.assertIn(commands.SHIM_MARKER, shim)
        self.assertIn("-m zen_app_runtime", shim)

    def test_desktop_entry_escaping(self):
        definition = definition_from_preset(PRESETS["discord"], name="Disc; ord")
        text = render_desktop_entry(definition, IconChoice("x", None, "theme"), Path("/opt/my apps/zen-app"))
        self.assertIn("Name=Disc; ord", text)
        self.assertIn('Exec="/opt/my apps/zen-app" "run" "discord" %U', text)

    def test_launch_starts_or_forwards_with_app_identity(self):
        definition, prepared = self.prepare()
        calls = []

        class Process:
            pid = 4242

        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return Process()

        outcome = launcher.launch(definition, self.paths, self.zen, action="settings", popen=popen)
        self.assertEqual(("started", 4242), (outcome.action, outcome.pid))
        argv, kwargs = calls[0]
        self.assertEqual(
            [str(self.zen.binary), "--name", "zen-app-discord", "--class", "zen-app-discord", "--profile", str(prepared.render.profile), "about:blank"],
            argv,
        )
        self.assertEqual("zen-app-discord", kwargs["env"]["MOZ_APP_REMOTINGNAME"])
        self.assertTrue(kwargs["start_new_session"])
        pending = json.loads((prepared.render.profile / "zen-app" / "pending.json").read_text())
        self.assertEqual("settings", pending["action"])

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(prepared.render.profile)])
        try:
            # Popen can return while the child is still inside execve, before
            # its new argv is visible in /proc; wait for it like a real lock
            # holder (Zen) that has long since started.
            deadline = time.monotonic() + 10
            while str(prepared.render.profile).encode() not in Path(f"/proc/{holder.pid}/cmdline").read_bytes():
                self.assertLess(time.monotonic(), deadline, "lock holder did not start")
                time.sleep(0.01)
            os.symlink(f"127.0.0.1:+{holder.pid}", prepared.render.profile / "lock")
            self.assertEqual(holder.pid, launcher.running_pid(prepared.render.profile))
            with mock.patch.object(launcher, "_hyprctl_focus", return_value=False):
                outcome = launcher.launch(definition, self.paths, self.zen, url="https://discord.com/channels/@me", popen=popen)
            self.assertEqual("forwarded", outcome.action)
            self.assertEqual(["--zen-app-open", "https://discord.com/channels/@me"], calls[-1][0][-2:])
        finally:
            holder.kill()
            holder.wait()
        self.assertIsNone(launcher.running_pid(prepared.render.profile))


class AppModeCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        zen = fake_zen(self.root)
        self.env = mock.patch.dict(os.environ, {"ZEN_APP_MODE_ROOT": str(self.root / "am"), "ZEN_APP_ZEN_BINARY": str(zen.binary)})
        self.env.start()
        self.patches = [
            mock.patch.object(render, "glass_sources", return_value={}),
            mock.patch.object(render, "inherited_render_prefs", return_value=({}, None)),
            mock.patch.object(icons, "_theme_chain", return_value=["hicolor"]),
            mock.patch.object(icons, "_all_installed_themes", return_value=[]),
            mock.patch.object(icons, "_find_in_theme", return_value=None),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.env.stop()
        self.temporary.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_new_list_styles_and_remove(self):
        code, out, err = self.run_cli("new", "spotify", "--no-launch")
        self.assertEqual((0, ""), (code, err))
        self.assertIn("Zen Internet: open.spotify.com.css", out)
        code, out, err = self.run_cli("new", "https://web.whatsapp.com/", "--id", "wa", "--no-launch")
        self.assertEqual(0, code, err)
        self.assertIn("appearance  transparent", out)
        code, out, _ = self.run_cli("apps")
        self.assertIn("spotify", out)
        self.assertIn("wa", out)
        code, out, _ = self.run_cli("styles", "show", "wa")
        self.assertIn("web.whatsapp.com.css", out)
        code, _, err = self.run_cli("new", "spotify", "--no-launch")
        self.assertEqual(2, code)
        self.assertIn("already exists", err)
        code, _, err = self.run_cli("rm", "wa")
        self.assertEqual(2, code)
        code, out, _ = self.run_cli("rm", "wa", "--yes", "--purge")
        self.assertEqual(0, code)
        self.assertFalse((self.root / "am" / "data" / "wa").exists())
        self.assertFalse((self.root / "am" / "applications" / "zen-app-wa.desktop").exists())

    def test_unknown_source_is_a_clean_error(self):
        code, _, err = self.run_cli("new", "not-a-preset", "--no-launch")
        self.assertEqual(2, code)
        self.assertIn("neither a preset", err)

    def test_presets_list_their_site_styles(self):
        code, out, err = self.run_cli("presets")
        self.assertEqual((0, ""), (code, err))
        spotify = next(line for line in out.splitlines() if line.startswith("spotify "))
        self.assertIn("open.spotify.com.css", spotify)
        self.assertIn("spotify-glass", spotify)
        messenger = next(line for line in out.splitlines() if line.startswith("messenger "))
        self.assertIn("facebook-messages-app", messenger)

    def test_glass_reports_sources_and_auto_choice(self):
        sources = {"ghostty": {"label": "Ghostty", "color": "#000000", "alpha": 0.4, "path": "/x/ghostty/config"}}
        with mock.patch("zen_app_runtime.appmode.commands.glass_sources", return_value=sources), mock.patch(
            "zen_app_runtime.appmode.commands.inherited_render_prefs", return_value=({"widget.dmabuf.force-enabled": True}, "/x/zen")
        ):
            code, out, err = self.run_cli("glass")
        self.assertEqual((0, ""), (code, err))
        self.assertIn("Ghostty  #000000   40%", out)
        self.assertIn("auto → ghostty", out)
        self.assertIn("widget.dmabuf.force-enabled = true", out)


if __name__ == "__main__":
    unittest.main()
