import json
import tempfile
import unittest
from pathlib import Path

from zen_app_runtime.app_shell import (
    APP_SHELL_ADAPTER_CAPABILITY_VERSION,
    APP_SHELL_ADAPTER_ID,
    APP_SHELL_ADAPTER_PROTOCOL_VERSION,
    APP_SHELL_HANDSHAKE_VERSION,
    APP_SHELL_POLICY_VERSION,
    AppShellAdapterCapability,
    AppShellError,
    AppShellPolicy,
    REQUIRED_ADAPTER_CAPABILITIES,
    adapter_capability_from_dict,
    adapter_capability_to_dict,
    app_shell_handshake_from_dict,
    app_shell_policy_sha256,
    app_shell_policy_to_dict,
    build_app_shell_policy,
    load_adapter_capability,
    render_app_shell_policy,
)
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.runtime import RuntimeIdentity, runtime_identity_to_dict


class AppShellContractTests(unittest.TestCase):
    def setUp(self):
        self.manifest = create_manifest(
            "mail-app",
            "Mail App",
            "https://MAIL.example.com:443/inbox",
            mode="Opaque",
        )
        self.identity = RuntimeIdentity(1, "/opt/zen/zen-bin", "a" * 64)

    def capability_dict(self):
        return {
            "adapterCapabilityVersion": APP_SHELL_ADAPTER_CAPABILITY_VERSION,
            "adapterId": APP_SHELL_ADAPTER_ID,
            "adapterProtocolVersion": APP_SHELL_ADAPTER_PROTOCOL_VERSION,
            "capabilities": list(REQUIRED_ADAPTER_CAPABILITIES),
            "runtimeIdentity": runtime_identity_to_dict(self.identity),
        }

    def test_policy_v2_freezes_real_prompt_download_portal_and_shortcut_boundaries(self):
        policy = build_app_shell_policy(self.manifest)
        data = app_shell_policy_to_dict(policy)
        self.assertEqual(2, APP_SHELL_POLICY_VERSION)
        self.assertEqual(2, data["appShellPolicyVersion"])
        self.assertEqual("https://mail.example.com", data["startOrigin"])
        self.assertEqual(
            {
                "addressBar": "hidden",
                "mode": "Opaque",
                "originIndicator": "always-available",
                "permissionIndicator": "always-available",
                "securityChrome": "minimal",
                "sidebar": "hidden",
                "tabStrip": "hidden",
                "topBar": "minimal-security-chrome",
            },
            data["presentation"],
        )
        self.assertEqual(True, data["onboarding"]["requiredBeforeSiteContent"])
        self.assertEqual(False, data["onboarding"]["defaultProfileImport"])
        self.assertEqual("same-origin", data["navigation"]["internalNavigation"])
        self.assertEqual(
            "final-commit-including-redirects",
            data["navigation"]["topLevelDecisionPoint"],
        )
        self.assertEqual(
            "show-origin-and-security-panel",
            data["shortcuts"]["remapped"]["Ctrl+L"],
        )
        self.assertIn("Ctrl+T", data["shortcuts"]["blocked"])
        self.assertIn("Escape", data["shortcuts"]["allowed"])
        self.assertIn("Ctrl+K", data["shortcuts"]["allowed"])
        self.assertIn("Ctrl+Shift+N", data["shortcuts"]["allowed"])
        self.assertNotIn("Ctrl+K", data["shortcuts"]["blocked"])
        self.assertNotIn("Ctrl+Shift+N", data["shortcuts"]["blocked"])
        self.assertEqual(
            {
                "defaultAction": "deny-unless-privileged-prompt",
                "maxOutstandingRequests": 1,
                "requestBinding": "exact-origin-document-navigation-epoch",
                "requestExpiryMs": 30_000,
                "requestKinds": {
                    "camera": "content-activation-and-session-prompt",
                    "clipboard-read": "content-activation-and-upstream-paste-consent",
                    "clipboard-write": "content-activation-only",
                    "file-picker": "content-activation-and-system-picker",
                    "microphone": "content-activation-and-session-prompt",
                    "notifications": "content-activation-and-session-prompt",
                    "screen-capture": "content-activation-shell-prompt-and-portal",
                    "speaker-selection": "content-activation-and-session-prompt",
                },
                "revokePendingOn": [
                    "navigation",
                    "reload",
                    "window-close",
                    "process-exit",
                ],
                "sessionOnly": True,
                "silentGrants": False,
            },
            data["permissions"],
        )
        self.assertEqual(
            {
                "defaultAction": "deny-unless-privileged-confirmation",
                "destination": "system-file-picker",
                "maxOutstandingRequests": 1,
                "openAfterDownload": False,
                "requestBinding": "exact-origin-document-navigation-epoch",
                "requestExpiryMs": 30_000,
                "silentDownloads": False,
            },
            data["downloads"],
        )
        self.assertEqual(
            {
                "directSourceSelection": False,
                "persistence": "none",
                "rePromptAfterStop": True,
                "screenCast": "xdg-desktop-portal-required-on-wayland",
                "transientUserActivation": True,
            },
            data["portal"],
        )
        rendered = render_app_shell_policy(policy)
        self.assertEqual(json.dumps(data, indent=2, sort_keys=True) + "\n", rendered)
        self.assertEqual(64, len(app_shell_policy_sha256(policy)))

    def test_policy_renderer_rejects_fabricated_authority(self):
        valid = build_app_shell_policy(self.manifest)
        cases = (
            AppShellPolicy(1, valid.app_id, valid.start_url, valid.start_origin, valid.presentation_mode),
            AppShellPolicy(2, "../escape", valid.start_url, valid.start_origin, valid.presentation_mode),
            AppShellPolicy(2, valid.app_id, "https://user@example.com", "https://example.com", valid.presentation_mode),
            AppShellPolicy(2, valid.app_id, valid.start_url, "https://other.example.com", valid.presentation_mode),
            AppShellPolicy(2, valid.app_id, valid.start_url, valid.start_origin, "Invisible"),
        )
        for policy in cases:
            with self.subTest(policy=policy), self.assertRaises(AppShellError):
                app_shell_policy_to_dict(policy)

    def test_capability_is_closed_versioned_runtime_bound_and_canonical(self):
        self.assertEqual(2, APP_SHELL_ADAPTER_CAPABILITY_VERSION)
        self.assertEqual(2, APP_SHELL_ADAPTER_PROTOCOL_VERSION)
        self.assertEqual(
            (
                "download-mediation-v2",
                "file-picker-boundary-v1",
                "minimal-security-chrome-v1",
                "navigation-policy-v1",
                "onboarding-v1",
                "permission-mediation-v2",
                "shortcut-policy-v2",
                "startup-handshake-v2",
                "wayland-screencast-portal-v1",
                "window-identity-v1",
            ),
            REQUIRED_ADAPTER_CAPABILITIES,
        )
        data = self.capability_dict()
        capability = adapter_capability_from_dict(data)
        self.assertEqual(self.identity, capability.runtime_identity)
        self.assertEqual(data, adapter_capability_to_dict(capability))

        invalid = dict(data)
        invalid["unknown"] = True
        with self.assertRaisesRegex(AppShellError, "unknown"):
            adapter_capability_from_dict(invalid)
        invalid = dict(data)
        invalid["capabilities"] = data["capabilities"][:-1]
        with self.assertRaisesRegex(AppShellError, "exactly"):
            adapter_capability_from_dict(invalid)
        invalid = dict(data)
        invalid["adapterProtocolVersion"] = 1
        with self.assertRaisesRegex(AppShellError, "adapterProtocolVersion"):
            adapter_capability_from_dict(invalid)

    def test_capability_file_is_bounded_strict_and_bound_to_verified_runtime(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            capability_path = root / "adapter.json"
            capability_path.write_text(
                json.dumps(self.capability_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            capability_path.chmod(0o600)
            loaded = load_adapter_capability(capability_path, self.identity)
            self.assertEqual(self.identity, loaded.runtime_identity)

            other = RuntimeIdentity(1, self.identity.path, "b" * 64)
            with self.assertRaisesRegex(AppShellError, "different runtime identity"):
                load_adapter_capability(capability_path, other)
            capability_path.write_text(
                '{"adapterCapabilityVersion":2,"adapterCapabilityVersion":2}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AppShellError, "duplicate"):
                load_adapter_capability(capability_path, self.identity)

    def test_handshake_must_ack_exact_v2_adapter_protocol_policy_and_precontent_state(self):
        self.assertEqual(2, APP_SHELL_HANDSHAKE_VERSION)
        policy_digest = app_shell_policy_sha256(build_app_shell_policy(self.manifest))
        handshake = {
            "adapterId": APP_SHELL_ADAPTER_ID,
            "adapterProtocolVersion": APP_SHELL_ADAPTER_PROTOCOL_VERSION,
            "handshakeVersion": APP_SHELL_HANDSHAKE_VERSION,
            "policySha256": policy_digest,
            "status": "ready-before-site-content",
        }
        parsed = app_shell_handshake_from_dict(
            handshake,
            expected_policy_sha256=policy_digest,
        )
        self.assertEqual("ready-before-site-content", parsed.status)

        for field, value in (
            ("adapterId", "other"),
            ("adapterProtocolVersion", 1),
            ("handshakeVersion", 1),
            ("policySha256", "b" * 64),
            ("status", "ready-after-site-content"),
        ):
            invalid = dict(handshake)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaises(AppShellError):
                app_shell_handshake_from_dict(
                    invalid,
                    expected_policy_sha256=policy_digest,
                )


if __name__ == "__main__":
    unittest.main()
