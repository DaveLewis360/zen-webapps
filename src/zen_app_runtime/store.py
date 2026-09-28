"""Descriptor-relative filesystem store for validated app definitions."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any, Iterator

from ._secure_fs import (
    PublishedIdentityError,
    SecureFsCapabilityError,
    publish_bytes_exclusive,
    rename_noreplace,
    same_inode,
)
from .manifest import APP_ID_PATTERN, AppManifest, ManifestError, manifest_from_dict, manifest_to_dict

MAX_DEFINITION_BYTES = 1024 * 1024
_TOMBSTONE_PATTERN = re.compile(
    r"^\.removed-(?P<app_id>.+)-(?P<token>[0-9a-f]{32})\.tombstone$"
)


def _is_generated_tombstone_name(name: str) -> bool:
    match = _TOMBSTONE_PATTERN.fullmatch(name)
    return bool(match and APP_ID_PATTERN.fullmatch(match.group("app_id")))


class StoreError(RuntimeError):
    """Base error for app-definition persistence."""


class StoreSafetyError(StoreError):
    """The store or a definition failed a safety check."""


class StoreDurabilityError(StoreSafetyError):
    """A mutation committed, but durable persistence could not be confirmed."""


class AppAlreadyExists(StoreError):
    """Creation would overwrite an existing app definition."""


class AppNotFound(StoreError):
    """The requested app definition does not exist."""


def _platform_flags() -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if nofollow is None or directory is None or close_on_exec is None or not hasattr(os, "geteuid"):
        raise StoreSafetyError(
            "app storage requires Linux/POSIX no-follow, close-on-exec directory operations"
        )
    common = os.O_RDONLY | nofollow | close_on_exec
    return common, common | directory


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _require_safe_ancestor(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise StoreSafetyError(f"{label} is not a directory")
    if info.st_uid not in {0, os.geteuid()}:
        raise StoreSafetyError(f"{label} owner must be root or the effective user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise StoreSafetyError(f"{label} is group/world writable")


def _require_managed_root(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise StoreSafetyError(f"{label} is not a directory")
    if info.st_uid != os.geteuid():
        raise StoreSafetyError(f"{label} must be owned by the effective user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise StoreSafetyError(f"{label} must have mode 0700")


def _open_error(label: str, error: OSError) -> StoreSafetyError:
    if error.errno == errno.ELOOP:
        return StoreSafetyError(f"{label} is or traverses a symbolic link")
    return StoreSafetyError(f"cannot open {label}: {error}")


def _reject_constant(value: str) -> None:
    raise StoreSafetyError(f"non-finite JSON number is not permitted: {value}")


class AppStore:
    def __init__(self, root: os.PathLike[str] | str):
        if isinstance(root, bytes):
            raise StoreSafetyError("store root path must be text")
        try:
            rendered = os.fspath(root)
        except TypeError as error:
            raise StoreSafetyError("store root path must be path-like text") from error
        if not isinstance(rendered, str):
            raise StoreSafetyError("store root path must be text")
        self._root_raw = rendered
        self.root = Path(rendered)

    def _root_components(self) -> tuple[str, list[str]]:
        rendered = self._root_raw
        if not isinstance(rendered, str) or not rendered.startswith("/") or rendered.startswith("//"):
            raise StoreSafetyError("store root must be an absolute normalized path")
        if rendered == "/":
            raise StoreSafetyError("store root must not be the filesystem root")
        components = rendered[1:].split("/")
        if any(component in ("", ".", "..") for component in components):
            raise StoreSafetyError("store root must be an absolute normalized path")
        if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
            raise StoreSafetyError("store root cannot contain control characters")
        return rendered, components

    @contextmanager
    def _open_root(self, *, create: bool, missing_app_id: str | None = None) -> Iterator[int]:
        rendered, components = self._root_components()
        common_flags, directory_flags = _platform_flags()
        del common_flags
        descriptors: list[int] = []
        try:
            try:
                current = os.open("/", directory_flags)
            except OSError as error:
                raise _open_error("store ancestor /", error) from error
            descriptors.append(current)
            _require_safe_ancestor(os.fstat(current), "store ancestor /")

            traversed = ""
            for index, component in enumerate(components):
                traversed += "/" + component
                created = False
                try:
                    child = os.open(component, directory_flags, dir_fd=current)
                except FileNotFoundError as error:
                    if not create:
                        if missing_app_id is not None:
                            raise AppNotFound(f"app definition not found: {missing_app_id}") from error
                        raise StoreSafetyError(f"store root does not exist: {rendered}") from error
                    try:
                        os.mkdir(component, 0o700, dir_fd=current)
                        created = True
                        child = os.open(component, directory_flags, dir_fd=current)
                        os.fchmod(child, 0o700)
                        os.fsync(current)
                    except OSError as create_error:
                        raise StoreSafetyError(
                            f"cannot create store directory {traversed}: {create_error}"
                        ) from create_error
                except OSError as error:
                    raise _open_error(f"store path {traversed}", error) from error
                descriptors.append(child)
                current = child
                info = os.fstat(current)
                if index == len(components) - 1:
                    _require_managed_root(info, "store root")
                elif created:
                    _require_managed_root(info, f"created store ancestor {traversed}")
                else:
                    _require_safe_ancestor(info, f"store ancestor {traversed}")
            yield current
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _filename(app_id: str) -> str:
        if not isinstance(app_id, str) or not APP_ID_PATTERN.fullmatch(app_id):
            raise StoreSafetyError("invalid app ID; refusing filesystem access")
        return f"{app_id}.json"

    @staticmethod
    def _fsync_directory(descriptor: int) -> None:
        os.fsync(descriptor)

    @staticmethod
    def _definition_payload(manifest: AppManifest) -> bytes:
        try:
            return (
                json.dumps(
                    manifest_to_dict(manifest),
                    allow_nan=False,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (ManifestError, TypeError, AttributeError, ValueError) as error:
            raise StoreSafetyError(f"refusing invalid manifest: {error}") from error

    def create(self, manifest: AppManifest) -> None:
        payload = self._definition_payload(manifest)
        filename = self._filename(manifest.app_id)
        with self._open_root(create=True) as root_fd:
            try:
                publish_bytes_exclusive(root_fd, filename, payload, mode=0o600)
            except FileExistsError as error:
                raise AppAlreadyExists(
                    f"app definition already exists: {manifest.app_id}"
                ) from error
            except PublishedIdentityError as error:
                try:
                    self._fsync_directory(root_fd)
                except OSError:
                    pass
                raise StoreDurabilityError(
                    f"app definition {manifest.app_id} was published, but its destination identity was lost: {error}"
                ) from error
            except (OSError, SecureFsCapabilityError) as error:
                raise StoreSafetyError(
                    f"cannot create app definition {manifest.app_id}: {error}"
                ) from error
            try:
                self._fsync_directory(root_fd)
            except OSError as error:
                raise StoreDurabilityError(
                    f"app definition {manifest.app_id} was created, but durability could not be confirmed: {error}"
                ) from error

    def _read_manifest_from_root(
        self,
        root_fd: int,
        app_id: str,
        *,
        retain_descriptor: bool = False,
        writable: bool = False,
    ) -> tuple[AppManifest, os.stat_result, int | None]:
        filename = self._filename(app_id)
        common_flags, _ = _platform_flags()
        open_flags = common_flags | (os.O_RDWR if writable else os.O_RDONLY)
        descriptor: int | None = None
        retained_descriptor: int | None = None

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise StoreSafetyError(
                        f"invalid app definition {app_id}: duplicate JSON field: {key}"
                    )
                value[key] = item
            return value

        try:
            try:
                descriptor = os.open(filename, open_flags, dir_fd=root_fd)
            except FileNotFoundError as error:
                raise AppNotFound(f"app definition not found: {app_id}") from error
            except OSError as error:
                raise _open_error(f"app definition {app_id}", error) from error
            initial_info = os.fstat(descriptor)
            if not stat.S_ISREG(initial_info.st_mode):
                raise StoreSafetyError(f"app definition is not a regular file: {app_id}")
            if initial_info.st_uid != os.geteuid():
                raise StoreSafetyError(f"app definition must be owned by the effective user: {app_id}")
            if stat.S_IMODE(initial_info.st_mode) != 0o600:
                raise StoreSafetyError(f"app definition must have mode 0600: {app_id}")
            if initial_info.st_nlink != 1:
                raise StoreSafetyError(f"app definition must have exactly one hard link: {app_id}")

            chunks: list[bytes] = []
            remaining = MAX_DEFINITION_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_DEFINITION_BYTES:
                raise StoreSafetyError(
                    f"invalid app definition {app_id}: exceeds the {MAX_DEFINITION_BYTES}-byte size limit"
                )
            final_info = os.fstat(descriptor)
            if _stat_identity(initial_info) != _stat_identity(final_info):
                raise StoreSafetyError(f"invalid app definition {app_id}: file changed while reading")
            text = payload.decode("utf-8", errors="strict")
            data = json.loads(
                text,
                object_pairs_hook=reject_duplicates,
                parse_constant=_reject_constant,
            )
            manifest = manifest_from_dict(data)
            if manifest.app_id != app_id:
                raise StoreSafetyError(f"app definition filename/ID mismatch: {filename}")
            if retain_descriptor:
                retained_descriptor = descriptor
                descriptor = None
        except StoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ManifestError) as error:
            raise StoreSafetyError(f"invalid app definition {app_id}: {error}") from error
        finally:
            primary_active = sys.exc_info()[0] is not None
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    if not primary_active:
                        raise StoreSafetyError(
                            f"validated app definition descriptor could not be closed: {error}"
                        ) from error
        return manifest, final_info, retained_descriptor

    def get(self, app_id: str) -> AppManifest:
        self._filename(app_id)
        with self._open_root(create=True) as root_fd:
            manifest, _, _ = self._read_manifest_from_root(root_fd, app_id)
            return manifest

    def get_read_only(self, app_id: str) -> AppManifest:
        """Read a definition without creating a missing store directory."""
        self._filename(app_id)
        with self._open_root(create=False, missing_app_id=app_id) as root_fd:
            manifest, _, _ = self._read_manifest_from_root(root_fd, app_id)
            return manifest

    def list_ids(self) -> list[str]:
        with self._open_root(create=True) as root_fd:
            try:
                names = sorted(os.listdir(root_fd))
            except OSError as error:
                raise StoreSafetyError(f"cannot list app definitions: {error}") from error
            app_ids: list[str] = []
            for name in names:
                if _is_generated_tombstone_name(name):
                    try:
                        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    except OSError as error:
                        raise StoreSafetyError(
                            f"cannot inspect removed-definition tombstone {name}: {error}"
                        ) from error
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1
                        or info.st_size != 0
                    ):
                        raise StoreSafetyError(
                            f"unsafe removed-definition tombstone in store: {name}"
                        )
                    continue
                path = Path(name)
                if path.suffix != ".json" or not APP_ID_PATTERN.fullmatch(path.stem):
                    raise StoreSafetyError(f"unexpected entry in app-definition store: {name}")
                manifest, _, _ = self._read_manifest_from_root(root_fd, path.stem)
                app_ids.append(manifest.app_id)
            return app_ids

    def remove(self, app_id: str) -> None:
        filename = self._filename(app_id)
        tombstone = f".removed-{app_id}-{secrets.token_hex(16)}.tombstone"
        with self._open_root(create=True) as root_fd:
            validated_fd: int | None = None
            opened_info: os.stat_result | None = None
            quarantined_info: os.stat_result | None = None
            renamed = False
            irreversible = False
            try:
                _, opened_info, validated_fd = self._read_manifest_from_root(
                    root_fd,
                    app_id,
                    retain_descriptor=True,
                    writable=True,
                )
                if validated_fd is None:  # pragma: no cover - internal contract.
                    raise StoreSafetyError("validated definition descriptor was not retained")
                try:
                    rename_noreplace(root_fd, filename, tombstone)
                    renamed = True
                except FileNotFoundError as error:
                    raise AppNotFound(f"app definition not found: {app_id}") from error
                except OSError as error:
                    raise StoreSafetyError(
                        f"cannot quarantine app definition {app_id}: {error}"
                    ) from error

                quarantined_info = os.stat(
                    tombstone,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                if not same_inode(opened_info, quarantined_info):
                    raise StoreSafetyError(
                        f"app definition {app_id} changed during atomic removal quarantine"
                    )

                irreversible = True
                os.ftruncate(validated_fd, 0)
                os.fchmod(validated_fd, 0o600)
                os.fsync(validated_fd)
                final_info = os.stat(
                    tombstone,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                held_info = os.fstat(validated_fd)
                if (
                    not same_inode(held_info, final_info)
                    or not stat.S_ISREG(final_info.st_mode)
                    or final_info.st_uid != os.geteuid()
                    or stat.S_IMODE(final_info.st_mode) != 0o600
                    or final_info.st_nlink != 1
                    or final_info.st_size != 0
                ):
                    raise StoreSafetyError(
                        f"removed app definition {app_id} tombstone identity changed"
                    )
                try:
                    os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise StoreSafetyError(
                        f"app definition {app_id} reappeared during removal"
                    )
                try:
                    self._fsync_directory(root_fd)
                except OSError as error:
                    raise StoreDurabilityError(
                        f"app definition {app_id} was removed, but durability could not be confirmed: {error}"
                    ) from error
            except BaseException as error:
                if renamed and not irreversible:
                    restoration_error: BaseException | None = None
                    try:
                        rename_noreplace(root_fd, tombstone, filename)
                        restored_info = os.stat(
                            filename,
                            dir_fd=root_fd,
                            follow_symlinks=False,
                        )
                        expected_info = quarantined_info or opened_info
                        if expected_info is None or not same_inode(expected_info, restored_info):
                            raise StoreSafetyError(
                                "restored definition entry does not match the quarantined inode"
                            )
                        self._fsync_directory(root_fd)
                        renamed = False
                    except BaseException as restore:
                        restoration_error = restore
                    if restoration_error is not None:
                        try:
                            self._fsync_directory(root_fd)
                        except OSError:
                            pass
                        raise StoreDurabilityError(
                            f"app definition {app_id} entered removal quarantine, but safe rollback failed"
                        ) from restoration_error
                    if isinstance(error, StoreError):
                        raise
                    if isinstance(error, OSError):
                        raise StoreSafetyError(
                            f"cannot remove app definition {app_id}: {error}"
                        ) from error
                    raise

                if renamed and irreversible:
                    if validated_fd is not None:
                        try:
                            os.fsync(validated_fd)
                        except OSError:
                            pass
                    try:
                        self._fsync_directory(root_fd)
                    except OSError:
                        pass
                    if isinstance(error, StoreDurabilityError):
                        raise
                    raise StoreDurabilityError(
                        f"app definition {app_id} removal changed visible state, but completion could not be confirmed"
                    ) from error

                if isinstance(error, StoreError):
                    raise
                if isinstance(error, OSError):
                    raise StoreSafetyError(
                        f"cannot remove app definition {app_id}: {error}"
                    ) from error
                raise
            finally:
                primary_active = sys.exc_info()[0] is not None
                if validated_fd is not None:
                    try:
                        os.close(validated_fd)
                    except OSError as error:
                        if not primary_active:
                            if renamed:
                                raise StoreDurabilityError(
                                    f"app definition {app_id} removal completed, but its descriptor could not be closed: {error}"
                                ) from error
                            raise StoreSafetyError(
                                f"app definition {app_id} descriptor could not be closed: {error}"
                            ) from error
