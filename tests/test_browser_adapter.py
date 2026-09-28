import os
from pathlib import Path
import shutil
import tempfile
import unittest

from zen_app_runtime.browser_adapter import (
    BROWSER_ADAPTER_BUNDLE_VERSION,
    BROWSER_ADAPTER_FILE_SHA256,
    EXPECTED_BROWSER_ADAPTER_TREE_SHA256,
    BrowserAdapterError,
    browser_adapter_application_path,
    browser_adapter_to_dict,
    bundled_adapter_capability,
    bundled_browser_adapter_path,
    open_bundled_browser_adapter,
    open_verified_browser_adapter,
    revalidate_open_browser_adapter,
)
from zen_app_runtime.runtime import RuntimeIdentity


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self):
        self.project_root = Path(__file__).resolve().parents[1]
        self.source = Path(bundled_browser_adapter_path())
        self.temporary = tempfile.TemporaryDirectory(dir=self.project_root)
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def copy_bundle(self, name: str) -> Path:
        destination = self.base / name
        shutil.copytree(self.source, destination)
        return destination

    def test_bundled_tree_is_exact_pinned_fd_backed_and_runtime_bound(self):
        self.assertEqual(2, BROWSER_ADAPTER_BUNDLE_VERSION)
        with open_bundled_browser_adapter() as opened:
            descriptor = opened.descriptor
            self.assertEqual(
                {
                    "browserAdapterBundleVersion": 2,
                    "files": [path for path, _digest in BROWSER_ADAPTER_FILE_SHA256],
                    "source": "package-bundled",
                    "treeSha256": EXPECTED_BROWSER_ADAPTER_TREE_SHA256,
                },
                browser_adapter_to_dict(opened),
            )
            application = browser_adapter_application_path(opened)
            self.assertEqual(
                f"/proc/self/fd/{descriptor}/application.ini",
                application,
            )
            self.assertEqual(
                os.stat(
                    "application.ini",
                    dir_fd=descriptor,
                    follow_symlinks=False,
                ).st_ino,
                os.stat(application).st_ino,
            )
            identity = RuntimeIdentity(1, "/opt/zen/zen-bin", "a" * 64)
            capability = bundled_adapter_capability(identity, opened)
            self.assertEqual(identity, capability.runtime_identity)
            self.assertEqual("org.zen-app-runtime.app-shell", capability.adapter_id)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_closed_tree_rejects_content_change_extra_entry_and_symlink(self):
        changed = self.copy_bundle("changed")
        (changed / "chrome/content/shell.css").write_text(
            "window { background: red; }\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BrowserAdapterError, "digest does not match"):
            with open_verified_browser_adapter(changed):
                pass

        extra = self.copy_bundle("extra")
        (extra / "unreviewed.js").write_text("// unreviewed\n", encoding="utf-8")
        with self.assertRaisesRegex(BrowserAdapterError, "unknown"):
            with open_verified_browser_adapter(extra):
                pass

        linked = self.copy_bundle("linked")
        manifest = linked / "chrome.manifest"
        manifest.unlink()
        manifest.symlink_to(self.source / "chrome.manifest")
        with self.assertRaisesRegex(BrowserAdapterError, "symbolic link|regular file"):
            with open_verified_browser_adapter(linked):
                pass

    def test_retained_directory_revalidation_detects_post_open_mutation(self):
        copied = self.copy_bundle("revalidate")
        with open_verified_browser_adapter(copied) as opened:
            revalidate_open_browser_adapter(opened)
            target = copied / "chrome/content/shell.js"
            target.write_bytes(target.read_bytes() + b"\n")
            with self.assertRaisesRegex(BrowserAdapterError, "digest does not match"):
                revalidate_open_browser_adapter(opened)

    def test_bundle_contains_real_parent_mediation_portal_and_security_baseline(self):
        script = (self.source / "chrome/content/shell.js").read_text(encoding="utf-8")
        preferences = (self.source / "defaults/preferences/prefs.js").read_text(
            encoding="utf-8"
        )
        markup = (self.source / "chrome/content/shell.xhtml").read_text(encoding="utf-8")
        styles = (self.source / "chrome/content/shell.css").read_text(encoding="utf-8")
        application = (self.source / "application.ini").read_text(encoding="utf-8")
        initialize = script[script.index("function initialize()") :]
        self.assertLess(initialize.index("installMediationAuthority()"), initialize.index("acknowledgeReady("))
        self.assertLess(initialize.index("acknowledgeReady("), initialize.index("openSite"))
        self.assertIn("ready-before-site-content", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_AUTH_PROMPT", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_AUTH_STARTED", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_AUTH_RETURNED", script)
        self.assertIn('browser.fxrPermissionPrompt = mediatePermissionRequest', script)
        self.assertIn('@mozilla.org/helperapplauncherdialog;1', script)
        self.assertIn("const originalCID = registrar.contractIDToCID(DOWNLOAD_DIALOG_CONTRACT);", script)
        self.assertIn("createInstance(iid)", script)
        self.assertNotIn("createInstance(outer, iid)", script)
        self.assertIn("registrar.unregisterFactory(DOWNLOAD_DIALOG_CLASS_ID, factory);", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_RESTORED download", script)
        self.assertIn("registerFactory", script)
        self.assertIn("promptForSaveToFileAsync", script)
        self.assertIn("Ci.nsIFilePicker.modeSave", script)
        self.assertIn("saveDestinationAvailable", script)
        self.assertIn("continue-to-portal", script)
        self.assertIn("xdg-desktop-portal", script)
        self.assertIn("isHandlingUserInput", script)
        self.assertIn('requestTypes.has("AudioCapture")', script)
        self.assertIn('requestTypes.has("Speaker")', script)
        self.assertIn("invalidatePendingMediation", script)
        self.assertIn("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_SELFTEST", script)
        self.assertIn('id="permission-panel"', markup)
        self.assertIn('id="download-panel"', markup)
        self.assertIn('id="permission-shared-audio-row"', markup)
        self.assertIn('id="permission-shared-audio-device"', markup)
        self.assertIn('id="permission-speaker-row"', markup)
        self.assertIn('id="permission-speaker-device"', markup)
        self.assertIn('id="permission-deny"', markup)
        self.assertIn('id="permission-allow"', markup)
        self.assertIn('id="download-cancel"', markup)
        self.assertIn('id="download-save"', markup)
        self.assertNotIn('id="key-block-search"', markup)
        self.assertNotIn('id="key-block-private-window"', markup)
        self.assertIn('id="content-browser-host"', markup)
        self.assertNotIn('<browser id="content-browser"', markup)
        self.assertIn('browser.setAttribute("remote", "true")', script)
        self.assertIn('browser.setAttribute("transparent", "true")', script)
        self.assertIn('transparent="true"', markup)
        self.assertIn('id="auth-panel"', markup)
        self.assertIn('id="chrome-reveal-button"', markup)
        self.assertIn('id="hide-chrome-button"', markup)
        self.assertIn('id="key-hide-chrome"', markup)
        self.assertIn('keyCommand("key-hide-chrome", dismissOverlayOrHideChrome)', script)
        self.assertNotIn('id="blocked-panel"', markup)
        self.assertIn(".floating-chrome", styles)
        self.assertIn(".mediation-card", styles)
        self.assertIn(':root[zen-chrome-visible="true"]', styles)
        self.assertIn("Version=1.4.0", application)
        self.assertIn('security.sandbox.content.level", 6', preferences)
        self.assertIn('security.sandbox.socket.process.level", 2', preferences)
        self.assertIn('fission.autostart", true', preferences)
        self.assertIn('browser.download.useDownloadDir", false', preferences)
        self.assertIn('browser.download.always_ask_before_handling_new_types", true', preferences)
        self.assertNotIn("no-sandbox", preferences.lower())


if __name__ == "__main__":
    unittest.main()
