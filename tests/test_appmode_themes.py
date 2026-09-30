"""App-mode themes: site extras (addon/themes, global user sheets) and page themes (addon/page-themes)."""

from pathlib import Path
import re
import unittest

from zen_app_runtime.appmode import render
from zen_app_runtime.appmode.presets import PRESETS

THEMES = Path(render.__file__).resolve().parent / "addon" / "themes"
PAGE_THEMES = Path(render.__file__).resolve().parent / "addon" / "page-themes"


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def top_level_blocks(css: str) -> list[tuple[str, str]]:
    """(prelude, body) of every top-level block; stray top-level text fails."""
    blocks: list[tuple[str, str]] = []
    index = 0
    while True:
        while index < len(css) and css[index].isspace():
            index += 1
        if index == len(css):
            return blocks
        start = css.find("{", index)
        if start < 0:
            raise AssertionError(f"stray text outside any block: {css[index:index + 60]!r}")
        depth = 0
        for end in range(start, len(css)):
            if css[end] == "{":
                depth += 1
            elif css[end] == "}":
                depth -= 1
                if depth == 0:
                    break
        else:
            raise AssertionError("unbalanced braces")
        blocks.append((css[index:start].strip(), css[start + 1:end]))
        index = end + 1


def declarations(css: str) -> list[str]:
    found: list[str] = []
    for body in re.findall(r"\{([^{}]*)\}", css):
        found.extend(part.strip() for part in body.split(";") if part.strip())
    return found


class ThemeTests(unittest.TestCase):
    def test_every_extra_has_a_theme_file(self):
        for extra in render.EXTRAS.values():
            self.assertTrue((THEMES / extra["file"]).is_file(), extra["file"])

    def test_every_page_theme_has_its_file_and_a_linked_colour_block(self):
        for key, theme in render.PAGE_THEMES.items():
            with self.subTest(key):
                css = (PAGE_THEMES / theme["file"]).read_text(encoding="utf-8")
                blocks = top_level_blocks(strip_comments(css).split(";", 1)[1])  # after the @import
                self.assertTrue(blocks)
                self.assertRegex(css, r'^/\*[\s\S]*?\*/\s*@import url\("https://')
                self.assertEqual(1, css.count("/* BEGIN CAELESTIA COLORS */"))
                self.assertIn(theme["scheme"], ("dark", "light", "any"))
                self.assertIn(theme["when"], ("glass", "always"))
        preset_themes = {preset.theme for preset in PRESETS.values() if preset.theme}
        self.assertEqual({"discord-midnight"}, preset_themes)
        self.assertLessEqual(preset_themes, set(render.PAGE_THEMES))

    def test_midnight_keeps_the_discord_logo_and_quiet_scrollbars(self):
        css = strip_comments((PAGE_THEMES / "discord-midnight.css").read_text(encoding="utf-8"))
        # Midnight's moon icon is an image Discord's content policy blocks.
        self.assertIn("--custom-dms-icon: off;", css)
        # Firefox's scrollbar-color would show Midnight's yellow placeholder.
        self.assertIn("--scrollbar-auto-scrollbar-color-thumb: color-mix(", css)
        # The sidebar does not collapse.
        self.assertNotIn("width: 0px", css)

    def test_themes_are_site_scoped_and_every_declaration_is_important(self):
        # The sheets are global: an unscoped rule would restyle every site and
        # the app's own UI, and a user-sheet declaration without !important
        # silently loses to the site's own CSS.
        for path in sorted(THEMES.glob("*.css")):
            css = strip_comments(path.read_text(encoding="utf-8"))
            blocks = top_level_blocks(css)
            self.assertTrue(blocks, path.name)
            for prelude, _ in blocks:
                self.assertTrue(prelude.startswith("@-moz-document "), f"{path.name}: {prelude[:60]}")
            for declaration in declarations(css):
                self.assertRegex(declaration, r"!\s*important$", f"{path.name}: {declaration}")


if __name__ == "__main__":
    unittest.main()
