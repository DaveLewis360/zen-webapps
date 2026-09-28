import unittest

from zen_app_runtime.manifest import (
    ManifestError,
    create_manifest,
    manifest_from_dict,
    manifest_to_dict,
)


class ManifestTests(unittest.TestCase):
    def test_create_opaque_manifest_has_safe_defaults_and_metadata_only_extensions(self):
        manifest = create_manifest(
            app_id="mail-example",
            name="Example Mail",
            start_url="https://mail.example.com/inbox",
            mode="Opaque",
            include_stylus=True,
        )

        data = manifest_to_dict(manifest)
        self.assertEqual(1, data["schemaVersion"])
        self.assertEqual("Opaque", data["configuration"]["mode"])
        self.assertEqual("external", data["linkPolicy"]["defaultAction"])
        self.assertEqual("same-origin", data["linkPolicy"]["internalNavigation"])
        self.assertEqual(["zen-internet", "stylus"], [item["id"] for item in data["extensions"]])
        self.assertTrue(all("downloadUrl" not in item and "path" not in item for item in data["extensions"]))

    def test_round_trip_preserves_manifest(self):
        original = create_manifest("docs", "Docs", "https://docs.example.com", "Transparent", False)
        self.assertEqual(original, manifest_from_dict(manifest_to_dict(original)))

    def test_rejects_unknown_schema_version(self):
        data = manifest_to_dict(create_manifest("docs", "Docs", "https://docs.example.com", "Opaque", False))
        data["schemaVersion"] = 2
        with self.assertRaisesRegex(ManifestError, "schemaVersion"):
            manifest_from_dict(data)

    def test_rejects_unsafe_id_url_mode_and_unknown_fields(self):
        invalid_cases = [
            {"app_id": "../escape"},
            {"app_id": "Upper Case"},
            {"start_url": "javascript:alert(1)"},
            {"start_url": "http://example.com"},
            {"start_url": "https://exa mple.com"},
            {"start_url": "https://example.com\\unexpected"},
            {"mode": "Invisible"},
            {"mode": []},
        ]
        for changes in invalid_cases:
            args = {
                "app_id": "safe-id",
                "name": "Safe",
                "start_url": "https://example.com",
                "mode": "Opaque",
                "include_stylus": False,
            }
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ManifestError):
                create_manifest(**args)

        data = manifest_to_dict(create_manifest("safe", "Safe", "https://example.com", "Opaque", False))
        data["surprise"] = True
        with self.assertRaisesRegex(ManifestError, "unknown"):
            manifest_from_dict(data)

    def test_rejects_extension_code_locations_and_non_boolean_scalars(self):
        data = manifest_to_dict(create_manifest("safe", "Safe", "https://example.com", "Opaque", True))
        data["extensions"][0]["downloadUrl"] = "https://extensions.example/code.xpi"
        with self.assertRaises(ManifestError):
            manifest_from_dict(data)

        data = manifest_to_dict(create_manifest("safe", "Safe", "https://example.com", "Opaque", True))
        data["extensions"][0]["codeIncluded"] = 0
        with self.assertRaisesRegex(ManifestError, "codeIncluded"):
            manifest_from_dict(data)

        data = manifest_to_dict(create_manifest("safe", "Safe", "https://example.com", "Opaque", False))
        data["linkPolicy"]["allowUserOverride"] = 0
        with self.assertRaisesRegex(ManifestError, "allowUserOverride"):
            manifest_from_dict(data)


if __name__ == "__main__":
    unittest.main()
