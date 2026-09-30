"""The add-on's palette (Palette.sys.mjs) and page theme rules (Config.sys.mjs), run under node."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from zen_app_runtime.appmode.palette import _hex_to_oklch

MODULES = Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules"
NODE = shutil.which("node")

DARK_SCHEME = {
    "name": "dynamic",
    "mode": "dark",
    "colours": {
        "primary": "aacae9", "onPrimary": "22435c", "surface": "0b0e11", "onSurface": "e0e6ee",
        "primary_paletteKeyColor": "5a7a96", "primaryPaletteKeyColor": "5a7a96", "secondaryPaletteKeyColor": "697988",
        "tertiaryPaletteKeyColor": "72739e", "neutralPaletteKeyColor": "75777b", "neutralVariantPaletteKeyColor": "72787f",
        "success": "B5CCBA", "mauve": "98c3ff",
    },
}
# A light wallpaper: Caelestia's smart scheme switches to light tones.
LIGHT_SCHEME = {
    "name": "dynamic",
    "mode": "light",
    "colours": {
        "primary": "8d4a5b", "onPrimary": "ffffff", "surface": "fff8f7", "onSurface": "22191b",
        "primaryPaletteKeyColor": "b06c7e", "secondaryPaletteKeyColor": "8e7176", "tertiaryPaletteKeyColor": "a07650",
        "neutralPaletteKeyColor": "827475", "neutralVariantPaletteKeyColor": "857374",
    },
}


def lightness(hex_colour: str) -> float:
    return _hex_to_oklch(hex_colour)[0]


def hue_distance(a: str, b: str) -> float:
    delta = abs(_hex_to_oklch(a)[2] - _hex_to_oklch(b)[2]) % 360
    return min(delta, 360 - delta)


@unittest.skipUnless(NODE, "node is not installed")
class PaletteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def node(self, body):
        script = (
            'import fs from "node:fs";\n'
            "globalThis.IOUtils = { readJSON: async path => JSON.parse(fs.readFileSync(path, 'utf8')) };\n"
            f"const P = await import({json.dumps((MODULES / 'Palette.sys.mjs').as_uri())});\n"
            f"const C = await import({json.dumps((MODULES / 'Config.sys.mjs').as_uri())});\n"
            + textwrap.dedent(body)
        )
        completed = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def scheme(self, data):
        path = self.root / "scheme.json"
        path.write_text(json.dumps(data))
        return str(path)

    def resolve(self, scheme, settings, system_dark):
        paths = [str(self.root / "missing.json"), scheme] if scheme else []
        return self.node(
            f"""
            const palette = await P.resolvePalette({json.dumps(settings)}, "#5865f2", {json.dumps(paths)}, {json.dumps(system_dark)});
            console.log(JSON.stringify(palette));
            """
        )

    def test_the_wallpaper_scheme_is_used_as_written(self):
        palette = self.resolve(self.scheme(DARK_SCHEME), {"palette": "system", "colorScheme": "system"}, False)
        self.assertEqual(("system", True), (palette["source"], palette["dark"]))
        self.assertEqual(("#aacae9", "#0b0e11"), (palette["roles"]["primary"], palette["roles"]["surface"]))

    def test_light_or_dark_is_the_scheme_own_mode(self):
        # Right after a new wallpaper the desktop may still report dark: the
        # scheme decides, as it does for the shell.
        palette = self.resolve(self.scheme(LIGHT_SCHEME), {"palette": "system", "colorScheme": "system"}, True)
        self.assertEqual(("system", False), (palette["source"], palette["dark"]))
        self.assertEqual("#8d4a5b", palette["roles"]["primary"])

    def test_a_light_wallpaper_in_dark_tones_keeps_its_hues(self):
        palette = self.resolve(self.scheme(LIGHT_SCHEME), {"palette": "system", "colorScheme": "dark"}, False)
        roles = palette["roles"]
        self.assertEqual(("system-seed", True), (palette["source"], palette["dark"]))
        self.assertLess(lightness(roles["surface"]), 0.25)
        self.assertGreater(lightness(roles["onSurface"]), 0.85)
        self.assertGreater(lightness(roles["primary"]), 0.7)
        # Each role family keeps its own key colour's hue.
        self.assertLess(hue_distance(roles["primary"], "#b06c7e"), 12)
        self.assertLess(hue_distance(roles["tertiary"], "#a07650"), 12)

    def test_without_a_scheme_the_accent_and_desktop_decide(self):
        palette = self.resolve(None, {"palette": "system", "colorScheme": "system"}, False)
        self.assertEqual(("seed", False), (palette["source"], palette["dark"]))
        self.assertLess(hue_distance(palette["roles"]["primary"], "#5865f2"), 12)

    def test_page_theme_states_and_prefs(self):
        result = self.node(
            """
            const config = {
              pageThemes: [{ id: "discord-midnight", scheme: "dark", when: "glass" }],
              settingsPrefs: { colorScheme: {
                system: { "layout.css.prefers-color-scheme.content-override": 2 },
                dark: { "layout.css.prefers-color-scheme.content-override": 0 },
                light: { "layout.css.prefers-color-scheme.content-override": 1 },
              } },
            };
            const base = { appearance: "transparent", colorScheme: "system", disabledFeatures: [] };
            const cases = {
              on: base,
              off: { ...base, disabledFeatures: [C.themeFeatureKey("discord-midnight")] },
              opaque: { ...base, appearance: "opaque" },
              light: { ...base, colorScheme: "light" },
            };
            const out = {};
            for (const [name, settings] of Object.entries(cases)) {
              const [state] = C.pageThemeStates(config, settings);
              out[name] = [state.enabled, state.active, state.blocked, C.effectivePrefs(settings, config)["layout.css.prefers-color-scheme.content-override"]];
            }
            out.none = C.pageThemeStates({}, base);
            console.log(JSON.stringify(out));
            """
        )
        self.assertEqual([True, True, None, 0], result["on"])
        self.assertEqual([False, False, None, 2], result["off"])
        self.assertEqual([True, False, "glass", 2], result["opaque"])
        self.assertEqual([True, False, "light", 1], result["light"])
        self.assertEqual([], result["none"])


if __name__ == "__main__":
    unittest.main()
