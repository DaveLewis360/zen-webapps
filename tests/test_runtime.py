import hashlib
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zen_app_runtime.runtime as runtime
from zen_app_runtime.runtime import (
    MAX_LOCK_BYTES,
    RuntimeSafetyError,
    open_verified_runtime,
    render_runtime_identity,
    verify_runtime,
)


class RuntimeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.binary = self.root / "runtime" / "zen"
        self.binary.parent.mkdir(mode=0o700)
        self.binary.write_bytes(b"verified runtime\n")
        self.binary.chmod(0o700)
        self.lock = self.root / "runtime.lock.json"
        self.write_lock()

    def tearDown(self):
        self.temporary.cleanup()

    def write_lock(self, **changes):
        default_digest = (
            hashlib.sha256(self.binary.read_bytes()).hexdigest()
            if self.binary.is_file()
            else "0" * 64
        )
        data = {
            "runtimeLockVersion": 1,
            "runtime": {
                "path": str(self.binary),
                "sha256": default_digest,
            },
        }
        for key, value in changes.items():
            if key in data["runtime"]:
                data["runtime"][key] = value
            else:
                data[key] = value
        self.lock.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.lock.chmod(0o600)

    def test_verified_identity_is_logical_versioned_and_deterministic(self):
        identity = verify_runtime(self.lock)
        expected = {
            "identityVersion": 1,
            "path": str(self.binary),
            "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        }
        self.assertEqual(expected, runtime.runtime_identity_to_dict(identity))
        self.assertEqual(json.dumps(expected, indent=2, sort_keys=True) + "\n", render_runtime_identity(identity))

    def test_open_verified_runtime_retains_then_closes_immutable_snapshot(self):
        with open_verified_runtime(self.lock) as opened:
            self.assertEqual(str(self.binary), opened.identity.path)
            self.assertTrue(opened.sealed_snapshot)
            self.assertEqual(self.binary.stat().st_size, opened.size)
            snapshot_info = os.fstat(opened.descriptor)
            self.assertNotEqual(self.binary.stat().st_ino, snapshot_info.st_ino)
            self.assertEqual(0o500, snapshot_info.st_mode & 0o777)
            self.assertEqual(
                runtime._REQUIRED_SEALS,
                runtime.fcntl.fcntl(opened.descriptor, runtime._F_GET_SEALS)
                & runtime._REQUIRED_SEALS,
            )
            os.lseek(opened.descriptor, 0, os.SEEK_SET)
            self.assertEqual(self.binary.read_bytes(), os.read(opened.descriptor, opened.size))
            with self.assertRaises(OSError):
                os.write(opened.descriptor, b"X")
            descriptor = opened.descriptor
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_lock_schema_is_closed_versioned_bounded_and_utf8(self):
        cases = (
            ({"runtimeLockVersion": 2}, "runtimeLockVersion"),
            ({"unknown": True}, "fields"),
            ({"sha256": "A" * 64}, "sha256"),
            ({"path": str(self.binary.parent / "." / "zen") + "/../zen"}, "normalized"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                self.write_lock(**changes)
                with self.assertRaisesRegex(RuntimeSafetyError, message):
                    verify_runtime(self.lock)
        self.lock.write_text(
            json.dumps(
                {
                    "lockVersion": 1,
                    "runtime": {"path": str(self.binary), "sha256": "0" * 64},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeSafetyError, "fields"):
            verify_runtime(self.lock)
        self.lock.write_bytes(
            b'{"runtimeLockVersion":1,"runtimeLockVersion":1,"runtime":{}}'
        )
        with self.assertRaisesRegex(RuntimeSafetyError, "duplicate"):
            verify_runtime(self.lock)
        self.lock.write_bytes(b"x" * (MAX_LOCK_BYTES + 1))
        with self.assertRaisesRegex(RuntimeSafetyError, "size limit"):
            verify_runtime(self.lock)
        self.lock.write_bytes(b"\xff")
        with self.assertRaisesRegex(RuntimeSafetyError, "UTF-8"):
            verify_runtime(self.lock)

    def test_lock_and_runtime_paths_must_be_absolute_normalized_and_not_symlinks(self):
        with self.assertRaisesRegex(RuntimeSafetyError, "lock path must be absolute"):
            verify_runtime("relative.lock")
        with self.assertRaisesRegex(RuntimeSafetyError, "normalized"):
            verify_runtime(str(self.lock.parent) + "/./runtime.lock.json")
        linked_lock = self.root / "linked.lock"
        linked_lock.symlink_to(self.lock)
        with self.assertRaisesRegex(RuntimeSafetyError, "symbolic link|cannot open"):
            verify_runtime(linked_lock)
        linked_runtime = self.root / "linked-runtime"
        linked_runtime.symlink_to(self.binary)
        self.write_lock(path=str(linked_runtime))
        with self.assertRaisesRegex(RuntimeSafetyError, "symbolic link|cannot open"):
            verify_runtime(self.lock)

    def test_unsafe_modes_non_regular_non_executable_and_wrong_hash_fail_closed(self):
        self.binary.chmod(0o720)
        with self.assertRaisesRegex(RuntimeSafetyError, "group/world writable"):
            verify_runtime(self.lock)
        self.binary.chmod(0o400)
        with self.assertRaisesRegex(RuntimeSafetyError, "effective user|cannot open runtime executable"):
            verify_runtime(self.lock)
        self.binary.chmod(0o700)
        self.write_lock(sha256="0" * 64)
        with self.assertRaisesRegex(RuntimeSafetyError, "digest"):
            verify_runtime(self.lock)
        self.binary.unlink()
        self.binary.mkdir(mode=0o700)
        self.write_lock(path=str(self.binary), sha256="0" * 64)
        with self.assertRaisesRegex(RuntimeSafetyError, "regular file"):
            verify_runtime(self.lock)

    def test_group_or_world_writable_ancestor_is_rejected(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o700)
        moved = unsafe / "zen"
        moved.write_bytes(b"runtime")
        moved.chmod(0o700)
        unsafe.chmod(0o777)
        self.binary = moved
        self.write_lock()
        with self.assertRaisesRegex(RuntimeSafetyError, "ancestor.*group/world writable"):
            verify_runtime(self.lock)

    def test_open_file_identity_change_during_snapshot_is_detected(self):
        original = runtime._write_all
        mutated = False

        def mutate_during_snapshot(descriptor, payload):
            nonlocal mutated
            original(descriptor, payload)
            if not mutated:
                mutated = True
                self.binary.chmod(0o500)

        with mock.patch.object(runtime, "_write_all", side_effect=mutate_during_snapshot):
            with self.assertRaisesRegex(RuntimeSafetyError, "changed while snapshotting"):
                verify_runtime(self.lock)

    def test_open_lock_identity_change_during_read_is_detected(self):
        original = runtime._read_bounded_utf8

        def mutate_after_read(descriptor, limit, label):
            text = original(descriptor, limit, label)
            os.fchmod(descriptor, 0o400)
            return text

        with mock.patch.object(runtime, "_read_bounded_utf8", side_effect=mutate_after_read):
            with self.assertRaisesRegex(RuntimeSafetyError, "changed while reading"):
                verify_runtime(self.lock)

    def test_required_platform_flags_fail_closed(self):
        with mock.patch.object(runtime.os, "O_CLOEXEC", None):
            with self.assertRaisesRegex(RuntimeSafetyError, "close-on-exec"):
                verify_runtime(self.lock)

    def test_verification_never_executes_or_uses_network(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("execution")), \
             mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            verify_runtime(self.lock)


if __name__ == "__main__":
    unittest.main()
