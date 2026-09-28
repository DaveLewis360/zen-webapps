"""Opt-in exact-Zen cgroup-v2 App Shell optimization benchmark."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from zen_app_runtime.native_app_shell_benchmark import run_native_app_shell_optimization
from zen_app_runtime.runtime_optimization import (
    APP_SHELL_OPTIMIZATION_REPETITIONS,
    app_shell_optimization_report_from_dict,
)
import json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_RUNTIME = os.environ.get("ZEN_REAL_RUNTIME")
REAL_OPTIMIZATION = os.environ.get("ZEN_REAL_APP_SHELL_OPTIMIZATION") == "1"


@unittest.skipUnless(
    REAL_RUNTIME and REAL_OPTIMIZATION,
    "set ZEN_REAL_RUNTIME and ZEN_REAL_APP_SHELL_OPTIMIZATION=1 for the native A/B benchmark",
)
class RealAppShellOptimizationTests(unittest.TestCase):
    def test_exact_wayland_cold_warm_baseline_candidate_matrix(self) -> None:
        runtime = Path(REAL_RUNTIME or "")
        digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            output = Path(temporary) / "optimization-report.json"
            report = run_native_app_shell_optimization(
                runtime=runtime,
                runtime_sha256=digest,
                output_path=output,
            )
            parsed = app_shell_optimization_report_from_dict(
                json.loads(output.read_text(encoding="ascii"))
            )
            self.assertEqual(report, parsed)
            self.assertEqual(4 * APP_SHELL_OPTIMIZATION_REPETITIONS, len(report.trials))
            self.assertTrue(all(trial.cleanup_complete for trial in report.trials))
            self.assertTrue(
                all(
                    trial.post_commit_new_secure_process_count >= 1
                    for trial in report.trials
                )
            )
            print(
                json.dumps(
                    {
                        "activeCandidateMedianKiB": report.evaluation.candidate.active_pss_kib.median,
                        "activeBaselineMedianKiB": report.evaluation.baseline.active_pss_kib.median,
                        "decision": report.evaluation.status,
                        "reasons": report.evaluation.reasons,
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    unittest.main()
