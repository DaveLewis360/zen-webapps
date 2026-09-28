"""Opt-in integration smoke for a locally pinned native Zen executable.

The default unit suite skips this module unless ZEN_REAL_RUNTIME names an
absolute executable. The test creates only temporary AppStore/profile state
inside the repository, launches each requested backend, records process and
sandbox evidence, terminates the supervising CLI, and removes all temporary
state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from zen_app_runtime.onboarding import default_web_app_id
from zen_app_runtime.store import AppStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_RUNTIME = os.environ.get("ZEN_REAL_RUNTIME")
REAL_BACKENDS = tuple(
    item.strip()
    for item in os.environ.get("ZEN_REAL_BACKENDS", "wayland,x11").split(",")
    if item.strip()
)


def _process_table() -> dict[int, dict[str, object]]:
    table: dict[int, dict[str, object]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            stat_fields = stat_text.rsplit(")", 1)[1].strip().split()
            state = stat_fields[0]
            ppid = int(stat_fields[1])
            process_group = int(stat_fields[2])
            process_session = int(stat_fields[3])
            start_time = int(stat_fields[19])
            status_text = (entry / "status").read_text(encoding="utf-8")
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError, ValueError):
            continue
        status: dict[str, str] = {}
        for line in status_text.splitlines():
            if ":" in line:
                name, value = line.split(":", 1)
                status[name] = value.strip()
        pss_kib = 0
        try:
            for line in (entry / "smaps_rollup").read_text(encoding="utf-8").splitlines():
                if line.startswith("Pss:"):
                    pss_kib = int(line.split()[1])
                    break
            final_stat = (entry / "stat").read_text(encoding="utf-8")
            final_fields = final_stat.rsplit(")", 1)[1].strip().split()
            if int(final_fields[19]) != start_time:
                continue
        except (FileNotFoundError, PermissionError, ProcessLookupError, IndexError, ValueError):
            continue
        table[pid] = {
            "command": command,
            "no_new_privs": int(status.get("NoNewPrivs", "0")),
            "pgrp": process_group,
            "ppid": ppid,
            "pss_kib": pss_kib,
            "rss_kib": int(status.get("VmRSS", "0 kB").split()[0]),
            "seccomp": int(status.get("Seccomp", "0")),
            "session": process_session,
            "start_time": start_time,
            "state": state,
        }
    return table


def _descendants(root_pid: int, table: dict[int, dict[str, object]]) -> set[int]:
    result: set[int] = set()
    frontier = {root_pid}
    while frontier:
        parents = frontier
        frontier = {
            pid
            for pid, record in table.items()
            if record["ppid"] in parents and pid not in result
        }
        result.update(frontier)
    return result


def _live_session_members(
    session_id: int,
    table: dict[int, dict[str, object]] | None = None,
) -> dict[int, dict[str, object]]:
    current = _process_table() if table is None else table
    return {
        pid: record
        for pid, record in current.items()
        if record["session"] == session_id and record["state"] != "Z"
    }


def _signal_session(session_id: int, signum: int) -> None:
    groups = {int(record["pgrp"]) for record in _live_session_members(session_id).values()}
    for process_group in groups:
        try:
            os.killpg(process_group, signum)
        except ProcessLookupError:
            pass


def _wait_session_empty(
    session_id: int,
    timeout: float,
) -> dict[int, dict[str, object]]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = _live_session_members(session_id)
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.1)


def _terminate_session(session_id: int) -> dict[int, dict[str, object]]:
    _signal_session(session_id, signal.SIGTERM)
    remaining = _wait_session_empty(session_id, 3)
    if remaining:
        _signal_session(session_id, signal.SIGKILL)
        remaining = _wait_session_empty(session_id, 5)
    return remaining


def _remaining_session_detail(members: dict[int, dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "command": record["command"],
            "pid": pid,
            "startTime": record["start_time"],
            "state": record["state"],
        }
        for pid, record in sorted(members.items())
    ]


def _process_environment(pid: int) -> dict[str, str]:
    payload = Path(f"/proc/{pid}/environ").read_bytes()
    environment: dict[str, str] = {}
    for item in payload.split(b"\0"):
        if not item or b"=" not in item:
            continue
        name, value = item.split(b"=", 1)
        environment[name.decode("utf-8")] = value.decode("utf-8", errors="strict")
    return environment


def _x11_window_pids() -> set[int]:
    try:
        root = subprocess.run(
            ["xprop", "-root", "_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for window_id in set(re.findall(r"0x[0-9a-fA-F]+", root.stdout)):
        try:
            properties = subprocess.run(
                ["xprop", "-id", window_id, "_NET_WM_PID", "WM_CLASS"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except subprocess.SubprocessError:
            continue
        match = re.search(r"_NET_WM_PID\([^)]*\) = (\d+)", properties.stdout)
        if match and "zen" in properties.stdout.lower():
            pids.add(int(match.group(1)))
    return pids


@unittest.skipUnless(REAL_RUNTIME, "set ZEN_REAL_RUNTIME for the opt-in native Zen smoke")
class RealZenIntegrationTests(unittest.TestCase):
    def test_configured_display_backends(self) -> None:
        runtime = Path(REAL_RUNTIME or "")
        self.assertTrue(runtime.is_absolute())
        self.assertTrue(runtime.is_file())
        self.assertTrue(os.access(runtime, os.X_OK))
        self.assertTrue(REAL_BACKENDS)
        self.assertTrue(set(REAL_BACKENDS) <= {"wayland", "x11"})

        for backend in REAL_BACKENDS:
            with self.subTest(backend=backend):
                self._run_backend(runtime, backend)

    def _run_backend(self, runtime: Path, backend: str) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as temporary:
            root = Path(temporary)
            store_root = root / "apps"
            profile_root = root / "profiles"
            start_url = "https://example.com/"
            app_id = default_web_app_id(start_url)
            runtime_lock = root / "runtime-lock.json"
            runtime_lock.write_text(
                json.dumps(
                    {
                        "runtimeLockVersion": 1,
                        "runtime": {
                            "path": str(runtime),
                            "sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_lock.chmod(0o600)
            profile = profile_root / app_id
            command = [
                sys.executable,
                "-m",
                "zen_app_runtime",
                "--store",
                str(store_root),
                "app",
                "onboard",
                start_url,
                "--profile-root",
                str(profile_root),
                "--runtime-lock",
                str(runtime_lock),
                "--display-backend",
                backend,
                "--launch",
                "--yes",
            ]
            baseline: dict[str, object] = {"backend": backend}
            for phase in ("cold", "warm"):
                evidence = self._launch_phase(root, profile, command, backend, phase)
                baseline[phase] = evidence
                onboarding_output = (root / f"launcher-{phase}.stdout").read_text(
                    encoding="utf-8"
                )
                self.assertIn('"webAppOnboardingResultVersion": 1', onboarding_output)
                self.assertIn('"onboardingStatus": "completed"', onboarding_output)
            manifest = AppStore(store_root).get_read_only(app_id)
            self.assertEqual(start_url, manifest.start_url)
            self.assertEqual("Example", manifest.name)
            print(json.dumps(baseline, sort_keys=True))
            self.assertTrue((profile / "compatibility.ini").is_file() or (profile / "prefs.js").is_file())

    def _launch_phase(
        self,
        root: Path,
        profile: Path,
        command: list[str],
        backend: str,
        phase: str,
    ) -> dict[str, object]:
        stdout_path = root / f"launcher-{phase}.stdout"
        stderr_path = root / f"launcher-{phase}.stderr"
        environment = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "src")}
        started_at = time.monotonic()
        with stdout_path.open("w+", encoding="utf-8") as stdout, stderr_path.open(
            "w+", encoding="utf-8"
        ) as stderr:
            launcher = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=True,
            )
            evidence: dict[str, object] | None = None
            runtime_session_id: int | None = None
            return_code: int | None = None
            cleanup_remaining: dict[int, dict[str, object]] = {}
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if launcher.poll() is not None:
                        break
                    table = _process_table()
                    descendants = _descendants(launcher.pid, table)
                    direct_children = sorted(
                        pid for pid in descendants if table[pid]["ppid"] == launcher.pid
                    )
                    if direct_children:
                        runtime_session_id = int(table[direct_children[0]]["session"])
                    content_ready = any(
                        "-contentproc" in str(table[pid]["command"])
                        for pid in descendants
                    )
                    initialized = (profile / "compatibility.ini").is_file() or (
                        profile / "prefs.js"
                    ).is_file()
                    if initialized and content_ready:
                        startup_milliseconds = round(
                            (time.monotonic() - started_at) * 1000
                        )
                        time.sleep(3)
                        table = _process_table()
                        descendants = _descendants(launcher.pid, table)
                        evidence = self._collect_evidence(
                            backend,
                            launcher.pid,
                            descendants,
                            table,
                        )
                        evidence["phase"] = phase
                        evidence["startupMilliseconds"] = startup_milliseconds
                        break
                    time.sleep(0.1)
                if evidence is None:
                    stderr.flush()
                    stderr.seek(0)
                    self.fail(
                        f"Zen {backend} {phase} did not initialize: "
                        f"return={launcher.poll()} stderr={stderr.read()}"
                    )
            finally:
                if launcher.poll() is None:
                    launcher.send_signal(signal.SIGTERM)
                try:
                    return_code = launcher.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if runtime_session_id is not None:
                        cleanup_remaining = _terminate_session(runtime_session_id)
                    try:
                        os.killpg(launcher.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        return_code = launcher.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        return_code = None
                if runtime_session_id is not None:
                    cleanup_remaining = _wait_session_empty(runtime_session_id, 10)
                    if cleanup_remaining:
                        cleanup_remaining = _terminate_session(runtime_session_id)
                if cleanup_remaining:
                    self.fail(
                        "live Zen session members after cleanup: "
                        f"{_remaining_session_detail(cleanup_remaining)}"
                    )
            stderr.flush()
            stderr.seek(0)
            self.assertEqual(143, return_code, stderr.read())

        if evidence is None:  # pragma: no cover - self.fail raises above.
            raise AssertionError("missing integration evidence")
        return evidence

    def _collect_evidence(
        self,
        backend: str,
        launcher_pid: int,
        descendants: set[int],
        table: dict[int, dict[str, object]],
    ) -> dict[str, object]:
        self.assertGreaterEqual(len(descendants), 2)
        direct_children = sorted(
            pid for pid in descendants if table[pid]["ppid"] == launcher_pid
        )
        self.assertTrue(direct_children)
        browser_pid = direct_children[0]
        self.assertEqual(browser_pid, table[browser_pid]["session"])
        browser_environment = _process_environment(browser_pid)
        browser_command = str(table[browser_pid]["command"])
        self.assertRegex(browser_command, r"--profile /proc/self/fd/\d+")
        self.assertRegex(
            browser_environment["HOME"],
            r"^/proc/self/fd/\d+/\.zen-app-runtime/home$",
        )
        commands = [str(table[pid]["command"]) for pid in sorted(descendants)]
        self.assertFalse(any("--no-sandbox" in command for command in commands))
        self.assertNotIn("MOZ_DISABLE_CONTENT_SANDBOX", browser_environment)
        self.assertEqual(backend, browser_environment["GDK_BACKEND"])
        if backend == "wayland":
            self.assertEqual("1", browser_environment["MOZ_ENABLE_WAYLAND"])
            self.assertIn("WAYLAND_DISPLAY", browser_environment)
            self.assertNotIn("DISPLAY", browser_environment)
        else:
            self.assertIn("DISPLAY", browser_environment)
            self.assertNotIn("WAYLAND_DISPLAY", browser_environment)
            self.assertNotIn("MOZ_ENABLE_WAYLAND", browser_environment)
            self.assertTrue(descendants & _x11_window_pids(), "no Zen X11 window was observed")

        content = [
            table[pid]
            for pid in descendants
            if "-contentproc" in str(table[pid]["command"])
        ]
        self.assertTrue(content, "no Zen content process was observed")
        sandboxed_content = [record for record in content if record["seccomp"] == 2]
        self.assertTrue(sandboxed_content, "no seccomp-filtered content process was observed")
        return {
            "backend": backend,
            "contentProcessCount": len(content),
            "fdBackedProfileObserved": True,
            "noNewPrivsContentCount": sum(
                record["no_new_privs"] == 1 for record in content
            ),
            "processCount": len(descendants),
            "pssKiB": sum(int(table[pid]["pss_kib"]) for pid in descendants),
            "rssKiB": sum(int(table[pid]["rss_kib"]) for pid in descendants),
            "seccompContentCount": len(sandboxed_content),
            "x11WindowObserved": bool(descendants & _x11_window_pids()),
        }


if __name__ == "__main__":
    unittest.main()
