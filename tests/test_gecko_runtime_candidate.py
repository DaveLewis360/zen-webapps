from __future__ import annotations

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

import zen_app_runtime.gecko_runtime_candidate as candidate


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class GeckoRuntimeCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir(mode=0o700)
        self.build_id = "20260102030405"
        self.version = "1.0"
        self.repository = "https://hg.example.test/releases/mozilla-release"
        self.source_stamp = "1" * 40
        self.primary_fingerprint = "A" * 40
        self.signing_fingerprint = "B" * 40

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def digest(path: Path, algorithm: str = "sha256") -> str:
        digest = hashlib.new(algorithm)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def write_bytes(path: Path, payload: bytes) -> None:
        path.write_bytes(payload)
        path.chmod(0o600)

    def application_ini(self) -> bytes:
        return (
            "[App]\n"
            "Name=Firefox\n"
            f"Version={self.version}\n"
            f"BuildID={self.build_id}\n"
            f"SourceRepository={self.repository}\n"
            f"SourceStamp={self.source_stamp}\n"
            "[Gecko]\n"
            f"MinVersion={self.version}\n"
            f"MaxVersion={self.version}\n"
        ).encode("ascii")

    def runtime_files(self) -> list[tuple[str, bytes, int]]:
        return [
            ("firefox", b"#!/bin/sh\nexit 0\n", 0o755),
            ("application.ini", self.application_ini(), 0o644),
            ("firefox-bin", b"fixture-bin\n", 0o755),
        ]

    def source_metadata(self) -> dict[str, bytes]:
        return {
            "browser/config/version.txt": f"{self.version}\n".encode("ascii"),
            "browser/config/version_display.txt": f"{self.version}\n".encode("ascii"),
            "config/milestone.txt": f"# fixture milestone\n\n{self.version}\n".encode("ascii"),
            "sourcestamp.txt": (
                f"{self.build_id}\n{self.repository}/rev/{self.source_stamp}"
            ).encode("ascii"),
        }

    def write_runtime_tar(
        self,
        path: Path,
        *,
        leading_members: list[tuple[tarfile.TarInfo, bytes]] | None = None,
        files: list[tuple[str, bytes, int]] | None = None,
    ) -> None:
        with tarfile.open(path, "w") as archive:
            for member, payload in leading_members or []:
                archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
            for relative, payload, mode in files or self.runtime_files():
                info = tarfile.TarInfo(f"firefox/{relative}")
                info.size = len(payload)
                info.mode = mode
                info.mtime = 123
                archive.addfile(info, io.BytesIO(payload))
        path.chmod(0o600)

    def write_source_tar(
        self,
        path: Path,
        *,
        leading_members: list[tuple[tarfile.TarInfo, bytes]] | None = None,
        metadata: dict[str, bytes] | None = None,
    ) -> None:
        with tarfile.open(path, "w") as archive:
            marker = tarfile.TarInfo(".")
            marker.type = tarfile.DIRTYPE
            marker.mode = 0o755
            archive.addfile(marker)
            for member, payload in leading_members or []:
                archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
            for relative, payload in (metadata or self.source_metadata()).items():
                info = tarfile.TarInfo(f"firefox-{self.version}/{relative}")
                info.size = len(payload)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(payload))
        path.chmod(0o600)

    def prepare_fixture(
        self,
        *,
        runtime_leading: list[tuple[tarfile.TarInfo, bytes]] | None = None,
        source_leading: list[tuple[tarfile.TarInfo, bytes]] | None = None,
        source_metadata: dict[str, bytes] | None = None,
    ) -> dict[str, Path]:
        runtime = self.inputs / "firefox.tar"
        source = self.inputs / "firefox.source.tar"
        self.write_runtime_tar(runtime, leading_members=runtime_leading)
        self.write_source_tar(
            source,
            leading_members=source_leading,
            metadata=source_metadata,
        )
        runtime_sha256 = self.digest(runtime)
        source_sha256 = self.digest(source)
        checksum_path = "linux-x86_64/en-US/firefox.tar"
        source_checksum_path = "source/firefox.source.tar"
        checksums = self.inputs / "SHA256SUMS"
        self.write_bytes(
            checksums,
            (
                f"{runtime_sha256}  {checksum_path}\n"
                f"{source_sha256}  {source_checksum_path}\n"
            ).encode("ascii"),
        )
        signature = self.inputs / "SHA256SUMS.asc"
        key = self.inputs / "KEY"
        self.write_bytes(signature, b"fixture-signature\n")
        self.write_bytes(key, b"fixture-public-key\n")

        runtime_entries = tuple(
            sorted(
                (
                    candidate.GeckoRuntimeTreeEntry(
                        path=relative,
                        size=len(payload),
                        mode=f"{mode:04o}",
                        sha256=hashlib.sha256(payload).hexdigest(),
                    )
                    for relative, payload, mode in self.runtime_files()
                ),
                key=lambda item: item.path,
            )
        )
        executable = next(item for item in runtime_entries if item.path == "firefox")
        expected_metadata = self.source_metadata()
        release_root = f"https://downloads.example.test/releases/{self.version}/"

        def artifact(path: Path, url: str) -> dict[str, object]:
            return {
                "filename": path.name,
                "sha256": self.digest(path),
                "size": path.stat().st_size,
                "url": url,
            }

        lock_data = {
            "artifacts": {
                "checksums": artifact(checksums, release_root + "SHA256SUMS"),
                "checksumsSignature": artifact(
                    signature, release_root + "SHA256SUMS.asc"
                ),
                "runtimeArchive": {
                    "checksumPath": checksum_path,
                    "filename": runtime.name,
                    "sha256": runtime_sha256,
                    "sha512": self.digest(runtime, "sha512"),
                    "size": runtime.stat().st_size,
                    "url": release_root + checksum_path,
                },
                "signingKey": artifact(key, release_root + "KEY"),
                "sourceArchive": {
                    "checksumPath": source_checksum_path,
                    "filename": source.name,
                    "sha256": source_sha256,
                    "sha512": self.digest(source, "sha512"),
                    "size": source.stat().st_size,
                    "url": release_root + source_checksum_path,
                },
            },
            "candidate": {
                "archiveRoot": "firefox",
                "buildId": self.build_id,
                "candidateId": f"firefox-{self.version}-linux-x86_64-en-US",
                "executable": {
                    "mode": executable.mode,
                    "path": executable.path,
                    "sha256": executable.sha256,
                    "size": executable.size,
                },
                "fileCount": len(runtime_entries),
                "locale": "en-US",
                "product": "Firefox",
                "target": "linux-x86_64",
                "totalBytes": sum(item.size for item in runtime_entries),
                "treeSha256": candidate.gecko_runtime_tree_sha256(runtime_entries),
                "version": self.version,
            },
            "geckoRuntimeSourceLockVersion": 1,
            "release": {
                "releaseRoot": release_root,
                "signature": {
                    "hashAlgorithm": "SHA512",
                    "primaryFingerprint": self.primary_fingerprint,
                    "publicKeyAlgorithm": "RSA",
                    "signatureDate": "2026-01-02",
                    "signatureTimestamp": 1767315845,
                    "signerUid": "Fixture Signer <fixture@example.test>",
                    "signingFingerprint": self.signing_fingerprint,
                },
                "sourceRepository": self.repository,
                "sourceStamp": self.source_stamp,
            },
            "source": {
                "archiveRoot": f"firefox-{self.version}",
                "buildStatus": "not-performed",
                "metadata": [
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                    }
                    for relative, payload in sorted(expected_metadata.items())
                ],
                "reproducibilityStatus": "not-tested",
                "toolchainSnapshot": {
                    "clang": "fixture-clang",
                    "cmake": "fixture-cmake",
                    "ninja": "fixture-ninja",
                    "purpose": "future-source-build-input-snapshot",
                    "rustc": "fixture-rustc",
                    "target": "x86_64-unknown-linux-gnu",
                },
            },
        }
        lock = candidate.gecko_runtime_source_lock_from_dict(lock_data)
        lock_path = self.root / "source-lock.json"
        lock_path.write_text(
            candidate.render_gecko_runtime_source_lock(lock), encoding="ascii"
        )
        lock_path.chmod(0o600)
        return {
            "checksums": checksums,
            "signature": signature,
            "key": key,
            "runtime": runtime,
            "source": source,
            "lock": lock_path,
        }

    def assemble(self, inputs: dict[str, Path], output: Path):
        with mock.patch.object(candidate, "_verify_signed_checksums") as verify:
            descriptor = candidate.assemble_gecko_runtime_candidate(
                source_lock_path=inputs["lock"],
                checksums_path=inputs["checksums"],
                checksums_signature_path=inputs["signature"],
                signing_key_path=inputs["key"],
                runtime_archive_path=inputs["runtime"],
                source_archive_path=inputs["source"],
                output_path=output,
            )
        verify.assert_called_once()
        return descriptor

    @staticmethod
    def snapshot(root: Path) -> tuple[tuple[str, str, int, int], ...]:
        values: list[tuple[str, str, int, int]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            kind = "d" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            values.append((relative, kind, stat.S_IMODE(info.st_mode), info.st_mtime_ns))
        return tuple(values)

    def test_bundled_source_lock_is_canonical_closed_and_exact(self) -> None:
        lock = candidate.load_bundled_gecko_runtime_source_lock()
        self.assertEqual(
            candidate.EXPECTED_BUNDLED_GECKO_RUNTIME_SOURCE_LOCK_SHA256,
            candidate.gecko_runtime_source_lock_sha256(lock),
        )
        self.assertEqual("154.0", lock.candidate.version)
        self.assertEqual("20260812182057", lock.candidate.build_id)
        self.assertEqual("9ce1ee6baeb9a3c326dbd180bdece65d8fc2eadc", lock.release.source_stamp)
        self.assertEqual("not-performed", lock.source.build_status)
        self.assertEqual("not-tested", lock.source.reproducibility_status)
        rendered = candidate.render_gecko_runtime_source_lock(lock)
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("username", rendered.lower())
        malformed = candidate.gecko_runtime_source_lock_to_dict(lock)
        malformed["hostPath"] = "/private/cache"
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "unknown"):
            candidate.gecko_runtime_source_lock_from_dict(malformed)

        noncanonical = self.root / "noncanonical.json"
        noncanonical.write_text(json.dumps(candidate.gecko_runtime_source_lock_to_dict(lock)), encoding="utf-8")
        noncanonical.chmod(0o600)
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "not canonical"):
            candidate.load_gecko_runtime_source_lock(noncanonical)

    def test_dual_offline_assembly_is_deterministic_and_fd_authoritative(self) -> None:
        inputs = self.prepare_fixture()
        first = self.root / "candidate-a"
        second = self.root / "candidate-b"
        first_descriptor = self.assemble(inputs, first)
        second_descriptor = self.assemble(inputs, second)
        self.assertEqual(first_descriptor, second_descriptor)
        self.assertEqual(self.snapshot(first), self.snapshot(second))
        self.assertEqual(
            {candidate.GECKO_RUNTIME_CANDIDATE_DESCRIPTOR, "firefox"},
            {path.name for path in first.iterdir()},
        )
        serialized = candidate.render_gecko_runtime_candidate_descriptor(first_descriptor)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("timestamp", serialized.lower())
        with candidate.open_verified_gecko_runtime_candidate(
            first, source_lock_path=inputs["lock"]
        ) as opened:
            self.assertEqual(first_descriptor, opened.descriptor)
            self.assertEqual(3, len(opened.files))
            executable = candidate.gecko_runtime_candidate_executable_path(
                opened, source_lock_path=inputs["lock"]
            )
            self.assertEqual(
                f"/proc/self/fd/{opened.runtime_descriptor}/firefox", executable
            )

    def test_existing_output_is_never_replaced(self) -> None:
        inputs = self.prepare_fixture()
        output = self.root / "existing-output"
        output.mkdir(mode=0o700)
        sentinel = output / "sentinel"
        sentinel.write_bytes(b"keep\n")
        with self.assertRaisesRegex(
            candidate.GeckoRuntimeCandidateError, "already exists"
        ):
            self.assemble(inputs, output)
        self.assertEqual(b"keep\n", sentinel.read_bytes())
        self.assertEqual(["sentinel"], sorted(path.name for path in output.iterdir()))

    def test_digest_and_signature_mismatches_fail_before_publication(self) -> None:
        inputs = self.prepare_fixture()
        runtime = bytearray(inputs["runtime"].read_bytes())
        runtime[-1] ^= 1
        self.write_bytes(inputs["runtime"], bytes(runtime))
        output = self.root / "digest-output"
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "SHA-256"):
            self.assemble(inputs, output)
        self.assertFalse(output.exists())

        shutil.rmtree(self.inputs)
        self.inputs.mkdir(mode=0o700)
        inputs = self.prepare_fixture()
        imported = subprocess.CompletedProcess(
            (),
            0,
            stdout=f"[GNUPG:] IMPORT_OK 1 {self.primary_fingerprint}\n",
            stderr="",
        )
        rejected = subprocess.CompletedProcess(
            (),
            1,
            stdout="[GNUPG:] BADSIG DEADBEEF Fixture Signer\n",
            stderr="bad signature",
        )
        output = self.root / "signature-output"
        with mock.patch.object(candidate, "_run_gpg", side_effect=(imported, rejected)):
            with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "signature is invalid"):
                candidate.assemble_gecko_runtime_candidate(
                    source_lock_path=inputs["lock"],
                    checksums_path=inputs["checksums"],
                    checksums_signature_path=inputs["signature"],
                    signing_key_path=inputs["key"],
                    runtime_archive_path=inputs["runtime"],
                    source_archive_path=inputs["source"],
                    output_path=output,
                )
        self.assertFalse(output.exists())

    def test_runtime_tar_rejects_traversal_links_and_special_members(self) -> None:
        variants: list[tuple[str, tarfile.TarInfo, bytes]] = []
        traversal = tarfile.TarInfo("firefox/../escape")
        traversal.size = 1
        traversal.mode = 0o644
        variants.append(("traversal", traversal, b"x"))
        absolute = tarfile.TarInfo("/firefox/escape")
        absolute.size = 1
        absolute.mode = 0o644
        variants.append(("absolute", absolute, b"x"))
        symlink = tarfile.TarInfo("firefox/link")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "firefox"
        variants.append(("symlink", symlink, b""))
        hardlink = tarfile.TarInfo("firefox/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "firefox/firefox"
        variants.append(("hardlink", hardlink, b""))
        fifo = tarfile.TarInfo("firefox/fifo")
        fifo.type = tarfile.FIFOTYPE
        variants.append(("special", fifo, b""))

        for index, (name, member, payload) in enumerate(variants):
            with self.subTest(name=name):
                case_root = self.root / f"case-{index}"
                case_root.mkdir(mode=0o700)
                old_inputs = self.inputs
                self.inputs = case_root / "inputs"
                self.inputs.mkdir(mode=0o700)
                try:
                    inputs = self.prepare_fixture(runtime_leading=[(member, payload)])
                    output = case_root / "output"
                    with self.assertRaises(candidate.GeckoRuntimeCandidateError):
                        self.assemble(inputs, output)
                    self.assertFalse(output.exists())
                finally:
                    self.inputs = old_inputs

    def test_source_tar_rejects_traversal_and_metadata_mismatch(self) -> None:
        traversal = tarfile.TarInfo("../source-escape")
        traversal.size = 1
        traversal.mode = 0o644
        inputs = self.prepare_fixture(source_leading=[(traversal, b"x")])
        output = self.root / "source-traversal-output"
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "unsafe tar path"):
            self.assemble(inputs, output)
        self.assertFalse(output.exists())

        shutil.rmtree(self.inputs)
        self.inputs.mkdir(mode=0o700)
        metadata = self.source_metadata()
        metadata["sourcestamp.txt"] = b"wrong provenance"
        inputs = self.prepare_fixture(source_metadata=metadata)
        output = self.root / "source-mismatch-output"
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "metadata (size|digest)"):
            self.assemble(inputs, output)
        self.assertFalse(output.exists())

    def test_descriptor_tamper_hardlink_and_rebind_fail_closed(self) -> None:
        inputs = self.prepare_fixture()
        output = self.root / "candidate"
        self.assemble(inputs, output)
        with candidate.open_verified_gecko_runtime_candidate(
            output, source_lock_path=inputs["lock"]
        ) as opened:
            target = output / "firefox" / "firefox-bin"
            target.write_bytes(b"tampered-bin\n")
            with self.assertRaises(candidate.GeckoRuntimeCandidateError):
                candidate.revalidate_open_gecko_runtime_candidate(
                    opened, source_lock_path=inputs["lock"]
                )

        shutil.rmtree(output)
        self.assemble(inputs, output)
        executable = output / "firefox" / "firefox"
        target = output / "firefox" / "firefox-bin"
        target.unlink()
        os.link(executable, target)
        os.utime(target, ns=(0, 0))
        with self.assertRaises(candidate.GeckoRuntimeCandidateError):
            with candidate.open_verified_gecko_runtime_candidate(
                output, source_lock_path=inputs["lock"]
            ):
                pass

        shutil.rmtree(output)
        self.assemble(inputs, output)
        moved = self.root / "candidate-moved"
        with candidate.open_verified_gecko_runtime_candidate(
            output, source_lock_path=inputs["lock"]
        ) as opened:
            output.rename(moved)
            with self.assertRaises(candidate.GeckoRuntimeCandidateError):
                candidate.revalidate_open_gecko_runtime_candidate(
                    opened, source_lock_path=inputs["lock"]
                )

    def test_descriptor_schema_is_closed_canonical_and_privacy_bounded(self) -> None:
        inputs = self.prepare_fixture()
        output = self.root / "candidate"
        descriptor = self.assemble(inputs, output)
        data = candidate.gecko_runtime_candidate_descriptor_to_dict(descriptor)
        data["provenance"]["cachePath"] = "/home/private/cache"
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "unknown"):
            candidate.gecko_runtime_candidate_descriptor_from_dict(data)

        descriptor_path = output / candidate.GECKO_RUNTIME_CANDIDATE_DESCRIPTOR
        document = json.loads(descriptor_path.read_text(encoding="ascii"))
        document["operator"] = "private-user"
        descriptor_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="ascii"
        )
        descriptor_path.chmod(0o644)
        os.utime(descriptor_path, ns=(0, 0))
        with self.assertRaisesRegex(candidate.GeckoRuntimeCandidateError, "unknown"):
            with candidate.open_verified_gecko_runtime_candidate(
                output, source_lock_path=inputs["lock"]
            ):
                pass


if __name__ == "__main__":
    unittest.main()
