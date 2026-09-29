"""The add-on's custom CSS compiler and style aliases, run under node.

addon/modules/UserCss.sys.mjs and Styles.sys.mjs are plain ES modules; node
imports them directly. Skipped when node is not installed.
"""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

MODULES = Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules"
NODE = shutil.which("node")

MESSENGER = {
    "hosts": ["www.facebook.com", "facebook.com", "web.facebook.com", "www.messenger.com", "messenger.com"],
    "aliases": {
        "messenger.com": [
            "https://www.facebook.com/messages",
            "https://web.facebook.com/messages",
            "https://facebook.com/messages",
        ]
    },
    "startUrl": "https://www.facebook.com/messages/",
}
DISCORD = {"hosts": ["discord.com", "ptb.discord.com"], "aliases": {}, "startUrl": "https://discord.com/app"}

# Independent brace check: the compiled sheet must be one balanced block that
# only closes at its very end (comments, strings and url() skipped).
BALANCE_JS = r"""
function outline(css) {
  let depth = 0, opened = false, closedEarly = false;
  for (let i = 0; i < css.length; i++) {
    const c = css[i];
    if (c === "/" && css[i + 1] === "*") { const e = css.indexOf("*/", i + 2); if (e < 0) return "open comment"; i = e + 1; continue; }
    if (c === '"' || c === "'") { let j = i + 1; for (; j < css.length && css[j] !== c && css[j] !== "\n"; j++) if (css[j] === "\\") j++; i = j; continue; }
    if (c === "\\") { i++; continue; }
    if (/^url\(\s*[^"'\s]/i.test(css.slice(i, i + 6)) && !/[\w-]/.test(css[i - 1] || "")) { const e = css.indexOf(")", i); i = e < 0 ? css.length : e; continue; }
    if (c === "{") { depth++; opened = true; }
    if (c === "}") { depth--; if (depth === 0 && css.slice(i + 1).trim()) closedEarly = true; if (depth < 0) return "negative"; }
  }
  return !opened ? "empty" : depth !== 0 ? `depth ${depth}` : closedEarly ? "closed early" : "ok";
}
"""


