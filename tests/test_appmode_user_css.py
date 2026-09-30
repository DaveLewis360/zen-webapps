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
USER_CSS = (MODULES / "UserCss.sys.mjs").as_uri()

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
DISCORD = {"hosts": ["discord.com", "*.discord.com"], "aliases": {}, "startUrl": "https://discord.com/app"}

# Independent brace check: a chunk must be balanced on its own (comments,
# strings and url() skipped), so no chunk can swallow the next one.
BALANCE_JS = r"""
function balanced(css) {
  let depth = 0;
  for (let i = 0; i < css.length; i++) {
    const c = css[i];
    if (c === "/" && css[i + 1] === "*") { const e = css.indexOf("*/", i + 2); if (e < 0) return false; i = e + 1; continue; }
    if (c === '"' || c === "'") { let j = i + 1; for (; j < css.length && css[j] !== c && css[j] !== "\n"; j++) if (css[j] === "\\") j++; i = j; continue; }
    if (c === "\\") { i++; continue; }
    if (/^url\(\s*[^"'\s]/i.test(css.slice(i, i + 6)) && !/[\w-]/.test(css[i - 1] || "")) { const e = css.indexOf(")", i); i = e < 0 ? css.length : e; continue; }
    if (c === "{") depth++;
    if (c === "}" && --depth < 0) return false;
  }
  return depth === 0;
}
"""


