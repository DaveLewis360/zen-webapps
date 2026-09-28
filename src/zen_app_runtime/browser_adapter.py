"""Pinned browser-side App Shell bundle verification and descriptor authority.

The package bundle is a closed set of reviewed files. Verification walks from
``/`` without following symlinks, hashes every regular file through retained
directory descriptors, rejects additional entries, and keeps the exact root
directory open for ``-app /proc/self/fd/N/application.ini`` execution.

A mutable effective-user-owned installation still permits a same-UID actor to
race Gecko after the final hash check. Production packaging should therefore
install this bundle root-owned and non-user-writable; the descriptor boundary
prevents pathname replacement but cannot make directory entries immutable.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
from typing import Any, Iterator

from .app_shell import (
    APP_SHELL_ADAPTER_CAPABILITY_VERSION,
    APP_SHELL_ADAPTER_ID,
    APP_SHELL_ADAPTER_PROTOCOL_VERSION,
    AppShellAdapterCapability,
    REQUIRED_ADAPTER_CAPABILITIES,
    adapter_capability_to_dict,
)
from .runtime import (
    RuntimeIdentity,
    RuntimeSafetyError,
    _absolute_normalized_path,
    _platform_flags,
    _safe_owner,
    _stat_identity,
)

BROWSER_ADAPTER_BUNDLE_VERSION = 2
BROWSER_ADAPTER_APPLICATION_RELATIVE_PATH = "application.ini"
MAX_BROWSER_ADAPTER_FILE_BYTES = 256 * 1024
MAX_BROWSER_ADAPTER_TOTAL_BYTES = 512 * 1024

# These hashes are part of the Python authority, not values read from the bundle.
BROWSER_ADAPTER_FILE_SHA256: tuple[tuple[str, str], ...] = (
    (
        "application.ini",
        "d4f27c2bd27e39f66e706006e4c9226fdef3792766379168109c2e9c76fe746b",
    ),
    (
        "chrome.manifest",
        "10f89eae9ce2107d3c4a41543e9195099d0f659efe985c800beebebe7989347e",
    ),
    (
        "chrome/chrome.manifest",
        "5fbce615e8a9552304939947463f3dca9e59c7747e763698d5050c778fbed77e",
    ),
    (
        "chrome/content/discord-zen-internet.css",
        "97aebae8ea9426c57864463c90e05102e0e3ef49b690eed73038ce39accdb094",
    ),
    (
        "chrome/content/shell.css",
        "7e6e2961040e813f67db6c35f194ff2d8506bf4f7c57162cb0dc08d4c49f7596",
    ),
    (
        "chrome/content/shell.js",
        "167f9c393d0bbd1e7ee05a9da563e15924becf0a4f1358a590b80ee5f7aeacb9",
    ),
    (
        "chrome/content/shell.xhtml",
        "facb181cdb09caa85c197bec60bf6804b53e6ac6efa3632195cfdc180c31c5cb",
    ),
    (
        "defaults/preferences/prefs.js",
        "6e454ed0f8fbc8695bcb63f03de104edfc6c41ac02cfb3cad717b254bc662b86",
    ),
)
EXPECTED_BROWSER_ADAPTER_TREE_SHA256 = (
    "3dbc0f9459b3aaf0daa1a1b32a00c531e70f9aa3e593f482c57ae23ed039e45a"
)


class BrowserAdapterError(ValueError):
    """The bundled browser adapter failed its closed authority checks."""


@dataclass(frozen=True)
class OpenVerifiedBrowserAdapter:
    """A verified bundle root whose descriptor is owned by its context."""

    bundle_version: int
    logical_path: str
    descriptor: int
    root_identity: tuple[int, ...]
    files: tuple[tuple[str, str], ...]
    tree_sha256: str


def bundled_browser_adapter_path() -> str:
    """Return the lexical package resource path without resolving symlinks."""
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "browser_adapter_bundle")
    )


def _browser_adapter_error(label: str, error: OSError) -> BrowserAdapterError:
    if error.errno == errno.ELOOP:
        return BrowserAdapterError(f"{label} is or traverses a symbolic link")
    return BrowserAdapterError(f"cannot open {label}: {error}")


def _require_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise BrowserAdapterError(f"{label} is not a directory")
    try:
        _safe_owner(info, label)
    except RuntimeSafetyError as error:
        raise BrowserAdapterError(str(error)) from error
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o500 != 0o500:
        raise BrowserAdapterError(f"{label} must be owner-readable and searchable")


def _require_file(info: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise BrowserAdapterError(f"{label} is not a regular file")
    try:
        _safe_owner(info, label)
    except RuntimeSafetyError as error:
        raise BrowserAdapterError(str(error)) from error
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o400 != 0o400 or mode & 0o111:
        raise BrowserAdapterError(
            f"{label} must be owner-readable and non-executable"
        )
    if info.st_nlink != 1:
        raise BrowserAdapterError(f"{label} must have exactly one hard link")
    if info.st_size > MAX_BROWSER_ADAPTER_FILE_BYTES:
        raise BrowserAdapterError(f"{label} exceeds the size limit")


def _expected_children() -> dict[str, frozenset[str]]:
    children: dict[str, set[str]] = {"": set()}
    for relative_path, _digest in BROWSER_ADAPTER_FILE_SHA256:
        components = relative_path.split("/")
        if any(
            component in ("", ".", "..")
            or "/" in component
            or "\x00" in component
            for component in components
        ):
            raise BrowserAdapterError("internal adapter file authority is invalid")
        parent = ""
        for component in components[:-1]:
            children.setdefault(parent, set()).add(component)
            parent = f"{parent}/{component}".strip("/")
            children.setdefault(parent, set())
        children[parent].add(components[-1])
    return {path: frozenset(entries) for path, entries in children.items()}


def _canonical_tree_digest(files: tuple[tuple[str, str], ...]) -> str:
    payload = (
        json.dumps(
            {
                "browserAdapterBundleVersion": BROWSER_ADAPTER_BUNDLE_VERSION,
                "files": dict(files),
            },
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_and_hash_file(
    directory_descriptor: int,
    name: str,
    relative_path: str,
    common_flags: int,
) -> tuple[str, int]:
    label = f"browser adapter file {relative_path}"
    try:
        descriptor = os.open(name, common_flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise _browser_adapter_error(label, error) from error
    try:
        initial = os.fstat(descriptor)
        _require_file(initial, label)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BROWSER_ADAPTER_FILE_BYTES:
                raise BrowserAdapterError(f"{label} exceeds the size limit")
            digest.update(chunk)
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final):
            raise BrowserAdapterError(f"{label} changed while hashing")
        if total != initial.st_size:
            raise BrowserAdapterError(f"{label} size changed while hashing")
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _verify_tree(root_descriptor: int) -> tuple[tuple[str, str], ...]:
    common_flags, directory_flags = _platform_flags()
    expected_files = dict(BROWSER_ADAPTER_FILE_SHA256)
    expected_children = _expected_children()
    observed: dict[str, str] = {}
    total_bytes = 0

    def visit(directory_descriptor: int, relative_directory: str) -> None:
        nonlocal total_bytes
        label = (
            "browser adapter root"
            if not relative_directory
            else f"browser adapter directory {relative_directory}"
        )
        initial = os.fstat(directory_descriptor)
        _require_directory(initial, label)
        try:
            entries = os.listdir(directory_descriptor)
        except OSError as error:
            raise BrowserAdapterError(f"cannot list {label}: {error}") from error
        if not all(isinstance(entry, str) for entry in entries):
            raise BrowserAdapterError(f"{label} contains a non-text entry")
        actual = frozenset(entries)
        expected = expected_children[relative_directory]
        if actual != expected:
            missing = sorted(expected - actual)
            unknown = sorted(actual - expected)
            details: list[str] = []
            if missing:
                details.append(f"missing {missing}")
            if unknown:
                details.append(f"unknown {unknown}")
            raise BrowserAdapterError(
                f"{label} entries do not match the closed bundle: {', '.join(details)}"
            )
        for name in sorted(entries):
            relative_path = f"{relative_directory}/{name}".strip("/")
            if relative_path in expected_children:
                child_label = f"browser adapter directory {relative_path}"
                try:
                    child = os.open(
                        name,
                        directory_flags,
                        dir_fd=directory_descriptor,
                    )
                except OSError as error:
                    raise _browser_adapter_error(child_label, error) from error
                try:
                    visit(child, relative_path)
                finally:
                    os.close(child)
            else:
                digest, size = _read_and_hash_file(
                    directory_descriptor,
                    name,
                    relative_path,
                    common_flags,
                )
                expected_digest = expected_files.get(relative_path)
                if expected_digest is None:
                    raise BrowserAdapterError(
                        f"browser adapter file is not in the closed authority: {relative_path}"
                    )
                if not hmac.compare_digest(digest, expected_digest):
                    raise BrowserAdapterError(
                        f"browser adapter file digest does not match: {relative_path}"
                    )
                observed[relative_path] = digest
                total_bytes += size
                if total_bytes > MAX_BROWSER_ADAPTER_TOTAL_BYTES:
                    raise BrowserAdapterError(
                        "browser adapter bundle exceeds the total size limit"
                    )
        final = os.fstat(directory_descriptor)
        if _stat_identity(initial) != _stat_identity(final):
            raise BrowserAdapterError(f"{label} changed while verifying")

    visit(root_descriptor, "")
    files = tuple(sorted(observed.items()))
    if files != tuple(sorted(BROWSER_ADAPTER_FILE_SHA256)):
        raise BrowserAdapterError("browser adapter file authority is incomplete")
    tree_digest = _canonical_tree_digest(files)
    if not hmac.compare_digest(tree_digest, EXPECTED_BROWSER_ADAPTER_TREE_SHA256):
        raise BrowserAdapterError("browser adapter canonical tree digest does not match")
    return files


def _open_absolute_directory(path: str) -> tuple[int, tuple[int, ...]]:
    try:
        normalized = _absolute_normalized_path(path, "browser adapter bundle")
        _common_flags, directory_flags = _platform_flags()
    except RuntimeSafetyError as error:
        raise BrowserAdapterError(str(error)) from error
    components = normalized[1:].split("/")
    descriptors: list[int] = []
    try:
        try:
            current = os.open("/", directory_flags)
        except OSError as error:
            raise _browser_adapter_error("browser adapter root ancestor", error) from error
        descriptors.append(current)
        _require_directory(os.fstat(current), "browser adapter ancestor /")
        traversed = ""
        for component in components:
            traversed += f"/{component}"
            label = f"browser adapter ancestor {traversed}"
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                raise _browser_adapter_error(label, error) from error
            descriptors.append(child)
            current = child
            _require_directory(os.fstat(current), label)
        descriptor = descriptors.pop()
        info = os.fstat(descriptor)
        return descriptor, _stat_identity(info)
    finally:
        for descriptor_to_close in reversed(descriptors):
            os.close(descriptor_to_close)


@contextmanager
def open_verified_browser_adapter(
    bundle_path: os.PathLike[str] | str,
) -> Iterator[OpenVerifiedBrowserAdapter]:
    """Open and hash an exact closed browser adapter directory."""
    try:
        normalized = _absolute_normalized_path(
            bundle_path,
            "browser adapter bundle",
        )
    except RuntimeSafetyError as error:
        raise BrowserAdapterError(str(error)) from error
    descriptor, root_identity = _open_absolute_directory(normalized)
    try:
        files = _verify_tree(descriptor)
        yield OpenVerifiedBrowserAdapter(
            bundle_version=BROWSER_ADAPTER_BUNDLE_VERSION,
            logical_path=normalized,
            descriptor=descriptor,
            root_identity=root_identity,
            files=files,
            tree_sha256=EXPECTED_BROWSER_ADAPTER_TREE_SHA256,
        )
    finally:
        os.close(descriptor)


@contextmanager
def open_bundled_browser_adapter() -> Iterator[OpenVerifiedBrowserAdapter]:
    """Open the only package-bundled adapter accepted by bundled mode."""
    with open_verified_browser_adapter(bundled_browser_adapter_path()) as opened:
        yield opened


def revalidate_open_browser_adapter(opened: OpenVerifiedBrowserAdapter) -> None:
    """Re-hash the retained bundle immediately before browser spawn."""
    if not isinstance(opened, OpenVerifiedBrowserAdapter):
        raise BrowserAdapterError("browser adapter authority was fabricated")
    try:
        current = os.fstat(opened.descriptor)
    except OSError as error:
        raise BrowserAdapterError(f"cannot revalidate browser adapter root: {error}") from error
    if _stat_identity(current) != opened.root_identity:
        raise BrowserAdapterError("browser adapter root changed after verification")
    files = _verify_tree(opened.descriptor)
    if files != opened.files:
        raise BrowserAdapterError("browser adapter files changed after verification")


def browser_adapter_application_path(opened: OpenVerifiedBrowserAdapter) -> str:
    """Return the fd-backed application.ini path after descriptor validation."""
    revalidate_open_browser_adapter(opened)
    return (
        f"/proc/self/fd/{opened.descriptor}/"
        f"{BROWSER_ADAPTER_APPLICATION_RELATIVE_PATH}"
    )


def bundled_adapter_capability(
    runtime_identity: RuntimeIdentity,
    opened: OpenVerifiedBrowserAdapter,
) -> AppShellAdapterCapability:
    """Bind the verified built-in adapter to one exact verified runtime."""
    revalidate_open_browser_adapter(opened)
    capability = AppShellAdapterCapability(
        capability_version=APP_SHELL_ADAPTER_CAPABILITY_VERSION,
        adapter_id=APP_SHELL_ADAPTER_ID,
        protocol_version=APP_SHELL_ADAPTER_PROTOCOL_VERSION,
        runtime_identity=runtime_identity,
        capabilities=REQUIRED_ADAPTER_CAPABILITIES,
    )
    adapter_capability_to_dict(capability)
    return capability


def browser_adapter_to_dict(opened: OpenVerifiedBrowserAdapter) -> dict[str, Any]:
    """Render stable bundle identity without exposing descriptor numbers."""
    revalidate_open_browser_adapter(opened)
    return {
        "browserAdapterBundleVersion": opened.bundle_version,
        "files": [path for path, _digest in opened.files],
        "source": "package-bundled",
        "treeSha256": opened.tree_sha256,
    }
