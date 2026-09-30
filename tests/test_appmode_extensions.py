"""The add-on's AMO helpers (addon/modules/Extensions.sys.mjs), run under node.

Only the pure parts are tested here: which links count as AMO add-ons and
which AMO records the app is willing to download. Gecko globals are stubbed.
Skipped when node is not installed.
"""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

MODULE = (Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules" / "Extensions.sys.mjs").as_uri()
NODE = shutil.which("node")

PRELUDE = """
globalThis.ChromeUtils = { defineESModuleGetters() {} };
const parts = v => String(v).split(".").map(n => parseInt(n, 10) || 0);
globalThis.Services = {
  appinfo: { platformVersion: "143.0.1" },
  vc: {
    compare(a, b) {
      const x = parts(a), y = parts(b);
      for (let i = 0; i < Math.max(x.length, y.length); i++) {
        if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0) ? -1 : 1;
      }
      return 0;
    },
  },
};
const ext = await import(%s);
""" % json.dumps(MODULE)

HASH = "sha256:" + "5b74415860456370644bd80f16125e865b0e6c356bb5dfcfb84069967eaa5287"


@unittest.skipUnless(NODE, "node is not installed")
class ExtensionHelperTests(unittest.TestCase):
    def run_js(self, body):
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", PRELUDE + textwrap.dedent(body)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_amo_links_name_their_add_on_and_nothing_else_does(self):
        result = self.run_js(
            """
            const links = [
              "https://addons.mozilla.org/hu/firefox/addon/ublock-origin/",
              "https://addons.mozilla.org/en-US/firefox/addon/darkreader/?utm_source=x",
              "https://addons.mozilla.org/firefox/addon/%7B5384767E-00D9-40E9-B72F-9CC39D655D6F%7D",
              "addons.mozilla.org/de/firefox/addon/stylus",
              "  https://addons.mozilla.org/pt-BR/android/addon/sponsorblock/reviews/  ",
              "http://addons.mozilla.org/firefox/addon/ublock-origin/",
              "https://addons.mozilla.org.evil.example/firefox/addon/ublock-origin/",
              "https://evil.example/firefox/addon/ublock-origin/",
              "https://addons.mozilla.org/hu/firefox/collections/x/",
              "dark reader",
              "",
            ];
            console.log(JSON.stringify(links.map(ext.amoReference)));
            """
        )
        self.assertEqual(
            [
                "ublock-origin",
                "darkreader",
                "{5384767E-00D9-40E9-B72F-9CC39D655D6F}",
                "stylus",
                "sponsorblock",
                None,
                None,
                None,
                None,
                None,
                None,
            ],
            result,
        )

    def test_translated_fields_fall_back_sensibly(self):
        result = self.run_js(
            """
            console.log(JSON.stringify([
              ext.localized({ hu: "Magyar", en: "English" }, "hu"),
              ext.localized({ "en-US": "Dark Reader", hu: null, _default: "en-US" }, "hu"),
              ext.localized({ de: "Nur Deutsch" }, "hu"),
              ext.localized("plain", "hu"),
              ext.localized(null, "hu"),
            ]));
            """
        )
        self.assertEqual(["Magyar", "Dark Reader", "Nur Deutsch", "plain", ""], result)

    def test_only_amo_downloads_with_a_hash_are_accepted(self):
        record = {
            "guid": "uBlock0@raymondhill.net",
            "slug": "ublock-origin",
            "type": "extension",
            "name": {"hu": "uBlock Origin"},
            "summary": {"hu": "Reklámblokkoló"},
            "average_daily_users": 11159735,
            "promoted": [{"category": "recommended", "apps": ["firefox"]}],
            "icons": {"64": "https://addons.mozilla.org/user-media/addon_icons/607/607454-64.png"},
            "current_version": {
                "version": "1.75.0",
                "compatibility": {"firefox": {"min": "115.0", "max": "*"}},
                "file": {"url": "https://addons.mozilla.org/firefox/downloads/file/5034826/ublock_origin-1.75.0.xpi", "hash": HASH},
            },
        }
        result = self.run_js(
            """
            const record = %s;
            const variant = patch => {
              const copy = structuredClone(record);
              patch(copy);
              return ext.describeAmo(copy, "hu");
            };
            console.log(JSON.stringify({
              good: ext.describeAmo(record, "hu"),
              elsewhere: variant(r => { r.current_version.file.url = "https://evil.example/firefox/downloads/file/1/x.xpi"; }).url,
              plainHttp: variant(r => { r.current_version.file.url = "http://addons.mozilla.org/firefox/downloads/file/1/x.xpi"; }).url,
              notDownload: variant(r => { r.current_version.file.url = "https://addons.mozilla.org/user-media/x.xpi"; }).url,
              badHash: variant(r => { r.current_version.file.hash = "md5:abc"; }).hash,
              foreignIcon: variant(r => { r.icons["64"] = "https://tracker.example/i.png"; }).icon,
              tooNew: variant(r => { r.current_version.compatibility.firefox = { min: "150.0", max: "*" }; }).compatible,
              tooOld: variant(r => { r.current_version.compatibility.firefox = { min: "60.0", max: "102.*" }; }).compatible,
            }));
            """ % json.dumps(record)
        )
        good = result["good"]
        self.assertEqual("uBlock0@raymondhill.net", good["guid"])
        self.assertEqual("uBlock Origin", good["name"])
        self.assertEqual(HASH, good["hash"])
        self.assertTrue(good["url"].startswith("https://addons.mozilla.org/firefox/downloads/"))
        self.assertTrue(good["recommended"])
        self.assertTrue(good["compatible"])
        self.assertEqual(11159735, good["users"])
        self.assertEqual("", result["elsewhere"])
        self.assertEqual("", result["plainHttp"])
        self.assertEqual("", result["notDownload"])
        self.assertEqual("", result["badHash"])
        self.assertEqual("", result["foreignIcon"])
        self.assertFalse(result["tooNew"])
        self.assertFalse(result["tooOld"])


if __name__ == "__main__":
    unittest.main()
