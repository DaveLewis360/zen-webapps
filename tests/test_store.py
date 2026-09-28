import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zen_app_runtime._secure_fs as secure_fs
import zen_app_runtime.store as store_module
from zen_app_runtime.manifest import create_manifest
from zen_app_runtime.store import (
    AppAlreadyExists,
    AppNotFound,
    AppStore,
    StoreSafetyError,
)


class AppStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name) / "apps"
        self.store = AppStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def manifest(self, app_id="demo"):
        return create_manifest(app_id, "Demo", "https://demo.example.com", "Opaque", False)

    def test_create_list_get_remove_lifecycle(self):
        self.store.create(self.manifest())
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, (self.root / "demo.json").stat().st_mode & 0o777)
        self.assertEqual(["demo"], self.store.list_ids())
        self.assertEqual(self.manifest(), self.store.get("demo"))
        self.store.remove("demo")
        self.assertEqual([], self.store.list_ids())
        self.assertFalse((self.root / "demo.json").exists())
        tombstones = list(self.root.glob(".removed-demo-*.tombstone"))
        self.assertEqual(1, len(tombstones))
        self.assertEqual(0, tombstones[0].stat().st_size)
        self.assertEqual(0o600, tombstones[0].stat().st_mode & 0o777)

    def test_create_never_overwrites_and_uses_no_mutable_temp_source(self):
        with mock.patch.object(store_module.os, "link", side_effect=AssertionError("pathname link")):
            self.store.create(self.manifest())
        self.assertFalse(any(item.name.startswith(".new-") for item in self.root.iterdir()))
        original = (self.root / "demo.json").read_text(encoding="utf-8")
        with self.assertRaises(AppAlreadyExists):
            self.store.create(self.manifest())
        self.assertEqual(original, (self.root / "demo.json").read_text(encoding="utf-8"))

    def test_create_close_failure_reports_committed_durability(self):
        actual_close = secure_fs.os.close
        calls = 0

        def close_then_raise_once(descriptor):
            nonlocal calls
            calls += 1
            actual_close(descriptor)
            if calls == 1:
                raise OSError("injected publication close failure")

        with mock.patch.object(secure_fs.os, "close", side_effect=close_then_raise_once):
            with self.assertRaisesRegex(StoreSafetyError, "published.*destination identity"):
                self.store.create(self.manifest())
        self.assertEqual(self.manifest(), self.store.get("demo"))

    def test_create_detects_postlink_content_mutation(self):
        actual_link = secure_fs._link_fd_exclusive

        def mutate_after_link(source_fd, destination_fd, name):
            actual_link(source_fd, destination_fd, name)
            size = os.fstat(source_fd).st_size
            os.lseek(source_fd, 0, os.SEEK_SET)
            os.write(source_fd, b"x" * size)
            os.fsync(source_fd)

        with mock.patch.object(secure_fs, "_link_fd_exclusive", side_effect=mutate_after_link):
            with self.assertRaisesRegex(StoreSafetyError, "destination identity was lost"):
                self.store.create(self.manifest())
        with self.assertRaises(StoreSafetyError):
            self.store.get("demo")

    def test_create_detects_postlink_hardlink_addition(self):
        actual_link = secure_fs._link_fd_exclusive

        def add_link_after_publication(source_fd, destination_fd, name):
            actual_link(source_fd, destination_fd, name)
            os.link(
                f"/proc/self/fd/{source_fd}",
                "unexpected-link",
                dst_dir_fd=destination_fd,
                follow_symlinks=True,
            )

        with mock.patch.object(
            secure_fs,
            "_link_fd_exclusive",
            side_effect=add_link_after_publication,
        ):
            with self.assertRaisesRegex(StoreSafetyError, "destination identity was lost"):
                self.store.create(self.manifest())
        self.assertEqual(2, (self.root / "demo.json").stat().st_nlink)

    def test_missing_app_is_explicit(self):
        with self.assertRaises(AppNotFound):
            self.store.get("missing")
        with self.assertRaises(AppNotFound):
            self.store.remove("missing")

    def test_read_only_get_does_not_create_a_missing_store(self):
        with self.assertRaises(AppNotFound):
            self.store.get_read_only("missing")
        self.assertFalse(self.root.exists())

    def test_invalid_id_and_nonabsolute_or_tilde_root_cannot_reach_filesystem(self):
        with self.assertRaises(StoreSafetyError):
            self.store.get("../escape")
        with self.assertRaises(StoreSafetyError):
            self.store.remove("../escape")
        with self.assertRaisesRegex(StoreSafetyError, "absolute"):
            AppStore("~/apps").list_ids()
        with self.assertRaisesRegex(StoreSafetyError, "normalized"):
            AppStore(str(self.root.parent) + "/./apps").list_ids()

    def test_list_rejects_corrupt_definition(self):
        self.root.mkdir(mode=0o700)
        broken = self.root / "broken.json"
        broken.write_text(json.dumps({"schemaVersion": 1}), encoding="utf-8")
        broken.chmod(0o600)
        with self.assertRaises(StoreSafetyError):
            self.store.list_ids()

    def test_malformed_nested_type_is_translated_to_store_error(self):
        self.store.create(self.manifest())
        path = self.root / "demo.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["configuration"]["mode"] = []
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(StoreSafetyError):
            self.store.get("demo")

    def test_symlinked_or_permission_unsafe_store_root_is_rejected(self):
        real_root = Path(self.temporary.name) / "real"
        real_root.mkdir(mode=0o700)
        linked_root = Path(self.temporary.name) / "linked"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(StoreSafetyError):
            AppStore(linked_root).list_ids()

        unsafe_root = Path(self.temporary.name) / "unsafe"
        unsafe_root.mkdir(mode=0o755)
        with self.assertRaisesRegex(StoreSafetyError, "mode 0700"):
            AppStore(unsafe_root).list_ids()

    def test_descriptor_read_rejects_symlinked_oversized_and_hardlinked_definitions(self):
        self.root.mkdir(mode=0o700)
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        (self.root / "demo.json").symlink_to(outside)
        with self.assertRaises(StoreSafetyError):
            self.store.get("demo")
        (self.root / "demo.json").unlink()
        definition = self.root / "demo.json"
        definition.write_bytes(b" " * (1024 * 1024 + 1))
        definition.chmod(0o600)
        with self.assertRaisesRegex(StoreSafetyError, "size limit"):
            self.store.get("demo")
        definition.unlink()

        self.store.create(self.manifest())
        os.link(definition, self.root / "copy.json")
        with self.assertRaisesRegex(StoreSafetyError, "exactly one hard link"):
            self.store.get("demo")

    def test_descriptor_read_rejects_duplicate_json_fields(self):
        self.store.create(self.manifest())
        path = self.root / "demo.json"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(StoreSafetyError, "duplicate"):
            self.store.get("demo")

    def test_remove_quarantine_detects_source_swap_without_deleting_replacement(self):
        self.store.create(self.manifest())
        original_rename = store_module.rename_noreplace
        saved_original = self.root / "saved-original.json"

        def swap_then_rename(directory_fd, source, destination):
            if source == "demo.json":
                (self.root / source).rename(saved_original)
                replacement = self.root / source
                replacement.write_text("replacement must survive\n", encoding="utf-8")
                replacement.chmod(0o600)
            return original_rename(directory_fd, source, destination)

        with mock.patch.object(store_module, "rename_noreplace", side_effect=swap_then_rename):
            with self.assertRaisesRegex(StoreSafetyError, "changed during atomic removal quarantine"):
                self.store.remove("demo")
        self.assertEqual("replacement must survive\n", (self.root / "demo.json").read_text(encoding="utf-8"))
        self.assertEqual([], list(self.root.glob(".removed-demo-*.tombstone")))
        self.assertTrue(saved_original.is_file())

    def test_remove_never_unlinks_a_postvalidation_path(self):
        self.store.create(self.manifest())
        with mock.patch.object(store_module.os, "unlink", side_effect=AssertionError("unsafe unlink")):
            self.store.remove("demo")
        self.assertFalse((self.root / "demo.json").exists())
        self.assertEqual([], self.store.list_ids())

    def test_remove_close_failure_reports_committed_durability(self):
        self.store.create(self.manifest())
        actual_close = store_module.os.close
        calls = 0

        def close_then_raise_once(descriptor):
            nonlocal calls
            calls += 1
            actual_close(descriptor)
            if calls == 1:
                raise OSError("injected removal close failure")

        with mock.patch.object(store_module.os, "close", side_effect=close_then_raise_once):
            with self.assertRaisesRegex(StoreSafetyError, "removal completed.*could not be closed"):
                self.store.remove("demo")
        self.assertFalse((self.root / "demo.json").exists())
        self.assertEqual(0, next(self.root.glob(".removed-demo-*.tombstone")).stat().st_size)

    def test_postquarantine_oserror_reports_partial_removal_state(self):
        self.store.create(self.manifest())
        with mock.patch.object(store_module.os, "ftruncate", side_effect=OSError("injected truncate failure")):
            with self.assertRaisesRegex(
                StoreSafetyError,
                "removal changed visible state",
            ):
                self.store.remove("demo")
        self.assertFalse((self.root / "demo.json").exists())
        tombstone = next(self.root.glob(".removed-demo-*.tombstone"))
        self.assertGreater(tombstone.stat().st_size, 0)

    def test_postquarantine_cancellation_becomes_partial_removal_error(self):
        self.store.create(self.manifest("cancelled"))
        with mock.patch.object(
            store_module.os,
            "ftruncate",
            side_effect=KeyboardInterrupt("injected cancellation"),
        ):
            with self.assertRaisesRegex(
                StoreSafetyError,
                "removal changed visible state",
            ):
                self.store.remove("cancelled")
        self.assertFalse((self.root / "cancelled.json").exists())
        self.assertEqual(1, len(list(self.root.glob(".removed-cancelled-*.tombstone"))))

    def test_malformed_safe_tombstone_name_is_unexpected(self):
        self.root.mkdir(mode=0o700)
        malformed = self.root / ".removed-not-generated.tombstone"
        malformed.write_bytes(b"")
        malformed.chmod(0o600)
        with self.assertRaisesRegex(StoreSafetyError, "unexpected entry"):
            self.store.list_ids()

    def test_unsafe_tombstone_is_not_silently_ignored(self):
        self.store.create(self.manifest())
        self.store.remove("demo")
        tombstone = next(self.root.glob(".removed-demo-*.tombstone"))
        tombstone.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(StoreSafetyError, "unsafe removed-definition tombstone"):
            self.store.list_ids()

    def test_sync_failure_reports_committed_state(self):
        with mock.patch.object(
            self.store,
            "_fsync_directory",
            side_effect=OSError("injected sync failure"),
        ):
            with self.assertRaisesRegex(StoreSafetyError, "created.*durability"):
                self.store.create(self.manifest())
            self.assertTrue((self.root / "demo.json").is_file())

            with self.assertRaisesRegex(StoreSafetyError, "removed.*durability"):
                self.store.remove("demo")
            self.assertFalse((self.root / "demo.json").exists())


if __name__ == "__main__":
    unittest.main()
