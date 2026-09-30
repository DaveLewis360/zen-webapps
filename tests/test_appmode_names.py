"""How an app names itself to the desktop: brand bundle and MPRIS model.

addon/modules/Brand.sys.mjs and MprisModel.sys.mjs are plain ES modules; the
pure parts run under node. Skipped when node is not installed.
"""

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

MODULES = Path(__file__).resolve().parents[1] / "src" / "zen_app_runtime" / "appmode" / "addon" / "modules"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class NodeModuleTestCase(unittest.TestCase):
    module = ""

    def run_js(self, body):
        script = f"const m = await import({json.dumps((MODULES / self.module).as_uri())});\n" + textwrap.dedent(body)
        completed = subprocess.run(
            [NODE, "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)


class BrandTests(NodeModuleTestCase):
    module = "Brand.sys.mjs"

    def test_every_product_name_becomes_the_app_name(self):
        text = self.run_js(
            """
            const entries = [
              ["brandShorterName", "Zen"], ["brandShortName", "Zen"], ["brandFullName", "Zen Browser"],
              ["vendorShortName", "Zen OSS Team"], ["bad key", "x"],
            ];
            console.log(JSON.stringify(m.renderBrandProperties(entries, "Spotify")));
            """
        )
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("#"))
        self.assertEqual(
            ["brandShorterName=Spotify", "brandShortName=Spotify", "brandFullName=Spotify", "vendorShortName=Zen OSS Team"],
            lines[1:],
        )

    def test_names_stay_on_one_line_and_missing_keys_are_added(self):
        text = self.run_js(
            """
            console.log(JSON.stringify(m.renderBrandProperties([], "Line\\nBreak \\\\ ok")));
            """
        )
        self.assertIn("brandShortName=Line Break \\\\ ok\n", text)
        self.assertIn("brandShorterName=", text)
        self.assertIn("brandFullName=", text)
        self.assertNotIn("brandProductName", text)


try:
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
except (ImportError, ValueError):  # pragma: no cover - depends on the system
    GLib = None

TRICKY = "Ünnepi 'dal' \\ \"idéző\"\nújsor\u0007 \U0001F3B5"


class MprisModelTests(NodeModuleTestCase):
    module = "MprisModel.sys.mjs"

    def player_values(self):
        """Every property of a sample player, plus the signal bodies, as [type, text]."""
        return self.run_js(
            """
            const player = {
              identity: "Zene próba", desktopEntry: "zen-app-zmp", status: "Playing", rate: 1.5,
              position: m.positionState({ duration: 200, playbackRate: 1.5, position: 10 }, 1000),
              caps: m.capabilities(["play", "pause", "playpause", "seekto", "nexttrack"], { seekable: true }),
              metadata: { trackId: m.TRACK_PREFIX + "7", title: %s, artist: "Előadó, Másik", album: "Album",
                          lengthUs: 200000000, artUrl: "file:///tmp/art%%20x.png" },
            };
            const out = {};
            for (const [iface, names] of [
              [m.ROOT_INTERFACE, ["CanQuit", "CanRaise", "HasTrackList", "Identity", "DesktopEntry", "SupportedUriSchemes", "SupportedMimeTypes"]],
              [m.PLAYER_INTERFACE, ["PlaybackStatus", "Rate", "Metadata", "Volume", "Position", "MinimumRate", "MaximumRate",
                                    "CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", "CanSeek", "CanControl"]],
            ]) {
              for (const name of names) out[name] = m.propertyValue(iface, name, player, 3000);
            }
            out.unknown = m.propertyValue(m.PLAYER_INTERFACE, "Shuffle", player, 3000);
            const values = m.signalledValues(player);
            out.changed = ["(sa{sv}as)", m.propertiesChangedText(["PlaybackStatus", "Metadata"], values)];
            out.seeked = ["(x)", m.seekedText(12345678)];
            out.empty = ["a{sv}", m.metadataText({})];
            console.log(JSON.stringify(out));
            """ % json.dumps(TRICKY)
        )

    def test_properties_describe_the_player(self):
        values = self.player_values()
        self.assertEqual(["s", "'Zene próba'"], values["Identity"])
        self.assertEqual(["s", "'zen-app-zmp'"], values["DesktopEntry"])
        self.assertEqual(["b", "false"], values["CanQuit"])
        self.assertEqual(["s", "'Playing'"], values["PlaybackStatus"])
        # 10 s + 2 s at rate 1.5 = 13 s.
        self.assertEqual(["x", "13000000"], values["Position"])
        self.assertEqual(["d", "1.5"], values["Rate"])
        self.assertEqual(["d", "1.0"], values["MinimumRate"])
        self.assertEqual(["d", "1.5"], values["MaximumRate"])
        self.assertEqual(["b", "true"], values["CanGoNext"])
        self.assertEqual(["b", "false"], values["CanGoPrevious"])
        self.assertEqual(["b", "true"], values["CanSeek"])
        self.assertIsNone(values["unknown"])

    @unittest.skipUnless(GLib, "PyGObject is not installed")
    def test_glib_parses_every_value_and_strings_survive(self):
        values = self.player_values()
        parsed = {}
        for name, value in values.items():
            if value is None:
                continue
            with self.subTest(property=name):
                variant = GLib.Variant.parse(GLib.VariantType.new(value[0]), value[1], None, None)
                self.assertEqual(value[0], variant.get_type_string())
                parsed[name] = variant.unpack()
        metadata = parsed["Metadata"]
        # Control characters become spaces; quotes, backslashes and emoji stay.
        self.assertEqual("Ünnepi 'dal' \\ \"idéző\" újsor  \U0001F3B5", metadata["xesam:title"])
        self.assertEqual(["Előadó, Másik"], metadata["xesam:artist"])
        self.assertEqual("/org/mpris/MediaPlayer2/Track/7", metadata["mpris:trackid"])
        self.assertEqual(200000000, metadata["mpris:length"])
        self.assertEqual("file:///tmp/art%20x.png", metadata["mpris:artUrl"])
        self.assertEqual({"mpris:trackid": "/org/mpris/MediaPlayer2/TrackList/NoTrack"}, parsed["empty"])
        interface, changed, invalidated = parsed["changed"]
        self.assertEqual("org.mpris.MediaPlayer2.Player", interface)
        self.assertEqual({"PlaybackStatus", "Metadata"}, set(changed))
        self.assertEqual([], invalidated)
        self.assertEqual((12345678,), parsed["seeked"])
        self.assertEqual([], parsed["SupportedUriSchemes"])

    def test_bus_names_follow_the_mpris_instance_convention(self):
        names = self.run_js(
            """
            console.log(JSON.stringify([m.busName("spotify", 4242), m.busName("app-slack-com", 7), m.busName("1password", 9), m.busName("x", 0)]));
            """
        )
        self.assertEqual(
            [
                "org.mpris.MediaPlayer2.spotify.instance4242",
                "org.mpris.MediaPlayer2.app_slack_com.instance7",
                "org.mpris.MediaPlayer2._1password.instance9",
                "org.mpris.MediaPlayer2.x",
            ],
            names,
        )

    def test_status_capabilities_and_positions(self):
        result = self.run_js(
            """
            const paused = m.positionState({ duration: 100, playbackRate: 0, position: 42 }, 1000);
            const playing = m.positionState({ duration: 100, playbackRate: 1, position: 42 }, 1000);
            console.log(JSON.stringify({
              status: [
                m.playbackStatus({ active: false, playing: true, state: "playing" }),
                m.playbackStatus({ active: true, playing: true, state: "playing" }),
                m.playbackStatus({ active: true, playing: false, state: "paused" }),
                m.playbackStatus({ active: true, playing: true, state: "none" }),
                m.playbackStatus({ active: true, playing: false, state: "none" }),
              ],
              pausedStays: m.positionSeconds(paused, 60000, false),
              playingMoves: m.positionSeconds(playing, 11000, true),
              clamped: m.positionSeconds(playing, 999000, true),
              liveNotSeekable: m.capabilities(["play", "seekto"], { seekable: false }).canSeek,
              defaults: m.capabilities(["focus", "play", "pause", "playpause", "stop", "seekto"]),
              jumpOnSeek: m.isJump(playing, m.positionState({ duration: 100, playbackRate: 1, position: 80 }, 2000), true),
              noJumpOnPause: m.isJump(playing, m.positionState({ duration: 100, playbackRate: 0, position: 43 }, 2000), true),
              noJumpOnResume: m.isJump(paused, m.positionState({ duration: 100, playbackRate: 1, position: 42 }, 9000), true),
            }));
            """
        )
        self.assertEqual(["Stopped", "Playing", "Paused", "Playing", "Paused"], result["status"])
        self.assertEqual(42, result["pausedStays"])
        self.assertEqual(52, result["playingMoves"])
        self.assertEqual(100, result["clamped"])
        self.assertFalse(result["liveNotSeekable"])
        defaults = result["defaults"]
        self.assertTrue(defaults["canPlay"] and defaults["canPause"] and defaults["canSeek"] and defaults["canSeekTo"])
        self.assertFalse(defaults["canGoNext"] or defaults["canGoPrevious"])
        self.assertTrue(result["jumpOnSeek"])
        self.assertFalse(result["noJumpOnPause"])
        self.assertFalse(result["noJumpOnResume"])

    def test_method_calls_become_actions(self):
        result = self.run_js(
            """
            const base = {
              status: "Playing",
              position: m.positionState({ duration: 100, playbackRate: 1, position: 30 }, 1000),
              caps: m.capabilities(["play", "pause", "playpause", "seekto", "nexttrack"]),
              metadata: { trackId: m.TRACK_PREFIX + "3", lengthUs: 100000000 },
            };
            const act = (method, args = {}, player = base) => m.playerAction(method, args, player, 1000);
            console.log(JSON.stringify({
              playPause: act("PlayPause"),
              playPausePaused: act("PlayPause", {}, { ...base, status: "Paused" }),
              previousUnsupported: act("Previous"),
              next: act("Next"),
              seekBack: act("Seek", { offset: -40000000 }),
              seekForward: act("Seek", { offset: 5000000 }),
              seekPastEnd: act("Seek", { offset: 90000000 }),
              setPosition: act("SetPosition", { trackId: m.TRACK_PREFIX + "3", position: 12000000 }),
              stale: act("SetPosition", { trackId: m.TRACK_PREFIX + "2", position: 12000000 }),
              setPastEnd: act("SetPosition", { trackId: m.TRACK_PREFIX + "3", position: 200000000 }),
              stopStopped: act("Stop", {}, { ...base, status: "Stopped" }),
              unknown: act("Shuffle"),
            }));
            """
        )
        self.assertEqual({"type": "pause"}, result["playPause"])
        self.assertEqual({"type": "play"}, result["playPausePaused"])
        self.assertIsNone(result["previousUnsupported"])
        self.assertEqual({"type": "next"}, result["next"])
        self.assertEqual({"type": "seek", "seconds": 0}, result["seekBack"])
        self.assertEqual({"type": "seek", "seconds": 35}, result["seekForward"])
        self.assertEqual({"type": "next"}, result["seekPastEnd"])
        self.assertEqual({"type": "seek", "seconds": 12}, result["setPosition"])
        self.assertIsNone(result["stale"])
        self.assertIsNone(result["setPastEnd"])
        self.assertIsNone(result["stopStopped"])
        self.assertIsNone(result["unknown"])

    def test_artwork_choice_and_image_types(self):
        result = self.run_js(
            """
            const art = (list, target) => m.pickArtwork(list, target);
            console.log(JSON.stringify({
              smallestBigEnough: art([
                { src: "https://i.example/64.jpg", sizes: "64x64" },
                { src: "https://i.example/640.jpg", sizes: "640x640" },
                { src: "https://i.example/300.jpg", sizes: "300x300" },
              ]),
              biggestOtherwise: art([{ src: "https://i.example/64.jpg", sizes: "64x64" }, { src: "https://i.example/128.jpg", sizes: "96x96 128x128" }]),
              scalable: art([{ src: "https://i.example/a.svg", sizes: "any" }, { src: "https://i.example/64.png", sizes: "64x64" }]),
              onlyDownloadable: art([{ src: "blob:https://x/1", sizes: "512x512" }, { src: "http://i.example/p.png", sizes: "512x512" }, { src: "data:image/png;base64,AA==" }]),
              none: art([]),
              png: m.imageExtension("application/octet-stream", [0x89, 0x50, 0x4e, 0x47]),
              jpeg: m.imageExtension("", [0xff, 0xd8, 0xff, 0xe0]),
              webp: m.imageExtension("image/webp", [0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50]),
              svg: m.imageExtension("image/svg+xml; charset=utf-8", [0x3c, 0x73]),
              html: m.imageExtension("image/png", [0x3c, 0x68, 0x74, 0x6d]),
            }));
            """
        )
        self.assertEqual("https://i.example/300.jpg", result["smallestBigEnough"])
        self.assertEqual("https://i.example/128.jpg", result["biggestOtherwise"])
        self.assertEqual("https://i.example/a.svg", result["scalable"])
        self.assertEqual("data:image/png;base64,AA==", result["onlyDownloadable"])
        self.assertEqual("", result["none"])
        self.assertEqual(["png", "jpg", "webp", "svg", ""], [result[k] for k in ("png", "jpeg", "webp", "svg", "html")])


if __name__ == "__main__":
    unittest.main()
