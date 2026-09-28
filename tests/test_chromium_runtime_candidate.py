from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import zen_app_runtime.chromium_runtime_candidate as candidate


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _open_plain_package(package_fd: int, _zstd_path: str):
    os.lseek(package_fd, 0, os.SEEK_SET)
    stream = os.fdopen(os.dup(package_fd), "rb")
    try:
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            yield archive
    finally:
        stream.close()


class ChromiumRuntimeCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir(mode=0o700)
        self.sandbox = self.root / "sandbox"
        self.sandbox.write_bytes(b"fixture sandbox\n")
        self.sandbox.chmod(0o600)
        self.fixture_index = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def snapshot(root: Path) -> tuple[tuple[str, str, int, int], ...]:
        values: list[tuple[str, str, int, int]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            kind = "d" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            values.append((relative, kind, stat.S_IMODE(info.st_mode), info.st_mtime_ns))
        return tuple(values)

    @staticmethod
    def _file_pin(path: str, payload: bytes, mode: str) -> dict[str, object]:
        return {
            "mode": mode,
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    def prepare_fixture(
        self,
        *,
        extra_members: list[tuple[tarfile.TarInfo, bytes]] | None = None,
        actual_overrides: dict[str, bytes] | None = None,
    ) -> dict[str, object]:
        self.fixture_index += 1
        data = candidate.chromium_runtime_source_lock_to_dict(
            candidate.load_bundled_chromium_runtime_source_lock()
        )
        executable_payload = b"fixture CEF shell\n"
        executable = self._file_pin("zen-cef-shell", executable_payload, "0755")
        data["candidate"]["executable"] = executable

        expected_payloads: dict[str, bytes] = {}
        runtime_entries = [
            candidate.ChromiumRuntimeTreeEntry(
                path=str(executable["path"]),
                size=int(executable["size"]),
                mode=str(executable["mode"]),
                sha256=str(executable["sha256"]),
            )
        ]
        for pin in data["candidate"]["runtimeFiles"]:
            payload = f"fixture runtime {pin['path']}\n".encode("ascii")
            expected_payloads[str(pin["archivePath"])] = payload
            pin["size"] = len(payload)
            pin["sha256"] = self.digest(payload)
            runtime_entries.append(
                candidate.ChromiumRuntimeTreeEntry(
                    path=str(pin["path"]),
                    size=len(payload),
                    mode=str(pin["mode"]),
                    sha256=self.digest(payload),
                )
            )
        data["candidate"]["fileCount"] = len(runtime_entries)
        data["candidate"]["totalBytes"] = sum(item.size for item in runtime_entries)
        data["candidate"]["treeSha256"] = candidate.chromium_runtime_tree_sha256(
            runtime_entries
        )

        sandbox_payload = b"fixture package sandbox\n"
        sandbox = data["candidate"]["sandbox"]
        sandbox["size"] = len(sandbox_payload)
        sandbox["sha256"] = self.digest(sandbox_payload)

        package = data["package"]
        metadata_payloads = {
            ".PKGINFO": (
                f"pkgname = {package['name']}\n"
                f"pkgver = {package['version']}\n"
                f"arch = {package['arch']}\n"
                f"builddate = {package['buildTimestamp']}\n"
                f"packager = {package['packager']}\n"
            ).encode("utf-8"),
            ".BUILDINFO": (
                f"pkgbuild_sha256sum = {data['packaging']['pkgbuildSha256']}\n"
            ).encode("ascii"),
            ".MTREE": b"#mtree fixture\n",
        }
        metadata_fields = {
            ".BUILDINFO": "buildInfo",
            ".MTREE": "mtree",
            ".PKGINFO": "packageInfo",
        }
        for path, field in metadata_fields.items():
            package[field] = self._file_pin(path, metadata_payloads[path], "0644")

        sdk_path = "usr/include/cef/include/cef_version.h"
        sdk_payload = b"#define CEF_VERSION \"fixture\"\n"
        sdk_entry = candidate.ChromiumRuntimeTreeEntry(
            path=sdk_path,
            size=len(sdk_payload),
            mode="0644",
            sha256=self.digest(sdk_payload),
        )
        toolchain = data["shell"]["toolchain"]
        toolchain["sdkFileCount"] = 1
        toolchain["sdkTotalBytes"] = len(sdk_payload)
        toolchain["sdkTreeSha256"] = candidate._versioned_tree_sha256(
            (sdk_entry,),
            "chromiumSdkTreeVersion",
            candidate.CHROMIUM_SDK_TREE_VERSION,
        )

        archive_members: list[tuple[tarfile.TarInfo, bytes]] = []
        directory = tarfile.TarInfo("usr")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive_members.append((directory, b""))
        archive_members.extend(extra_members or [])
        for path, payload in metadata_payloads.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            info.mode = 0o644
            archive_members.append((info, payload))
        for pin in data["candidate"]["runtimeFiles"]:
            archive_path = str(pin["archivePath"])
            payload = (actual_overrides or {}).get(
                archive_path, expected_payloads[archive_path]
            )
            info = tarfile.TarInfo(archive_path)
            info.size = len(payload)
            info.mode = int(str(pin["mode"]), 8)
            archive_members.append((info, payload))
        sandbox_path = str(sandbox["installedPath"]).removeprefix("/")
        actual_sandbox = (actual_overrides or {}).get(sandbox_path, sandbox_payload)
        sandbox_info = tarfile.TarInfo(sandbox_path)
        sandbox_info.size = len(actual_sandbox)
        sandbox_info.mode = 0o4755
        archive_members.append((sandbox_info, actual_sandbox))
        actual_sdk = (actual_overrides or {}).get(sdk_path, sdk_payload)
        sdk_info = tarfile.TarInfo(sdk_path)
        sdk_info.size = len(actual_sdk)
        sdk_info.mode = 0o644
        archive_members.append((sdk_info, actual_sdk))

        package["archiveEntries"] = len(archive_members)
        package["archiveDirectories"] = 1
        package["archiveRegularFiles"] = len(archive_members) - 1
        package_path = self.inputs / f"package-{self.fixture_index}.tar"
        with tarfile.open(package_path, mode="w") as archive:
            for info, payload in archive_members:
                archive.addfile(info, io.BytesIO(payload) if info.isfile() else None)
        package_path.chmod(0o600)
        package_bytes = package_path.read_bytes()
        data["artifacts"]["package"]["size"] = len(package_bytes)
        data["artifacts"]["package"]["sha256"] = self.digest(package_bytes)

        signature_path = self.inputs / f"package-{self.fixture_index}.sig"
        signature_path.write_bytes(b"fixture detached signature\n")
        signature_path.chmod(0o600)
        signature_bytes = signature_path.read_bytes()
        data["artifacts"]["packageSignature"]["size"] = len(signature_bytes)
        data["artifacts"]["packageSignature"]["sha256"] = self.digest(
            signature_bytes
        )

        lock = candidate.chromium_runtime_source_lock_from_dict(data)
        lock_path = self.inputs / f"source-lock-{self.fixture_index}.json"
        lock_path.write_text(
            candidate.render_chromium_runtime_source_lock(lock), encoding="ascii"
        )
        lock_path.chmod(0o600)
        return {
            "data": data,
            "executablePayload": executable_payload,
            "lock": lock,
            "lockPath": lock_path,
            "package": package_path,
            "signature": signature_path,
        }

    @contextmanager
    def patched_assembly(self, fixture: dict[str, object]):
        executable_payload = fixture["executablePayload"]
        assert isinstance(executable_payload, bytes)

        def fake_build(
            lock: candidate.ChromiumRuntimeSourceLock,
            _staging_path: str,
            staging_fd: int,
            runtime_fd: int,
            _source_payloads: object,
        ) -> candidate.ChromiumRuntimeTreeEntry:
            for name in ("source", "build", "home", "tmp"):
                os.mkdir(name, 0o700, dir_fd=staging_fd)
            pin = candidate.chromium_runtime_source_lock_to_dict(lock)["candidate"][
                "executable"
            ]
            descriptor = os.open(
                str(pin["path"]),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=runtime_fd,
            )
            try:
                candidate.write_all(descriptor, executable_payload)
                os.fchmod(descriptor, 0o755)
                os.utime(descriptor, ns=(0, 0))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return candidate.ChromiumRuntimeTreeEntry(
                path=str(pin["path"]),
                size=int(pin["size"]),
                mode=str(pin["mode"]),
                sha256=str(pin["sha256"]),
            )

        def fake_sandbox(
            _lock: candidate.ChromiumRuntimeSourceLock,
        ) -> tuple[int, tuple[int, ...]]:
            descriptor = os.open(self.sandbox, os.O_RDONLY | os.O_CLOEXEC)
            return descriptor, candidate._stat_identity(os.fstat(descriptor))

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(candidate, "_open_package_tar", _open_plain_package)
            )
            stack.enter_context(
                mock.patch.object(candidate, "_verify_package_signature")
            )
            stack.enter_context(
                mock.patch.object(candidate, "_verify_closed_source_tree", return_value={})
            )
            stack.enter_context(mock.patch.object(candidate, "_verify_tool"))
            stack.enter_context(mock.patch.object(candidate, "_tool_version"))
            stack.enter_context(mock.patch.object(candidate, "_build_shell", fake_build))
            stack.enter_context(
                mock.patch.object(candidate, "_open_verified_sandbox", fake_sandbox)
            )
            yield

    def scan_fixture(self, fixture: dict[str, object]):
        lock = fixture["lock"]
        package_path = fixture["package"]
        assert isinstance(lock, candidate.ChromiumRuntimeSourceLock)
        assert isinstance(package_path, Path)
        descriptor = os.open(package_path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            with mock.patch.object(candidate, "_open_package_tar", _open_plain_package):
                return candidate._scan_package(lock, descriptor)
        finally:
            os.close(descriptor)

    def assemble(self, fixture: dict[str, object], output: Path):
        with self.patched_assembly(fixture):
            return candidate.assemble_chromium_runtime_candidate(
                source_lock_path=fixture["lockPath"],
                package_path=fixture["package"],
                package_signature_path=fixture["signature"],
                packaging_source_path=self.root,
                output_path=output,
            )

    def test_bundled_lock_is_canonical_closed_and_privacy_bounded(self) -> None:
        lock = candidate.load_bundled_chromium_runtime_source_lock()
        self.assertEqual(
            candidate.EXPECTED_BUNDLED_CHROMIUM_RUNTIME_SOURCE_LOCK_SHA256,
            candidate.chromium_runtime_source_lock_sha256(lock),
        )
        data = candidate.chromium_runtime_source_lock_to_dict(lock)
        self.assertEqual("not-performed", data["provenance"]["cefSourceBuildStatus"])
        self.assertEqual(
            "not-tested", data["provenance"]["cefSourceReproducibilityStatus"]
        )
        self.assertEqual(
            {"path": "LICENSES/0BSD.txt", "target": "../LICENSE", "type": "symlink"},
            next(item for item in data["packaging"]["files"] if item["path"] == "LICENSES/0BSD.txt"),
        )
        rendered = candidate.render_chromium_runtime_source_lock(lock)
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("cachePath", rendered)

        malformed = candidate.chromium_runtime_source_lock_to_dict(lock)
        malformed["privatePath"] = "/home/operator/cache"
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "unknown"):
            candidate.chromium_runtime_source_lock_from_dict(malformed)

        noncanonical = self.root / "noncanonical.json"
        noncanonical.write_text(json.dumps(data), encoding="utf-8")
        noncanonical.chmod(0o600)
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "not canonical"):
            candidate.load_chromium_runtime_source_lock(noncanonical)

    def test_lock_rejects_unsafe_flags_links_and_derived_tree_mismatch(self) -> None:
        base = candidate.chromium_runtime_source_lock_to_dict(
            candidate.load_bundled_chromium_runtime_source_lock()
        )
        unsafe_flags = json.loads(json.dumps(base))
        unsafe_flags["packaging"]["buildFlags"].append("is_debug=true")
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "buildFlags"):
            candidate.chromium_runtime_source_lock_from_dict(unsafe_flags)

        unsafe_link = json.loads(json.dumps(base))
        link = next(
            item
            for item in unsafe_link["packaging"]["files"]
            if item["path"] == "LICENSES/0BSD.txt"
        )
        link["target"] = "../../outside"
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "inside"):
            candidate.chromium_runtime_source_lock_from_dict(unsafe_link)

        wrong_tree = json.loads(json.dumps(base))
        wrong_tree["candidate"]["treeSha256"] = "0" * 64
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "tree digest"):
            candidate.chromium_runtime_source_lock_from_dict(wrong_tree)

    def test_hash_and_package_signature_mismatches_fail_closed(self) -> None:
        artifact = self.root / "artifact"
        artifact.write_bytes(b"artifact\n")
        artifact.chmod(0o600)
        pin = {
            "size": artifact.stat().st_size,
            "sha256": "0" * 64,
        }
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "SHA-256"):
            with candidate._open_pinned_input(artifact, pin, "fixture artifact"):
                pass

        lock = candidate.load_bundled_chromium_runtime_source_lock()
        package_fd = os.open(artifact, os.O_RDONLY | os.O_CLOEXEC)
        signature_fd = os.dup(package_fd)
        rejected = subprocess.CompletedProcess(
            (),
            1,
            stdout=b"[GNUPG:] BADSIG DEADBEEF Fixture Signer\n",
            stderr=b"bad signature\n",
        )
        try:
            with mock.patch.object(candidate.subprocess, "run", return_value=rejected):
                with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "signature is invalid"):
                    candidate._verify_package_signature(lock, package_fd, signature_fd)
        finally:
            os.close(package_fd)
            os.close(signature_fd)

    def test_package_tar_rejects_traversal_links_special_and_duplicates(self) -> None:
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        traversal.mode = 0o644
        absolute = tarfile.TarInfo("/escape")
        absolute.size = 1
        absolute.mode = 0o644
        symlink = tarfile.TarInfo("unsafe-link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "target"
        hardlink = tarfile.TarInfo("unsafe-hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = ".PKGINFO"
        fifo = tarfile.TarInfo("unsafe-fifo")
        fifo.type = tarfile.FIFOTYPE
        duplicate_a = tarfile.TarInfo("duplicate")
        duplicate_a.size = 1
        duplicate_a.mode = 0o644
        duplicate_b = tarfile.TarInfo("duplicate")
        duplicate_b.size = 1
        duplicate_b.mode = 0o644
        variants = (
            ("traversal", [(traversal, b"x")]),
            ("absolute", [(absolute, b"x")]),
            ("symlink", [(symlink, b"")]),
            ("hardlink", [(hardlink, b"")]),
            ("special", [(fifo, b"")]),
            ("duplicate", [(duplicate_a, b"a"), (duplicate_b, b"b")]),
        )
        for name, members in variants:
            with self.subTest(name=name):
                fixture = self.prepare_fixture(extra_members=members)
                with self.assertRaises(candidate.ChromiumRuntimeCandidateError):
                    self.scan_fixture(fixture)

    def test_package_scan_binds_runtime_sdk_metadata_and_sandbox(self) -> None:
        valid = self.prepare_fixture()
        scan = self.scan_fixture(valid)
        self.assertEqual(1, len(scan.sdk_files))

        runtime_path = candidate._RUNTIME_ARCHIVE_PATHS[0]
        runtime_mismatch = self.prepare_fixture(
            actual_overrides={runtime_path: b"wrong runtime\n"}
        )
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "runtime package member mismatch"):
            self.scan_fixture(runtime_mismatch)

        sdk_mismatch = self.prepare_fixture(
            actual_overrides={
                "usr/include/cef/include/cef_version.h": b"wrong SDK\n"
            }
        )
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "SDK tree"):
            self.scan_fixture(sdk_mismatch)

        sandbox_mismatch = self.prepare_fixture(
            actual_overrides={"usr/lib/cef/chrome-sandbox": b"wrong sandbox\n"}
        )
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "sandbox member mismatch"):
            self.scan_fixture(sandbox_mismatch)

    def test_shell_config_is_sealed_private_and_rejects_malformed_urls(self) -> None:
        profile = (self.root / "private-profile").resolve()
        payload = candidate.render_chromium_shell_config(
            profile_root=profile,
            start_url="http://127.0.0.1:8765/fixture",
            display_backend="wayland",
            fixture_mode=True,
            test_auto_activate=True,
        )
        self.assertEqual(6, len(payload.rstrip(b"\n").splitlines()))
        descriptor = candidate.create_chromium_shell_config_descriptor(payload)
        try:
            self.assertEqual(
                candidate._REQUIRED_SEALS,
                fcntl.fcntl(descriptor, candidate._F_GET_SEALS)
                & candidate._REQUIRED_SEALS,
            )
            command = ("zen-cef-shell", f"--zen-config-fd={descriptor}")
            self.assertNotIn(str(profile), " ".join(command))
            self.assertNotIn("127.0.0.1", " ".join(command))
        finally:
            os.close(descriptor)

        for value in (
            "http://127.0.0.1:not-a-port/",
            "http://user@127.0.0.1:8765/",
            "http://127.0.0.1:8765/\nunsafe=1",
        ):
            with self.subTest(value=value):
                with self.assertRaises(candidate.ChromiumRuntimeCandidateError):
                    candidate.render_chromium_shell_config(
                        profile_root=profile,
                        start_url=value,
                        display_backend="wayland",
                        fixture_mode=True,
                    )

    def test_dual_offline_assembly_is_deterministic_and_never_overwrites(self) -> None:
        fixture = self.prepare_fixture()
        first = self.root / "candidate-a"
        second = self.root / "candidate-b"
        first_descriptor = self.assemble(fixture, first)
        second_descriptor = self.assemble(fixture, second)
        self.assertEqual(first_descriptor, second_descriptor)
        self.assertEqual(self.snapshot(first), self.snapshot(second))
        serialized = candidate.render_chromium_runtime_candidate_descriptor(
            first_descriptor
        )
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("timestamp", serialized.lower())
        with self.patched_assembly(fixture):
            with candidate.open_verified_chromium_runtime_candidate(
                first, source_lock_path=fixture["lockPath"]
            ) as opened:
                self.assertEqual(13, len(opened.files))
                self.assertEqual(
                    f"/proc/self/fd/{opened.runtime_descriptor}/zen-cef-shell",
                    candidate.chromium_runtime_candidate_executable_path(
                        opened, source_lock_path=fixture["lockPath"]
                    ),
                )
        with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "already exists"):
            self.assemble(fixture, first)
        self.assertEqual(self.snapshot(first), self.snapshot(second))

    def test_candidate_tamper_hardlink_and_rebind_fail_closed(self) -> None:
        fixture = self.prepare_fixture()
        output = self.root / "candidate"
        self.assemble(fixture, output)
        with self.patched_assembly(fixture):
            with candidate.open_verified_chromium_runtime_candidate(
                output, source_lock_path=fixture["lockPath"]
            ) as opened:
                target = output / "runtime" / "resources.pak"
                target.write_bytes(b"tampered\n")
                with self.assertRaises(candidate.ChromiumRuntimeCandidateError):
                    candidate.revalidate_open_chromium_runtime_candidate(
                        opened, source_lock_path=fixture["lockPath"]
                    )

        shutil.rmtree(output)
        self.assemble(fixture, output)
        executable = output / "runtime" / "zen-cef-shell"
        target = output / "runtime" / "resources.pak"
        target.unlink()
        os.link(executable, target)
        os.utime(target, ns=(0, 0))
        with self.patched_assembly(fixture):
            with self.assertRaises(candidate.ChromiumRuntimeCandidateError):
                with candidate.open_verified_chromium_runtime_candidate(
                    output, source_lock_path=fixture["lockPath"]
                ):
                    pass

        shutil.rmtree(output)
        self.assemble(fixture, output)
        moved = self.root / "candidate-moved"
        with self.patched_assembly(fixture):
            with candidate.open_verified_chromium_runtime_candidate(
                output, source_lock_path=fixture["lockPath"]
            ) as opened:
                output.rename(moved)
                with self.assertRaises(candidate.ChromiumRuntimeCandidateError):
                    candidate.revalidate_open_chromium_runtime_candidate(
                        opened, source_lock_path=fixture["lockPath"]
                    )

    def test_installed_sandbox_content_mismatch_fails_closed(self) -> None:
        lock = candidate.load_bundled_chromium_runtime_source_lock()

        @contextmanager
        def fake_open(_path: str, _label: str):
            descriptor = os.open(self.sandbox, os.O_RDONLY | os.O_CLOEXEC)
            try:
                yield descriptor, os.fstat(descriptor)
            finally:
                os.close(descriptor)

        with mock.patch.object(candidate, "_open_absolute_file", fake_open):
            with self.assertRaisesRegex(candidate.ChromiumRuntimeCandidateError, "sandbox"):
                candidate._open_verified_sandbox(lock)


if __name__ == "__main__":
    unittest.main()
