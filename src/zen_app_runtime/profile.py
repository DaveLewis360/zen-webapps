"""Provision sentinel-marked, isolated profile directories without launching a browser."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterator, Literal

from ._secure_fs import (
    PublishedIdentityError,
    SecureFsCapabilityError,
    publish_bytes_exclusive,
    same_inode,
)
from .manifest import APP_ID_PATTERN, AppManifest, ManifestError, manifest_to_dict
from .runtime import (
    OpenVerifiedRuntime,
    RuntimeIdentity,
    RuntimeSafetyError,
    _absolute_normalized_path,
    _platform_flags,
    open_verified_runtime,
    runtime_identity_to_dict,
    verify_runtime,
)

PROFILE_ROOT_MARKER_VERSION = 1
PROFILE_MARKER_VERSION = 1
PROFILE_ROOT_MARKER_NAME = ".zen-app-runtime-root.json"
PROFILE_MARKER_NAME = ".zen-app-runtime-profile.json"
MAX_MARKER_BYTES = 64 * 1024


class ProfileError(RuntimeError):
    """Base error for managed profile provisioning."""


class ProfileSafetyError(ProfileError):
    """A profile path, marker, or binding failed closed validation."""


class ProfileDurabilityError(ProfileSafetyError):
    """A profile committed, but durable persistence could not be confirmed."""


class ProfileRollbackError(ProfileSafetyError):
    """A pre-commit failure occurred and cleanup could not be safely completed."""


class _PublishedMarkerIdentityError(ProfileDurabilityError):
    """Marker publication occurred, but its destination identity was lost."""


@dataclass(frozen=True)
class ProfileProvisionResult:
    path: Path
    status: Literal["created", "existing"]


@dataclass(frozen=True)
class ProfileBinding:
    path: Path
    manifest_sha256: str
    runtime_identity: RuntimeIdentity


@dataclass(frozen=True)
class OpenVerifiedProfile:
    """Retained descriptors for an exact managed-profile binding."""

    binding: ProfileBinding
    runtime: OpenVerifiedRuntime
    parent_descriptor: int
    root_descriptor: int
    profile_descriptor: int
    root_name: str
    app_id: str


def _profile_root_path(value: os.PathLike[str] | str) -> str:
    try:
        return _absolute_normalized_path(value, "profile root")
    except RuntimeSafetyError as error:
        raise ProfileSafetyError(str(error)) from error


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _manifest_bytes(manifest: AppManifest) -> bytes:
    try:
        return _canonical_json(manifest_to_dict(manifest))
    except (ManifestError, TypeError, AttributeError, ValueError) as error:
        raise ProfileSafetyError(f"invalid manifest binding: {error}") from error


def manifest_sha256(manifest: AppManifest) -> str:
    return hashlib.sha256(_manifest_bytes(manifest)).hexdigest()


def _root_marker_payload(root: str) -> bytes:
    return _canonical_json({"markerVersion": PROFILE_ROOT_MARKER_VERSION, "profileRoot": root})


def _profile_marker_payload(
    root: str,
    manifest: AppManifest,
    identity: RuntimeIdentity,
) -> bytes:
    try:
        runtime_data = runtime_identity_to_dict(identity)
    except RuntimeSafetyError as error:
        raise ProfileSafetyError(f"invalid runtime identity binding: {error}") from error
    return _canonical_json(
        {
            "appId": manifest.app_id,
            "manifest": {
                "schemaVersion": manifest.schema_version,
                "sha256": manifest_sha256(manifest),
            },
            "markerVersion": PROFILE_MARKER_VERSION,
            "profileRoot": root,
            "runtimeIdentity": runtime_data,
        }
    )


def _require_owner(info: os.stat_result, label: str, *, exact_user: bool) -> None:
    allowed = {os.geteuid()} if exact_user else {0, os.geteuid()}
    if info.st_uid not in allowed:
        expectation = "the effective user" if exact_user else "root or the effective user"
        raise ProfileSafetyError(f"{label} must be owned by {expectation}")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ProfileSafetyError(f"{label} is group/world writable")


def _require_directory(info: os.stat_result, label: str, *, managed: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise ProfileSafetyError(f"{label} is not a directory")
    _require_owner(info, label, exact_user=managed)
    if managed and stat.S_IMODE(info.st_mode) != 0o700:
        raise ProfileSafetyError(f"{label} must have mode 0700")


def _open_failure(label: str, error: OSError) -> ProfileSafetyError:
    if getattr(error, "errno", None) == getattr(os, "ELOOP", 40):
        return ProfileSafetyError(f"{label} is or traverses a symbolic link")
    return ProfileSafetyError(f"cannot open {label}: {error}")


@contextmanager
def _open_parent_directory(path: str) -> Iterator[tuple[int, str]]:
    try:
        _, directory_flags = _platform_flags()
    except RuntimeSafetyError as error:
        raise ProfileSafetyError(str(error)) from error
    components = path[1:].split("/")
    descriptors: list[int] = []
    try:
        try:
            current = os.open("/", directory_flags)
        except OSError as error:
            raise _open_failure("profile root ancestor /", error) from error
        descriptors.append(current)
        _require_directory(os.fstat(current), "profile root ancestor /", managed=False)
        traversed = ""
        for component in components[:-1]:
            traversed += "/" + component
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                raise _open_failure(f"profile root ancestor {traversed}", error) from error
            descriptors.append(child)
            current = child
            _require_directory(os.fstat(current), f"profile root ancestor {traversed}", managed=False)
        yield current, components[-1]
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_managed_directory(parent_fd: int, name: str, label: str) -> int:
    try:
        _, directory_flags = _platform_flags()
        descriptor = os.open(name, directory_flags, dir_fd=parent_fd)
    except (OSError, RuntimeSafetyError) as error:
        if isinstance(error, OSError):
            raise _open_failure(label, error) from error
        raise ProfileSafetyError(str(error)) from error
    try:
        _require_directory(os.fstat(descriptor), label, managed=True)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def _publish_exclusive(directory_fd: int, name: str, payload: bytes) -> os.stat_result:
    try:
        return publish_bytes_exclusive(directory_fd, name, payload, mode=0o600)
    except FileExistsError:
        raise
    except PublishedIdentityError as error:
        raise _PublishedMarkerIdentityError(
            f"managed marker {name} was published, but its destination identity was lost: {error}"
        ) from error
    except (OSError, SecureFsCapabilityError) as error:
        raise ProfileSafetyError(
            f"cannot publish managed profile marker {name}: {error}"
        ) from error


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


def _require_directory_entry(
    parent_fd: int,
    name: str,
    descriptor: int,
    label: str,
) -> None:
    try:
        current_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        held_info = os.fstat(descriptor)
    except OSError as error:
        raise ProfileSafetyError(f"cannot verify {label} directory entry: {error}") from error
    if not same_inode(current_info, held_info):
        raise ProfileSafetyError(f"{label} directory entry changed during provisioning")
    _require_directory(current_info, label, managed=True)


def _read_exact_marker(directory_fd: int, name: str, expected: bytes, label: str) -> None:
    try:
        common_flags, _ = _platform_flags()
        descriptor = os.open(name, common_flags, dir_fd=directory_fd)
    except (OSError, RuntimeSafetyError) as error:
        if isinstance(error, OSError):
            raise _open_failure(f"{label} sentinel", error) from error
        raise ProfileSafetyError(str(error)) from error
    try:
        initial_info = os.fstat(descriptor)
        if not stat.S_ISREG(initial_info.st_mode):
            raise ProfileSafetyError(f"{label} sentinel is not a regular file")
        _require_owner(initial_info, f"{label} sentinel", exact_user=True)
        if stat.S_IMODE(initial_info.st_mode) != 0o600:
            raise ProfileSafetyError(f"{label} sentinel must have mode 0600")
        if initial_info.st_nlink != 1:
            raise ProfileSafetyError(f"{label} sentinel must have exactly one hard link")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(16 * 1024, MAX_MARKER_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_MARKER_BYTES:
                raise ProfileSafetyError(f"{label} sentinel exceeds marker size limit")
        final_info = os.fstat(descriptor)
        if _stat_identity(initial_info) != _stat_identity(final_info):
            raise ProfileSafetyError(f"{label} sentinel changed while reading")
        if b"".join(chunks) != expected:
            mismatch = "binding mismatch" if label == "profile" else "sentinel mismatch"
            raise ProfileSafetyError(f"managed {label} {mismatch}")
    finally:
        primary_active = sys.exc_info()[0] is not None
        try:
            os.close(descriptor)
        except OSError as error:
            if not primary_active:
                raise ProfileSafetyError(
                    f"{label} sentinel descriptor could not be closed: {error}"
                ) from error


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _sync_committed_profile(profile_fd: int, root_fd: int, parent_fd: int) -> None:
    _fsync_directory(profile_fd)
    _fsync_directory(root_fd)
    _fsync_directory(parent_fd)


def _rollback(
    parent_fd: int,
    root_name: str,
    root_fd: int | None,
    profile_fd: int | None,
    app_id: str,
    root_marker: bytes,
    *,
    root_created: bool,
    root_marker_published: bool,
    profile_created: bool,
) -> None:
    if root_fd is None:
        if root_created:
            try:
                os.rmdir(root_name, dir_fd=parent_fd)
                _fsync_directory(parent_fd)
            except OSError as error:
                raise ProfileRollbackError(
                    "managed profile creation failed before the root descriptor was retained, "
                    "and the empty provisional root could not be removed"
                ) from error
        return
    if profile_created:
        if profile_fd is None:
            raise ProfileRollbackError(
                "managed profile creation failed before its directory identity could be retained; "
                "the marked root was preserved"
            )
        try:
            _require_directory_entry(root_fd, app_id, profile_fd, "managed profile")
            os.rmdir(app_id, dir_fd=root_fd)
            _fsync_directory(root_fd)
        except (OSError, ProfileSafetyError) as error:
            raise ProfileRollbackError(
                "managed profile creation failed and the provisional app directory could not be "
                "identity-verified and removed; the marked root was preserved"
            ) from error
    if not root_created:
        return

    try:
        _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
    except ProfileSafetyError as error:
        raise ProfileRollbackError(
            "managed profile rollback refused to modify a rebound root entry; "
            "the original root sentinel was preserved"
        ) from error

    marker_removed = False
    try:
        if root_marker_published:
            os.unlink(PROFILE_ROOT_MARKER_NAME, dir_fd=root_fd)
            marker_removed = True
            _fsync_directory(root_fd)
        os.rmdir(root_name, dir_fd=parent_fd)
        _fsync_directory(parent_fd)
    except OSError as error:
        restoration_error: BaseException | None = None
        if marker_removed:
            try:
                _publish_exclusive(root_fd, PROFILE_ROOT_MARKER_NAME, root_marker)
                _fsync_directory(root_fd)
                _fsync_directory(parent_fd)
            except BaseException as restore:
                restoration_error = restore
        detail = "managed profile rollback could not remove the provisional root"
        if restoration_error is not None:
            detail += "; restoring its safety sentinel also failed"
        else:
            detail += "; its safety sentinel was preserved or restored"
        raise ProfileRollbackError(detail) from restoration_error or error


def profile_provision_result_to_dict(result: ProfileProvisionResult) -> dict[str, Any]:
    if result.status not in ("created", "existing"):
        raise ProfileSafetyError("invalid profile provision status")
    path = _profile_root_path(result.path)
    return {"profilePath": path, "status": result.status}


def render_profile_provision_result(result: ProfileProvisionResult) -> str:
    return json.dumps(profile_provision_result_to_dict(result), indent=2, sort_keys=True) + "\n"


def _validate_binding_inputs(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
) -> tuple[str, bytes]:
    root = _profile_root_path(profile_root)
    _manifest_bytes(manifest)
    if not isinstance(manifest.app_id, str) or not APP_ID_PATTERN.fullmatch(manifest.app_id):
        raise ProfileSafetyError("manifest app ID is unsafe")
    return root, _root_marker_payload(root)


def revalidate_open_profile_binding(
    opened: OpenVerifiedProfile,
    manifest: AppManifest,
) -> None:
    """Recheck retained profile path entries and exact marker bindings."""
    root = str(opened.binding.path.parent)
    if manifest_sha256(manifest) != opened.binding.manifest_sha256:
        raise ProfileSafetyError("managed profile manifest binding changed")
    if opened.runtime.identity != opened.binding.runtime_identity:
        raise ProfileSafetyError("managed profile runtime binding changed")
    _require_directory_entry(
        opened.parent_descriptor,
        opened.root_name,
        opened.root_descriptor,
        "managed profile root",
    )
    _read_exact_marker(
        opened.root_descriptor,
        PROFILE_ROOT_MARKER_NAME,
        _root_marker_payload(root),
        "profile root",
    )
    _require_directory_entry(
        opened.root_descriptor,
        opened.app_id,
        opened.profile_descriptor,
        "managed profile",
    )
    _read_exact_marker(
        opened.profile_descriptor,
        PROFILE_MARKER_NAME,
        _profile_marker_payload(root, manifest, opened.runtime.identity),
        "profile",
    )
    _require_directory_entry(
        opened.parent_descriptor,
        opened.root_name,
        opened.root_descriptor,
        "managed profile root",
    )
    _require_directory_entry(
        opened.root_descriptor,
        opened.app_id,
        opened.profile_descriptor,
        "managed profile",
    )


@contextmanager
def open_verified_profile_binding(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
) -> Iterator[OpenVerifiedProfile]:
    """Open an existing exact profile binding without creating or changing it."""
    root, root_marker = _validate_binding_inputs(profile_root, manifest)
    profile_marker: bytes
    try:
        with open_verified_runtime(runtime_lock) as runtime:
            profile_marker = _profile_marker_payload(root, manifest, runtime.identity)
            with _open_parent_directory(root) as (parent_fd, root_name):
                root_fd: int | None = None
                profile_fd: int | None = None
                try:
                    root_fd = _open_managed_directory(
                        parent_fd,
                        root_name,
                        "managed profile root",
                    )
                    _require_directory_entry(
                        parent_fd,
                        root_name,
                        root_fd,
                        "managed profile root",
                    )
                    _read_exact_marker(
                        root_fd,
                        PROFILE_ROOT_MARKER_NAME,
                        root_marker,
                        "profile root",
                    )
                    profile_fd = _open_managed_directory(
                        root_fd,
                        manifest.app_id,
                        "managed profile",
                    )
                    _require_directory_entry(
                        root_fd,
                        manifest.app_id,
                        profile_fd,
                        "managed profile",
                    )
                    _read_exact_marker(
                        profile_fd,
                        PROFILE_MARKER_NAME,
                        profile_marker,
                        "profile",
                    )
                    opened = OpenVerifiedProfile(
                        binding=ProfileBinding(
                            path=Path(root) / manifest.app_id,
                            manifest_sha256=manifest_sha256(manifest),
                            runtime_identity=runtime.identity,
                        ),
                        runtime=runtime,
                        parent_descriptor=parent_fd,
                        root_descriptor=root_fd,
                        profile_descriptor=profile_fd,
                        root_name=root_name,
                        app_id=manifest.app_id,
                    )
                    revalidate_open_profile_binding(opened, manifest)
                    yield opened
                finally:
                    primary_active = sys.exc_info()[0] is not None
                    close_errors: list[OSError] = []
                    for descriptor in (profile_fd, root_fd):
                        if descriptor is not None:
                            try:
                                os.close(descriptor)
                            except OSError as error:
                                close_errors.append(error)
                    if close_errors and not primary_active:
                        detail = "; ".join(str(error) for error in close_errors)
                        raise ProfileSafetyError(
                            f"verified profile descriptors could not be closed: {detail}"
                        ) from close_errors[0]
    except RuntimeSafetyError as error:
        raise ProfileSafetyError(f"runtime verification failed: {error}") from error


def verify_profile_binding(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
) -> ProfileBinding:
    """Verify an existing profile and return its deterministic logical binding."""
    with open_verified_profile_binding(profile_root, manifest, runtime_lock) as opened:
        return opened.binding


def _provision_verified_profile(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_identity: RuntimeIdentity,
) -> ProfileProvisionResult:
    """Internal transaction; callers must use :func:`provision_profile`."""
    root = _profile_root_path(profile_root)
    _manifest_bytes(manifest)
    if not isinstance(manifest.app_id, str) or not APP_ID_PATTERN.fullmatch(manifest.app_id):
        raise ProfileSafetyError("manifest app ID is unsafe")
    root_marker = _root_marker_payload(root)
    profile_marker = _profile_marker_payload(root, manifest, runtime_identity)
    result_path = Path(root) / manifest.app_id

    root_fd: int | None = None
    profile_fd: int | None = None
    root_created = False
    root_marker_published = False
    profile_created = False
    committed = False
    preserve_root = False
    with _open_parent_directory(root) as (parent_fd, root_name):
        try:
            try:
                os.mkdir(root_name, 0o700, dir_fd=parent_fd)
                root_created = True
            except FileExistsError:
                pass
            except OSError as error:
                raise ProfileSafetyError(f"cannot create managed profile root: {error}") from error
            root_fd = _open_managed_directory(parent_fd, root_name, "managed profile root")
            _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")

            if root_created:
                try:
                    _publish_exclusive(root_fd, PROFILE_ROOT_MARKER_NAME, root_marker)
                    root_marker_published = True
                except _PublishedMarkerIdentityError as error:
                    root_marker_published = True
                    preserve_root = True
                    try:
                        _fsync_directory(root_fd)
                        _fsync_directory(parent_fd)
                    except OSError:
                        pass
                    raise ProfileDurabilityError(
                        f"managed profile root was marked, but its destination identity was lost: {error}"
                    ) from error
                except FileExistsError as error:
                    raise ProfileSafetyError(
                        "managed profile root sentinel appeared concurrently"
                    ) from error
                try:
                    _require_directory_entry(
                        parent_fd,
                        root_name,
                        root_fd,
                        "managed profile root",
                    )
                except ProfileSafetyError as error:
                    preserve_root = True
                    raise ProfileDurabilityError(
                        "managed profile root was marked, but its directory entry changed"
                    ) from error
                try:
                    _fsync_directory(root_fd)
                    _fsync_directory(parent_fd)
                except OSError as error:
                    raise ProfileSafetyError(
                        f"cannot synchronize provisional profile root: {error}"
                    ) from error
            else:
                try:
                    _read_exact_marker(
                        root_fd,
                        PROFILE_ROOT_MARKER_NAME,
                        root_marker,
                        "profile root",
                    )
                except ProfileSafetyError as error:
                    if "cannot open" in str(error):
                        raise ProfileSafetyError(
                            "managed profile root sentinel is missing; refusing adoption"
                        ) from error
                    raise
                _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")

            try:
                os.mkdir(manifest.app_id, 0o700, dir_fd=root_fd)
                profile_created = True
            except FileExistsError:
                pass
            except OSError as error:
                raise ProfileSafetyError(
                    f"cannot create managed profile directory: {error}"
                ) from error
            profile_fd = _open_managed_directory(root_fd, manifest.app_id, "managed profile")
            _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
            _require_directory_entry(root_fd, manifest.app_id, profile_fd, "managed profile")

            if not profile_created:
                try:
                    _read_exact_marker(profile_fd, PROFILE_MARKER_NAME, profile_marker, "profile")
                except ProfileSafetyError as error:
                    if "cannot open" in str(error):
                        raise ProfileSafetyError(
                            "managed profile sentinel is missing; refusing adoption"
                        ) from error
                    raise
                _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
                _require_directory_entry(root_fd, manifest.app_id, profile_fd, "managed profile")
                return ProfileProvisionResult(result_path, "existing")

            _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
            _require_directory_entry(root_fd, manifest.app_id, profile_fd, "managed profile")
            try:
                _publish_exclusive(profile_fd, PROFILE_MARKER_NAME, profile_marker)
            except _PublishedMarkerIdentityError as error:
                committed = True
                try:
                    _sync_committed_profile(profile_fd, root_fd, parent_fd)
                except OSError:
                    pass
                raise ProfileDurabilityError(
                    f"managed profile {manifest.app_id} was marked, but its destination identity was lost: {error}"
                ) from error
            except FileExistsError as error:
                raise ProfileSafetyError("managed profile marker appeared concurrently") from error
            committed = True
            try:
                _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
                _require_directory_entry(root_fd, manifest.app_id, profile_fd, "managed profile")
                _sync_committed_profile(profile_fd, root_fd, parent_fd)
                _require_directory_entry(parent_fd, root_name, root_fd, "managed profile root")
                _require_directory_entry(root_fd, manifest.app_id, profile_fd, "managed profile")
            except ProfileSafetyError as error:
                raise ProfileDurabilityError(
                    f"managed profile {manifest.app_id} was created, but its visible path identity changed"
                ) from error
            except OSError as error:
                raise ProfileDurabilityError(
                    f"managed profile {manifest.app_id} was created, but durability could not be confirmed: {error}"
                ) from error
            return ProfileProvisionResult(result_path, "created")
        except BaseException as error:
            if not committed and not preserve_root:
                try:
                    _rollback(
                        parent_fd,
                        root_name,
                        root_fd,
                        profile_fd,
                        manifest.app_id,
                        root_marker,
                        root_created=root_created,
                        root_marker_published=root_marker_published,
                        profile_created=profile_created,
                    )
                except ProfileRollbackError as rollback_error:
                    raise rollback_error from error
            raise
        finally:
            primary_active = sys.exc_info()[0] is not None
            close_errors: list[OSError] = []
            if profile_fd is not None:
                try:
                    os.close(profile_fd)
                except OSError as error:
                    close_errors.append(error)
            if root_fd is not None:
                try:
                    os.close(root_fd)
                except OSError as error:
                    close_errors.append(error)
            if close_errors and not primary_active:
                detail = "; ".join(str(error) for error in close_errors)
                if committed:
                    raise ProfileDurabilityError(
                        f"managed profile {manifest.app_id} committed, but descriptor cleanup failed: {detail}"
                    ) from close_errors[0]
                raise ProfileSafetyError(
                    f"managed profile descriptors could not be closed: {detail}"
                ) from close_errors[0]


def provision_profile(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
) -> ProfileProvisionResult:
    """Verify a pinned lock, then provision exactly ``profile_root/app_id``.

    The lock verification and profile transaction are one authoritative API;
    a caller-provided runtime identity is never accepted as proof.
    """
    _manifest_bytes(manifest)
    if not isinstance(manifest.app_id, str) or not APP_ID_PATTERN.fullmatch(manifest.app_id):
        raise ProfileSafetyError("manifest app ID is unsafe")
    try:
        identity = verify_runtime(runtime_lock)
    except RuntimeSafetyError as error:
        raise ProfileSafetyError(f"runtime verification failed: {error}") from error
    return _provision_verified_profile(profile_root, manifest, identity)
