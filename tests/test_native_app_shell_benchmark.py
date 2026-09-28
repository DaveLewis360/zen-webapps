import tempfile
import unittest
from pathlib import Path

from zen_app_runtime.benchmark_runner import CgroupSample, ProcessMemorySample
from zen_app_runtime.native_app_shell_benchmark import (
    NativeAppShellBenchmarkError,
    _CANDIDATE_PREFS_LINE,
    _prepare_profile,
    _new_secure_process_count,
    _validate_worker_status,
    _verify_profile_preference,
    _verify_static_runtime_invariants,
)


class NativeAppShellBenchmarkUnitTests(unittest.TestCase):
    def sample(self, processes):
        return CgroupSample(
            sample_version=1,
            control_group="/test.scope",
            started_monotonic_ns=1,
            completed_monotonic_ns=2,
            memory_current_bytes=1,
            memory_peak_bytes=1,
            memory_swap_current_bytes=0,
            memory_stat=(("anon", 1), ("file", 0), ("kernel", 0)),
            cpu_stat=(("system_usec", 0), ("usage_usec", 0), ("user_usec", 0)),
            pids_current=len(processes),
            processes=tuple(processes),
        )

    def process(self, pid, start, seccomp=0, no_new_privs=0):
        return ProcessMemorySample(
            pid=pid,
            start_time_ticks=start,
            state="S",
            rss_kib=1,
            pss_kib=1,
            private_kib=1,
            swap_pss_kib=0,
            seccomp=seccomp,
            no_new_privs=no_new_privs,
        )

    def test_static_bundle_keeps_isolation_webrtc_acceleration_and_alpha(self):
        _verify_static_runtime_invariants()

    def test_post_commit_security_delta_uses_pid_incarnations_not_commands(self):
        onboarding = self.sample((self.process(10, 100), self.process(20, 200, 2, 1)))
        active = self.sample(
            (
                self.process(10, 100),
                self.process(20, 200, 2, 1),
                self.process(30, 300, 2, 1),
                self.process(40, 400, 2, 0),
            )
        )
        self.assertEqual(1, _new_secure_process_count(onboarding, active))

        reused = self.sample((self.process(20, 999, 2, 1),))
        self.assertEqual(1, _new_secure_process_count(onboarding, reused))

    def test_candidate_profile_is_exact_and_must_reach_prefs(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            root = Path(temporary)
            profile = root / "profile"
            _prepare_profile(profile, "gc-empty-chunks-0")
            self.assertFalse(_verify_profile_preference(profile, "gc-empty-chunks-0"))
            (profile / "prefs.js").write_text(_CANDIDATE_PREFS_LINE + "\n", encoding="utf-8")
            self.assertTrue(_verify_profile_preference(profile, "gc-empty-chunks-0"))
            with self.assertRaisesRegex(NativeAppShellBenchmarkError, "candidate preference"):
                _prepare_profile(profile, "baseline")

    def test_worker_status_is_closed_and_contains_no_persistable_browser_data(self):
        status = {
            "browserPid": 123,
            "contentCommitted": False,
            "failureCode": None,
            "handshakeReady": True,
            "onboardingMilliseconds": 1000,
            "preferenceVerified": False,
            "state": "onboarding",
            "usableMilliseconds": None,
            "workerStatusVersion": 1,
        }
        self.assertEqual(status, _validate_worker_status(status))
        with self.assertRaisesRegex(NativeAppShellBenchmarkError, "fields"):
            _validate_worker_status({**status, "url": "https://private.invalid/"})
        with self.assertRaisesRegex(NativeAppShellBenchmarkError, "failure code"):
            _validate_worker_status({**status, "failureCode": "private diagnostic"})


if __name__ == "__main__":
    unittest.main()
