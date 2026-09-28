import copy
from dataclasses import replace
import hashlib
import json
import unittest

from zen_app_runtime.benchmark_runner import CgroupSample, ProcessMemorySample
from zen_app_runtime.discord_conformance import build_discord_conformance_suite
from zen_app_runtime.gecko_memory import (
    GECKO_MEMORY_ROLES,
    GeckoMemoryError,
    attribute_gecko_memory,
    build_gecko_process_role_evidence,
    cgroup_sample_sha256,
    gecko_memory_attribution_from_dict,
    gecko_memory_attribution_sha256,
    gecko_memory_attribution_to_dict,
    gecko_process_role_evidence_from_dict,
    gecko_process_role_evidence_sha256,
    gecko_process_role_evidence_to_dict,
    render_gecko_memory_attribution,
    render_gecko_process_role_evidence,
)
from zen_app_runtime.runtime_driver import build_zen_runtime_driver_descriptor


class GeckoMemoryTests(unittest.TestCase):
    def setUp(self):
        self.suite = build_discord_conformance_suite()
        self.driver = build_zen_runtime_driver_descriptor(self.suite)
        self.sample = CgroupSample(
            sample_version=1,
            control_group="/user.slice/example.scope",
            started_monotonic_ns=1_000_000_000,
            completed_monotonic_ns=1_002_000_000,
            memory_current_bytes=400 * 1024,
            memory_peak_bytes=500 * 1024,
            memory_swap_current_bytes=4 * 1024,
            memory_stat=(("anon", 300_000), ("file", 80_000), ("kernel", 20_000)),
            cpu_stat=(("system_usec", 20), ("usage_usec", 100), ("user_usec", 80)),
            pids_current=8,
            processes=(
                self.process(101, 1001, 20, 15, 10, 0, 0, 0),
                self.process(102, 1002, 80, 50, 30, 1, 0, 0),
                self.process(103, 1003, 180, 120, 90, 2, 2, 1),
                self.process(104, 1004, 210, 140, 100, 3, 2, 1),
                self.process(105, 1005, 100, 70, 50, 0, 2, 1),
            ),
        )
        self.roles = {
            101: "runtime-supervisor",
            102: "browser-parent",
            103: "web-content",
            104: "web-content",
            105: "gpu",
        }

    @staticmethod
    def process(pid, start, rss, pss, private, swap, seccomp, no_new_privs):
        return ProcessMemorySample(
            pid=pid,
            start_time_ticks=start,
            state="S",
            rss_kib=rss,
            pss_kib=pss,
            private_kib=private,
            swap_pss_kib=swap,
            seccomp=seccomp,
            no_new_privs=no_new_privs,
        )

    def evidence(self, roles=None):
        return build_gecko_process_role_evidence(
            self.sample,
            self.roles if roles is None else roles,
            driver=self.driver,
            suite=self.suite,
        )

    def test_role_evidence_is_canonical_exact_sample_and_driver_bound(self):
        evidence = self.evidence()
        data = gecko_process_role_evidence_to_dict(
            evidence,
            sample=self.sample,
            driver=self.driver,
            suite=self.suite,
        )
        self.assertEqual(1, data["geckoProcessRoleEvidenceVersion"])
        self.assertEqual(cgroup_sample_sha256(self.sample), data["cgroupSampleSha256"])
        self.assertEqual("runtime-driver-privileged-process-info-v1", data["source"])
        self.assertEqual([101, 102, 103, 104, 105], [item["pid"] for item in data["processes"]])
        self.assertEqual([1001, 1002, 1003, 1004, 1005], [item["startTimeTicks"] for item in data["processes"]])
        rendered = render_gecko_process_role_evidence(
            evidence,
            sample=self.sample,
            driver=self.driver,
            suite=self.suite,
        )
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            gecko_process_role_evidence_sha256(
                evidence,
                sample=self.sample,
                driver=self.driver,
                suite=self.suite,
            ),
        )
        self.assertEqual(
            evidence,
            gecko_process_role_evidence_from_dict(
                copy.deepcopy(data),
                sample=self.sample,
                driver=self.driver,
                suite=self.suite,
            ),
        )

    def test_role_evidence_requires_every_exact_pid_incarnation_once_and_one_parent(self):
        with self.assertRaisesRegex(GeckoMemoryError, "exactly match"):
            self.evidence({pid: role for pid, role in self.roles.items() if pid != 105})
        with self.assertRaisesRegex(GeckoMemoryError, "unknown role"):
            self.evidence({**self.roles, 105: "discord-private-tab"})
        with self.assertRaisesRegex(GeckoMemoryError, "browser-parent"):
            self.evidence({**self.roles, 102: "utility"})

        data = gecko_process_role_evidence_to_dict(
            self.evidence(),
            sample=self.sample,
            driver=self.driver,
            suite=self.suite,
        )
        invalid = copy.deepcopy(data)
        invalid["processes"][2]["startTimeTicks"] += 1
        with self.assertRaisesRegex(GeckoMemoryError, "incarnation"):
            gecko_process_role_evidence_from_dict(
                invalid,
                sample=self.sample,
                driver=self.driver,
                suite=self.suite,
            )
        invalid = copy.deepcopy(data)
        invalid["processes"].append(copy.deepcopy(invalid["processes"][-1]))
        with self.assertRaisesRegex(GeckoMemoryError, "sorted and unique"):
            gecko_process_role_evidence_from_dict(
                invalid,
                sample=self.sample,
                driver=self.driver,
                suite=self.suite,
            )

    def test_attribution_aggregates_coarse_roles_and_exactly_reconciles_total_pss(self):
        evidence = self.evidence()
        attribution = attribute_gecko_memory(
            self.sample,
            evidence,
            driver=self.driver,
            suite=self.suite,
        )
        data = gecko_memory_attribution_to_dict(
            attribution,
            sample=self.sample,
            evidence=evidence,
            driver=self.driver,
            suite=self.suite,
        )
        self.assertEqual(1, data["geckoMemoryAttributionVersion"])
        self.assertEqual("complete", data["status"])
        self.assertEqual(0, data["unknownProcessCount"])
        self.assertEqual(list(GECKO_MEMORY_ROLES), [item["role"] for item in data["roles"]])
        by_role = {item["role"]: item for item in data["roles"]}
        self.assertEqual(2, by_role["web-content"]["processCount"])
        self.assertEqual(260, by_role["web-content"]["pssKiB"])
        self.assertEqual(190, by_role["web-content"]["privateKiB"])
        self.assertEqual(5, by_role["web-content"]["swapPssKiB"])
        self.assertEqual(2, by_role["web-content"]["seccompFilteredCount"])
        self.assertEqual(2, by_role["web-content"]["noNewPrivsCount"])
        self.assertEqual(self.sample.summed_pss_kib, data["summedProcessMemory"]["pssKiB"])
        self.assertEqual(395, data["summedProcessMemory"]["pssKiB"])
        self.assertEqual(len(self.sample.processes), data["summedProcessMemory"]["processCount"])
        self.assertEqual(self.sample.memory_current_bytes, data["wholeCgroup"]["memoryCurrentBytes"])
        self.assertEqual(self.sample.pids_current, data["wholeCgroup"]["pidsCurrent"])
        self.assertEqual(
            data["summedProcessMemory"]["pssKiB"],
            sum(item["pssKiB"] for item in data["roles"]),
        )

    def test_unknown_role_is_visible_partial_and_cannot_hide_memory(self):
        roles = dict(self.roles)
        roles[105] = "unknown"
        evidence = self.evidence(roles)
        attribution = attribute_gecko_memory(
            self.sample,
            evidence,
            driver=self.driver,
            suite=self.suite,
        )
        data = gecko_memory_attribution_to_dict(
            attribution,
            sample=self.sample,
            evidence=evidence,
            driver=self.driver,
            suite=self.suite,
        )
        by_role = {item["role"]: item for item in data["roles"]}
        self.assertEqual("partial", data["status"])
        self.assertEqual(1, data["unknownProcessCount"])
        self.assertEqual(70, by_role["unknown"]["pssKiB"])
        self.assertEqual(395, data["summedProcessMemory"]["pssKiB"])
        self.assertEqual(395, sum(item["pssKiB"] for item in data["roles"]))

    def test_persistable_attribution_has_no_per_process_or_private_browser_data(self):
        evidence = self.evidence()
        attribution = attribute_gecko_memory(
            self.sample,
            evidence,
            driver=self.driver,
            suite=self.suite,
        )
        rendered = render_gecko_memory_attribution(
            attribution,
            sample=self.sample,
            evidence=evidence,
            driver=self.driver,
            suite=self.suite,
        )
        lowered = rendered.lower()
        for forbidden in (
            '"pid"',
            "starttimeticks",
            "processname",
            "commandline",
            "cmdline",
            "environment",
            "origin",
            "url",
            "message",
            "maps",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("example.scope", rendered)
        data = gecko_memory_attribution_to_dict(
            attribution,
            sample=self.sample,
            evidence=evidence,
            driver=self.driver,
            suite=self.suite,
        )
        self.assertEqual(
            attribution,
            gecko_memory_attribution_from_dict(
                copy.deepcopy(data),
                sample=self.sample,
                evidence=evidence,
                driver=self.driver,
                suite=self.suite,
            ),
        )
        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            gecko_memory_attribution_sha256(
                attribution,
                sample=self.sample,
                evidence=evidence,
                driver=self.driver,
                suite=self.suite,
            ),
        )

    def test_private_or_engine_raw_fields_and_report_tampering_fail_closed(self):
        evidence = self.evidence()
        evidence_data = gecko_process_role_evidence_to_dict(
            evidence,
            sample=self.sample,
            driver=self.driver,
            suite=self.suite,
        )
        for field, value in (
            ("origin", "https://private.example"),
            ("processName", "private account"),
            ("commandLine", ["--private"]),
        ):
            invalid = copy.deepcopy(evidence_data)
            invalid["processes"][2][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(GeckoMemoryError, "unknown"):
                gecko_process_role_evidence_from_dict(
                    invalid,
                    sample=self.sample,
                    driver=self.driver,
                    suite=self.suite,
                )

        attribution = attribute_gecko_memory(
            self.sample,
            evidence,
            driver=self.driver,
            suite=self.suite,
        )
        report = gecko_memory_attribution_to_dict(
            attribution,
            sample=self.sample,
            evidence=evidence,
            driver=self.driver,
            suite=self.suite,
        )
        invalid = copy.deepcopy(report)
        invalid["roles"][2]["pssKiB"] -= 1
        with self.assertRaisesRegex(GeckoMemoryError, "bound evidence"):
            gecko_memory_attribution_from_dict(
                invalid,
                sample=self.sample,
                evidence=evidence,
                driver=self.driver,
                suite=self.suite,
            )
        invalid = copy.deepcopy(report)
        invalid["roles"].pop()
        with self.assertRaisesRegex(GeckoMemoryError, "exact role order"):
            gecko_memory_attribution_from_dict(
                invalid,
                sample=self.sample,
                evidence=evidence,
                driver=self.driver,
                suite=self.suite,
            )

    def test_sample_and_driver_rebinding_is_rejected(self):
        evidence = self.evidence()
        changed_process = replace(self.sample.processes[2], pss_kib=121)
        changed_sample = replace(
            self.sample,
            processes=(
                self.sample.processes[0],
                self.sample.processes[1],
                changed_process,
                self.sample.processes[3],
                self.sample.processes[4],
            ),
        )
        with self.assertRaisesRegex(GeckoMemoryError, "cgroup sample"):
            gecko_process_role_evidence_to_dict(
                evidence,
                sample=changed_sample,
                driver=self.driver,
                suite=self.suite,
            )
        changed_driver = replace(self.driver, driver_id="other-gecko-driver")
        with self.assertRaisesRegex(GeckoMemoryError, "driver"):
            gecko_process_role_evidence_to_dict(
                evidence,
                sample=self.sample,
                driver=changed_driver,
                suite=self.suite,
            )


if __name__ == "__main__":
    unittest.main()
