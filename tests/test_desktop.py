import unittest

from zen_app_runtime.desktop import DesktopError, build_desktop_preview, render_desktop_preview, render_exec
from zen_app_runtime.launch_plan import build_launch_plan
from zen_app_runtime.manifest import create_manifest


class DesktopTests(unittest.TestCase):
    def setUp(self):
        manifest = create_manifest("mail", "Mail % App", "https://mail.example.com")
        self.plan = build_launch_plan(
            manifest,
            runtime_path="/opt/Zen Runtime/zen%stable",
            launcher_path='/usr/local/bin/zen app%preview',
            profile_root="/var/lib/zen profiles",
        )

    def test_preview_is_deterministic_plan_only_desktop_entry(self):
        preview = build_desktop_preview(self.plan, "/var/lib/zen definitions")
        expected = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Version=1.0\n"
            "Name=Mail % App\n"
            'Exec="/usr/local/bin/zen app%%preview" "--store" "/var/lib/zen definitions" "plan" "mail" "--runtime" "/opt/Zen Runtime/zen%%stable" "--launcher" "/usr/local/bin/zen app%%preview" "--profile-root" "/var/lib/zen profiles"\n'
            "Terminal=true\n"
            "Icon=application-x-executable\n"
            "X-Zen-App-Runtime-Plan-Only=true\n"
        )
        rendered = render_desktop_preview(preview)
        self.assertEqual(expected, rendered)
        self.assertEqual(rendered, render_desktop_preview(preview))
        self.assertNotIn("StartupWMClass", rendered)
        self.assertNotIn("DBusActivatable", rendered)
        self.assertNotIn("MimeType", rendered)
        self.assertNotIn(self.plan.start_url, rendered)

    def test_exec_escaping_quotes_every_token_and_neutralizes_field_codes(self):
        rendered = render_exec(
            (
                "/abs/launcher",
                "space value",
                'double"quote',
                "back\\slash",
                "$dollar",
                "`backtick`",
                "%f %F %u %U %i %c %k %% %x",
                "semi;and&pipe|glob*?hash#parens()<>~",
            )
        )
        self.assertEqual(
            r'"/abs/launcher" "space value" "double\\"quote" "back\\\\slash" "\\$dollar" "\\`backtick\\`" "%%f %%F %%u %%U %%i %%c %%k %%%% %%x" "semi;and&pipe|glob*?hash#parens()<>~"',
            rendered,
        )

    def test_exec_rejects_empty_argv_and_control_characters(self):
        with self.assertRaises(DesktopError):
            render_exec(())
        for value in ("line\nbreak", "carriage\rreturn", "nul\0byte", "tab\tvalue"):
            with self.subTest(value=value):
                with self.assertRaises(DesktopError):
                    render_exec(("/abs/launcher", value))

    def test_store_path_must_be_absolute(self):
        with self.assertRaisesRegex(DesktopError, "store path"):
            build_desktop_preview(self.plan, "relative/store")


if __name__ == "__main__":
    unittest.main()
