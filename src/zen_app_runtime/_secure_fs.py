"""Linux-only descriptor publication and rename primitives.

The helpers in this module never resolve a mutable source pathname when they
publish data. New files are created as unnamed ``O_TMPFILE`` inodes and linked
from their held descriptors with ``linkat(AT_EMPTY_PATH)``.
"""

from __future__ import annotations

import ctypes
import errno
import hmac
import os
import stat
import sys

AT_EMPTY_PATH = 0x1000
RENAME_NOREPLACE = 1


class SecureFsCapabilityError(OSError):
    """The kernel/filesystem lacks a required descriptor operation."""


class PublishedIdentityError(OSError):
    """Publication occurred, but the destination no longer names its inode."""


def _libc_function(name: str):
    try:
        function = getattr(ctypes.CDLL(None, use_errno=True), name)
    except (AttributeError, OSError) as error:
        raise SecureFsCapabilityError(f"required Linux operation {name} is unavailable") from error
    function.restype = ctypes.c_int
    return function


def _link_fd_exclusive(source_fd: int, destination_fd: int, name: str) -> None:
    linkat = _libc_function("linkat")
    encoded = os.fsencode(name)
    ctypes.set_errno(0)
    result = linkat(
        ctypes.c_int(source_fd),
        ctypes.c_char_p(b""),
        ctypes.c_int(destination_fd),
        ctypes.c_char_p(encoded),
        ctypes.c_int(AT_EMPTY_PATH),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
            raise SecureFsCapabilityError(
                "descriptor publication requires Linux linkat(AT_EMPTY_PATH) support"
            )
        raise OSError(error_number, os.strerror(error_number), name)


def rename_noreplace(directory_fd: int, source: str, destination: str) -> None:
    renameat2 = _libc_function("renameat2")
    ctypes.set_errno(0)
    result = renameat2(
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_int(directory_fd),
        ctypes.c_char_p(os.fsencode(destination)),
        ctypes.c_uint(RENAME_NOREPLACE),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in (errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS):
            raise SecureFsCapabilityError(
                "safe removal requires Linux renameat2(RENAME_NOREPLACE) support"
            )
        raise OSError(error_number, os.strerror(error_number), source)


def same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def write_all(descriptor: int, payload: bytes) -> None:
    position = 0
    while position < len(payload):
        written = os.write(descriptor, payload[position:])
        if written <= 0:  # pragma: no cover - os.write contract.
            raise OSError("short file write")
        position += written


def _publication_identity(info: os.stat_result) -> tuple[int, ...]:
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


def _validate_published_state(
    info: os.stat_result,
    *,
    mode: int,
    size: int,
    links: int,
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise PublishedIdentityError("published inode is no longer a regular file")
    if info.st_uid != os.geteuid():
        raise PublishedIdentityError("published inode owner changed")
    if stat.S_IMODE(info.st_mode) != mode:
        raise PublishedIdentityError("published inode mode changed")
    if info.st_nlink != links:
        raise PublishedIdentityError("published inode link count changed")
    if info.st_size != size:
        raise PublishedIdentityError("published inode size changed")


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def publish_bytes_exclusive(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o600,
) -> os.stat_result:
    """Synchronize and publish bytes from an unnamed inode without overwrite."""
    temporary_flag = getattr(os, "O_TMPFILE", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if temporary_flag is None or close_on_exec is None:
        raise SecureFsCapabilityError(
            "descriptor publication requires Linux O_TMPFILE and O_CLOEXEC"
        )
    descriptor: int | None = None
    published = False
    try:
        try:
            descriptor = os.open(
                ".",
                os.O_RDWR | temporary_flag | close_on_exec,
                mode,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise SecureFsCapabilityError(
                f"filesystem does not support secure unnamed temporary files: {error}"
            ) from error
        os.fchmod(descriptor, mode)
        write_all(descriptor, payload)
        os.fsync(descriptor)
        source_info = os.fstat(descriptor)
        try:
            _validate_published_state(source_info, mode=mode, size=len(payload), links=0)
        except PublishedIdentityError as error:
            raise SecureFsCapabilityError(
                f"unnamed publication source has an unsafe identity: {error}"
            ) from error
        _link_fd_exclusive(descriptor, directory_fd, name)
        published = True

        post_link_info = os.fstat(descriptor)
        _validate_published_state(post_link_info, mode=mode, size=len(payload), links=1)
        actual_payload = _read_exact(descriptor, len(payload))
        final_source_info = os.fstat(descriptor)
        _validate_published_state(final_source_info, mode=mode, size=len(payload), links=1)
        if _publication_identity(post_link_info) != _publication_identity(final_source_info):
            raise PublishedIdentityError("published inode changed during post-link verification")
        if not hmac.compare_digest(actual_payload, payload):
            raise PublishedIdentityError("published inode content changed")

        destination_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not same_inode(final_source_info, destination_info):
            raise PublishedIdentityError(
                f"published destination {name} no longer identifies the synchronized inode"
            )
        if _publication_identity(final_source_info) != _publication_identity(destination_info):
            raise PublishedIdentityError(
                f"published destination {name} metadata differs from the synchronized inode"
            )
        return destination_info
    except PublishedIdentityError:
        raise
    except OSError as error:
        if published:
            raise PublishedIdentityError(
                f"published destination {name} could not be identity-verified: {error}"
            ) from error
        raise
    finally:
        primary_active = sys.exc_info()[0] is not None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                if not primary_active:
                    if published:
                        raise PublishedIdentityError(
                            f"published destination {name} is visible, but its descriptor could not be closed: {error}"
                        ) from error
                    raise SecureFsCapabilityError(
                        f"unnamed publication descriptor could not be closed: {error}"
                    ) from error
