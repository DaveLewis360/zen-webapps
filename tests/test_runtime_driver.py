import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from zen_app_runtime.discord_conformance import (
    build_discord_conformance_suite,
    discord_conformance_suite_sha256,
    evaluate_discord_conformance,
)
from zen_app_runtime.runtime_driver import (
    RuntimeDriverActionRequest,
    RuntimeDriverActionResult,
    RuntimeDriverError,
    RuntimeDriverSessionReady,
    RuntimeDriverSessionStart,
    build_zen_runtime_driver_descriptor,
    build_zen_runtime_driver_plan,
    decode_runtime_driver_message,
    encode_runtime_driver_message,
    render_runtime_driver_descriptor,
    runtime_driver_descriptor_from_dict,
    runtime_driver_descriptor_sha256,
    runtime_driver_descriptor_to_dict,
    start_runtime_driver_session,
)


class FakeScope:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.closed = False
        self.samples = 0

    def poll(self):
        return self.returncode

    def sample(self, *, max_attempts=3):
        if self.closed:
            raise AssertionError("sample after close")
        self.samples += 1
        return {"sample": self.samples, "maxAttempts": max_attempts}

    def terminate(self, *, timeout=5.0):
        self.terminated = True
        self.closed = True
        self.returncode = -15
        return self.returncode

    def close(self):
        if self.returncode is None:
            self.terminate()
        else:
            self.closed = True


class FakeSemanticChannel:
    def __init__(self, suite, *, failure=None):
        self.suite = suite
        self.failure = failure
        self.closed = False
        self.messages = []
        self.events = {
            (case.case_id, step.action_id): step.expected_event
            for case in suite.cases
            for step in case.steps
        }

    def exchange(self, payload, *, timeout_ms):
        message = decode_runtime_driver_message(payload)
        self.messages.append((message, timeout_ms))
        if isinstance(message, RuntimeDriverSessionStart):
            if self.failure == "handshake-malformed":
                return b'{"kind":"session-ready"}\n'
            return encode_runtime_driver_message(
                RuntimeDriverSessionReady(
                    protocol_version=message.protocol_version,
                    driver_id=message.driver_id,
                    suite_sha256=message.suite_sha256,
                    fixture_protocol_version=message.fixture_protocol_version,
                    execution_mode=message.execution_mode,
                    authority=message.authority,
                )
            )
        if not isinstance(message, RuntimeDriverActionRequest):
            raise AssertionError(f"unexpected request {message!r}")
        if self.failure == "timeout":
            raise TimeoutError("private timeout detail")
        event_id = self.events[(message.case_id, message.action_id)]
        if self.failure == "wrong-event":
            event_id = "fabricated-private-event"
        if self.failure == "wrong-sequence":
            sequence = message.sequence + 1
        else:
            sequence = message.sequence
        return encode_runtime_driver_message(
            RuntimeDriverActionResult(
                protocol_version=message.protocol_version,
                sequence=sequence,
                case_id=message.case_id,
                action_id=message.action_id,
                status="passed",
                event_id=event_id,
                failure_code=None,
            )
        )

    def close(self):
        self.closed = True


