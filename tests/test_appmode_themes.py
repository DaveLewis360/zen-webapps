"""App-mode site themes (addon/themes) are registered as global user sheets."""

from pathlib import Path
import re
import unittest

from zen_app_runtime.appmode import render

THEMES = Path(render.__file__).resolve().parent / "addon" / "themes"


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
