import unittest

from zen_app_runtime.appmode.definition import (
    AppSettings,
    definition_from_dict,
    definition_from_preset,
    definition_from_url,
    definition_to_dict,
    host_matches,
    settings_from_dict,
    with_settings,
)
from zen_app_runtime.appmode.errors import AppModeError
from zen_app_runtime.appmode.presets import PRESETS


class AppModeDefinitionTests(unittest.TestCase):
    def test_every_preset_builds_a_valid_round_tripping_definition(self):
        for key, preset in PRESETS.items():
            with self.subTest(preset=key):
                definition = definition_from_preset(preset)
                self.assertEqual(key, definition.id)
                self.assertTrue(definition.needs_onboarding)
                data = definition_to_dict(definition)
                self.assertEqual(definition, definition_from_dict(data))
                self.assertIn(definition.start_host, [h.lstrip("*.") for h in definition.scope_hosts] or [""])

    def test_messenger_uses_facebook_messages_and_path_scope(self):
        definition = definition_from_preset(PRESETS["messenger"])
        self.assertEqual("https://www.facebook.com/messages/", definition.start_url)
        self.assertIn("/messages", definition.scope_paths)
        self.assertEqual(("facebook-messages-app", "facebook-messages-glass"), definition.extras)

    def test_url_definition_derives_id_name_and_appearance(self):
        opaque = definition_from_url("https://web.whatsapp.com/")
        self.assertEqual(("web-whatsapp-com", "Whatsapp", "opaque"), (opaque.id, opaque.name, opaque.settings.appearance))
        self.assertEqual("Slack", definition_from_url("https://app.slack.com/client").name)
        self.assertEqual("Mail", definition_from_url("https://mail.google.com/").name)
        translucent = definition_from_url("https://www.github.com/", has_translucent_style=True)
        self.assertEqual(("github-com", "Github", "transparent"), (translucent.id, translucent.name, translucent.settings.appearance))
        self.assertEqual(("www.github.com",), translucent.scope_hosts)

    def test_settings_validation_rejects_bad_values(self):
        for bad in (
            {"appearance": "glassy"},
            {"glass": "kitty"},
            {"tint": 95},
            {"tint": True},
            {"notifications": "yes"},
            {"unknown": 1},
            {"disabledFeatures": ["no-separator"]},
            {"disabledFeatures": "discord.css|x"},
            {"customCss": "yes"},
            {"adblock": 1},
            {"webcompat": "on"},
        ):
            with self.subTest(settings=bad):
                with self.assertRaises(AppModeError):
                    settings_from_dict(bad)

    def test_custom_css_is_on_by_default_and_round_trips(self):
        self.assertTrue(settings_from_dict({}).custom_css)
        definition = with_settings(definition_from_preset(PRESETS["messenger"]), custom_css=False)
        data = definition_to_dict(definition)
        self.assertFalse(data["settings"]["customCss"])
        self.assertFalse(definition_from_dict(data).settings.custom_css)

    def test_ad_blocking_and_compat_fixes_are_on_by_default_and_round_trip(self):
        # Definitions written before these settings existed get them switched on.
        defaults = settings_from_dict({"appearance": "opaque"})
        self.assertTrue(defaults.adblock)
        self.assertTrue(defaults.webcompat)
        definition = with_settings(definition_from_preset(PRESETS["spotify"]), adblock=False, webcompat=False)
        data = definition_to_dict(definition)
        self.assertEqual((False, False), (data["settings"]["adblock"], data["settings"]["webcompat"]))
        restored = definition_from_dict(data).settings
        self.assertEqual((False, False), (restored.adblock, restored.webcompat))

    def test_only_messenger_asks_for_the_encrypted_calls_fix(self):
        from zen_app_runtime.appmode.render import COMPAT_FIXES

        for key, preset in PRESETS.items():
            with self.subTest(preset=key):
                self.assertTrue(set(preset.compat) <= set(COMPAT_FIXES))
        self.assertEqual(("encoded-streams",), PRESETS["messenger"].compat)
        self.assertEqual((), PRESETS["discord"].compat)

    def test_settings_round_trip_including_features(self):
        settings = settings_from_dict(
            {"glass": "vesktop", "disabledFeatures": ["+discord.com.css|collapse sidebar", "+discord.com.css|collapse sidebar"]}
        )
        self.assertEqual("vesktop", settings.glass)
        self.assertEqual(("+discord.com.css|collapse sidebar",), settings.disabled_features)
        self.assertEqual(AppSettings().appearance, settings.appearance)

    def test_definition_rejects_start_url_outside_scope_and_unknown_fields(self):
        data = definition_to_dict(definition_from_preset(PRESETS["discord"]))
        outside = dict(data, startUrl="https://example.com/")
        with self.assertRaisesRegex(AppModeError, "inside scope"):
            definition_from_dict(outside)
        with self.assertRaisesRegex(AppModeError, "unknown fields"):
            definition_from_dict(dict(data, themes=["discord"]))
        with self.assertRaises(AppModeError):
            definition_from_dict(dict(data, scope={"hosts": ["Discord.com"]}))

    def test_with_settings_and_onboarding_state(self):
        definition = with_settings(definition_from_preset(PRESETS["spotify"]), tint=12)
        self.assertEqual(12, definition.settings.tint)
        data = definition_to_dict(definition)
        data["onboarding"] = {"completed": True, "version": 1}
        self.assertFalse(definition_from_dict(data).needs_onboarding)

    def test_host_patterns(self):
        self.assertTrue(host_matches("discord.com", "discord.com"))
        self.assertFalse(host_matches("cdn.discord.com", "discord.com"))
        self.assertTrue(host_matches("cdn.discord.com", "*.discord.com"))
        self.assertTrue(host_matches("discord.com", "*.discord.com"))
        self.assertFalse(host_matches("evildiscord.com", "*.discord.com"))


if __name__ == "__main__":
    unittest.main()