@unittest.skipUnless(NODE, "node is not installed")
class UserCssCompilerTests(unittest.TestCase):
    def compile(self, css, scope=MESSENGER):
        return self.node(
            f"""
            import {{ compileUserCss }} from {json.dumps((MODULES / "UserCss.sys.mjs").as_uri())};
            {BALANCE_JS}
            const result = compileUserCss({json.dumps(css)}, {json.dumps(scope)});
            result.outline = result.css ? outline(result.css) : "none";
            console.log(JSON.stringify(result));
            """
        )

    def node(self, script):
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", textwrap.dedent(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_plain_css_is_scoped_to_the_app_and_made_important(self):
        result = self.compile("a { color: red; background: url(data:image/png;base64,AA;BB) }\n:root { --x: 1px }", DISCORD)
        css = result["css"]
        self.assertIn('@-moz-document domain("discord.com"), domain("ptb.discord.com") {', css)
        self.assertIn("color: red !important;", css)
        self.assertIn("url(data:image/png;base64,AA;BB) !important", css)
        self.assertIn("--x: 1px !important", css)
        self.assertEqual("ok", result["outline"])
        self.assertEqual((True, 0, []), (result["info"]["plain"], result["info"]["sections"], result["info"]["warnings"]))

    def test_existing_important_and_descriptor_blocks_are_left_alone(self):
        css = self.compile(
            "a { color: red ! important; }\n"
            "@keyframes k { from { opacity: 0 } to { opacity: 1 } }\n"
            "@font-face { font-family: X; src: url(x.woff2) }",
            DISCORD,
        )["css"]
        self.assertEqual(1, css.count("important"))
        self.assertIn("from { opacity: 0 }", css)
        self.assertIn("font-family: X; src: url(x.woff2) }", css)

    def test_nesting_and_group_rules_inside_rules_are_made_important(self):
        css = self.compile(".a { color: red; &:hover { color: blue } @media (min-width: 1px) { color: green } }", DISCORD)["css"]
        for declaration in ("color: red !important", "color: blue !important", "color: green !important"):
            self.assertIn(declaration, css)

    def test_messenger_userstyle_follows_messenger_to_facebook(self):
        result = self.compile(
            "/* ==UserStyle==\n@name           messenger.com\n@version        1.0.0\n==/UserStyle== */\n"
            '@-moz-document domain("messenger.com") {\n  .x16sw7j7 { background-color: rgba(0,0,0,.5) }\n}\n'
            '@-moz-document url-prefix("https://www.messenger.com/t/") { p { color: red } }'
        )
        css, info = result["css"], result["info"]
        self.assertIn(
            '@-moz-document domain("messenger.com"), url-prefix("https://www.facebook.com/messages"), '
            'url-prefix("https://web.facebook.com/messages"), url-prefix("https://facebook.com/messages") {',
            css,
        )
        self.assertIn('url-prefix("https://www.facebook.com/messages/t/")', css)
        self.assertIn("background-color: rgba(0,0,0,.5) !important", css)
        self.assertEqual(("messenger.com", "1.0.0", 2, 2), (info["name"], info["version"], info["sections"], info["matching"]))
        self.assertEqual(["messenger.com → facebook.com/messages"], info["aliases"])
        self.assertEqual("ok", result["outline"])

    def test_sections_for_other_sites_are_reported(self):
        info = self.compile('@-moz-document domain("discord.com") { p { color: red } }')["info"]
        self.assertEqual((1, 0, ["noMatch"], ["discord.com"]), (info["sections"], info["matching"], info["warnings"], info["elsewhere"]))

    def test_imports_are_dropped_and_namespaces_hoisted(self):
        result = self.compile('@charset "utf-8";\n@import url("https://evil.example/x.css");\n@namespace url(http://www.w3.org/1999/xhtml);\np { color: red }', DISCORD)
        css = result["css"]
        self.assertNotIn("@import", css)
        self.assertNotIn("@charset", css)
        self.assertTrue(css.startswith("@namespace url(http://www.w3.org/1999/xhtml);"))
        self.assertEqual(["import"], result["info"]["warnings"])

    def test_broken_or_hostile_files_cannot_leave_the_scope(self):
        for source in (
            "} } p { color: red }",
            "p { color: red;",
            "p { color: red } /* never closed",
            'p { color: red; content: "abc',
            "p { color: red; background: url(x.png",
            "/* } */ p { color: red } /*",
            ".x\\} p { color: red }",
            '@-moz-document url-prefix("") { p { color: red } }',
            '@-moz-document domain("discord.com") { } } p { color: red }',
            "p { color: red } \\",
        ):
            with self.subTest(source=source):
                result = self.compile(source, DISCORD)
                self.assertEqual("ok", result["outline"], result["css"])
                self.assertTrue(result["css"].rstrip().endswith("}"))

    def test_stylus_preprocessors_are_flagged(self):
        info = self.compile("/* ==UserStyle==\n@name x\n@preprocessor stylus\n@var color c 'C' red\n==/UserStyle== */\np { color: red }", DISCORD)["info"]
        self.assertEqual({"preprocessor", "vars"}, set(info["warnings"]))

    def test_nothing_to_apply_compiles_to_nothing(self):
        self.assertEqual("", self.compile("/* only a comment */", DISCORD)["css"])

    def test_import_header_round_trip(self):
        result = self.node(
            f"""
            import {{ importHeader, importedFrom, stripImportHeader }} from {json.dumps((MODULES / "UserCss.sys.mjs").as_uri())};
            const text = importHeader('my "style"*/.css') + "p{{}}";
            console.log(JSON.stringify([importedFrom(text), stripImportHeader(text), importHeader("")]));
            """
        )
        self.assertEqual(["my _style___.css", "p{}", ""], result)

    def test_catalog_styles_follow_aliases(self):
        result = self.node(
            f"""
            import {{ StyleCatalog }} from {json.dumps((MODULES / "Styles.sys.mjs").as_uri())};
            const catalog = new StyleCatalog({{ website: {{ "messenger.com.css": {{ "fbm-transparecy": ".x {{ color: red !important; }}" }} }} }});
            const sheets = catalog.sheets({json.dumps(MESSENGER["hosts"])}, {{ translucent: true, aliases: {json.dumps(MESSENGER["aliases"])} }});
            console.log(JSON.stringify(sheets.map(sheet => sheet.css)));
            """
        )
        self.assertEqual(1, len(result))
        self.assertIn('domain("messenger.com")', result[0])
        self.assertIn('url-prefix("https://www.facebook.com/messages")', result[0])


if __name__ == "__main__":
    unittest.main()
