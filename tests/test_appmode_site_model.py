"""The new-app wizard's suggestions (addon/modules/SiteModel.sys.mjs), run under node."""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

MODULE = (Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules" / "SiteModel.sys.mjs").as_uri()
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class SiteModelTests(unittest.TestCase):
    def run_js(self, body):
        script = f"const m = await import({json.dumps(MODULE)});\n" + textwrap.dedent(body)
        completed = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_addresses_become_https(self):
        result = self.run_js(
            """
            console.log(JSON.stringify(["app.slack.com/client", "http://example.com/x", "https://web.whatsapp.com/",
              "  notion.so  ", "ftp://example.com/", "https://user:pw@example.com/", "localhost", "", "not a url"].map(m.normalizeAddress)));
            """
        )
        self.assertEqual(
            ["https://app.slack.com/client", "https://example.com/x", "https://web.whatsapp.com/", "https://notion.so/", None, None, None, None, None],
            result,
        )

    def test_names_prefer_the_manifest_then_the_site_then_the_title(self):
        result = self.run_js(
            """
            console.log(JSON.stringify({
              manifestShort: m.suggestName({ manifest: { short_name: "Slack", name: "Slack: Team communication" }, title: "x" }),
              manifestLong: m.suggestName({ manifest: { short_name: "A very long short name that is not short", name: "Notion" } }),
              meta: m.suggestName({ meta: { "application-name": "Linear" }, title: "Linear – Plan and build" }),
              ogSite: m.suggestName({ meta: { "og:site_name": "Figma" }, title: "Dashboard" }),
              titleHost: m.suggestName({ title: "Home | Slack", host: "app.slack.com" }),
              titleUnread: m.suggestName({ title: "(3) Discord | Friends", host: "discord.com" }),
              titleShortLast: m.suggestName({ title: "Your inbox - Proton Mail", host: "account.proton.me" }),
              titleFirst: m.suggestName({ title: "Todoist: The to-do list to organize work & life", host: "app.todoist.com" }),
              empty: m.suggestName({ title: "   " }),
              hostOnly: m.suggestName({ title: "", host: "nonexistent-zen-app-test.invalid" }),
              hosts: ["app.slack.com", "www.example.co.uk", "my-site.dev", "localhost", ""].map(m.nameFromHost),
            }));
            """
        )
        self.assertEqual("Nonexistent Zen App Test", result["hostOnly"])
        self.assertEqual(["Slack", "Example", "My Site", "Localhost", ""], result["hosts"])
        self.assertEqual("Slack", result["manifestShort"])
        self.assertEqual("Notion", result["manifestLong"])
        self.assertEqual("Linear", result["meta"])
        self.assertEqual("Figma", result["ogSite"])
        self.assertEqual("Slack", result["titleHost"])
        self.assertEqual("Discord", result["titleUnread"])
        self.assertEqual("Proton Mail", result["titleShortLast"])
        self.assertEqual("Todoist", result["titleFirst"])
        self.assertEqual("", result["empty"])

    def test_icons_best_first(self):
        result = self.run_js(
            """
            const list = m.iconCandidates({
              pageUrl: "https://app.example.com/client",
              manifestUrl: "https://app.example.com/static/manifest.json",
              manifest: { icons: [
                { src: "icons/192.png", sizes: "192x192", type: "image/png" },
                { src: "icons/512-mask.png", sizes: "512x512", purpose: "maskable" },
                { src: "/icons/512.png", sizes: "512x512", purpose: "any maskable" },
              ] },
              links: [
                { rel: ["icon"], href: "https://app.example.com/favicon-16.png", sizes: "16x16", type: "image/png" },
                { rel: ["apple-touch-icon"], href: "https://app.example.com/apple.png" },
                { rel: ["icon"], href: "https://app.example.com/logo.svg", type: "image/svg+xml" },
                { rel: ["mask-icon"], href: "https://app.example.com/pinned.svg" },
                { rel: ["icon"], href: "javascript:alert(1)" },
              ],
            });
            console.log(JSON.stringify(list.map(c => [c.src.replace("https://app.example.com", ""), c.source])));
            """
        )
        self.assertEqual(
            [
                ["/logo.svg", "icon"],
                ["/icons/512.png", "manifest"],
                ["/static/icons/192.png", "manifest"],
                ["/apple.png", "apple"],
                ["/static/icons/512-mask.png", "manifest"],
                ["/favicon-16.png", "icon"],
            ],
            result,
        )

    def test_sign_ins_scope_and_colours(self):
        result = self.run_js(
            """
            const html = `<script src="https://accounts.google.com/gsi/client"></script><a href="https://github.com/login/oauth/authorize">`;
            console.log(JSON.stringify({
              detected: m.detectSignIns(html),
              none: m.detectSignIns("<p>hello</p>"),
              hosts: m.signInHosts(["google", "github"], ["*.github.com"]),
              moved: m.scopeSuggestion({ startHost: "slack.com", finalHost: "app.slack.com", baseDomain: "slack.com" }),
              stayed: m.scopeSuggestion({ startHost: "app.slack.com", finalHost: "app.slack.com", baseDomain: "slack.com" }),
              elsewhere: m.scopeSuggestion({ startHost: "go.example.com", finalHost: "www.example.org", baseDomain: "example.com" }),
              colours: [m.hexColor("#ABC"), m.hexColor("#123456ff"), m.hexColor("rgb(18, 52, 86)"), m.hexColor("rgba(0,0,0,0)"), m.hexColor("red")],
              choices: m.colorChoices(["#4a154b", "#4b164c", "rgb(255,255,255)", "nope"]),
              colorful: ["#1ed760", "#ffffff", "#111111", "#888888"].map(m.isColorful),
            }));
            """
        )
        self.assertEqual(["google", "github"], result["detected"])
        self.assertEqual([], result["none"])
        self.assertEqual(["accounts.google.com", "accounts.youtube.com"], result["hosts"])
        self.assertEqual({"wholeDomain": "*.slack.com", "wholeDomainDefault": True, "extraHosts": []}, result["moved"])
        self.assertEqual({"wholeDomain": "*.slack.com", "wholeDomainDefault": False, "extraHosts": []}, result["stayed"])
        self.assertEqual(["www.example.org"], result["elsewhere"]["extraHosts"])
        self.assertEqual(["#aabbcc", "#123456", "#123456", None, None], result["colours"])
        # Accent-worthy colours come first; white stays a choice.
        self.assertEqual(["#4a154b", "#7b6cff", "#ffffff"], result["choices"])
        self.assertEqual([True, False, False, False], result["colorful"])

    def test_host_patterns_match_the_cli(self):
        result = self.run_js(
            """
            console.log(JSON.stringify(["slack.com", "*.slack-edge.com", "localhost", "Slack.com", "*.", "com", "a_b.com",
              "-x.com", "x-.com", "*.*.x.com", "x..com", ""].map(m.isHostPattern)));
            """
        )
        self.assertEqual([True, True, True, False, False, False, False, False, False, False, False, False], result)

    def test_ids_follow_the_name(self):
        result = self.run_js(
            """
            console.log(JSON.stringify(["GitHub", "Proton Mail", "Példa Ügyfélkapu", "  --Linear!! ", "日本語", "", "x".repeat(80) + " y"].map(m.idFromName)));
            """
        )
        self.assertEqual(["github", "proton-mail", "pelda-ugyfelkapu", "linear", "", "", "x" * 64], result)


if __name__ == "__main__":
    unittest.main()
