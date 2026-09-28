import copy
import hashlib
import json
import unittest

from zen_app_runtime.benchmark_contract import (
    BenchmarkContractError,
    benchmark_acceptance_to_dict,
    benchmark_result_from_dict,
    benchmark_result_sha256,
    benchmark_result_to_dict,
    benchmark_scenario_from_dict,
    benchmark_scenario_sha256,
    benchmark_scenario_to_dict,
    build_benchmark_scenario,
    evaluate_benchmark_result,
    render_benchmark_acceptance,
    render_benchmark_result,
    render_benchmark_scenario,
)


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        self.scenario = build_benchmark_scenario()
        self.scenario_data = benchmark_scenario_to_dict(self.scenario)

    def environment(self):
        return {
            "environmentVersion": 1,
            "hardware": {
                "architecture": "x86_64",
                "cpuModel": "Example 16-Core CPU",
                "logicalCpuCount": 16,
                "physicalMemoryBytes": 32 * 1024 * 1024 * 1024,
            },
            "kernel": {"release": "6.12.0-example"},
            "operatingSystem": {"id": "example-linux", "versionId": "1"},
            "runtime": {
                "adapterTreeSha256": "b" * 64,
                "appShellBuildId": "20260830030000",
                "appShellVersion": "1.3.0",
                "driverId": "zen-gecko",
                "driverProtocolVersion": 2,
                "engine": "gecko",
                "engineVersion": "142.0",
                "identity": {
                    "identityVersion": 1,
                    "path": "/opt/zen-browser-bin/zen-bin",
                    "sha256": "a" * 64,
                },
            },
            "session": {
                "compositor": {"name": "Hyprland", "version": "0.50.1"},
                "portal": {
                    "desktop": "Hyprland",
                    "implementation": "xdg-desktop-portal-hyprland",
                    "version": "1.3.10",
                },
                "type": "wayland",
            },
        }

    def complete_result_data(self, *, measured=True, checks_pass=True):
        values = {
            ("cold-start", "usable-time"): (2500, 2300, 3000),
            ("warm-start", "usable-time"): (1800, 1600, 2200),
            ("onboarding", "summed-pss"): (118000, 116000, 122880),
            ("active", "summed-pss"): (345000, 340000, 358400),
            ("active", "cpu"): (250, 220, 290),
            ("idle", "summed-pss"): (310000, 305000, 320000),
            ("idle", "cpu"): (70, 60, 99),
            ("voice", "summed-pss"): (330000, 320000, 340000),
            ("voice", "cpu"): (900, 850, 950),
            ("video", "summed-pss"): (340000, 330000, 350000),
            ("video", "cpu"): (1800, 1700, 1900),
            ("screen-share", "summed-pss"): (350000, 340000, 360000),
            ("screen-share", "cpu"): (2200, 2100, 2300),
            ("soak", "summed-pss-start"): (101000, 100000, 102000),
            ("soak", "summed-pss-end"): (109000, 108000, 109999),
        }
        phases = []
        for phase in self.scenario_data["phases"]:
            observations = []
            if measured:
                for metric in phase["requiredMetrics"]:
                    value, lower, upper = values[(phase["phaseId"], metric["metricId"])]
                    observations.append(
                        {
                            "confidence95": {"lower": lower, "upper": upper},
                            "metricId": metric["metricId"],
                            "sampleCount": max(phase["repetitions"], 5),
                            "value": value,
                        }
                    )
            phases.append(
                {
                    "observations": observations,
                    "phaseId": phase["phaseId"],
                    "status": "measured" if measured else "not-run",
                }
            )
        checks = [
            {
                "checkId": check_id,
                "status": "passed" if checks_pass else "not-run",
            }
            for check_id in self.scenario_data["requiredChecks"]
        ]
        return {
            "benchmarkResultVersion": 1,
            "checks": checks,
            "completedAtUtc": "2026-08-31T10:01:00Z",
            "environment": self.environment(),
            "phases": phases,
            "scenarioSha256": benchmark_scenario_sha256(self.scenario),
            "startedAtUtc": "2026-08-30T10:00:00Z",
        }

    def test_default_scenario_is_closed_versioned_and_encodes_every_required_phase(self):
        data = self.scenario_data
        self.assertEqual(1, data["benchmarkScenarioVersion"])
        self.assertEqual("discord-app-shell-acceptance-v1", data["scenarioId"])
        self.assertEqual(
            [
                "cold-start",
                "warm-start",
                "onboarding",
                "active",
                "idle",
                "voice",
                "video",
                "screen-share",
                "soak",
            ],
            [phase["phaseId"] for phase in data["phases"]],
        )
        self.assertEqual("cold", data["phases"][0]["cacheState"])
        self.assertEqual("warm", data["phases"][1]["cacheState"])
        self.assertEqual(600, data["phases"][3]["measurementOffsetSeconds"])
        self.assertEqual(24 * 60 * 60, data["phases"][-1]["durationSeconds"])
        self.assertEqual(
            ["summed-pss-start", "summed-pss-end"],
            [metric["metricId"] for metric in data["phases"][-1]["requiredMetrics"]],
        )

        gates = {gate["gateId"]: gate for gate in data["hardGates"]}
        self.assertEqual(3000, gates["cold-start-usable-time"]["threshold"]["value"])
        self.assertEqual(3000, gates["warm-start-usable-time"]["threshold"]["value"])
        self.assertEqual(120 * 1024, gates["onboarding-pss"]["threshold"]["value"])
        self.assertEqual(350 * 1024, gates["active-pss"]["threshold"]["value"])
        self.assertEqual("less-than", gates["idle-cpu"]["threshold"]["comparison"])
        self.assertEqual(100, gates["idle-cpu"]["threshold"]["value"])
        self.assertEqual(10, gates["soak-pss-growth"]["threshold"]["value"])
        self.assertEqual(
            {
                "alpha-surface",
                "content-sandbox-enforced",
                "discord-web-parity",
                "fission-enabled",
                "general-linux-wayland",
                "native-wayland",
                "no-new-privileges",
                "permission-mediated",
                "release-debug-authority-disabled",
                "screen-share-transient-activation",
            },
            set(data["requiredChecks"]),
        )

    def test_scenario_round_trip_and_render_are_canonical(self):
        parsed = benchmark_scenario_from_dict(copy.deepcopy(self.scenario_data))
        self.assertEqual(self.scenario, parsed)
        rendered = render_benchmark_scenario(parsed)
        self.assertEqual(
            json.dumps(self.scenario_data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            benchmark_scenario_sha256(parsed),
        )
        self.assertTrue(rendered.endswith("\n"))
        self.assertFalse(rendered.endswith("\n\n"))

    def test_scenario_rejects_unknown_fields_versions_reordering_and_non_integer_limits(self):
        invalid = copy.deepcopy(self.scenario_data)
        invalid["unknown"] = True
        with self.assertRaisesRegex(BenchmarkContractError, "unknown"):
            benchmark_scenario_from_dict(invalid)

        invalid = copy.deepcopy(self.scenario_data)
        invalid["benchmarkScenarioVersion"] = 2
        with self.assertRaisesRegex(BenchmarkContractError, "benchmarkScenarioVersion"):
            benchmark_scenario_from_dict(invalid)

        invalid = copy.deepcopy(self.scenario_data)
        invalid["phases"][0], invalid["phases"][1] = invalid["phases"][1], invalid["phases"][0]
        with self.assertRaisesRegex(BenchmarkContractError, "phase"):
            benchmark_scenario_from_dict(invalid)

        invalid = copy.deepcopy(self.scenario_data)
        invalid["hardGates"][0]["threshold"]["value"] = 3000.0
        with self.assertRaisesRegex(BenchmarkContractError, "integer"):
            benchmark_scenario_from_dict(invalid)

    def test_result_round_trip_records_host_portal_compositor_and_exact_runtime_identity(self):
        data = self.complete_result_data()
        result = benchmark_result_from_dict(copy.deepcopy(data), scenario=self.scenario)
        self.assertEqual(data, benchmark_result_to_dict(result, scenario=self.scenario))
        rendered = render_benchmark_result(result, scenario=self.scenario)
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            benchmark_result_sha256(result, scenario=self.scenario),
        )
        environment = benchmark_result_to_dict(result, scenario=self.scenario)["environment"]
        self.assertEqual("6.12.0-example", environment["kernel"]["release"])
        self.assertEqual("Hyprland", environment["session"]["compositor"]["name"])
        self.assertEqual(
            "xdg-desktop-portal-hyprland",
            environment["session"]["portal"]["implementation"],
        )
        self.assertEqual("a" * 64, environment["runtime"]["identity"]["sha256"])
        self.assertNotIn("hostname", environment)
        self.assertNotIn("machineId", environment)
        self.assertNotIn("username", environment)

    def test_result_schema_is_closed_scenario_bound_and_requires_exact_phase_metrics_and_checks(self):
        invalid = self.complete_result_data()
        invalid["environment"]["hardware"]["hostname"] = "private-host"
        with self.assertRaisesRegex(BenchmarkContractError, "unknown"):
            benchmark_result_from_dict(invalid, scenario=self.scenario)

        invalid = self.complete_result_data()
        invalid["scenarioSha256"] = "f" * 64
        with self.assertRaisesRegex(BenchmarkContractError, "scenario"):
            benchmark_result_from_dict(invalid, scenario=self.scenario)

        invalid = self.complete_result_data()
        invalid["phases"][0]["observations"] = []
        with self.assertRaisesRegex(BenchmarkContractError, "observations"):
            benchmark_result_from_dict(invalid, scenario=self.scenario)

        invalid = self.complete_result_data()
        invalid["phases"][0]["observations"][0]["confidence95"]["upper"] = 2000
        with self.assertRaisesRegex(BenchmarkContractError, "confidence"):
            benchmark_result_from_dict(invalid, scenario=self.scenario)

        invalid = self.complete_result_data()
        invalid["checks"] = invalid["checks"][:-1]
        with self.assertRaisesRegex(BenchmarkContractError, "check"):
            benchmark_result_from_dict(invalid, scenario=self.scenario)

    def test_hard_gate_evaluation_uses_upper_confidence_bounds_and_exact_strictness(self):
        result = benchmark_result_from_dict(self.complete_result_data(), scenario=self.scenario)
        acceptance = evaluate_benchmark_result(self.scenario, result)
        self.assertEqual("pass", acceptance.overall_status)
        statuses = {gate.gate_id: gate.status for gate in acceptance.gates}
        self.assertTrue(statuses)
        self.assertEqual({"pass"}, set(statuses.values()))

        invalid = self.complete_result_data()
        idle = next(phase for phase in invalid["phases"] if phase["phaseId"] == "idle")
        idle_cpu = next(item for item in idle["observations"] if item["metricId"] == "cpu")
        idle_cpu["confidence95"]["upper"] = 100
        idle_cpu["value"] = 100
        result = benchmark_result_from_dict(invalid, scenario=self.scenario)
        acceptance = evaluate_benchmark_result(self.scenario, result)
        statuses = {gate.gate_id: gate.status for gate in acceptance.gates}
        self.assertEqual("fail", statuses["idle-cpu"])
        self.assertEqual("fail", acceptance.overall_status)

        invalid = self.complete_result_data()
        soak = next(phase for phase in invalid["phases"] if phase["phaseId"] == "soak")
        soak_end = next(item for item in soak["observations"] if item["metricId"] == "summed-pss-end")
        soak_end["confidence95"]["upper"] = 110000
        soak_end["value"] = 109000
        result = benchmark_result_from_dict(invalid, scenario=self.scenario)
        acceptance = evaluate_benchmark_result(self.scenario, result)
        statuses = {gate.gate_id: gate.status for gate in acceptance.gates}
        self.assertEqual("fail", statuses["soak-pss-growth"])

    def test_unmeasured_result_is_incomplete_not_a_false_pass(self):
        result = benchmark_result_from_dict(
            self.complete_result_data(measured=False, checks_pass=False),
            scenario=self.scenario,
        )
        acceptance = evaluate_benchmark_result(self.scenario, result)
        self.assertEqual("incomplete", acceptance.overall_status)
        self.assertEqual({"not-measured"}, {gate.status for gate in acceptance.gates})

    def test_acceptance_is_canonical_and_bound_to_both_documents(self):
        result = benchmark_result_from_dict(self.complete_result_data(), scenario=self.scenario)
        acceptance = evaluate_benchmark_result(self.scenario, result)
        data = benchmark_acceptance_to_dict(acceptance)
        self.assertEqual(1, data["benchmarkAcceptanceVersion"])
        self.assertEqual(benchmark_scenario_sha256(self.scenario), data["scenarioSha256"])
        self.assertEqual(
            benchmark_result_sha256(result, scenario=self.scenario),
            data["resultSha256"],
        )
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            render_benchmark_acceptance(acceptance),
        )


if __name__ == "__main__":
    unittest.main()
