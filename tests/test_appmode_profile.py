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
        # Zen's 10% white film over transparent web views must not come back:
        # the window glass is the only background (same contrast as Ghostty).
        self.assertIn("--zen-transparent-browser-background: transparent !important;", chrome)
        self.assertIn('#tabbrowser-tabpanels browser[type="content"] {\n    background: transparent !important;', chrome)
        config = json.loads((profile / "zen-app" / "app.json").read_text())
        self.assertEqual("zen-app-discord", config["desktopId"])
        self.assertEqual("ghostty", config["glass"]["resolved"])
        self.assertEqual("bundled", config["zenInternet"]["origin"])
        self.assertTrue(Path(config["zenInternet"]["path"]).is_file())
        self.assertEqual(str(self.paths.library / "discord.css"), config["customCss"]["path"])
        self.assertEqual(render.MAX_CUSTOM_CSS_BYTES, config["customCss"]["maxBytes"])
        self.assertEqual({}, config["styleAliases"])
        self.assertTrue(config["definition"]["settings"]["customCss"])
        with zipfile.ZipFile(profile / "extensions" / f"{render.ADDON_ID}.xpi") as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
        self.assertIn("modules/ZenApp.sys.mjs", names)
        self.assertIn("modules/Styles.sys.mjs", names)
        self.assertIn("content/studio.html", names)
        self.assertEqual(prepared.render.addon_version, manifest["version"])
        self.assertNotIn("@VERSION@", json.dumps(manifest))
        self.assertIn("modules/UserCss.sys.mjs", names)

    def test_messenger_config_carries_its_style_aliases(self):
        _, prepared = self.prepare("messenger")
        config = json.loads((prepared.render.profile / "zen-app" / "app.json").read_text())
        self.assertEqual(
            {"messenger.com": ["https://www.facebook.com/messages", "https://web.facebook.com/messages", "https://facebook.com/messages"]},
            config["styleAliases"],
        )

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

    def test_process_link_follows_the_zen_binary_and_falls_back(self):
        link = self.paths.app_dir("spotify") / "zen-app-spotify"
        self.assertEqual(link, launcher.process_executable(self.paths, "spotify", self.zen))
        self.assertEqual(str(self.zen.binary), os.readlink(link))
        # A moved Zen installation re-points the link (atomically, no leftovers).
        other = self.root / "other-zen" / "zen-bin"
        other.parent.mkdir()
        other.write_text("#!/bin/sh\n")
        moved = ZenRuntime(binary=other, version="9.9b", build_id="1", gecko="156.0")
        self.assertEqual(link, launcher.process_executable(self.paths, "spotify", moved))
        self.assertEqual(str(other), os.readlink(link))
        self.assertEqual(["zen-app-spotify"], sorted(p.name for p in link.parent.iterdir() if "zen-app-spotify" in p.name))
        # Something that cannot be replaced by a link: use Zen directly.
        blocked = self.paths.app_dir("blocked") / "zen-app-blocked"
        (blocked / "inside").mkdir(parents=True)
        self.assertEqual(self.zen.binary, launcher.process_executable(self.paths, "blocked", self.zen))
        self.assertEqual(["zen-app-blocked"], sorted(p.name for p in blocked.parent.iterdir()))

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
        # Started through <app dir>/zen-app-discord → Zen, so the process is
        # named after the app and `killall zen-bin` leaves it alone.
        link = self.paths.app_dir("discord") / "zen-app-discord"
        self.assertEqual(str(self.zen.binary), os.readlink(link))
        self.assertEqual(
            [str(link), "--name", "zen-app-discord", "--class", "zen-app-discord", "--profile", str(prepared.render.profile), "about:blank"],
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

    def test_custom_css_command_sets_switches_and_removes(self):
        self.assertEqual(0, self.run_cli("new", "messenger", "--no-launch")[0])
        library = self.root / "am" / "library"
        style = self.root / "messenger.user.css"
        style.write_bytes("\ufeff/* ==UserStyle==\n@name messenger.com\n==/UserStyle== */\np { color: red }\n".encode())
        code, out, err = self.run_cli("css", "messenger", str(style))
        self.assertEqual((0, ""), (code, err))
        saved = library / "messenger.css"
        self.assertEqual(0o600, saved.stat().st_mode & 0o777)
        text = saved.read_text()
        self.assertTrue(text.startswith('/* zen-app: imported from "messenger.user.css" */\n/* ==UserStyle=='), text[:80])
        self.assertNotIn("\ufeff", text)
        # Importing the saved copy again does not stack headers.
        self.assertEqual(0, self.run_cli("css", "messenger", str(saved))[0])
        self.assertEqual(1, saved.read_text().count("zen-app: imported from"))
        code, out, _ = self.run_cli("css", "messenger")
        self.assertIn("custom CSS on", out)
        self.assertEqual(0, self.run_cli("css", "messenger", "--off")[0])
        definition = json.loads((library / "messenger.json").read_text())
        self.assertFalse(definition["settings"]["customCss"])
        self.assertIn("custom CSS off", self.run_cli("css", "messenger")[1])
        code, out, _ = self.run_cli("styles", "show", "messenger")
        self.assertIn("Styles for messenger.com also apply at: https://www.facebook.com/messages", out)
        self.assertIn(f"Custom CSS (off): {saved}", out)
        # A new file switches it back on.
        self.assertEqual(0, self.run_cli("css", "messenger", str(style))[0])
        self.assertTrue(json.loads((library / "messenger.json").read_text())["settings"]["customCss"])
        code, out, _ = self.run_cli("css", "messenger", "--remove")
        self.assertEqual((0, "Messenger: custom CSS removed\n"), (code, out))
        self.assertFalse(saved.exists())

    def test_custom_css_command_rejects_unusable_files(self):
        self.assertEqual(0, self.run_cli("new", "discord", "--no-launch")[0])
        cases = {
            "big.css": b"a{}" * (render.MAX_CUSTOM_CSS_BYTES // 3 + 1),
            "latin1.css": "p { content: 'é' }".encode("latin-1"),
            "empty.css": b"  \n",
            "nul.css": b"p{}\x00",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                code, _, err = self.run_cli("css", "discord", str(path))
                self.assertEqual(2, code)
                self.assertTrue(err)
        self.assertFalse((self.root / "am" / "library" / "discord.css").exists())
        code, _, err = self.run_cli("css", "discord", str(self.root / "missing.css"))
        self.assertEqual(2, code)
        self.assertIn("cannot read", err)
        self.assertEqual(2, self.run_cli("css", "discord", str(self.root / "big.css"), "--off")[0])

    def test_custom_css_for_a_running_app_explains_when_it_applies(self):
        self.assertEqual(0, self.run_cli("new", "discord", "--no-launch")[0])
        style = self.root / "d.css"
        style.write_text("p { color: red }")
        status = self.root / "am" / "data" / "discord" / "profile" / "zen-app" / "status.json"
        status.write_text(json.dumps({"statusVersion": 1}))
        with mock.patch.object(commands, "running_pid", return_value=4242):
            code, out, _ = self.run_cli("css", "discord", str(style))
            self.assertEqual(0, code)
            self.assertIn("older version: close and reopen it", out)
            status.write_text(json.dumps({"statusVersion": 1, "customCss": {"state": "none"}}))
            self.assertIn("applies it when you switch to its window", self.run_cli("css", "discord", str(style))[1])
            code, _, err = self.run_cli("css", "discord", "--off")
            self.assertEqual(2, code)
            self.assertIn("is running", err)

    def test_purge_also_deletes_the_custom_css(self):
        self.assertEqual(0, self.run_cli("new", "discord", "--no-launch")[0])
        style = self.root / "d.css"
        style.write_text("p { color: red }")
        self.assertEqual(0, self.run_cli("css", "discord", str(style))[0])
        saved = self.root / "am" / "library" / "discord.css"
        code, out, _ = self.run_cli("rm", "discord", "--yes")
        self.assertIn(f"custom CSS kept: {saved}", out)
        self.assertTrue(saved.exists())
        self.assertEqual(0, self.run_cli("rm", "discord", "--yes", "--purge")[0])
        self.assertFalse(saved.exists())

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
