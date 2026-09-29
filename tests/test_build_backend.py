import hashlib
import io
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from zen_app_runtime.build_backend import _build_sdist, build_sdist, build_wheel


class BuildBackendTests(unittest.TestCase):
    def test_wheel_and_sdist_are_reproducible_and_complete(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as first, tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as second:
            first_wheel = Path(first) / build_wheel(first)
            second_wheel = Path(second) / build_wheel(second)
            first_sdist = Path(first) / build_sdist(first)
            second_sdist = Path(second) / build_sdist(second)

            self.assertEqual(self.digest(first_wheel), self.digest(second_wheel))
            self.assertEqual(self.digest(first_sdist), self.digest(second_sdist))
            with zipfile.ZipFile(first_wheel) as archive:
                names = archive.namelist()
                self.assertIn("zen_app_runtime/cli.py", names)
                self.assertIn("zen_app_runtime/launch_plan.py", names)
                self.assertIn("zen_app_runtime/desktop.py", names)
                self.assertIn("zen_app_runtime/runtime.py", names)
                self.assertIn("zen_app_runtime/profile.py", names)
                self.assertIn("zen_app_runtime/launch.py", names)
                self.assertIn("zen_app_runtime/app_shell.py", names)
                self.assertIn("zen_app_runtime/app_shell_mediation.py", names)
                self.assertIn("zen_app_runtime/benchmark_contract.py", names)
                self.assertIn("zen_app_runtime/benchmark_runner.py", names)
                self.assertIn("zen_app_runtime/discord_conformance.py", names)
                self.assertIn("zen_app_runtime/runtime_driver.py", names)
                self.assertIn("zen_app_runtime/gecko_memory.py", names)
                self.assertIn("zen_app_runtime/runtime_optimization.py", names)
                self.assertIn("zen_app_runtime/native_app_shell_benchmark.py", names)
                self.assertIn("zen_app_runtime/gecko_runtime_candidate.py", names)
                self.assertIn("zen_app_runtime/chromium_runtime_candidate.py", names)
                self.assertIn(
                    "zen_app_runtime/runtime_candidates/gecko-firefox-154.0-linux-x86_64-en-US.lock.json",
                    names,
                )
                self.assertIn(
                    "zen_app_runtime/runtime_candidates/chromium-cef-151.3.24-1-x86_64.lock.json",
                    names,
                )
                self.assertIn("zen_app_runtime/chromium_shell/CMakeLists.txt", names)
                self.assertIn("zen_app_runtime/chromium_shell/main.cc", names)
                self.assertIn("zen_app_runtime/browser_adapter.py", names)
                self.assertIn(
                    "zen_app_runtime/browser_adapter_bundle/application.ini",
                    names,
                )
                self.assertIn(
                    "zen_app_runtime/browser_adapter_bundle/chrome/content/shell.js",
                    names,
                )
                self.assertIn(
                    "zen_app_runtime/browser_adapter_bundle/defaults/preferences/prefs.js",
                    names,
                )
                self.assertIn("zen_app_runtime/workflow.py", names)
                self.assertIn("zen_app_runtime/_secure_fs.py", names)
                for app_mode_file in (
                    "appmode/commands.py",
                    "appmode/zen_internet.py",
                    "appmode/desktop_look.py",
                    "appmode/profile/userChrome.css",
                    "appmode/data/zen-internet-styles.json",
                    "appmode/data/LICENSE-zen-internet.txt",
                    "appmode/addon/manifest.json",
                    "appmode/addon/api.js",
                    "appmode/addon/modules/ZenApp.sys.mjs",
                    "appmode/addon/modules/Styles.sys.mjs",
                    "appmode/addon/content/studio.html",
                    "appmode/addon/themes/spotify-glass.css",
                ):
                    self.assertIn(f"zen_app_runtime/{app_mode_file}", names)
                self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
                self.assertIn("zen_app_runtime-0.1.0.dist-info/RECORD", names)
                self.assertFalse(any(name.endswith((".xpi", ".zip", ".tar.xz", ".pkg.tar.zst")) for name in names))
                self.assertFalse(any("gecko-runtime-candidate.json" in name for name in names))
                self.assertFalse(any("chromium-runtime-candidate.json" in name for name in names))
                self.assertFalse(any("/sdk/" in name or "/candidate-" in name for name in names))
            with tarfile.open(first_sdist, mode="r:gz") as archive:
                members = archive.getmembers()
                member_names = [item.name for item in members]
                self.assertIn("zen_app_runtime-0.1.0/PKG-INFO", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/launch.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/app_shell_mediation.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/browser_adapter.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/benchmark_contract.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/benchmark_runner.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/discord_conformance.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/runtime_driver.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/gecko_memory.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/runtime_optimization.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/native_app_shell_benchmark.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/gecko_runtime_candidate.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/src/zen_app_runtime/chromium_runtime_candidate.py", member_names)
                self.assertIn(
                    "zen_app_runtime-0.1.0/src/zen_app_runtime/runtime_candidates/gecko-firefox-154.0-linux-x86_64-en-US.lock.json",
                    member_names,
                )
                self.assertIn(
                    "zen_app_runtime-0.1.0/src/zen_app_runtime/runtime_candidates/chromium-cef-151.3.24-1-x86_64.lock.json",
                    member_names,
                )
                self.assertIn(
                    "zen_app_runtime-0.1.0/src/zen_app_runtime/chromium_shell/CMakeLists.txt",
                    member_names,
                )
                self.assertIn(
                    "zen_app_runtime-0.1.0/src/zen_app_runtime/chromium_shell/main.cc",
                    member_names,
                )
                self.assertIn(
                    "zen_app_runtime-0.1.0/src/zen_app_runtime/browser_adapter_bundle/chrome/content/shell.xhtml",
                    member_names,
                )
                self.assertIn("zen_app_runtime-0.1.0/tests/test_app_shell.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_app_shell_mediation.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_benchmark_contract.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_benchmark_runner.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_discord_conformance.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_runtime_driver.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_gecko_memory.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_runtime_optimization.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_native_app_shell_benchmark.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_gecko_runtime_candidate.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_chromium_runtime_candidate.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_real_chromium_candidate.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_real_gecko_candidate.py", member_names)
                self.assertIn(
                    "zen_app_runtime-0.1.0/tests/test_real_app_shell_optimization.py",
                    member_names,
                )
                self.assertIn("zen_app_runtime-0.1.0/tests/test_browser_adapter.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_launch.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_workflow.py", member_names)
                self.assertIn("zen_app_runtime-0.1.0/tests/test_real_zen.py", member_names)
                self.assertIn(
                    "zen_app_runtime-0.1.0/tests/test_real_app_shell.py",
                    member_names,
                )
                self.assertNotIn(
                    "zen_app_runtime-0.1.0/docs/zen-runtime-optimization-evidence.json",
                    member_names,
                )
                self.assertFalse(any(name.endswith((".tar.xz", ".pkg.tar.zst")) for name in member_names))
                self.assertFalse(
                    any("gecko-runtime-candidate.json" in name for name in member_names)
                )
                self.assertFalse(
                    any("chromium-runtime-candidate.json" in name for name in member_names)
                )
                self.assertFalse(
                    any("/sdk/" in name or "/candidate-" in name for name in member_names)
                )
                self.assertTrue(all(item.mode == 0o644 for item in members if item.isfile()))

    def test_sdist_ignores_source_permission_differences(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            first_root = Path(temporary) / "first"
            second_root = Path(temporary) / "second"
            for destination in (first_root, second_root):
                destination.mkdir()
                for filename in ("pyproject.toml", "README.md"):
                    shutil.copy2(project_root / filename, destination / filename)
                for directory in ("src", "tests", "docs"):
                    shutil.copytree(project_root / directory, destination / directory)
            (second_root / "README.md").chmod(0o600)
            first_output = io.BytesIO()
            second_output = io.BytesIO()
            _build_sdist(first_root, first_output)
            _build_sdist(second_root, second_output)
            self.assertEqual(hashlib.sha256(first_output.getvalue()).digest(), hashlib.sha256(second_output.getvalue()).digest())

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
