import hashlib
import json
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zen_app_runtime.profile as profile
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.profile import (
    PROFILE_MARKER_NAME,
    ProfileDurabilityError,
    ProfileRollbackError,
    ProfileSafetyError,
    provision_profile,
)
from zen_app_runtime.runtime import RuntimeIdentity, verify_runtime


class ProfileProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.base = Path(self.temporary.name)
        self.profile_root = self.base / "managed-profiles"
        self.manifest = create_manifest("demo", "Demo", "https://demo.example.com")
        self.runtime_binary = self.base / "runtime" / "zen"
        self.runtime_binary.parent.mkdir(mode=0o700)
        self.runtime_binary.write_bytes(b"verified profile runtime\n")
        self.runtime_binary.chmod(0o700)
        self.runtime_lock = self.base / "runtime-lock.json"
        self._write_lock(self.runtime_lock, self.runtime_binary)
        self.identity = verify_runtime(self.runtime_lock)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_lock(lock: Path, binary: Path) -> None:
        lock.write_text(
            json.dumps(
                {
                    "runtimeLockVersion": 1,
                    "runtime": {
                        "path": str(binary),
                        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        lock.chmod(0o600)

    def test_provision_creates_only_managed_root_app_directory_and_canonical_markers(self):
        result = provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual("created", result.status)
        self.assertEqual(self.profile_root / "demo", result.path)
        self.assertEqual(0o700, stat.S_IMODE(self.profile_root.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(result.path.stat().st_mode))

        root_marker = self.profile_root / ".zen-app-runtime-root.json"
        app_marker = result.path / PROFILE_MARKER_NAME
        self.assertEqual(0o600, stat.S_IMODE(root_marker.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(app_marker.stat().st_mode))
        self.assertEqual(1, root_marker.stat().st_nlink)
        self.assertEqual(1, app_marker.stat().st_nlink)
        expected_root = {"markerVersion": 1, "profileRoot": str(self.profile_root)}
        self.assertEqual(json.dumps(expected_root, indent=2, sort_keys=True) + "\n", root_marker.read_text())
        marker = json.loads(app_marker.read_text())
        self.assertEqual(
            {"appId", "manifest", "markerVersion", "profileRoot", "runtimeIdentity"},
            set(marker),
        )
        self.assertEqual(1, marker["markerVersion"])
        self.assertEqual("demo", marker["appId"])
        self.assertEqual(str(self.profile_root), marker["profileRoot"])
        self.assertEqual(1, marker["manifest"]["schemaVersion"])
        self.assertEqual(profile.manifest_sha256(self.manifest), marker["manifest"]["sha256"])
        self.assertEqual(profile.runtime_identity_to_dict(self.identity), marker["runtimeIdentity"])
        self.assertFalse(any(item.name.startswith(".new-") for item in self.profile_root.rglob("*")))

    def test_authoritative_api_rejects_fabricated_runtime_identity(self):
        fabricated = RuntimeIdentity(1, "/never/verified/runtime", "a" * 64)
        with self.assertRaisesRegex(ProfileSafetyError, "runtime verification failed"):
            provision_profile(self.profile_root, self.manifest, fabricated)
        self.assertFalse(self.profile_root.exists())

    def test_exact_match_is_idempotent_and_any_binding_mismatch_fails(self):
        first = provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        marker = first.path / PROFILE_MARKER_NAME
        before = (marker.read_bytes(), marker.stat().st_ino)
        second = provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual("existing", second.status)
        self.assertEqual(first.path, second.path)
        self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino))

        changed_binary = self.base / "runtime" / "other-zen"
        changed_binary.write_bytes(b"different verified runtime\n")
        changed_binary.chmod(0o700)
        changed_lock = self.base / "changed-runtime-lock.json"
        self._write_lock(changed_lock, changed_binary)
        with self.assertRaisesRegex(ProfileSafetyError, "binding mismatch"):
            provision_profile(self.profile_root, self.manifest, changed_lock)
        changed_manifest = create_manifest("demo", "Changed", "https://demo.example.com")
        with self.assertRaisesRegex(ProfileSafetyError, "binding mismatch"):
            provision_profile(self.profile_root, changed_manifest, self.runtime_lock)

    def test_requires_absolute_normalized_root_and_never_adopts_unmarked_or_legacy_root(self):
        with self.assertRaisesRegex(ProfileSafetyError, "absolute"):
            provision_profile("profiles", self.manifest, self.runtime_lock)
        with self.assertRaisesRegex(ProfileSafetyError, "normalized"):
            provision_profile(str(self.base) + "/./profiles", self.manifest, self.runtime_lock)
        unmarked = self.base / "unmarked"
        unmarked.mkdir(mode=0o700)
        with self.assertRaisesRegex(ProfileSafetyError, "sentinel"):
            provision_profile(unmarked, self.manifest, self.runtime_lock)
        self.assertEqual([], list(unmarked.iterdir()))

        legacy = self.base / "legacy"
        legacy.mkdir(mode=0o700)
        (legacy / ".zen-app-runtime-profile-root.json").write_text("{}\n", encoding="utf-8")
        (legacy / ".zen-app-runtime-profile-root.json").chmod(0o600)
        with self.assertRaisesRegex(ProfileSafetyError, "sentinel"):
            provision_profile(legacy, self.manifest, self.runtime_lock)

    def test_symlinks_unsafe_modes_and_hardlinked_markers_fail_closed(self):
        real = self.base / "real"
        real.mkdir(mode=0o700)
        linked = self.base / "linked"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ProfileSafetyError, "symbolic link|cannot open"):
            provision_profile(linked, self.manifest, self.runtime_lock)

        result = provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        result.path.chmod(0o750)
        with self.assertRaisesRegex(ProfileSafetyError, "mode 0700"):
            provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        result.path.chmod(0o700)
        marker = result.path / PROFILE_MARKER_NAME
        marker.chmod(0o640)
        with self.assertRaisesRegex(ProfileSafetyError, "mode 0600"):
            provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        marker.chmod(0o600)
        os.link(marker, result.path / "marker-hardlink")
        with self.assertRaisesRegex(ProfileSafetyError, "exactly one hard link"):
            provision_profile(self.profile_root, self.manifest, self.runtime_lock)

    def test_precommit_failure_rolls_back_resources_created_by_the_attempt(self):
        actual_publish = profile._publish_exclusive
        calls = 0

        def fail_second_publish(directory_fd, name, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ProfileSafetyError("injected publication failure")
            return actual_publish(directory_fd, name, payload)

        with mock.patch.object(profile, "_publish_exclusive", side_effect=fail_second_publish):
            with self.assertRaisesRegex(ProfileSafetyError, "publication failure"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertFalse(self.profile_root.exists())

    def test_base_exception_before_commit_runs_rollback(self):
        actual_publish = profile._publish_exclusive
        calls = 0

        def interrupt_second_publish(directory_fd, name, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("injected cancellation")
            return actual_publish(directory_fd, name, payload)

        with mock.patch.object(profile, "_publish_exclusive", side_effect=interrupt_second_publish):
            with self.assertRaises(KeyboardInterrupt):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertFalse(self.profile_root.exists())

    def test_managed_directory_validation_cancellation_closes_fd_and_rolls_back_root(self):
        actual_require = profile._require_directory
        before = len(os.listdir("/proc/self/fd"))

        def interrupt_managed_root(info, label, *, managed):
            if label == "managed profile root":
                raise KeyboardInterrupt("injected directory validation cancellation")
            return actual_require(info, label, managed=managed)

        actual_close = profile.os.close
        injected = False

        def close_managed_root_then_raise(descriptor):
            nonlocal injected
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                target = ""
            actual_close(descriptor)
            if not injected and target == str(self.profile_root):
                injected = True
                raise OSError("injected cleanup close failure")

        with mock.patch.object(profile, "_require_directory", side_effect=interrupt_managed_root), \
             mock.patch.object(profile.os, "close", side_effect=close_managed_root_then_raise):
            with self.assertRaises(KeyboardInterrupt):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))
        self.assertFalse(self.profile_root.exists())

    def test_failed_rollback_preserves_marked_root_and_reports_cleanup_failure(self):
        actual_publish = profile._publish_exclusive
        actual_rmdir = os.rmdir
        publish_calls = 0

        def fail_profile_publish(directory_fd, name, payload):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 2:
                raise ProfileSafetyError("injected publication failure")
            return actual_publish(directory_fd, name, payload)

        def fail_app_rmdir(path, **kwargs):
            if path == "demo":
                raise OSError("injected rollback failure")
            return actual_rmdir(path, **kwargs)

        with mock.patch.object(profile, "_publish_exclusive", side_effect=fail_profile_publish), \
             mock.patch.object(profile.os, "rmdir", side_effect=fail_app_rmdir):
            with self.assertRaisesRegex(ProfileRollbackError, "marked root was preserved"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertTrue((self.profile_root / ".zen-app-runtime-root.json").is_file())
        self.assertTrue((self.profile_root / "demo").is_dir())

    def test_rebound_app_directory_never_returns_success_or_marks_replacement(self):
        actual_publish = profile._publish_exclusive
        calls = 0

        def rebind_before_profile_publish(directory_fd, name, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                original = self.profile_root / "demo"
                detached = self.profile_root / "detached"
                original.rename(detached)
                original.mkdir(mode=0o700)
            return actual_publish(directory_fd, name, payload)

        with mock.patch.object(profile, "_publish_exclusive", side_effect=rebind_before_profile_publish):
            with self.assertRaisesRegex(ProfileDurabilityError, "visible path identity changed"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertFalse((self.profile_root / "demo" / PROFILE_MARKER_NAME).exists())
        self.assertTrue((self.profile_root / "detached" / PROFILE_MARKER_NAME).is_file())

    def test_rebound_root_directory_never_returns_success_or_marks_replacement(self):
        actual_publish = profile._publish_exclusive
        calls = 0

        def rebind_root_before_profile_publish(directory_fd, name, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                detached = self.base / "detached-root"
                self.profile_root.rename(detached)
                self.profile_root.mkdir(mode=0o700)
            return actual_publish(directory_fd, name, payload)

        with mock.patch.object(profile, "_publish_exclusive", side_effect=rebind_root_before_profile_publish):
            with self.assertRaisesRegex(ProfileDurabilityError, "visible path identity changed"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertEqual([], list(self.profile_root.iterdir()))
        self.assertTrue((self.base / "detached-root" / "demo" / PROFILE_MARKER_NAME).is_file())

    def test_marker_identity_change_during_read_is_detected(self):
        provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        original_read = profile.os.read
        mutated = False

        def mutate_after_read(descriptor, size):
            nonlocal mutated
            payload = original_read(descriptor, size)
            if not payload and not mutated:
                mutated = True
                os.fchmod(descriptor, 0o400)
            return payload

        with mock.patch.object(profile.os, "read", side_effect=mutate_after_read):
            with self.assertRaisesRegex(ProfileSafetyError, "changed while reading"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)

    def test_committed_profile_close_failure_reports_durability(self):
        actual_close = profile.os.close
        injected = False
        expected_target = str(self.profile_root / "demo")

        def close_profile_then_raise(descriptor):
            nonlocal injected
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                target = ""
            actual_close(descriptor)
            if not injected and target == expected_target:
                injected = True
                raise OSError("injected committed close failure")

        with mock.patch.object(profile.os, "close", side_effect=close_profile_then_raise):
            with self.assertRaisesRegex(ProfileDurabilityError, "committed.*cleanup failed"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertTrue((self.profile_root / "demo" / PROFILE_MARKER_NAME).is_file())

    def test_postcommit_sync_failure_reports_durability_and_preserves_profile(self):
        with mock.patch.object(profile, "_sync_committed_profile", side_effect=OSError("injected sync failure")):
            with self.assertRaisesRegex(ProfileDurabilityError, "created.*durability"):
                provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        self.assertTrue((self.profile_root / "demo" / PROFILE_MARKER_NAME).is_file())

    def test_manifest_hash_format_is_frozen_for_unicode(self):
        manifest = create_manifest(
            "unicode",
            "Árvíztűrő tükörfúrógép",
            "https://example.com",
        )
        self.assertEqual(
            "5375fb7943193233efe6b142d8d071b5b3ae0a93fab6d09f2517c3359a70f515",
            profile.manifest_sha256(manifest),
        )

    def test_does_not_access_default_firefox_profile_execute_or_use_network(self):
        with mock.patch.object(Path, "home", side_effect=AssertionError("default profile access")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("execution")), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("execution")), \
             mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
            provision_profile(self.profile_root, self.manifest, self.runtime_lock)
        touched = {item.name.lower() for item in self.profile_root.rglob("*")}
        self.assertFalse(any("firefox" in name or "mozilla" in name for name in touched))


if __name__ == "__main__":
    unittest.main()
