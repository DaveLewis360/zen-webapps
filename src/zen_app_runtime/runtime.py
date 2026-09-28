"""Verify a pinned local runtime without executing or discovering one.

This module is intentionally Linux/POSIX-specific. It opens every component
from ``/`` with directory descriptors and no-follow flags, hashes the already
opened executable, and never launches, downloads, or generates a lock.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from typing import Any, Iterator, Mapping

RUNTIME_LOCK_VERSION = 1
RUNTIME_IDENTITY_VERSION = 1
MAX_LOCK_BYTES = 64 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LOCK_KEYS = frozenset({"runtimeLockVersion", "runtime"})
_LOCK_RUNTIME_KEYS = frozenset({"path", "sha256"})
_IDENTITY_KEYS = frozenset({"identityVersion", "path", "sha256"})
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
_REQUIRED_SEALS = _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE


class RuntimeError(ValueError):
    """Base error for runtime lock and verification failures."""


class RuntimeSafetyError(RuntimeError):
    """The lock or runtime failed a closed schema or filesystem check."""


@dataclass(frozen=True)
class RuntimeLock:
    lock_version: int
    path: str
    sha256: str


@dataclass(frozen=True)
class RuntimeIdentity:
    identity_version: int
    path: str
    sha256: str


@dataclass(frozen=True)
class OpenVerifiedRuntime:
    """Verified execution authority owned by the surrounding context."""

    identity: RuntimeIdentity
    descriptor: int
    sealed_snapshot: bool
    size: int


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise RuntimeSafetyError(f"{label} fields: {', '.join(details)}")


def _absolute_normalized_path(value: os.PathLike[str] | str, label: str) -> str:
    if isinstance(value, bytes):
        raise RuntimeSafetyError(f"{label} path must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise RuntimeSafetyError(f"{label} path must be path-like text") from error
    if not isinstance(rendered, str):
        raise RuntimeSafetyError(f"{label} path must be text")
    if not rendered.startswith("/"):
        raise RuntimeSafetyError(f"{label} path must be absolute")
    if rendered == "/" or rendered.startswith("//"):
        raise RuntimeSafetyError(f"{label} path must name an absolute normalized file")
    components = rendered[1:].split("/")
    if any(component in ("", ".", "..") for component in components):
        raise RuntimeSafetyError(f"{label} path must be absolute and normalized")
    if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
        raise RuntimeSafetyError(f"{label} path cannot contain control characters")
    return rendered


def _platform_flags() -> tuple[int, int]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if (
        nofollow is None
        or directory is None
        or close_on_exec is None
        or not hasattr(os, "geteuid")
    ):
        raise RuntimeSafetyError(
            "runtime verification requires Linux/POSIX no-follow, close-on-exec directory operations"
        )
    common = os.O_RDONLY | nofollow | close_on_exec
    return common, common | directory


def _safe_owner(info: os.stat_result, label: str) -> None:
    allowed = {0, os.geteuid()}
    if info.st_uid not in allowed:
        raise RuntimeSafetyError(f"{label} owner must be root or the effective user")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeSafetyError(f"{label} is group/world writable")


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


def _open_error(label: str, error: OSError) -> RuntimeSafetyError:
    if error.errno == errno.ELOOP:
        return RuntimeSafetyError(f"{label} is or traverses a symbolic link")
    return RuntimeSafetyError(f"cannot open {label}: {error}")


@contextmanager
def _open_absolute_file(path: str, label: str) -> Iterator[tuple[int, os.stat_result]]:
    common_flags, directory_flags = _platform_flags()
    components = path[1:].split("/")
    descriptors: list[int] = []
    try:
        try:
            current = os.open("/", directory_flags)
        except OSError as error:
            raise _open_error(f"{label} root ancestor", error) from error
        descriptors.append(current)
        root_info = os.fstat(current)
        if not stat.S_ISDIR(root_info.st_mode):  # pragma: no cover - POSIX invariant.
            raise RuntimeSafetyError(f"{label} root ancestor is not a directory")
        _safe_owner(root_info, f"{label} ancestor /")

        traversed = ""
        for component in components[:-1]:
            traversed += "/" + component
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                raise _open_error(f"{label} ancestor {traversed}", error) from error
            descriptors.append(child)
            current = child
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeSafetyError(f"{label} ancestor is not a directory: {traversed}")
            _safe_owner(info, f"{label} ancestor {traversed}")

        try:
            descriptor = os.open(components[-1], common_flags, dir_fd=current)
        except OSError as error:
            raise _open_error(label, error) from error
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeSafetyError(f"{label} is not a regular file")
        _safe_owner(info, label)
        yield descriptor, info
    finally:
        primary_active = sys.exc_info()[0] is not None
        close_errors: list[OSError] = []
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                close_errors.append(error)
        if close_errors and not primary_active:
            detail = "; ".join(str(error) for error in close_errors)
            raise RuntimeSafetyError(
                f"verified {label} descriptors could not be closed: {detail}"
            ) from close_errors[0]


def _read_bounded_utf8(descriptor: int, limit: int, label: str) -> str:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise RuntimeSafetyError(f"{label} exceeds the {limit}-byte size limit")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeSafetyError(f"{label} must be valid UTF-8") from error


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeSafetyError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise RuntimeSafetyError(f"non-finite JSON number is not permitted: {value}")


def _load_json(text: str, label: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except RuntimeSafetyError:
        raise
    except json.JSONDecodeError as error:
        raise RuntimeSafetyError(f"{label} is not strict JSON: {error}") from error


def load_runtime_lock(lock_path: os.PathLike[str] | str) -> RuntimeLock:
    """Load a bounded strict-JSON lock through a safe absolute descriptor walk."""
    path = _absolute_normalized_path(lock_path, "lock")
    with _open_absolute_file(path, "runtime lock") as (descriptor, initial_info):
        text = _read_bounded_utf8(descriptor, MAX_LOCK_BYTES, "runtime lock")
        final_info = os.fstat(descriptor)
        if _stat_identity(initial_info) != _stat_identity(final_info):
            raise RuntimeSafetyError("runtime lock changed while reading")

    data = _load_json(text, "runtime lock")
    if not isinstance(data, dict):
        raise RuntimeSafetyError("runtime lock must be an object")
    _exact_keys(data, _LOCK_KEYS, "runtime lock")
    version = data["runtimeLockVersion"]
    if type(version) is not int or version != RUNTIME_LOCK_VERSION:
        raise RuntimeSafetyError(f"runtimeLockVersion must be {RUNTIME_LOCK_VERSION}")
    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise RuntimeSafetyError("runtime must be an object")
    _exact_keys(runtime, _LOCK_RUNTIME_KEYS, "runtime")
    runtime_path = _absolute_normalized_path(runtime["path"], "runtime")
    digest = runtime["sha256"]
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise RuntimeSafetyError("runtime.sha256 must be exactly 64 lowercase hexadecimal characters")
    return RuntimeLock(RUNTIME_LOCK_VERSION, runtime_path, digest)


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - os.write contract.
            raise OSError(errno.EIO, "short write to runtime snapshot")
        view = view[written:]


def _create_memfd() -> int:
    flags = _MFD_CLOEXEC | _MFD_ALLOW_SEALING
    native = getattr(os, "memfd_create", None)
    if native is not None:
        try:
            return native("zen-app-runtime", flags)
        except OSError as error:
            raise RuntimeSafetyError(f"cannot create sealed runtime snapshot: {error}") from error
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        create = libc.memfd_create
    except (AttributeError, OSError) as error:
        raise RuntimeSafetyError("sealed runtime snapshots require Linux memfd_create") from error
    create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    create.restype = ctypes.c_int
    descriptor = create(b"zen-app-runtime", flags)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        error = OSError(error_number, os.strerror(error_number))
        raise RuntimeSafetyError(f"cannot create sealed runtime snapshot: {error}") from error
    return descriptor


def probe_sealed_runtime_support() -> None:
    """Fail closed unless an anonymous runtime fd can be write-sealed."""
    descriptor = _create_memfd()
    try:
        os.fchmod(descriptor, 0o500)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _REQUIRED_SEALS)
        seals = fcntl.fcntl(descriptor, _F_GET_SEALS)
        if seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise RuntimeSafetyError("runtime snapshot did not retain every required write seal")
    except RuntimeSafetyError:
        raise
    except OSError as error:
        raise RuntimeSafetyError(f"cannot validate sealed runtime snapshot support: {error}") from error
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _copy_to_sealed_memfd(
    source_descriptor: int,
    initial_info: os.stat_result,
    expected_digest: str,
) -> tuple[int, int]:
    snapshot = _create_memfd()
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(snapshot, chunk)
            size += len(chunk)
        final_info = os.fstat(source_descriptor)
        if _stat_identity(initial_info) != _stat_identity(final_info):
            raise RuntimeSafetyError("runtime executable changed while snapshotting")
        if not hmac.compare_digest(digest.hexdigest(), expected_digest):
            raise RuntimeSafetyError("runtime executable digest does not match the lock")
        snapshot_info = os.fstat(snapshot)
        if snapshot_info.st_size != size or size != initial_info.st_size:
            raise RuntimeSafetyError("runtime snapshot size does not match the verified source")
        os.fchmod(snapshot, 0o500)
        fcntl.fcntl(snapshot, _F_ADD_SEALS, _REQUIRED_SEALS)
        seals = fcntl.fcntl(snapshot, _F_GET_SEALS)
        if seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise RuntimeSafetyError("runtime snapshot did not retain every required write seal")
        return snapshot, size
    except RuntimeSafetyError:
        try:
            os.close(snapshot)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            os.close(snapshot)
        except OSError:
            pass
        raise RuntimeSafetyError(f"cannot create sealed runtime snapshot: {error}") from error


def _effectively_executable(info: os.stat_result) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    effective_user = os.geteuid()
    if effective_user == 0:
        return bool(mode & 0o111)
    if info.st_uid == effective_user:
        return bool(mode & stat.S_IXUSR)
    groups = {os.getegid(), *os.getgroups()}
    if info.st_gid in groups:
        return bool(mode & stat.S_IXGRP)
    return bool(mode & stat.S_IXOTH)


@contextmanager
def open_verified_runtime(
    lock_path: os.PathLike[str] | str,
) -> Iterator[OpenVerifiedRuntime]:
    """Verify a pinned executable and retain immutable execution authority.

    A root-owned executable that the effective user cannot modify remains on
    its original descriptor so package-relative resource lookup is preserved.
    An effective-user-owned executable is copied while hashing into a sealed
    memfd. The yielded descriptor is valid only inside this context.
    """
    lock = load_runtime_lock(lock_path)
    snapshot_descriptor: int | None = None
    with _open_absolute_file(lock.path, "runtime executable") as (descriptor, initial_info):
        if not _effectively_executable(initial_info):
            raise RuntimeSafetyError("runtime executable is not executable by the effective user")
        identity = RuntimeIdentity(RUNTIME_IDENTITY_VERSION, lock.path, lock.sha256)
        root_immutable = initial_info.st_uid == 0 and os.geteuid() != 0
        if root_immutable:
            actual_digest = _hash_fd(descriptor)
            final_info = os.fstat(descriptor)
            if _stat_identity(initial_info) != _stat_identity(final_info):
                raise RuntimeSafetyError("runtime executable changed while hashing")
            if not hmac.compare_digest(actual_digest, lock.sha256):
                raise RuntimeSafetyError("runtime executable digest does not match the lock")
            yield OpenVerifiedRuntime(identity, descriptor, False, initial_info.st_size)
            return
        snapshot_descriptor, snapshot_size = _copy_to_sealed_memfd(
            descriptor,
            initial_info,
            lock.sha256,
        )

    try:
        yield OpenVerifiedRuntime(identity, snapshot_descriptor, True, snapshot_size)
    finally:
        primary_active = sys.exc_info()[0] is not None
        try:
            os.close(snapshot_descriptor)
        except OSError as error:
            if not primary_active:
                raise RuntimeSafetyError(
                    f"sealed runtime snapshot descriptor could not be closed: {error}"
                ) from error


def reverify_open_runtime(opened: OpenVerifiedRuntime) -> None:
    """Validate retained immutable execution authority immediately before exec."""
    descriptor = opened.descriptor
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        initial_info = os.fstat(descriptor)
        if not stat.S_ISREG(initial_info.st_mode):
            raise RuntimeSafetyError("runtime executable is not a regular file")
        if initial_info.st_size != opened.size:
            raise RuntimeSafetyError("runtime execution authority size changed")
        if not _effectively_executable(initial_info):
            raise RuntimeSafetyError("runtime executable is not executable by the effective user")
        if opened.sealed_snapshot:
            if initial_info.st_uid != os.geteuid() or stat.S_IMODE(initial_info.st_mode) != 0o500:
                raise RuntimeSafetyError("sealed runtime snapshot metadata changed")
            seals = fcntl.fcntl(descriptor, _F_GET_SEALS)
            if seals & _REQUIRED_SEALS != _REQUIRED_SEALS:
                raise RuntimeSafetyError("sealed runtime snapshot lost required write seals")
        else:
            _safe_owner(initial_info, "runtime executable")
            if initial_info.st_uid != 0 or os.geteuid() == 0:
                raise RuntimeSafetyError("unsealed runtime must be immutable to the effective user")
        actual_digest = _hash_fd(descriptor)
        final_info = os.fstat(descriptor)
    except RuntimeSafetyError:
        raise
    except OSError as error:
        raise RuntimeSafetyError(f"cannot re-verify runtime executable: {error}") from error
    if _stat_identity(initial_info) != _stat_identity(final_info):
        raise RuntimeSafetyError("runtime execution authority changed during verification")
    if not hmac.compare_digest(actual_digest, opened.identity.sha256):
        raise RuntimeSafetyError("runtime execution authority digest changed")


def verify_runtime(lock_path: os.PathLike[str] | str) -> RuntimeIdentity:
    """Verify and identify the pinned executable without ever executing it."""
    with open_verified_runtime(lock_path) as opened:
        return opened.identity


def runtime_identity_to_dict(identity: RuntimeIdentity) -> dict[str, Any]:
    data = {
        "identityVersion": identity.identity_version,
        "path": identity.path,
        "sha256": identity.sha256,
    }
    runtime_identity_from_dict(data)
    return data


def runtime_identity_from_dict(value: Any) -> RuntimeIdentity:
    if not isinstance(value, dict):
        raise RuntimeSafetyError("runtime identity must be an object")
    _exact_keys(value, _IDENTITY_KEYS, "runtime identity")
    if type(value["identityVersion"]) is not int or value["identityVersion"] != RUNTIME_IDENTITY_VERSION:
        raise RuntimeSafetyError(f"identityVersion must be {RUNTIME_IDENTITY_VERSION}")
    path = _absolute_normalized_path(value["path"], "runtime identity")
    digest = value["sha256"]
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise RuntimeSafetyError("runtime identity sha256 must be lowercase hexadecimal")
    return RuntimeIdentity(RUNTIME_IDENTITY_VERSION, path, digest)


def render_runtime_identity(identity: RuntimeIdentity) -> str:
    return json.dumps(runtime_identity_to_dict(identity), indent=2, sort_keys=True) + "\n"


verify_runtime_lock = verify_runtime
