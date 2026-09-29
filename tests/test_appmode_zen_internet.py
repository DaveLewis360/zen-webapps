import io
import json
from pathlib import Path
import tempfile
import unittest

from zen_app_runtime.appmode import zen_internet
from zen_app_runtime.appmode.errors import AppModeError
from zen_app_runtime.appmode.paths import AppModePaths
from zen_app_runtime.appmode.presets import PRESETS

WEBSITE = {
    "open.spotify.com.css": {"spotify-transparency": "a{}"},
    "+discord.com.css": {"discord-transparency": "b{}", "collapse sidebar": "c{}"},
    "-google.com.css": {"transparency": "d{}"},
    "youtube.com.css": {"transparency": "e{}"},
    "example.com.css": {"transparency": "f{}"},
}
MAPPING = {"example.com.css": ["alternativeto.net"]}


class ZenInternetMatchingTests(unittest.TestCase):
    def test_precedence_mirrors_zen_internet(self):
        self.assertEqual("open.spotify.com.css", zen_internet.best_match(WEBSITE, "open.spotify.com"))
        self.assertEqual("+discord.com.css", zen_internet.best_match(WEBSITE, "discord.com"))
        self.assertEqual("+discord.com.css", zen_internet.best_match(WEBSITE, "ptb.discord.com"))
        self.assertEqual("-google.com.css", zen_internet.best_match(WEBSITE, "google.de"))
        self.assertEqual("youtube.com.css", zen_internet.best_match(WEBSITE, "www.youtube.com"))
        self.assertEqual("youtube.com.css", zen_internet.best_match(WEBSITE, "music.youtube.com"))
        self.assertEqual("example.com.css", zen_internet.best_match(WEBSITE, "alternativeto.net", MAPPING))
        self.assertIsNone(zen_internet.best_match(WEBSITE, "accounts.spotify.com"))

    def test_match_hosts_groups_hosts_per_style(self):
        catalog = {"website": WEBSITE, "mapping": MAPPING}
        self.assertEqual(
            {"+discord.com.css": ["discord.com", "ptb.discord.com"]},
            zen_internet.match_hosts(catalog, ["discord.com", "ptb.discord.com", "discord.gg"]),
        )

    def test_bundled_catalog_is_valid_and_covers_the_presets(self):
        catalog = zen_internet.load_catalog(zen_internet.BUNDLED_CATALOG)
        self.assertGreater(len(catalog["website"]), 500)
        for key in ("spotify", "discord", "messenger", "whatsapp", "telegram", "youtube-music"):
            with self.subTest(preset=key):
                preset = PRESETS[key]
                matches = zen_internet.match_hosts(catalog, list(preset.scope_hosts))
                self.assertTrue(matches, f"no Zen Internet style for {key}")
                self.assertTrue(
                    any(zen_internet.site_has_transparency(catalog["website"][site]) for site in matches)
                )
        meta = json.loads(zen_internet.BUNDLED_META.read_text())
        self.assertEqual(meta["bytes"], zen_internet.BUNDLED_CATALOG.stat().st_size)

    def test_validation_rejects_malformed_catalogs(self):
        for bad in ({}, {"website": []}, {"website": {"a.css": {"f": 1}}}, {"website": {}, "mapping": {"a.css": "b"}}):
            with self.subTest(catalog=bad):
                with self.assertRaises(AppModeError):
                    zen_internet.validate_catalog(bad)

    def test_update_writes_a_validated_catalog_copy(self):
        payload = json.dumps({"website": WEBSITE, "mapping": MAPPING}).encode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as root:
            paths = AppModePaths.under(Path(root))
            result = zen_internet.update_catalog(paths, opener=lambda request, timeout: Response(payload))
            self.assertTrue(result["changed"])
            self.assertEqual(len(WEBSITE), result["sites"])
            self.assertEqual(zen_internet.updated_catalog_path(paths), zen_internet.catalog_path(paths))
            self.assertEqual("updated", zen_internet.catalog_meta(paths)["origin"])
            with self.assertRaises(AppModeError):
                zen_internet.update_catalog(paths, opener=lambda request, timeout: Response(b"{\"nope\": 1}"))


if __name__ == "__main__":
    unittest.main()
