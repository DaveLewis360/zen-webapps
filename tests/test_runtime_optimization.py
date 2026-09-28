import json
import unittest
from dataclasses import replace

from zen_app_runtime.benchmark_runner import CgroupSample, ProcessMemorySample
from zen_app_runtime.runtime_optimization import (
    APP_SHELL_OPTIMIZATION_REPETITIONS,
    AppShellOptimizationTrial,
    AppShellScopeEvidence,
    RuntimeOptimizationError,
    app_shell_optimization_report_from_dict,
    app_shell_optimization_report_sha256,
    app_shell_optimization_report_to_dict,
    build_app_shell_optimization_report,
    build_scope_evidence,
    evaluate_app_shell_optimization,
    render_app_shell_optimization_report,
)


class RuntimeOptimizationTests(unittest.TestCase):
    def scope(self, pss):
        return AppShellScopeEvidence(
            evidence_version=1,
            process_count=8,
            pss_kib=pss,
            rss_kib=pss * 2,
            private_kib=pss // 2,
            swap_pss_kib=0,
            memory_current_bytes=pss * 1024,
            memory_peak_bytes=(pss + 1000) * 1024,
            memory_swap_current_bytes=0,
            seccomp_filtered_count=4,
            no_new_privs_count=4,
        )

    def trial(self, variant, pair_index, startup_kind):
        candidate = variant == "gc-empty-chunks-0"
        delta = pair_index * 100
        return AppShellOptimizationTrial(
            trial_version=1,
            variant=variant,
            pair_index=pair_index,
            startup_kind=startup_kind,
            onboarding_milliseconds=900 + delta,
            usable_milliseconds=(1800 if startup_kind == "cold" else 1400) + delta,
            onboarding=self.scope((105_000 if candidate else 110_000) + delta),
            active=self.scope((290_000 if candidate else 300_000) + delta),
            idle=self.scope((270_000 if candidate else 280_000) + delta),
            idle_cpu_basis_points=25 + pair_index,
            post_commit_new_secure_process_count=1,
            preference_value=0 if candidate else 1,
            fission_enabled=True,
            remote_content_enabled=True,
            content_sandbox_level=6,
            socket_sandbox_level=2,
            native_wayland=True,
            hardware_acceleration_disable_pref_absent=True,
            transparent_surface_configured=True,
            cleanup_complete=True,
        )

    def trials(self):
        return tuple(
            self.trial(variant, pair_index, startup_kind)
            for pair_index in range(1, APP_SHELL_OPTIMIZATION_REPETITIONS + 1)
            for variant in ("baseline", "gc-empty-chunks-0")
            for startup_kind in ("cold", "warm")
        )

    def report(self):
        return build_app_shell_optimization_report(
            runtime_sha256="a" * 64,
            measured_adapter_tree_sha256="b" * 64,
            app_shell_version="1.4.0",
            trials=self.trials(),
        )

    def test_exact_paired_matrix_accepts_measurable_memory_reduction(self):
        evaluation = evaluate_app_shell_optimization(self.trials())
        self.assertEqual("accepted", evaluation.status)
        self.assertEqual((), evaluation.reasons)
        self.assertEqual(300_200, evaluation.baseline.active_pss_kib.median)
        self.assertEqual(290_200, evaluation.candidate.active_pss_kib.median)
        self.assertEqual(3, evaluation.candidate.cold_usable_milliseconds.sample_count)

    def test_hard_gates_security_and_cleanup_fail_closed(self):
        trials = list(self.trials())
        candidate_index = next(
            index for index, trial in enumerate(trials) if trial.variant == "gc-empty-chunks-0"
        )
        broken = replace(
            trials[candidate_index],
            active=self.scope(400_000),
            fission_enabled=False,
            post_commit_new_secure_process_count=0,
            cleanup_complete=False,
        )
        trials[candidate_index] = broken
        evaluation = evaluate_app_shell_optimization(trials)
        self.assertEqual("rejected", evaluation.status)
        self.assertIn("active-pss-hard-gate", evaluation.reasons)
        self.assertIn("runtime-security-invariant", evaluation.reasons)
        self.assertIn("content-security-invariant", evaluation.reasons)
        self.assertIn("cleanup-incomplete", evaluation.reasons)

    def test_neutral_cpu_startup_but_no_memory_gain_is_rejected(self):
        trials = tuple(
            replace(
                trial,
                onboarding=self.scope(110_000 + trial.pair_index * 100),
                active=self.scope(300_000 + trial.pair_index * 100),
                idle=self.scope(280_000 + trial.pair_index * 100),
            )
            if trial.variant == "gc-empty-chunks-0"
            else trial
            for trial in self.trials()
        )
        evaluation = evaluate_app_shell_optimization(trials)
        self.assertEqual("rejected", evaluation.status)
        self.assertIn("memory-reduction-below-threshold", evaluation.reasons)

    def test_report_round_trip_is_canonical_bound_and_privacy_safe(self):
        report = self.report()
        data = app_shell_optimization_report_to_dict(report)
        parsed = app_shell_optimization_report_from_dict(data)
        self.assertEqual(report, parsed)
        rendered = render_app_shell_optimization_report(report)
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(64, len(app_shell_optimization_report_sha256(report)))
        lowered = rendered.lower()
        for forbidden in (
            '"pid"',
            "cgroup",
            '"path"',
            '"url"',
            "account",
            "message",
            "screenshot",
            "cmdline",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_report_rejects_trial_summary_and_preference_tampering(self):
        data = app_shell_optimization_report_to_dict(self.report())
        data["evaluation"]["candidate"]["activePssKiB"]["median"] += 1
        with self.assertRaisesRegex(RuntimeOptimizationError, "tampered"):
            app_shell_optimization_report_from_dict(data)

        data = app_shell_optimization_report_to_dict(self.report())
        data["preference"]["candidateValue"] = 1
        with self.assertRaisesRegex(RuntimeOptimizationError, "preference authority"):
            app_shell_optimization_report_from_dict(data)

    def test_matrix_rejects_missing_duplicate_and_unknown_trials(self):
        trials = self.trials()
        with self.assertRaisesRegex(RuntimeOptimizationError, "exact paired"):
            evaluate_app_shell_optimization(trials[:-1])
        with self.assertRaises(RuntimeOptimizationError):
            evaluate_app_shell_optimization((*trials[:-1], trials[0]))
        with self.assertRaisesRegex(RuntimeOptimizationError, "variant"):
            evaluate_app_shell_optimization((replace(trials[0], variant="other"), *trials[1:]))

    def test_scope_projection_uses_authoritative_sums_but_drops_process_identity(self):
        sample = CgroupSample(
            sample_version=1,
            control_group="/private.scope",
            started_monotonic_ns=1,
            completed_monotonic_ns=2,
            memory_current_bytes=4096,
            memory_peak_bytes=8192,
            memory_swap_current_bytes=0,
            memory_stat=(("anon", 1), ("file", 2), ("kernel", 3)),
            cpu_stat=(("system_usec", 1), ("usage_usec", 3), ("user_usec", 2)),
            pids_current=1,
            processes=(
                ProcessMemorySample(
                    pid=999,
                    start_time_ticks=100,
                    state="S",
                    rss_kib=20,
                    pss_kib=10,
                    private_kib=7,
                    swap_pss_kib=0,
                    seccomp=2,
                    no_new_privs=1,
                ),
            ),
        )
        scope = build_scope_evidence(sample)
        self.assertEqual(10, scope.pss_kib)
        self.assertEqual(1, scope.seccomp_filtered_count)
        self.assertFalse(hasattr(scope, "pid"))
        self.assertFalse(hasattr(scope, "control_group"))


if __name__ == "__main__":
    unittest.main()
