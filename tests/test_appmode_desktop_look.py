import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from zen_app_runtime.appmode import desktop_look


class DesktopLookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.config = self.home / ".config"
        self.patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}),
            mock.patch("pathlib.Path.home", return_value=self.home),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_ghostty_includes_override_the_main_file(self):
        self.write(".config/ghostty/theme.conf", "background = #112233\n")
        self.write(
            ".config/ghostty/config",
            "config-file = theme.conf\nbackground = #000000\nbackground-opacity = 0.4\n# comment\n",
        )
        glass = desktop_look.ghostty_glass()
        self.assertEqual(("#112233", 0.4, "Ghostty"), (glass["color"], glass["alpha"], glass["label"]))

    def test_vesktop_reads_the_enabled_theme_layer(self):
        self.write(".config/vesktop/settings/settings.json", json.dumps({"transparent": True, "enabledThemes": ["a.theme.css"]}))
        self.write(".config/vesktop/themes/a.theme.css", ":root{--app-layer: rgba(1,2,3,.5);}\n#app-mount { background-color: rgba(0, 0, 0, 0.3) !important; }\n")
        glass = desktop_look.vesktop_glass()
        self.assertEqual(("#000000", 0.3), (glass["color"], glass["alpha"]))

    def test_vesktop_without_window_transparency_is_opaque(self):
        self.write(".config/vesktop/settings/settings.json", json.dumps({"transparent": False}))
        self.assertEqual(1.0, desktop_look.vesktop_glass()["alpha"])

    def test_zen_profile_glass_and_inherited_render_prefs(self):
        self.write(".zen/installs.ini", "[ABC]\nDefault=abc.Default\n")
        self.write(
            ".zen/abc.Default/prefs.js",
            'user_pref("zen.widget.linux.transparency", true);\n'
            'user_pref("mod.sameerasw.zen_bg_color_enabled", true);\n'
            'user_pref("mod.sameerasw.zen_transparency_color", "#00000080");\n'
            'user_pref("widget.dmabuf.force-enabled", true);\n'
            'user_pref("media.ffmpeg.vaapi.enabled", true);\n'
            'user_pref("gfx.blacklist.canvas2d", "x");\n'
            'user_pref("browser.startup.page", 3);\n',
        )
        glass = desktop_look.zen_glass()
        self.assertEqual(("#000000", "Transparent Zen"), (glass["color"], glass.get("mod")))
        self.assertAlmostEqual(0.502, glass["alpha"], places=2)
        prefs, profile = desktop_look.inherited_render_prefs()
        self.assertEqual(self.home / ".zen/abc.Default", profile)
        self.assertEqual({"media.ffmpeg.vaapi.enabled": True, "widget.dmabuf.force-enabled": True}, prefs)

    def test_zen_mod_colour_is_ignored_while_its_switch_is_off(self):
        self.write(".zen/installs.ini", "[ABC]\nDefault=abc.Default\n")
        prefs = self.write(
            ".zen/abc.Default/prefs.js",
            'user_pref("zen.widget.linux.transparency", true);\n'
            'user_pref("mod.sameerasw.zen_bg_color_enabled", false);\n'
            'user_pref("mod.sameerasw.zen_transparency_color", "#00000000");\n'
            'user_pref("zen.view.window.scheme", 0);\n',
        )
        glass = desktop_look.zen_glass()
        self.assertEqual(("#000000", 0.4, "Zen default"), (glass["color"], glass["alpha"], glass["note"]))
        prefs.write_text(prefs.read_text() + 'user_pref("zen.view.window.scheme", 1);\n')
        self.assertEqual(0.0, desktop_look.zen_glass()["alpha"])
        prefs.write_text('user_pref("zen.widget.linux.transparency", false);\n')
        self.assertEqual(1.0, desktop_look.zen_glass()["alpha"])

    def test_resolve_glass(self):
        sources = {"vesktop": {"color": "#000000", "alpha": 0.3}}
        self.assertEqual("vesktop", desktop_look.resolve_glass("auto", sources))
        self.assertEqual("custom", desktop_look.resolve_glass("ghostty", sources))
        self.assertEqual("custom", desktop_look.resolve_glass("auto", {}))
        self.assertEqual("custom", desktop_look.resolve_glass("custom", sources))


if __name__ == "__main__":
    unittest.main()
