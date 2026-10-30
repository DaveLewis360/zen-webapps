"""The tray icon and unread badge model (addon/modules/TrayModel.sys.mjs), run under node.

The GVariant texts are also parsed with GLib when PyGObject is installed,
so what the add-on hands to GDBus is checked against GLib's own parser.
"""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

MODULES = Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules"
NODE = shutil.which("node")

try:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
except (ImportError, ValueError):  # pragma: no cover - depends on the system
    GLib = None

LABELS = {
    "show": "Show {app}",
    "hide": "Hide {app}",
    "settings": "Settings…",
    "quit": "Quit {app}",
    "unread": "{count} unread",
    "unreadDot": "Unread messages",
}


@unittest.skipUnless(NODE, "node is not installed")
class TrayModelTests(unittest.TestCase):
    def run_js(self, body):
        script = (
            f"const m = await import({json.dumps((MODULES / 'TrayModel.sys.mjs').as_uri())});\n"
            f"const LABELS = {json.dumps(LABELS)};\n" + textwrap.dedent(body)
        )
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_unread_counts_come_from_the_page_title(self):
        result = self.run_js(
            """
            const titles = ["(3) Discord | #general | Server", "WhatsApp (12)", "(9+) Inbox", "• Discord | Friends",
              "Discord | Friends", "(0) Messenger", "Song • Artist", "(I Can't Get No) Satisfaction", "[5] Chat",
              "", null, "(1234567) Too many", "Messenger (2) friends"];
            console.log(JSON.stringify(titles.map(m.unreadFromTitle)));
            """
        )
        none = {"count": 0, "more": False, "dot": False}
        self.assertEqual(
            [
                {"count": 3, "more": False, "dot": False},
                {"count": 12, "more": False, "dot": False},
                {"count": 9, "more": True, "dot": False},
                {"count": 0, "more": False, "dot": True},
                none,
                none,
                none,
                none,
                {"count": 5, "more": False, "dot": False},
                none,
                none,
                none,
                none,
            ],
            result,
        )

    def test_badge_labels_and_geometry(self):
        result = self.run_js(
            """
            const labels = [{ count: 0 }, { count: 0, dot: true }, { count: 3 }, { count: 9, more: true }, { count: 100 }, null]
              .map(m.badgeLabel);
            const pill = m.badgeGeometry(32, "12", 13.4);
            const dot = m.badgeGeometry(64, "•");
            const wide = m.badgeGeometry(32, "99+", 400);
            console.log(JSON.stringify({ labels, pill, dot, wide, none: m.badgeGeometry(32, "") }));
            """
        )
        self.assertEqual(["", "•", "3", "9+", "99+", ""], result["labels"])
        pill = result["pill"]
        self.assertFalse(pill["dot"])
        self.assertEqual((32, 0), (pill["x"] + pill["width"], pill["y"]))
        self.assertGreaterEqual(pill["width"], pill["height"])
        self.assertEqual({"dot": True, "x": 51, "y": 13, "radius": 13, "height": 26, "width": 26}, result["dot"])
        self.assertEqual(32, result["wide"]["width"])
        self.assertIsNone(result["none"])

    def test_pixels_become_argb_in_network_byte_order(self):
        result = self.run_js(
            """
            const rgba = new Uint8ClampedArray([10, 20, 30, 255, 1, 2, 3, 0, 9]);
            console.log(JSON.stringify(Array.from(m.argbFromRgba(rgba))));
            """
        )
        self.assertEqual([255, 10, 20, 30, 0, 1, 2, 3], result)

    def item_state(self, **changes):
        state = {
            "id": "zen-app-discord",
            "title": "Discord",
            "category": "Communications",
            "labels": LABELS,
            "badge": True,
            "unread": {"count": 0, "more": False, "dot": False},
            "visible": True,
            "revision": 1,
        }
        state.update(changes)
        return state

    def values(self, state):
        return self.run_js(
            """
            const state = %s;
            const names = ["Category", "Id", "Title", "Status", "WindowId", "IconThemePath", "IconName", "IconPixmap",
              "OverlayIconName", "AttentionIconName", "AttentionMovieName", "ToolTip", "ItemIsMenu", "Menu"];
            const item = Object.fromEntries(names.map(name => [name, m.itemProperty(name, state)]));
            const menu = Object.fromEntries(["Version", "TextDirection", "Status", "IconThemePath"].map(name => [name, m.menuProperty(name)]));
            console.log(JSON.stringify({
              item, menu,
              unknown: m.itemProperty("Nope", state),
              layout: ["(u(ia{sv}av))", m.layoutReply(state, 0, -1, [])],
              layoutShallow: ["(u(ia{sv}av))", m.layoutReply(state, 0, 0, [])],
              layoutItem: ["(u(ia{sv}av))", m.layoutReply(state, 6, -1, ["label"])],
              layoutMissing: m.layoutReply(state, 77),
              group: ["(a(ia{sv}))", m.groupPropertiesReply(state, [], [])],
              groupSome: ["(a(ia{sv}))", m.groupPropertiesReply(state, [4, 6], ["label"])],
              groupNone: ["(a(ia{sv}))", m.groupPropertiesReply(state, [99], [])],
              property: ["(v)", m.propertyReply(state, 6, "label")],
              separator: ["(v)", m.propertyReply(state, 3, "type")],
              signals: m.itemSignals(null, state),
              launcher: ["(sa{sv})", m.launcherEntryText(state.id, state.unread, state.badge)],
              launcherOff: ["(sa{sv})", m.launcherEntryText(state.id, state.unread, false)],
              launcherPath: m.launcherEntryPath(state.id),
              items: m.menuItems(state),
            }));
            """
            % json.dumps(state)
        )

    def test_item_properties_describe_the_app(self):
        values = self.values(self.item_state(title="Discord 'Beta' \\ app"))
        item = values["item"]
        self.assertEqual(["s", "'Communications'"], item["Category"])
        self.assertEqual(["s", "'zen-app-discord'"], item["Id"])
        self.assertEqual(["s", "'Discord \\'Beta\\' \\\\ app'"], item["Title"])
        self.assertEqual(["s", "'Active'"], item["Status"])
        self.assertEqual(["a(iiay)", None], item["IconPixmap"])
        self.assertEqual(["s", "''"], item["IconName"])
        self.assertEqual(["b", "false"], item["ItemIsMenu"])
        self.assertEqual(["o", "'/MenuBar'"], item["Menu"])
        self.assertIsNone(values["unknown"])
        self.assertEqual(["u", "uint32 3"], values["menu"]["Version"])
        self.assertEqual("/com/canonical/unity/launcherentry/zen_app_discord", values["launcherPath"])

    def test_unread_turns_into_status_tooltip_and_launcher_count(self):
        counted = self.values(self.item_state(unread={"count": 4, "more": False, "dot": False}))
        self.assertEqual(["s", "'NeedsAttention'"], counted["item"]["Status"])
        self.assertIn("'4 unread'", counted["item"]["ToolTip"][1])
        self.assertIn("{'count': <int64 4>, 'count-visible': <true>}", counted["launcher"][1])
        self.assertIn("'application://zen-app-discord.desktop'", counted["launcher"][1])
        self.assertIn("'count-visible': <false>", counted["launcherOff"][1])
        dot = self.values(self.item_state(unread={"count": 0, "more": False, "dot": True}))
        self.assertEqual(["s", "'Active'"], dot["item"]["Status"])
        self.assertIn("'Unread messages'", dot["item"]["ToolTip"][1])
        off = self.values(self.item_state(badge=False, unread={"count": 4, "more": False, "dot": False}))
        self.assertEqual(["s", "'Active'"], off["item"]["Status"])
        self.assertIn("'Discord', '')", off["item"]["ToolTip"][1])

    def test_menu_shows_or_hides_and_quits(self):
        shown = self.values(self.item_state(visible=True))["items"]
        hidden = self.values(self.item_state(visible=False))["items"]
        self.assertEqual("Hide Discord", shown[0]["label"])
        self.assertEqual("Show Discord", hidden[0]["label"])
        self.assertEqual(["separator", "separator"], [item.get("type") for item in shown if item.get("type")])
        self.assertEqual("Quit Discord", shown[-1]["label"])
        result = self.run_js(
            """
            console.log(JSON.stringify({
              clicks: [1, 2, 4, 6, 3, 99].map(id => m.menuAction(id, "clicked")),
              hovered: m.menuAction(1, "hovered"),
              methods: ["Activate", "SecondaryActivate", "ContextMenu", "Scroll"].map(m.itemAction),
            }));
            """
        )
        self.assertEqual(
            [{"type": "show"}, {"type": "hide"}, {"type": "settings"}, {"type": "quit"}, None, None], result["clicks"]
        )
        self.assertIsNone(result["hovered"])
        self.assertEqual([{"type": "show"}, {"type": "toggle"}, None, None], result["methods"])

    def test_signals_follow_what_changed(self):
        result = self.run_js(
            """
            const base = { id: "zen-app-x", title: "X", labels: LABELS, badge: true, unread: { count: 0 }, visible: true };
            console.log(JSON.stringify({
              first: m.itemSignals(null, base),
              same: m.itemSignals(base, { ...base }),
              unread: m.itemSignals(base, { ...base, unread: { count: 2 } }),
              renamed: m.itemSignals(base, { ...base, title: "Y" }),
              names: [m.itemBusName(4242), m.itemBusName(-1, 0)],
              categories: [m.itemCategory(["Network", "InstantMessaging"]), m.itemCategory(["AudioVideo"]), m.itemCategory(null)],
            }));
            """
        )
        self.assertEqual(["NewTitle", "NewToolTip", "NewStatus"], [signal[0] for signal in result["first"]])
        self.assertEqual([], result["same"])
        self.assertEqual([["NewToolTip"], ["NewStatus", "(s)", "('NeedsAttention',)"]], result["unread"])
        self.assertEqual(["NewTitle", "NewToolTip"], [signal[0] for signal in result["renamed"]])
        self.assertEqual(["org.kde.StatusNotifierItem-4242-1", "org.kde.StatusNotifierItem-0-1"], result["names"])
        self.assertEqual(["Communications", "ApplicationStatus", "ApplicationStatus"], result["categories"])

    @unittest.skipUnless(GLib, "PyGObject is not installed")
    def test_glib_parses_every_reply_and_signal(self):
        values = self.values(self.item_state(title="Ünnepi 'dal' \\ app", unread={"count": 120, "more": False, "dot": False}))
        texts = [value for value in values["item"].values() if value and value[1] is not None]
        texts += [value for value in values["menu"].values()]
        texts += [values[key] for key in ("layout", "layoutShallow", "layoutItem", "group", "groupSome", "groupNone", "property", "separator", "launcher", "launcherOff")]
        texts += [[signal[1], signal[2]] for signal in values["signals"] if len(signal) > 1]
        for type_string, text in texts:
            with self.subTest(text=text[:60]):
                variant = GLib.Variant.parse(GLib.VariantType.new(type_string), text, None, None)
                self.assertEqual(type_string, variant.get_type_string())
        revision, root = GLib.Variant.parse(GLib.VariantType.new(values["layout"][0]), values["layout"][1], None, None).unpack()
        self.assertEqual(1, revision)
        self.assertEqual(0, root[0])
        children = root[2]
        self.assertEqual([2, 3, 4, 5, 6], [child[0] for child in children])
        self.assertEqual("Hide Ünnepi 'dal' \\ app", children[0][1]["label"])
        self.assertEqual("separator", children[1][1]["type"])
        shallow = GLib.Variant.parse(GLib.VariantType.new("(u(ia{sv}av))"), values["layoutShallow"][1], None, None).unpack()
        self.assertEqual([], shallow[1][2])
        uri, properties = GLib.Variant.parse(GLib.VariantType.new("(sa{sv})"), values["launcher"][1], None, None).unpack()
        self.assertEqual(("application://zen-app-discord.desktop", 120, True), (uri, properties["count"], properties["count-visible"]))

    def test_introspection_declares_every_property_the_model_answers(self):
        result = self.run_js(
            """
            console.log(JSON.stringify({ xml: m.INTROSPECTION_XML, pixmaps: [...m.PIXMAP_PROPERTIES] }));
            """
        )
        xml = result["xml"]
        for name in ("Category", "Id", "Title", "Status", "IconPixmap", "AttentionIconPixmap", "ToolTip", "Menu", "ItemIsMenu"):
            self.assertIn(f'<property name="{name}"', xml)
        for method in ("Activate", "SecondaryActivate", "GetLayout", "GetGroupProperties", "Event", "EventGroup", "AboutToShow"):
            self.assertIn(f'<method name="{method}"', xml)
        if GLib is not None:
            from gi.repository import Gio  # noqa: PLC0415 - optional dependency

            node = Gio.DBusNodeInfo.new_for_xml(xml)
            self.assertEqual(
                ["org.kde.StatusNotifierItem", "com.canonical.dbusmenu"], [interface.name for interface in node.interfaces]
            )