@unittest.skipUnless(NODE, "node is not installed")
class UserCssCompilerTests(unittest.TestCase):
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

    def compile(self, css, scope=DISCORD, urls=(), **options):
        """Compile, then select the CSS each of `urls` would get."""
        return self.node(
            f"""
            import {{ compileUserCss, selectCss }} from {json.dumps(USER_CSS)};
            {BALANCE_JS}
            const result = compileUserCss({json.dumps(css)}, {{ ...{json.dumps(scope)}, ...{json.dumps(options)} }});
            result.balanced = (result.program?.chunks || []).every(chunk => balanced(chunk.css));
            result.pages = Object.fromEntries({json.dumps(list(urls))}.map(url => [url, selectCss(result.program, url)]));
            console.log(JSON.stringify(result));
            """
        )

    def test_plain_css_applies_to_the_app_pages_as_written(self):
        source = "a { color: red; background: url(data:image/png;base64,AA;BB) }\n:root { --x: 1px }"
        result = self.compile(
            source,
            urls=["https://discord.com/channels/@me", "https://ptb.discord.com/app", "https://example.com/", "chrome://zenapp/content/studio.html"],
        )
        program = result["program"]
        self.assertEqual({"hosts": ["discord.com", "*.discord.com"], "prefixes": []}, program["scope"])
        self.assertEqual([[]], [chunk["levels"] for chunk in program["chunks"]])
        # Author semantics like Stylus/Vencord: nothing is turned into !important.
        self.assertEqual(source, result["pages"]["https://discord.com/channels/@me"])
        self.assertEqual(source, result["pages"]["https://ptb.discord.com/app"])
        self.assertEqual("", result["pages"]["https://example.com/"])
        self.assertEqual("", result["pages"]["chrome://zenapp/content/studio.html"])
        self.assertEqual((True, 0, []), (result["info"]["plain"], result["info"]["sections"], result["info"]["warnings"]))

    def test_messenger_userstyle_follows_messenger_to_facebook(self):
        source = (
            "/* ==UserStyle==\n@name           messenger.com\n@version        1.0.0\n==/UserStyle== */\n"
            '@-moz-document domain("messenger.com") {\n  .x16sw7j7 { background-color: rgba(0,0,0,.5) }\n}\n'
            '@-moz-document url-prefix("https://www.messenger.com/t/") { p { color: red } }'
        )
        result = self.compile(
            source,
            MESSENGER,
            urls=["https://www.facebook.com/messages/t/123", "https://www.facebook.com/", "https://www.messenger.com/t/9", "https://example.com/messages"],
        )
        info, pages = result["info"], result["pages"]
        first = result["program"]["chunks"][0]["levels"][0]
        self.assertIn({"fn": "domain", "arg": "messenger.com"}, first)
        self.assertIn({"fn": "url-prefix", "arg": "https://www.facebook.com/messages"}, first)
        self.assertIn("background-color: rgba(0,0,0,.5)", pages["https://www.facebook.com/messages/t/123"])
        self.assertIn("color: red", pages["https://www.facebook.com/messages/t/123"])  # t/ moved along
        self.assertEqual("", pages["https://www.facebook.com/"])  # not Messenger: no section applies
        self.assertIn("color: red", pages["https://www.messenger.com/t/9"])
        self.assertEqual("", pages["https://example.com/messages"])
        self.assertEqual(("messenger.com", "1.0.0", 2, 2), (info["name"], info["version"], info["sections"], info["matching"]))
        self.assertEqual(["messenger.com → facebook.com/messages"], info["aliases"])

    def test_section_conditions_are_evaluated_per_page(self):
        source = (
            '@-moz-document regexp("https://discord\\\\.com/channels/.*") { .a { color: red } }\n'
            '@-moz-document url("https://discord.com/app") { .b { color: blue } }\n'
            '@-moz-document domain("discord.com") { @-moz-document url-prefix("https://ptb.") { .c { color: green } } }'
        )
        pages = self.compile(
            source, urls=["https://discord.com/channels/1", "https://discord.com/app", "https://ptb.discord.com/x", "https://discord.com/channelsX"]
        )["pages"]
        self.assertEqual(".a { color: red }", pages["https://discord.com/channels/1"].strip())
        self.assertEqual(".b { color: blue }", pages["https://discord.com/app"].strip())
        self.assertEqual(".c { color: green }", pages["https://ptb.discord.com/x"].strip())
        self.assertEqual("", pages["https://discord.com/channelsX"])

    def test_sections_for_other_sites_are_reported(self):
        info = self.compile('@-moz-document domain("example.org") { p { color: red } }')["info"]
        self.assertEqual((1, 0, ["noMatch"], ["example.org"]), (info["sections"], info["matching"], info["warnings"], info["elsewhere"]))

    def test_imports_are_listed_then_inlined_with_absolute_urls(self):
        midnight = "https://refact0r.github.io/midnight-discord/build/midnight.css"
        fonts = "https://refact0r.github.io/midnight-discord/build/fonts.css"
        source = f'@charset "utf-8";\n@import url("{midnight}");\n@import url("http://insecure.example/x.css");\n@namespace url(http://www.w3.org/1999/xhtml);\np {{ color: red }}'
        first = self.compile(source)
        self.assertEqual([midnight], first["info"]["imports"]["pending"])
        self.assertEqual(["import"], first["info"]["warnings"])  # the http:// one
        imports = {
            midnight: '@import "fonts.css" screen;\n.m { background: url(../assets/moon.svg) }',
            fonts: "@font-face { font-family: F; src: url(f.woff2) }",
        }
        result = self.compile(source, urls=["https://discord.com/app"], imports=imports)
        css = result["pages"]["https://discord.com/app"]
        self.assertEqual([], result["info"]["imports"]["pending"])
        self.assertEqual([midnight, fonts], result["info"]["imports"]["inlined"])
        self.assertTrue(css.startswith("@namespace url(http://www.w3.org/1999/xhtml);"))
        self.assertNotIn("@import", css)
        self.assertNotIn("@charset", css)
        self.assertIn('url("https://refact0r.github.io/midnight-discord/assets/moon.svg")', css)
        self.assertIn('@media screen {@font-face { font-family: F; src: url("https://refact0r.github.io/midnight-discord/build/f.woff2") }}', css)
        self.assertLess(css.index(".m {"), css.index("p { color: red }"))  # imports come first, like in CSS
        failed = self.compile(source, imports={midnight: ""})["info"]
        self.assertEqual([midnight], failed["imports"]["failed"])
        self.assertIn("importFailed", failed["warnings"])

    def test_import_depth_is_limited(self):
        base = "https://example.org/"
        imports = {f"{base}{i}.css": f'@import "{i + 1}.css";\n.l{i} {{ color: red }}' for i in range(6)}
        result = self.compile(f'@import "{base}0.css";', urls=["https://discord.com/app"], imports=imports)
        self.assertEqual(3, len(result["info"]["imports"]["inlined"]))
        self.assertIn("import", result["info"]["warnings"])

    def test_caelestia_block_is_linked_to_the_palette(self):
        source = (
            ":root { --primary: #111111; }\n"
            "/* BEGIN CAELESTIA COLORS */\n:root {\n  --primary: #f9b7a5;\n  --onSurface: #f9e0da !important;\n  --shade: #123;\n}\n/* END CAELESTIA COLORS */\n"
            "body { --primary: #222222; }"
        )
        result = self.compile(source, urls=["https://discord.com/app"])
        css = result["pages"]["https://discord.com/app"]
        self.assertEqual({"blocks": 1, "names": ["primary", "onSurface", "shade"]}, result["info"]["palette"])
        # The written colour stays the fallback; outside the block nothing changes.
        self.assertIn("--primary: var(--zenapp-c-primary, #f9b7a5);", css)
        self.assertIn("--onSurface: var(--zenapp-c-onSurface, #f9e0da) !important;", css)
        self.assertIn("--shade: var(--zenapp-c-shade, #123);", css)
        self.assertIn(":root { --primary: #111111; }", css)
        self.assertIn("body { --primary: #222222; }", css)
        self.assertEqual([{"prefix": "--zenapp-c-", "names": ["primary", "onSurface", "shade"]}], result["program"]["palette"])
        dark = self.compile(source, urls=["https://discord.com/app"], palettePrefix="--zenapp-cd-")
        self.assertIn("--primary: var(--zenapp-cd-primary, #f9b7a5);", dark["pages"]["https://discord.com/app"])
        self.assertEqual([], self.compile("p { color: red }")["program"]["palette"])

    def test_palette_values_and_sheet(self):
        values, sheet, empty = self.node(
            f"""
            import {{ paletteSheet, paletteValues }} from {json.dumps(USER_CSS)};
            const values = paletteValues([
              {{ prefix: "--zenapp-cd-", names: ["primary", "onSurface", "missing", "bad name"], colors: {{ primary: "#AABBCC", onSurface: "#010203", "bad name": "#000000" }} }},
              {{ prefix: "--zenapp-cd-", names: ["primary"], colors: {{ primary: "#ffffff" }} }},
              {{ prefix: "--zenapp-c-", names: ["surface"], colors: {{ surface: "not a colour" }} }},
              {{ prefix: "not-a-prefix", names: ["x"], colors: {{ x: "#000000" }} }},
            ]);
            console.log(JSON.stringify([values, paletteSheet(values), paletteSheet({{}})]));
            """
        )
        # The first value of a property wins; bad names and values are left out.
        self.assertEqual({"--zenapp-cd-primary": "#aabbcc", "--zenapp-cd-onSurface": "#010203"}, values)
        self.assertIn('@property --zenapp-cd-primary { syntax: "<color>"; inherits: true; initial-value: transparent; }', sheet)
        self.assertIn(":root {\n  --zenapp-cd-primary: #aabbcc;\n  --zenapp-cd-onSurface: #010203;\n}", sheet)
        self.assertEqual("", empty)

    def test_programs_merge_in_order(self):
        result = self.node(
            f"""
            import {{ compileUserCss, mergePrograms, selectCss }} from {json.dumps(USER_CSS)};
            const scope = {json.dumps(DISCORD)};
            const theme = compileUserCss("/* BEGIN CAELESTIA COLORS */ :root {{ --a: #111111; }} /* END CAELESTIA COLORS */ .t {{ color: var(--a) }}", {{ ...scope, palettePrefix: "--zenapp-cd-" }}).program;
            const own = compileUserCss("@namespace url(http://www.w3.org/1999/xhtml);\\n.t {{ color: red }}", scope).program;
            const merged = mergePrograms([theme, null, own]);
            console.log(JSON.stringify({{ merged, css: selectCss(merged, "https://discord.com/app"), single: mergePrograms([null, own]) === own, none: mergePrograms([null]) }}));
            """
        )
        css = result["css"]
        self.assertTrue(css.startswith("@namespace url(http://www.w3.org/1999/xhtml);"))
        self.assertLess(css.index("var(--a)"), css.index("color: red"))  # the app's CSS can override the theme
        self.assertEqual([{"prefix": "--zenapp-cd-", "names": ["a"]}], result["merged"]["palette"])
        self.assertTrue(result["single"])
        self.assertIsNone(result["none"])

    def test_broken_or_hostile_files_stay_balanced(self):
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
            "@media screen { @-moz-document domain(\"discord.com\") { p { color: red } } }",
            "p { color: red } \\",
        ):
            with self.subTest(source=source):
                result = self.compile(source, urls=["https://discord.com/app"])
                self.assertTrue(result["balanced"], result["program"])

    def test_stylus_preprocessors_are_flagged(self):
        info = self.compile("/* ==UserStyle==\n@name x\n@preprocessor stylus\n@var color c 'C' red\n==/UserStyle== */\np { color: red }")["info"]
        self.assertEqual({"preprocessor", "vars"}, set(info["warnings"]))

    def test_nothing_to_apply_compiles_to_nothing(self):
        self.assertIsNone(self.compile("/* only a comment */")["program"])
        self.assertIsNone(self.compile("p { color: red }", {"hosts": [], "aliases": {}})["program"])

    def test_import_header_round_trip(self):
        result = self.node(
            f"""
            import {{ importHeader, importedFrom, stripImportHeader }} from {json.dumps(USER_CSS)};
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

    def test_palette_colors_cover_the_caelestia_names(self):
        result = self.node(
            f"""
            import {{ paletteColors, derivePalette }} from {json.dumps((MODULES / "Palette.sys.mjs").as_uri())};
            const seed = derivePalette("#7b6cff", true);
            const system = {{ ...seed, source: "system", scheme: {{ success: "#B5CCBA", primary: "#000001", mauve: "#98c3ff" }} }};
            console.log(JSON.stringify([paletteColors(seed), paletteColors(system), paletteColors(derivePalette("#7b6cff", false)), paletteColors(null)]));
            """
        )
        seed, system, light, none = result
        self.assertEqual(seed["surface"], seed["background"])
        self.assertEqual(seed["primary"], seed["surfaceTint"])
        self.assertEqual(seed["primary"], seed["primaryDim"])
        # Caelestia's fixed success colours, in the palette's brightness.
        self.assertEqual("#b5ccba", seed["success"])
        self.assertNotEqual(seed["success"], light["success"])
        self.assertNotIn("mauve", seed)
        self.assertEqual(("#b5ccba", "#98c3ff"), (system["success"], system["mauve"]))
        self.assertNotEqual("#000001", system["primary"])  # the M3 roles win over raw scheme names
        self.assertIsNone(none)


if __name__ == "__main__":
    unittest.main()
