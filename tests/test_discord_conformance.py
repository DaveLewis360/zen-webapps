import copy
import hashlib
import json
import unittest

from zen_app_runtime.discord_conformance import (
    DiscordConformanceError,
    build_discord_conformance_suite,
    discord_conformance_report_to_dict,
    discord_conformance_suite_from_dict,
    discord_conformance_suite_sha256,
    discord_conformance_suite_to_dict,
    discord_conformance_transcript_from_dict,
    discord_conformance_transcript_sha256,
    discord_conformance_transcript_to_dict,
    discord_web_parity_check,
    evaluate_discord_conformance,
    render_discord_conformance_report,
    render_discord_conformance_suite,
    render_discord_conformance_transcript,
)


class DiscordConformanceTests(unittest.TestCase):
    def setUp(self):
        self.suite = build_discord_conformance_suite()
        self.suite_data = discord_conformance_suite_to_dict(self.suite)

    def transcript_data(self, *, live=True):
        elapsed = 100
        cases = []
        for case in self.suite_data["cases"]:
            should_run = case["layer"] == "synthetic" or live
            events = []
            if should_run:
                for step in case["steps"]:
                    events.append(
                        {
                            "elapsedMs": elapsed,
                            "eventId": step["expectedEvent"],
                        }
                    )
                    elapsed += 100
            cases.append(
                {
                    "caseId": case["caseId"],
                    "events": events,
                    "failureCode": None,
                    "status": "passed" if should_run else "not-run",
                }
            )
        return {
            "cases": cases,
            "completedAtUtc": "2026-08-30T12:00:00Z",
            "discordConformanceTranscriptVersion": 1,
            "driver": {"id": "test-runtime-driver", "protocolVersion": 2},
            "fixtureProtocolVersion": 1,
            "liveOrigin": "https://discord.com",
            "mode": "synthetic-and-live",
            "startedAtUtc": "2026-08-30T11:00:00Z",
            "suiteSha256": discord_conformance_suite_sha256(self.suite),
        }

    def test_suite_is_versioned_engine_neutral_complete_and_explicit_about_linux_web_scope(self):
        data = self.suite_data
        self.assertEqual(1, data["discordConformanceSuiteVersion"])
        self.assertEqual("discord-web-on-linux-v1", data["suiteId"])
        self.assertEqual(2, data["requiredDriverProtocolVersion"])
        self.assertEqual(1, data["fixtureProtocolVersion"])
        self.assertEqual("https://discord.com", data["liveOrigin"])
        case_ids = [case["caseId"] for case in data["cases"]]
        self.assertGreaterEqual(len(case_ids), 30)
        for required in (
            "synthetic-websocket-gateway-resume",
            "synthetic-service-worker-cache",
            "synthetic-microphone-webrtc",
            "synthetic-camera-webrtc",
            "synthetic-screen-share-portal",
            "synthetic-notification-focus",
            "synthetic-file-upload-download",
            "synthetic-keyboard-accessibility",
            "live-message-lifecycle",
            "live-upload-download-preview",
            "live-voice-channel",
            "live-video-call",
            "live-screen-share",
            "live-shortcuts-keyboard-accessibility",
            "live-gateway-reconnect-resume",
        ):
            self.assertIn(required, case_ids)
        self.assertEqual({"synthetic", "live-discord"}, {case["layer"] for case in data["cases"]})
        self.assertTrue(all(case["required"] is True for case in data["cases"]))
        exclusions = {item["capabilityId"]: item["reasonCode"] for item in data["scopeExclusions"]}
        self.assertEqual(
            "not-supported-by-upstream-linux-web",
            exclusions["screen-share-system-audio"],
        )
        self.assertEqual(
            "desktop-client-only",
            exclusions["custom-global-keybinds"],
        )
        self.assertEqual(
            "browser-client-does-not-offer-control",
            exclusions["screen-share-quality-control"],
        )
        rendered = render_discord_conformance_suite(self.suite)
        self.assertNotIn("selector", rendered.lower())
        self.assertNotIn("screenshot", rendered.lower())
        self.assertNotIn("messageContent", rendered)

    def test_suite_round_trip_render_and_digest_are_canonical(self):
        parsed = discord_conformance_suite_from_dict(copy.deepcopy(self.suite_data))
        self.assertEqual(self.suite, parsed)
        rendered = render_discord_conformance_suite(parsed)
        self.assertEqual(
            json.dumps(self.suite_data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            discord_conformance_suite_sha256(parsed),
        )

    def test_suite_rejects_unknown_fields_versions_case_reordering_and_mutated_steps(self):
        invalid = copy.deepcopy(self.suite_data)
        invalid["unknown"] = True
        with self.assertRaisesRegex(DiscordConformanceError, "unknown"):
            discord_conformance_suite_from_dict(invalid)

        invalid = copy.deepcopy(self.suite_data)
        invalid["discordConformanceSuiteVersion"] = 2
        with self.assertRaisesRegex(DiscordConformanceError, "Version"):
            discord_conformance_suite_from_dict(invalid)

        invalid = copy.deepcopy(self.suite_data)
        invalid["cases"][0], invalid["cases"][1] = invalid["cases"][1], invalid["cases"][0]
        with self.assertRaisesRegex(DiscordConformanceError, "exactly match"):
            discord_conformance_suite_from_dict(invalid)

        invalid = copy.deepcopy(self.suite_data)
        invalid["cases"][0]["steps"][0]["timeoutMs"] += 1
        with self.assertRaisesRegex(DiscordConformanceError, "exactly match"):
            discord_conformance_suite_from_dict(invalid)

    def test_complete_transcript_round_trip_and_all_required_cases_pass(self):
        data = self.transcript_data()
        transcript = discord_conformance_transcript_from_dict(data, suite=self.suite)
        self.assertEqual(data, discord_conformance_transcript_to_dict(transcript, suite=self.suite))
        rendered = render_discord_conformance_transcript(transcript, suite=self.suite)
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            discord_conformance_transcript_sha256(transcript, suite=self.suite),
        )
        report = evaluate_discord_conformance(self.suite, transcript)
        self.assertEqual("pass", report.synthetic_status)
        self.assertEqual("pass", report.live_status)
        self.assertEqual("pass", report.overall_status)
        self.assertEqual(len(self.suite.cases), report.passed_cases)
        self.assertEqual(0, report.failed_cases)
        self.assertEqual(0, report.not_run_cases)
        check = discord_web_parity_check(report)
        self.assertEqual("discord-web-parity", check.check_id)
        self.assertEqual("passed", check.status)

    def test_synthetic_only_evidence_is_incomplete_and_never_claims_discord_parity(self):
        transcript = discord_conformance_transcript_from_dict(
            self.transcript_data(live=False),
            suite=self.suite,
        )
        report = evaluate_discord_conformance(self.suite, transcript)
        self.assertEqual("pass", report.synthetic_status)
        self.assertEqual("incomplete", report.live_status)
        self.assertEqual("incomplete", report.overall_status)
        self.assertGreater(report.not_run_cases, 0)
        self.assertEqual("not-run", discord_web_parity_check(report).status)

    def test_failed_case_fails_parity_without_free_form_private_diagnostics(self):
        data = self.transcript_data()
        case = next(item for item in data["cases"] if item["caseId"] == "live-voice-channel")
        case["status"] = "failed"
        case["failureCode"] = "assertion-failed"
        case["events"] = case["events"][:2]
        transcript = discord_conformance_transcript_from_dict(data, suite=self.suite)
        report = evaluate_discord_conformance(self.suite, transcript)
        self.assertEqual("fail", report.live_status)
        self.assertEqual("fail", report.overall_status)
        self.assertEqual(1, report.failed_cases)
        self.assertEqual("failed", discord_web_parity_check(report).status)
        rendered = render_discord_conformance_transcript(transcript, suite=self.suite)
        self.assertNotIn("diagnostic", rendered.lower())
        self.assertNotIn("messageContent", rendered)
        self.assertNotIn("private message", rendered)

    def test_transcript_is_closed_suite_bound_and_requires_exact_ordered_event_evidence(self):
        invalid = self.transcript_data()
        invalid["suiteSha256"] = "f" * 64
        with self.assertRaisesRegex(DiscordConformanceError, "suite"):
            discord_conformance_transcript_from_dict(invalid, suite=self.suite)

        invalid = self.transcript_data()
        invalid["driver"]["protocolVersion"] = 1
        with self.assertRaisesRegex(DiscordConformanceError, "driver"):
            discord_conformance_transcript_from_dict(invalid, suite=self.suite)

        invalid = self.transcript_data()
        invalid["cases"][0]["events"][0]["eventId"] = "fabricated-event"
        with self.assertRaisesRegex(DiscordConformanceError, "event"):
            discord_conformance_transcript_from_dict(invalid, suite=self.suite)

        invalid = self.transcript_data()
        invalid["cases"][0]["events"][0]["domText"] = "private message"
        with self.assertRaisesRegex(DiscordConformanceError, "unknown"):
            discord_conformance_transcript_from_dict(invalid, suite=self.suite)

        invalid = self.transcript_data()
        invalid["cases"][0]["events"][1]["elapsedMs"] = invalid["cases"][0]["events"][0]["elapsedMs"]
        with self.assertRaisesRegex(DiscordConformanceError, "increasing"):
            discord_conformance_transcript_from_dict(invalid, suite=self.suite)

    def test_report_is_canonical_and_hash_bound_to_suite_and_transcript(self):
        transcript = discord_conformance_transcript_from_dict(
            self.transcript_data(),
            suite=self.suite,
        )
        report = evaluate_discord_conformance(self.suite, transcript)
        data = discord_conformance_report_to_dict(report)
        self.assertEqual(1, data["discordConformanceReportVersion"])
        self.assertEqual(discord_conformance_suite_sha256(self.suite), data["suiteSha256"])
        self.assertEqual(
            discord_conformance_transcript_sha256(transcript, suite=self.suite),
            data["transcriptSha256"],
        )
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            render_discord_conformance_report(report),
        )


if __name__ == "__main__":
    unittest.main()