class TraySettingsTests(unittest.TestCase):
    def test_settings_default_and_round_trip(self):
        from zen_app_runtime.appmode.definition import (
            definition_from_dict,
            definition_from_preset,
            definition_to_dict,
            settings_from_dict,
            with_settings,
        )
        from zen_app_runtime.appmode.errors import AppModeError
        from zen_app_runtime.appmode.presets import PRESETS

        # Definitions written before these settings existed: tray and badge on, background off.
        old = settings_from_dict({"appearance": "opaque"})
        self.assertEqual((True, False, True), (old.tray, old.background, old.unread_badge))
        definition = with_settings(definition_from_preset(PRESETS["spotify"]), tray=False, background=True, unread_badge=False)
        data = definition_to_dict(definition)
        self.assertEqual((False, True, False), (data["settings"]["tray"], data["settings"]["background"], data["settings"]["unreadBadge"]))
        restored = definition_from_dict(data).settings
        self.assertEqual((False, True, False), (restored.tray, restored.background, restored.unread_badge))
        for bad in ({"tray": "yes"}, {"background": 1}, {"unreadBadge": None}):
            with self.subTest(settings=bad):
                with self.assertRaises(AppModeError):
                    settings_from_dict(bad)

    def test_chat_presets_keep_running_in_the_background(self):
        from zen_app_runtime.appmode.definition import definition_from_preset, definition_from_url
        from zen_app_runtime.appmode.presets import PRESETS

        background = {key for key, preset in PRESETS.items() if definition_from_preset(preset).settings.background}
        self.assertEqual({"discord", "messenger", "whatsapp", "telegram"}, background)
        self.assertFalse(definition_from_url("https://example.com/").settings.background)

    def test_web_apps_itself_has_no_tray(self):
        from zen_app_runtime.appmode.manager import manager_definition

        settings = manager_definition().settings
        self.assertEqual((False, False, False), (settings.tray, settings.background, settings.unread_badge))

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_add_on_accepts_exactly_the_python_booleans(self):
        from zen_app_runtime.appmode.definition import AppSettings, _SETTINGS_FIELDS

        defaults = AppSettings()
        python_booleans = sorted(key for key, attribute in _SETTINGS_FIELDS.items() if isinstance(getattr(defaults, attribute), bool))
        script = (
            f"const C = await import({json.dumps((MODULES / 'Config.sys.mjs').as_uri())});\n"
            "const next = C.sanitizeSettings({ tray: false, background: true, unreadBadge: 'no' }, { tray: true, background: false, unreadBadge: true });\n"
            "console.log(JSON.stringify({ booleans: [...C.BOOLEAN_SETTINGS].sort(), next }));\n"
        )
        completed = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(python_booleans, result["booleans"])
        self.assertEqual({"tray": False, "background": True, "unreadBadge": True}, result["next"])


if __name__ == "__main__":
    unittest.main()
