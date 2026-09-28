import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from zen_app_runtime.benchmark_runner import (
    BenchmarkRunnerError,
    CgroupV2Reader,
    aggregate_integer_observation,
    build_systemd_scope_plan,
    cgroup_cpu_basis_points,
    cgroup_sample_to_dict,
    launch_systemd_scope,
    render_cgroup_sample,
    systemd_scope_argv,
)


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


class BenchmarkRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.cgroup_root = self.root / "cgroup"
        self.proc_root = self.root / "proc"
        self.scope = self.cgroup_root / "bench.scope"
        self.cgroup_root.mkdir()
        self.proc_root.mkdir()
        self.scope.mkdir()
        self.write(self.cgroup_root / "cgroup.controllers", "cpu memory pids\n")
        self.write(self.scope / "cgroup.procs", "101\n102\n")
        self.write(self.scope / "memory.current", "1048576\n")
        self.write(self.scope / "memory.peak", "2097152\n")
        self.write(self.scope / "memory.swap.current", "4096\n")
        self.write(
            self.scope / "memory.stat",
            "anon 700000\nfile 200000\nkernel 100000\nshmem 50000\nslab 40000\n",
        )
        self.write(
            self.scope / "cpu.stat",
            "usage_usec 250000\nuser_usec 200000\nsystem_usec 50000\nnice_usec 0\n",
        )
        self.write(self.scope / "pids.current", "7\n")
        self.make_process(
            101,
            start_time=1001,
            rss=100,
            pss=60,
            private_clean=10,
            private_dirty=20,
            private_hugetlb=1,
            swap_pss=2,
            seccomp=2,
            no_new_privs=1,
        )
        self.make_process(
            102,
            start_time=1002,
            rss=200,
            pss=140,
            private_clean=20,
            private_dirty=30,
            private_hugetlb=2,
            swap_pss=3,
            seccomp=0,
            no_new_privs=0,
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(path, text):
        path.write_text(text, encoding="utf-8")

    def make_process(
        self,
        pid,
        *,
        start_time,
        rss,
        pss,
        private_clean,
        private_dirty,
        private_hugetlb,
        swap_pss,
        seccomp,
        no_new_privs,
    ):
        process = self.proc_root / str(pid)
        process.mkdir()
        # Fields after comm start at field 3. starttime is field 22, index 19 here.
        post_comm = ["S", "1", str(pid), str(pid), "0", "0", "0", "0", "0", "0"]
        post_comm.extend(["0"] * 9)
        post_comm.append(str(start_time))
        post_comm.extend(["0"] * 8)
        self.write(process / "stat", f"{pid} (private name {pid}) " + " ".join(post_comm) + "\n")
        self.write(
            process / "status",
            f"Name:\tprivate-name-{pid}\nNoNewPrivs:\t{no_new_privs}\nSeccomp:\t{seccomp}\n",
        )
        self.write(
            process / "smaps_rollup",
            "00400000-7fffffffffff ---p 00000000 00:00 0 [rollup]\n"
            f"Rss: {rss} kB\n"
            f"Pss: {pss} kB\n"
            f"Private_Clean: {private_clean} kB\n"
            f"Private_Dirty: {private_dirty} kB\n"
            f"Private_Hugetlb: {private_hugetlb} kB\n"
            f"SwapPss: {swap_pss} kB\n",
        )
        # Deliberately private and never read or serialized by the collector.
        (process / "cmdline").write_bytes(b"browser\0https://private.example/secret\0")

    def reader(self, clock=None):
        return CgroupV2Reader.open(
            self.cgroup_root,
            "/bench.scope",
            proc_root=self.proc_root,
            monotonic_ns=clock,
        )

    def test_stable_sample_sums_process_pss_and_records_whole_cgroup_secondary_evidence(self):
        ticks = iter((1_000_000_000, 1_002_000_000))
        with self.reader(clock=lambda: next(ticks)) as reader:
            sample = reader.sample()
        self.assertEqual("/bench.scope", sample.control_group)
        self.assertEqual(1048576, sample.memory_current_bytes)
        self.assertEqual(2097152, sample.memory_peak_bytes)
        self.assertEqual(4096, sample.memory_swap_current_bytes)
        self.assertEqual(7, sample.pids_current)
        self.assertEqual((101, 102), tuple(item.pid for item in sample.processes))
        self.assertEqual(200, sample.summed_pss_kib)
        self.assertEqual(83, sample.summed_private_kib)
        self.assertEqual(300, sample.summed_rss_kib)
        self.assertEqual(5, sample.summed_swap_pss_kib)
        self.assertEqual(700000, dict(sample.memory_stat)["anon"])
        self.assertEqual(250000, dict(sample.cpu_stat)["usage_usec"])
        self.assertEqual((2, 0), tuple(item.seccomp for item in sample.processes))
        self.assertEqual((1, 0), tuple(item.no_new_privs for item in sample.processes))

    def test_sample_render_is_versioned_canonical_and_excludes_command_lines(self):
        ticks = iter((1_000_000_000, 1_002_000_000))
        with self.reader(clock=lambda: next(ticks)) as reader:
            sample = reader.sample()
        data = cgroup_sample_to_dict(sample)
        self.assertEqual(1, data["cgroupSampleVersion"])
        self.assertEqual(200, data["summedProcessMemory"]["pssKiB"])
        rendered = render_cgroup_sample(sample)
        self.assertEqual(
            json.dumps(data, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            rendered,
        )
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("private-name", rendered)
        self.assertNotIn("cmdline", rendered.lower())

    def test_reader_rejects_path_escape_symlinks_and_missing_or_malformed_controller_data(self):
        with self.assertRaisesRegex(BenchmarkRunnerError, "absolute"):
            CgroupV2Reader.open(self.cgroup_root, "bench.scope", proc_root=self.proc_root)
        with self.assertRaisesRegex(BenchmarkRunnerError, "segment"):
            CgroupV2Reader.open(self.cgroup_root, "/../escape", proc_root=self.proc_root)

        linked = self.cgroup_root / "linked.scope"
        linked.symlink_to(self.scope, target_is_directory=True)
        with self.assertRaises(BenchmarkRunnerError):
            CgroupV2Reader.open(self.cgroup_root, "/linked.scope", proc_root=self.proc_root)

        self.write(self.scope / "memory.stat", "anon 1\nanon 2\n")
        with self.reader() as reader, self.assertRaisesRegex(BenchmarkRunnerError, "duplicate"):
            reader.sample()

    def test_reader_fails_closed_on_incomplete_process_memory_or_pid_identity_change(self):
        (self.proc_root / "102" / "smaps_rollup").unlink()
        with self.reader() as reader, self.assertRaisesRegex(BenchmarkRunnerError, "process 102"):
            reader.sample(max_attempts=1)

        self.make_process(
            103,
            start_time=1003,
            rss=1,
            pss=1,
            private_clean=0,
            private_dirty=1,
            private_hugetlb=0,
            swap_pss=0,
            seccomp=2,
            no_new_privs=1,
        )
        self.write(self.scope / "cgroup.procs", "101\n103\n")
        with self.reader() as reader:
            original = reader._read_process_file
            stat_reads = 0

            def mutate(pid, name, maximum):
                nonlocal stat_reads
                value = original(pid, name, maximum)
                if pid == 103 and name == "stat":
                    stat_reads += 1
                    if stat_reads == 2:
                        return value.replace(" 1003 ", " 9999 ", 1)
                return value

            reader._read_process_file = mutate
            with self.assertRaisesRegex(BenchmarkRunnerError, "identity changed"):
                reader.sample(max_attempts=1)

    def test_reader_accepts_scheduler_state_transition_for_same_pid_incarnation(self):
        ticks = iter((1_000_000_000, 1_002_000_000))
        with self.reader(clock=lambda: next(ticks)) as reader:
            original = reader._read_process_file
            stat_reads = 0

            def transition(pid, name, maximum):
                nonlocal stat_reads
                value = original(pid, name, maximum)
                if pid == 101 and name == "stat":
                    stat_reads += 1
                    if stat_reads == 2:
                        return value.replace(") S ", ") R ", 1)
                return value

            reader._read_process_file = transition
            sample = reader.sample(max_attempts=1)
        process = next(item for item in sample.processes if item.pid == 101)
        self.assertEqual(1001, process.start_time_ticks)
        self.assertEqual("R", process.state)

    def test_cpu_interval_uses_cgroup_usage_and_integer_basis_points(self):
        first_ticks = iter((0, 0))
        with self.reader(clock=lambda: next(first_ticks)) as reader:
            first = reader.sample()
        self.write(
            self.scope / "cpu.stat",
            "usage_usec 500000\nuser_usec 400000\nsystem_usec 100000\nnice_usec 0\n",
        )
        second_ticks = iter((500_000_000, 500_000_000))
        with self.reader(clock=lambda: next(second_ticks)) as reader:
            second = reader.sample()
        self.assertEqual(5000, cgroup_cpu_basis_points(first, second))
        with self.assertRaisesRegex(BenchmarkRunnerError, "regressed"):
            cgroup_cpu_basis_points(second, first)
        with self.assertRaisesRegex(BenchmarkRunnerError, "same control group"):
            cgroup_cpu_basis_points(first, replace(second, control_group="/other.scope"))

    def test_integer_observation_aggregation_is_deterministic_and_bounded(self):
        first = aggregate_integer_observation(
            "summed-pss",
            [100, 110, 120, 130, 140],
            aggregation="median",
        )
        second = aggregate_integer_observation(
            "summed-pss",
            [100, 110, 120, 130, 140],
            aggregation="median",
        )
        self.assertEqual(first, second)
        self.assertEqual(120, first.value)
        self.assertEqual(5, first.sample_count)
        self.assertLessEqual(first.confidence95.lower, first.value)
        self.assertGreaterEqual(first.confidence95.upper, first.value)

        p95 = aggregate_integer_observation(
            "cpu",
            list(range(1, 21)),
            aggregation="p95",
        )
        self.assertEqual(19, p95.value)
        with self.assertRaises(BenchmarkRunnerError):
            aggregate_integer_observation("cpu", [], aggregation="p95")
        with self.assertRaises(BenchmarkRunnerError):
            aggregate_integer_observation("cpu", [1, -1], aggregation="p95")

    def test_systemd_scope_plan_is_exact_tokenized_and_never_a_shell_command(self):
        plan = build_systemd_scope_plan(
            ("/opt/tool path/bin", "arg;$(touch /tmp/not-executed)"),
            scope_name="zen-app-benchmark-test-1",
            working_directory=self.root,
            cgroup_root=self.cgroup_root,
            proc_root=self.proc_root,
        )
        argv = systemd_scope_argv(plan)
        self.assertEqual("/usr/bin/systemd-run", argv[0])
        self.assertIn("--scope", argv)
        self.assertIn("--collect", argv)
        self.assertNotIn("--wait", argv)
        self.assertIn("--property=MemoryAccounting=yes", argv)
        self.assertNotIn("--property=CPUAccounting=yes", argv)
        self.assertIn("--property=TasksAccounting=yes", argv)
        self.assertEqual(("--", "/opt/tool path/bin", "arg;$(touch /tmp/not-executed)"), argv[-3:])
        self.assertNotIn("sh", argv)
        self.assertNotIn("-c", argv)

        with self.assertRaises(BenchmarkRunnerError):
            build_systemd_scope_plan(
                ("/bin/true",),
                scope_name="../../victim",
                working_directory=self.root,
                cgroup_root=self.cgroup_root,
                proc_root=self.proc_root,
            )

    def test_scope_launch_uses_explicit_environment_and_resolved_isolated_cgroup(self):
        plan = build_systemd_scope_plan(
            ("/bin/sleep", "10"),
            scope_name="zen-app-benchmark-test-2",
            working_directory=self.root,
            cgroup_root=self.cgroup_root,
            proc_root=self.proc_root,
        )
        control_group = f"/{plan.unit_id}"
        self.scope.rename(self.cgroup_root / plan.unit_id)
        process = FakeProcess()
        call = {}

        def popen(argv, **kwargs):
            call["argv"] = tuple(argv)
            call["kwargs"] = kwargs
            return process

        environment = {"PATH": "/usr/bin", "WAYLAND_DISPLAY": "wayland-1"}
        handle = launch_systemd_scope(
            plan,
            environment,
            popen_factory=popen,
            control_group_resolver=lambda _plan, _environment: control_group,
        )
        try:
            self.assertEqual(systemd_scope_argv(plan), call["argv"])
            self.assertEqual(environment, call["kwargs"]["env"])
            self.assertEqual(str(self.root), call["kwargs"]["cwd"])
            self.assertIs(False, call["kwargs"]["shell"])
            self.assertIs(True, call["kwargs"]["close_fds"])
            self.assertEqual(control_group, handle.control_group)
            sample = handle.sample()
            self.assertEqual(200, sample.summed_pss_kib)
        finally:
            process.returncode = 0
            handle.close()
        self.assertFalse(process.terminated)

    def test_scope_launch_rejects_a_resolved_different_scope_and_cleans_up(self):
        plan = build_systemd_scope_plan(
            ("/bin/sleep", "10"),
            scope_name="zen-app-benchmark-test-3",
            working_directory=self.root,
            cgroup_root=self.cgroup_root,
            proc_root=self.proc_root,
        )
        process = FakeProcess()
        with self.assertRaisesRegex(BenchmarkRunnerError, "does not match"):
            launch_systemd_scope(
                plan,
                {"PATH": "/usr/bin"},
                popen_factory=lambda _argv, **_kwargs: process,
                control_group_resolver=lambda _plan, _environment: "/bench.scope",
            )
        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)


if __name__ == "__main__":
    unittest.main()