class RuntimeDriverTests(unittest.TestCase):
    def setUp(self):
        self.suite = build_discord_conformance_suite()
        self.temporary = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1]
        )
        self.root = Path(self.temporary.name)
        self.command = (
            "/usr/bin/python3.11",
            "-m",
            "zen_app_runtime",
            "launch",
            "execute",
            "--profile-root",
            str(self.root / "profiles"),
            "--runtime-lock",
            str(self.root / "runtime.lock.json"),
            "--bundled-app-shell",
            "--yes",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, *, test_automation=True, execution_mode="synthetic-only", command=None, environment=None):
        return build_zen_runtime_driver_plan(
            self.suite,
            self.command if command is None else command,
            scope_name="zen-app-benchmark-runtime-driver-test",
            working_directory=self.root,
            environment={"PATH": "/usr/bin"} if environment is None else environment,
            execution_mode=execution_mode,
            test_automation=test_automation,
        )

    @staticmethod
    def clocks():
        utc_values = iter(
            (
                datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 30, 13, 0, 0, tzinfo=timezone.utc),
            )
        )
        monotonic_value = -100_000_000

        def utc_now():
            return next(utc_values)

        def monotonic_ns():
            nonlocal monotonic_value
            monotonic_value += 100_000_000
            return monotonic_value

        return utc_now, monotonic_ns

    def test_zen_descriptor_is_canonical_suite_bound_and_release_debug_closed(self):
        descriptor = build_zen_runtime_driver_descriptor(self.suite)
        data = runtime_driver_descriptor_to_dict(descriptor, suite=self.suite)
        self.assertEqual(1, data["runtimeDriverDescriptorVersion"])
        self.assertEqual(2, data["runtimeDriverProtocolVersion"])
        self.assertEqual("zen-app-shell-runtime-driver", data["driverId"])
        self.assertEqual("gecko", data["engineFamily"])
        self.assertEqual("org.zen-app-runtime.app-shell", data["adapter"]["id"])
        self.assertEqual("none", data["releaseDebugAuthority"])
        self.assertEqual(
            "explicit-local-semantic-channel",
            data["testAutomationAuthority"],
        )
        self.assertEqual(
            discord_conformance_suite_sha256(self.suite),
            data["suite"]["sha256"],
        )
        rendered = render_runtime_driver_descriptor(descriptor, suite=self.suite)
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            runtime_driver_descriptor_sha256(descriptor, suite=self.suite),
        )
        self.assertEqual(
            descriptor,
            runtime_driver_descriptor_from_dict(copy.deepcopy(data), suite=self.suite),
        )
        lowered = rendered.lower()
        for forbidden in ("selector", "screenshot", "evaluate-script", "remote-debugging-port"):
            self.assertNotIn(forbidden, lowered)

    def test_descriptor_rejects_unknown_fields_versions_and_other_suite(self):
        descriptor = build_zen_runtime_driver_descriptor(self.suite)
        data = runtime_driver_descriptor_to_dict(descriptor, suite=self.suite)
        invalid = copy.deepcopy(data)
        invalid["unknown"] = True
        with self.assertRaisesRegex(RuntimeDriverError, "unknown"):
            runtime_driver_descriptor_from_dict(invalid, suite=self.suite)
        invalid = copy.deepcopy(data)
        invalid["runtimeDriverProtocolVersion"] = 1
        with self.assertRaisesRegex(RuntimeDriverError, "ProtocolVersion"):
            runtime_driver_descriptor_from_dict(invalid, suite=self.suite)
        invalid = copy.deepcopy(data)
        invalid["suite"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(RuntimeDriverError, "suite"):
            runtime_driver_descriptor_from_dict(invalid, suite=self.suite)

    def test_wire_messages_are_bounded_canonical_and_have_no_generic_payload(self):
        digest = discord_conformance_suite_sha256(self.suite)
        message = RuntimeDriverActionRequest(
            protocol_version=2,
            sequence=7,
            case_id="synthetic-startup-boundary",
            action_id="observe-onboarding",
        )
        payload = encode_runtime_driver_message(message)
        self.assertEqual(message, decode_runtime_driver_message(payload))
        self.assertTrue(payload.endswith(b"\n"))
        self.assertNotIn(b"selector", payload.lower())
        self.assertNotIn(b"script", payload.lower())

        invalid = json.loads(payload)
        invalid["selector"] = "#private-message"
        with self.assertRaisesRegex(RuntimeDriverError, "unknown"):
            decode_runtime_driver_message(
                (json.dumps(invalid, sort_keys=True, indent=2) + "\n").encode("utf-8")
            )
        with self.assertRaisesRegex(RuntimeDriverError, "duplicate"):
            decode_runtime_driver_message(
                b'{"actionId":"observe-onboarding","actionId":"duplicate",'
                b'"caseId":"synthetic-startup-boundary","kind":"action",'
                b'"runtimeDriverProtocolVersion":2,"sequence":7}\n'
            )
        with self.assertRaisesRegex(RuntimeDriverError, "canonical"):
            decode_runtime_driver_message(payload.rstrip())

        start = RuntimeDriverSessionStart(
            protocol_version=2,
            driver_id="zen-app-shell-runtime-driver",
            suite_sha256=digest,
            fixture_protocol_version=1,
            execution_mode="synthetic-only",
            authority="test-only-semantic-channel",
        )
        self.assertEqual(start, decode_runtime_driver_message(encode_runtime_driver_message(start)))
        with self.assertRaisesRegex(RuntimeDriverError, "passed"):
            encode_runtime_driver_message(
                RuntimeDriverActionResult(2, 1, "case", "action", "passed", None, None)
            )

    def test_plan_wraps_current_app_shell_in_exact_cgroup_and_rejects_debug_authority(self):
        plan = self.plan()
        self.assertEqual(2, plan.driver.protocol_version)
        self.assertEqual("test-only-semantic-channel", plan.automation_authority)
        self.assertEqual(self.command, plan.scope_plan.command)
        self.assertEqual(
            "zen-app-benchmark-runtime-driver-test.scope",
            plan.scope_plan.unit_id,
        )
        self.assertEqual((("PATH", "/usr/bin"),), plan.environment)

        release = self.plan(test_automation=False)
        self.assertEqual("none", release.automation_authority)
        launch_calls = []
        with self.assertRaisesRegex(RuntimeDriverError, "test-only"):
            start_runtime_driver_session(
                release,
                FakeSemanticChannel(self.suite),
                launch_scope=lambda *_args, **_kwargs: launch_calls.append(True),
            )
        self.assertEqual([], launch_calls)

        with self.assertRaisesRegex(RuntimeDriverError, "bundled App Shell"):
            self.plan(command=("/usr/bin/python3.11", "--yes"))
        with self.assertRaisesRegex(RuntimeDriverError, "remote debugging"):
            self.plan(command=(*self.command, "--remote-debugging-port=9222"))
        with self.assertRaisesRegex(RuntimeDriverError, "debug authority"):
            self.plan(environment={"PATH": "/usr/bin", "MOZ_MARIONETTE": "1"})

    def test_synthetic_run_builds_valid_incomplete_transcript_and_exposes_cgroup_sample(self):
        plan = self.plan()
        channel = FakeSemanticChannel(self.suite)
        scope = FakeScope()
        launch_calls = []
        utc_now, monotonic_ns = self.clocks()

        def launch_scope(scope_plan, environment):
            launch_calls.append((scope_plan, dict(environment)))
            return scope

        session = start_runtime_driver_session(
            plan,
            channel,
            launch_scope=launch_scope,
            utc_now=utc_now,
            monotonic_ns=monotonic_ns,
        )
        try:
            self.assertEqual({"sample": 1, "maxAttempts": 5}, session.sample(max_attempts=5))
            transcript = session.run_conformance()
            report = evaluate_discord_conformance(self.suite, transcript)
            self.assertEqual("pass", report.synthetic_status)
            self.assertEqual("incomplete", report.live_status)
            self.assertEqual("incomplete", report.overall_status)
            self.assertEqual("zen-app-shell-runtime-driver", transcript.driver_id)
            self.assertEqual(2, transcript.driver_protocol_version)
            synthetic = [
                result
                for case, result in zip(self.suite.cases, transcript.cases, strict=True)
                if case.layer == "synthetic"
            ]
            live = [
                result
                for case, result in zip(self.suite.cases, transcript.cases, strict=True)
                if case.layer == "live-discord"
            ]
            self.assertTrue(all(result.status == "passed" for result in synthetic))
            self.assertTrue(all(result.status == "not-run" for result in live))
            expected_actions = sum(len(case.steps) for case in self.suite.cases if case.layer == "synthetic")
            action_messages = [
                message
                for message, _timeout in channel.messages
                if isinstance(message, RuntimeDriverActionRequest)
            ]
            self.assertEqual(expected_actions, len(action_messages))
            self.assertEqual(list(range(1, expected_actions + 1)), [item.sequence for item in action_messages])
        finally:
            session.close()
        self.assertEqual(1, len(launch_calls))
        self.assertEqual({"PATH": "/usr/bin"}, launch_calls[0][1])
        self.assertTrue(scope.terminated)
        self.assertTrue(channel.closed)

    def test_wrong_event_timeout_and_response_rebinding_fail_closed_without_private_detail(self):
        for failure, expected_code in (
            ("wrong-event", "target-changed"),
            ("timeout", "timeout"),
            ("wrong-sequence", "driver-error"),
        ):
            with self.subTest(failure=failure):
                plan = self.plan()
                channel = FakeSemanticChannel(self.suite, failure=failure)
                scope = FakeScope()
                utc_now, monotonic_ns = self.clocks()
                session = start_runtime_driver_session(
                    plan,
                    channel,
                    launch_scope=lambda _plan, _environment: scope,
                    utc_now=utc_now,
                    monotonic_ns=monotonic_ns,
                )
                try:
                    transcript = session.run_conformance()
                finally:
                    session.close()
                self.assertEqual("failed", transcript.cases[0].status)
                self.assertEqual(expected_code, transcript.cases[0].failure_code)
                self.assertEqual((), transcript.cases[0].events)
                self.assertTrue(all(item.status == "not-run" for item in transcript.cases[1:]))
                rendered = json.dumps(
                    {
                        "failureCode": transcript.cases[0].failure_code,
                        "events": [],
                    },
                    sort_keys=True,
                )
                self.assertNotIn("private timeout detail", rendered)
                self.assertTrue(scope.terminated)
                self.assertTrue(channel.closed)

    def test_handshake_failure_closes_channel_and_scope(self):
        plan = self.plan()
        channel = FakeSemanticChannel(self.suite, failure="handshake-malformed")
        scope = FakeScope()
        with self.assertRaises(RuntimeDriverError):
            start_runtime_driver_session(
                plan,
                channel,
                launch_scope=lambda _plan, _environment: scope,
            )
        self.assertTrue(scope.terminated)
        self.assertTrue(channel.closed)


if __name__ == "__main__":
    unittest.main()
