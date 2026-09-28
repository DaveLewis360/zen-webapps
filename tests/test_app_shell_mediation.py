import copy
import hashlib
import json
import unittest

from zen_app_runtime.app_shell_mediation import (
    APP_SHELL_MEDIATION_DECISION_VERSION,
    APP_SHELL_MEDIATION_REQUEST_VERSION,
    MEDIATION_REQUEST_TTL_MS,
    AppShellMediationAuthority,
    AppShellMediationContext,
    AppShellMediationDecision,
    AppShellMediationError,
    AppShellMediationRequest,
    app_shell_mediation_decision_from_dict,
    app_shell_mediation_decision_sha256,
    app_shell_mediation_decision_to_dict,
    app_shell_mediation_request_from_dict,
    app_shell_mediation_request_sha256,
    app_shell_mediation_request_to_dict,
    build_app_shell_mediation_request,
    render_app_shell_mediation_decision,
    render_app_shell_mediation_request,
)


class AppShellMediationTests(unittest.TestCase):
    def setUp(self):
        self.policy_sha256 = "a" * 64
        self.context = AppShellMediationContext(
            origin="https://discord.com",
            document_id=41,
            navigation_epoch=7,
        )

    def request(
        self,
        *,
        request_id: str = "1" * 32,
        kinds: tuple[str, ...] = ("microphone",),
        requested_at_ms: int = 1_000,
        content_user_activation: bool = True,
    ) -> AppShellMediationRequest:
        return build_app_shell_mediation_request(
            request_id=request_id,
            policy_sha256=self.policy_sha256,
            context=self.context,
            kinds=kinds,
            requested_at_ms=requested_at_ms,
            content_user_activation=content_user_activation,
        )

    def test_request_and_decision_are_closed_canonical_hash_bound_and_private(self):
        request = self.request(kinds=("camera", "microphone"))
        request_data = app_shell_mediation_request_to_dict(request)
        self.assertEqual(APP_SHELL_MEDIATION_REQUEST_VERSION, request_data["mediationRequestVersion"])
        self.assertEqual(
            request,
            app_shell_mediation_request_from_dict(copy.deepcopy(request_data)),
        )
        request_rendered = render_app_shell_mediation_request(request)
        self.assertEqual(
            json.dumps(request_data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            request_rendered,
        )
        self.assertEqual(
            hashlib.sha256(request_rendered.encode("ascii")).hexdigest(),
            app_shell_mediation_request_sha256(request),
        )

        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        authority.open_request(request, now_ms=1_000)
        decision = authority.decide(
            request_id=request.request_id,
            request_sha256=app_shell_mediation_request_sha256(request),
            outcome="allow-once",
            now_ms=1_500,
            current_context=self.context,
            privileged_user_activation=True,
        )
        decision_data = app_shell_mediation_decision_to_dict(decision)
        self.assertEqual(APP_SHELL_MEDIATION_DECISION_VERSION, decision_data["mediationDecisionVersion"])
        self.assertEqual(True, decision_data["sessionOnly"])
        self.assertEqual("none", decision_data["persistence"])
        self.assertEqual(
            decision,
            app_shell_mediation_decision_from_dict(copy.deepcopy(decision_data)),
        )
        decision_rendered = render_app_shell_mediation_decision(decision)
        self.assertEqual(
            hashlib.sha256(decision_rendered.encode("ascii")).hexdigest(),
            app_shell_mediation_decision_sha256(decision),
        )
        private_output = f"{request_rendered}\n{decision_rendered}".lower()
        for forbidden in (
            "filename",
            "deviceid",
            "devicelabel",
            "message",
            "pathname",
            "query",
            "urlhistory",
        ):
            self.assertNotIn(forbidden, private_output)

    def test_only_one_request_may_be_pending_and_context_rebinding_fails_closed(self):
        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        first = self.request()
        authority.open_request(first, now_ms=1_000)
        with self.assertRaisesRegex(AppShellMediationError, "outstanding"):
            authority.open_request(
                self.request(request_id="2" * 32, kinds=("camera",)),
                now_ms=1_001,
            )
        rebound = AppShellMediationContext(
            origin=self.context.origin,
            document_id=self.context.document_id + 1,
            navigation_epoch=self.context.navigation_epoch,
        )
        with self.assertRaisesRegex(AppShellMediationError, "context"):
            authority.decide(
                request_id=first.request_id,
                request_sha256=app_shell_mediation_request_sha256(first),
                outcome="allow-once",
                now_ms=1_100,
                current_context=rebound,
                privileged_user_activation=True,
            )
        invalidated = authority.invalidate("navigation", next_context=rebound)
        self.assertEqual(first, invalidated)
        self.assertIsNone(authority.pending_request)

    def test_sensitive_kinds_require_content_activation_and_screen_is_portal_only(self):
        for kind in (
            "camera",
            "clipboard-read",
            "clipboard-write",
            "file-picker",
            "microphone",
            "notifications",
            "screen-capture",
            "speaker-selection",
        ):
            with self.subTest(kind=kind), self.assertRaisesRegex(
                AppShellMediationError,
                "content user activation",
            ):
                self.request(kinds=(kind,), content_user_activation=False)

        screen = self.request(kinds=("screen-capture",))
        self.assertTrue(screen.portal_required)
        self.assertFalse(screen.system_picker_required)
        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        authority.open_request(screen, now_ms=1_000)
        with self.assertRaisesRegex(AppShellMediationError, "continue-to-portal"):
            authority.decide(
                request_id=screen.request_id,
                request_sha256=app_shell_mediation_request_sha256(screen),
                outcome="allow-once",
                now_ms=1_100,
                current_context=self.context,
                privileged_user_activation=True,
            )
        decision = authority.decide(
            request_id=screen.request_id,
            request_sha256=app_shell_mediation_request_sha256(screen),
            outcome="continue-to-portal",
            now_ms=1_101,
            current_context=self.context,
            privileged_user_activation=True,
        )
        self.assertEqual("xdg-desktop-portal-required", decision.downstream_authority)
        self.assertFalse(decision.claims_downstream_consent)

    def test_speaker_selection_is_canonical_one_shot_and_non_persistent(self):
        speaker = self.request(kinds=("speaker-selection",))
        data = app_shell_mediation_request_to_dict(speaker)
        self.assertEqual(["speaker-selection"], data["kinds"])
        self.assertEqual(
            speaker,
            app_shell_mediation_request_from_dict(copy.deepcopy(data)),
        )
        self.assertEqual(
            hashlib.sha256(
                render_app_shell_mediation_request(speaker).encode("ascii")
            ).hexdigest(),
            app_shell_mediation_request_sha256(speaker),
        )

        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        authority.open_request(speaker, now_ms=1_000)
        decision = authority.decide(
            request_id=speaker.request_id,
            request_sha256=app_shell_mediation_request_sha256(speaker),
            outcome="allow-once",
            now_ms=1_100,
            current_context=self.context,
            privileged_user_activation=True,
        )
        self.assertEqual("none", decision.downstream_authority)
        self.assertTrue(decision.session_only)
        self.assertEqual("none", decision.persistence)
        self.assertFalse(decision.claims_downstream_consent)

    def test_download_requires_privileged_activation_and_system_destination_picker(self):
        download = self.request(
            kinds=("download",),
            content_user_activation=False,
        )
        self.assertFalse(download.portal_required)
        self.assertTrue(download.system_picker_required)
        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        authority.open_request(download, now_ms=1_000)
        with self.assertRaisesRegex(AppShellMediationError, "privileged user activation"):
            authority.decide(
                request_id=download.request_id,
                request_sha256=app_shell_mediation_request_sha256(download),
                outcome="allow-once",
                now_ms=1_010,
                current_context=self.context,
                privileged_user_activation=False,
            )
        decision = authority.decide(
            request_id=download.request_id,
            request_sha256=app_shell_mediation_request_sha256(download),
            outcome="allow-once",
            now_ms=1_011,
            current_context=self.context,
            privileged_user_activation=True,
        )
        self.assertEqual("system-file-picker-required", decision.downstream_authority)

    def test_expired_replayed_and_rebound_requests_fail_closed(self):
        authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        request = self.request()
        authority.open_request(request, now_ms=1_000)
        with self.assertRaisesRegex(AppShellMediationError, "expired"):
            authority.decide(
                request_id=request.request_id,
                request_sha256=app_shell_mediation_request_sha256(request),
                outcome="allow-once",
                now_ms=1_000 + MEDIATION_REQUEST_TTL_MS + 1,
                current_context=self.context,
                privileged_user_activation=True,
            )
        self.assertIsNone(authority.pending_request)

        replay_authority = AppShellMediationAuthority(
            policy_sha256=self.policy_sha256,
            initial_context=self.context,
        )
        replay_authority.open_request(request, now_ms=1_000)
        digest = app_shell_mediation_request_sha256(request)
        replay_authority.decide(
            request_id=request.request_id,
            request_sha256=digest,
            outcome="allow-once",
            now_ms=1_100,
            current_context=self.context,
            privileged_user_activation=True,
        )
        with self.assertRaisesRegex(AppShellMediationError, "replayed"):
            replay_authority.open_request(request, now_ms=1_200)

        rebound_data = app_shell_mediation_request_to_dict(request)
        rebound_data["policySha256"] = "b" * 64
        rebound = app_shell_mediation_request_from_dict(rebound_data)
        with self.assertRaisesRegex(AppShellMediationError, "policy"):
            AppShellMediationAuthority(
                policy_sha256=self.policy_sha256,
                initial_context=self.context,
            ).open_request(rebound, now_ms=1_000)

    def test_malformed_request_and_decision_documents_are_rejected(self):
        request_data = app_shell_mediation_request_to_dict(self.request())
        cases = []
        unknown = copy.deepcopy(request_data)
        unknown["unknown"] = True
        cases.append(unknown)
        duplicate_kind = copy.deepcopy(request_data)
        duplicate_kind["kinds"] = ["microphone", "microphone"]
        cases.append(duplicate_kind)
        wrong_portal = copy.deepcopy(request_data)
        wrong_portal["portalRequired"] = True
        cases.append(wrong_portal)
        wrong_expiry = copy.deepcopy(request_data)
        wrong_expiry["expiresAtMonotonicMs"] += 1
        cases.append(wrong_expiry)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(AppShellMediationError):
                app_shell_mediation_request_from_dict(value)

        valid_decision = AppShellMediationDecision(
            decision_version=APP_SHELL_MEDIATION_DECISION_VERSION,
            request_id="1" * 32,
            request_sha256="b" * 64,
            outcome="deny",
            decided_at_monotonic_ms=1_500,
            privileged_user_activation=False,
            session_only=True,
            persistence="none",
            downstream_authority="none",
            claims_downstream_consent=False,
        )
        invalid_decision = app_shell_mediation_decision_to_dict(valid_decision)
        invalid_decision["sessionOnly"] = False
        with self.assertRaises(AppShellMediationError):
            app_shell_mediation_decision_from_dict(invalid_decision)

        invalid_portal_claim = AppShellMediationDecision(
            decision_version=APP_SHELL_MEDIATION_DECISION_VERSION,
            request_id="1" * 32,
            request_sha256="b" * 64,
            outcome="allow-once",
            decided_at_monotonic_ms=1_500,
            privileged_user_activation=True,
            session_only=True,
            persistence="none",
            downstream_authority="xdg-desktop-portal-required",
            claims_downstream_consent=False,
        )
        with self.assertRaisesRegex(
            AppShellMediationError,
            "cannot claim xdg-desktop-portal",
        ):
            app_shell_mediation_decision_to_dict(invalid_portal_claim)


if __name__ == "__main__":
    unittest.main()
