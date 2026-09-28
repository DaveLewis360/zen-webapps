"""Offline assembly and retained authority for one pinned Chromium/CEF candidate.

The runnable reference is built from the SDK contained in one exact,
Arch-signed CEF package.  The package is verified before decompression, every
archive member is safety-scanned, only a closed runtime/SDK subset is extracted,
and the small project-owned shell is built with one compiler job and hardened
link flags.  The package's setuid sandbox is deliberately never extracted into
a user-owned tree; native execution is authorized only while the exact
root-owned installed helper remains at the pinned path.

This is a runnable bakeoff reference, not a claim that Chromium/CEF was rebuilt
locally.  Camera/microphone/screen permissions are denied by the reference shell
and Wayland capture parity remains blocked by upstream CEF issue 4053.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import stat
import subprocess
import sys
import tarfile
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from ._secure_fs import rename_noreplace, write_all
from .runtime import (
    RuntimeSafetyError,
    _F_ADD_SEALS,
    _F_GET_SEALS,
    _REQUIRED_SEALS,
    _absolute_normalized_path,
    _create_memfd,
    _open_absolute_file,
    _platform_flags,
    _read_bounded_utf8,
    _safe_owner,
    _stat_identity,
)

CHROMIUM_RUNTIME_SOURCE_LOCK_VERSION = 1
CHROMIUM_RUNTIME_TREE_VERSION = 1
CHROMIUM_SHELL_SOURCE_TREE_VERSION = 1
CHROMIUM_SDK_TREE_VERSION = 1
CHROMIUM_RUNTIME_CANDIDATE_VERSION = 1
CHROMIUM_RUNTIME_CANDIDATE_DESCRIPTOR = "chromium-runtime-candidate.json"
BUNDLED_CHROMIUM_RUNTIME_SOURCE_LOCK = (
    "chromium-cef-151.3.24-1-x86_64.lock.json"
)
EXPECTED_BUNDLED_CHROMIUM_RUNTIME_SOURCE_LOCK_SHA256 = (
    "fd802275a477c912f9c54cf8f967855008a11449c63ae667f622a60875f6007a"
)
PACMAN_GPG_HOME = "/etc/pacman.d/gnupg"
MAX_LOCK_BYTES = 512 * 1024
MAX_DESCRIPTOR_BYTES = 128 * 1024
MAX_PACKAGE_METADATA_BYTES = 128 * 1024
MAX_PACKAGE_MEMBERS = 100_000
MAX_RELATIVE_PATH_BYTES = 4096
MAX_PATH_DEPTH = 64
MAX_TOOL_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_BUILD_SECONDS = 1200

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}$")
_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

_RUNTIME_ARCHIVE_PATHS = (
    "usr/lib/cef/chrome_100_percent.pak",
    "usr/lib/cef/chrome_200_percent.pak",
    "usr/lib/cef/icudtl.dat",
    "usr/lib/cef/libEGL.so",
    "usr/lib/cef/libGLESv2.so",
    "usr/lib/cef/libcef.so",
    "usr/lib/cef/libvk_swiftshader.so",
    "usr/lib/cef/libvulkan.so.1",
    "usr/lib/cef/locales/en-US.pak",
    "usr/lib/cef/resources.pak",
    "usr/lib/cef/v8_context_snapshot.bin",
    "usr/lib/cef/vk_swiftshader_icd.json",
)
_SDK_PREFIXES = ("usr/include/cef/", "usr/src/cef/")
_SDK_EXACT_PATHS = frozenset({"usr/share/cmake/Modules/FindCEF.cmake"})
_SHELL_SOURCE_PATHS = ("CMakeLists.txt", "main.cc")
_PACKAGE_METADATA_PATHS = (".BUILDINFO", ".MTREE", ".PKGINFO")
_BUILD_FLAGS = (
    'custom_toolchain="//build/toolchain/linux/unbundle:default"',
    'host_toolchain="//build/toolchain/linux/unbundle:default"',
    "is_official_build=true",
    "symbol_level=0",
    "treat_warnings_as_errors=false",
    "fatal_linker_warnings=false",
    "disable_fieldtrial_testing_config=true",
    "blink_enable_generated_code_formatting=false",
    'ffmpeg_branding="Chrome"',
    "proprietary_codecs=true",
    "rtc_use_pipewire=true",
    "link_pulseaudio=true",
    "use_custom_libcxx=true",
    "use_sysroot=false",
    "use_system_libffi=true",
    "use_qt5=false",
    "use_qt6=false",
    "use_gtk=false",
    "use_cups=false",
    "use_gio=false",
    "use_kerberos=false",
    "use_libpci=false",
    "use_udev=true",
    "cef_use_gtk=false",
    "enable_remoting=false",
    "use_vaapi=false",
    "use_clang_modules=false",
    "devtools_bundle=false",
)
_TOOL_NAMES = ("clang", "cmake", "gpg", "ninja", "zstd")


class ChromiumRuntimeCandidateError(ValueError):
    """A Chromium source lock, package, build, or candidate failed closed."""


class ChromiumRuntimeCandidateSafetyError(ChromiumRuntimeCandidateError):
    """A filesystem/archive safety invariant failed."""


@dataclass(frozen=True)
class ChromiumRuntimeSourceLock:
    """Validated immutable canonical source-lock bytes."""

    canonical: bytes


@dataclass(frozen=True)
class ChromiumRuntimeTreeEntry:
    path: str
    size: int
    mode: str
    sha256: str


@dataclass(frozen=True)
class ChromiumRuntimeCandidateDescriptor:
    """Validated immutable canonical candidate-descriptor bytes."""

    canonical: bytes


@dataclass(frozen=True)
class OpenVerifiedChromiumRuntimeCandidate:
    logical_path: str
    root_descriptor: int
    runtime_descriptor: int
    sandbox_descriptor: int
    root_identity: tuple[int, ...]
    runtime_identity: tuple[int, ...]
    sandbox_identity: tuple[int, ...]
    descriptor: ChromiumRuntimeCandidateDescriptor
    files: tuple[ChromiumRuntimeTreeEntry, ...]


@dataclass(frozen=True)
class _OpenedPinnedInput:
    label: str
    logical_path: str
    descriptor: int
    initial_identity: tuple[int, ...]


@dataclass(frozen=True)
class _PackageScan:
    sdk_files: tuple[ChromiumRuntimeTreeEntry, ...]


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual == expected:
        return
    details: list[str] = []
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        details.append(f"missing {missing}")
    if unknown:
        details.append(f"unknown {unknown}")
    raise ChromiumRuntimeCandidateError(f"{label} fields: {', '.join(details)}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ChromiumRuntimeCandidateError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ChromiumRuntimeCandidateError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChromiumRuntimeCandidateError(f"{label} must be nonempty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ChromiumRuntimeCandidateError(f"{label} cannot contain control characters")
    return value


def _integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ChromiumRuntimeCandidateError(f"{label} must be a {qualifier} integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ChromiumRuntimeCandidateError(f"{label} must be boolean")
    return value


def _digest(value: Any, label: str, pattern: re.Pattern[str] = _SHA256_PATTERN) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ChromiumRuntimeCandidateError(f"{label} has an invalid digest")
    return value


def _safe_relative_path(value: Any, label: str, *, basename: bool = False) -> str:
    path = _text(value, label)
    if path.startswith("/") or "\\" in path or len(path.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise ChromiumRuntimeCandidateError(f"{label} must be a safe relative POSIX path")
    components = path.split("/")
    if (
        any(component in ("", ".", "..") for component in components)
        or len(components) > MAX_PATH_DEPTH
        or (basename and len(components) != 1)
    ):
        raise ChromiumRuntimeCandidateError(f"{label} must be a safe relative POSIX path")
    return path


def _absolute_path_text(value: Any, label: str) -> str:
    path = _text(value, label)
    if not path.startswith("/") or path == "/" or path.endswith("/") or "//" in path:
        raise ChromiumRuntimeCandidateError(f"{label} must be an absolute normalized path")
    components = path[1:].split("/")
    if any(component in ("", ".", "..") for component in components):
        raise ChromiumRuntimeCandidateError(f"{label} must be an absolute normalized path")
    return path


def _https_url(value: Any, label: str) -> str:
    rendered = _text(value, label)
    parsed = urlsplit(rendered)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.geturl() != rendered
    ):
        raise ChromiumRuntimeCandidateError(
            f"{label} must be a canonical credential-free HTTPS URL"
        )
    return rendered


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True)
        + "\n"
    ).encode("ascii")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChromiumRuntimeCandidateError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ChromiumRuntimeCandidateError(
        f"non-finite JSON number is not permitted: {value}"
    )


def _parse_json_bytes(payload: bytes, label: str) -> Any:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChromiumRuntimeCandidateError(f"{label} must be UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except ChromiumRuntimeCandidateError:
        raise
    except json.JSONDecodeError as error:
        raise ChromiumRuntimeCandidateError(f"{label} is not strict JSON: {error}") from error


def _file_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"mode", "path", "sha256", "size"}), label)
    path = _safe_relative_path(data["path"], f"{label}.path")
    mode = _text(data["mode"], f"{label}.mode")
    if _MODE_PATTERN.fullmatch(mode) is None or mode not in ("0644", "0755"):
        raise ChromiumRuntimeCandidateError(f"{label}.mode is not allowed")
    _integer(data["size"], f"{label}.size", allow_zero=True)
    _digest(data["sha256"], f"{label}.sha256")
    return {**data, "path": path, "mode": mode}


def _packaging_entry_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    if data.get("type") != "symlink":
        return _file_pin(data, label)
    _exact_keys(data, frozenset({"path", "target", "type"}), label)
    path = _safe_relative_path(data["path"], f"{label}.path")
    target = _text(data["target"], f"{label}.target")
    if (
        target.startswith("/")
        or "\\" in target
        or len(target.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
    ):
        raise ChromiumRuntimeCandidateError(
            f"{label}.target must be a bounded relative POSIX link"
        )
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if resolved in (".", "..") or resolved.startswith("../"):
        raise ChromiumRuntimeCandidateError(
            f"{label}.target must remain inside the packaging tree"
        )
    return {**data, "path": path, "target": target}


def _archive_file_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset({"archivePath", "mode", "path", "sha256", "size"}),
        label,
    )
    _safe_relative_path(data["archivePath"], f"{label}.archivePath")
    _file_pin(
        {key: data[key] for key in ("mode", "path", "sha256", "size")}, label
    )
    return data


def _artifact_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"filename", "sha256", "size", "url"}), label)
    filename = _safe_relative_path(data["filename"], f"{label}.filename", basename=True)
    url = _https_url(data["url"], f"{label}.url")
    if not urlsplit(url).path.endswith("/" + filename):
        raise ChromiumRuntimeCandidateError(f"{label}.url does not end in filename")
    _integer(data["size"], f"{label}.size")
    _digest(data["sha256"], f"{label}.sha256")
    return data


def _signature_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    _exact_keys(
        data,
        frozenset(
            {
                "fingerprint",
                "hashAlgorithm",
                "hashAlgorithmCode",
                "publicKeyAlgorithm",
                "publicKeyAlgorithmCode",
                "signatureDate",
                "signatureTimestamp",
                "signerUid",
                "trust",
            }
        ),
        label,
    )
    _digest(data["fingerprint"], f"{label}.fingerprint", _FINGERPRINT_PATTERN)
    _text(data["signerUid"], f"{label}.signerUid")
    date = _text(data["signatureDate"], f"{label}.signatureDate")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date) is None:
        raise ChromiumRuntimeCandidateError(f"{label}.signatureDate is invalid")
    if (
        data["hashAlgorithm"] != "SHA512"
        or data["hashAlgorithmCode"] != 10
        or data["publicKeyAlgorithm"] != "EdDSA"
        or data["publicKeyAlgorithmCode"] != 22
        or data["trust"] != "fully"
    ):
        raise ChromiumRuntimeCandidateError(f"{label} algorithms/trust are not closed")
    _integer(data["signatureTimestamp"], f"{label}.signatureTimestamp")
    return data


def _tool_pin(value: Any, label: str) -> Mapping[str, Any]:
    data = _mapping(value, label)
    _exact_keys(data, frozenset({"path", "sha256", "size", "version"}), label)
    _absolute_path_text(data["path"], f"{label}.path")
    _digest(data["sha256"], f"{label}.sha256")
    _integer(data["size"], f"{label}.size")
    _text(data["version"], f"{label}.version")
    return data


def _tree_entry_to_dict(entry: ChromiumRuntimeTreeEntry) -> dict[str, Any]:
    return {
        "mode": entry.mode,
        "path": entry.path,
        "sha256": entry.sha256,
        "size": entry.size,
    }


def _tree_entries_from_pins(pins: Sequence[Mapping[str, Any]]) -> tuple[ChromiumRuntimeTreeEntry, ...]:
    return tuple(
        sorted(
            (
                ChromiumRuntimeTreeEntry(
                    path=str(pin["path"]),
                    size=int(pin["size"]),
                    mode=str(pin["mode"]),
                    sha256=str(pin["sha256"]),
                )
                for pin in pins
            ),
            key=lambda item: item.path,
        )
    )


def chromium_runtime_tree_sha256(entries: Sequence[ChromiumRuntimeTreeEntry]) -> str:
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    if len({item.path for item in ordered}) != len(ordered):
        raise ChromiumRuntimeCandidateError("Chromium runtime tree has duplicate paths")
    for index, entry in enumerate(ordered):
        _file_pin(_tree_entry_to_dict(entry), f"runtime.files[{index}]")
    payload = _canonical_json_bytes(
        {
            "chromiumRuntimeTreeVersion": CHROMIUM_RUNTIME_TREE_VERSION,
            "files": [_tree_entry_to_dict(item) for item in ordered],
        }
    )
    return hashlib.sha256(payload).hexdigest()


def _versioned_tree_sha256(
    entries: Sequence[ChromiumRuntimeTreeEntry], version_field: str, version: int
) -> str:
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    if len({item.path for item in ordered}) != len(ordered):
        raise ChromiumRuntimeCandidateError(f"{version_field} has duplicate paths")
    return hashlib.sha256(
        _canonical_json_bytes(
            {version_field: version, "files": [_tree_entry_to_dict(item) for item in ordered]}
        )
    ).hexdigest()


def _validate_lock(value: Any) -> Mapping[str, Any]:
    data = _mapping(value, "Chromium runtime source lock")
    _exact_keys(
        data,
        frozenset(
            {
                "artifacts",
                "candidate",
                "chromiumRuntimeSourceLockVersion",
                "package",
                "packaging",
                "provenance",
                "shell",
            }
        ),
        "Chromium runtime source lock",
    )
    if data["chromiumRuntimeSourceLockVersion"] != CHROMIUM_RUNTIME_SOURCE_LOCK_VERSION:
        raise ChromiumRuntimeCandidateError(
            f"chromiumRuntimeSourceLockVersion must be {CHROMIUM_RUNTIME_SOURCE_LOCK_VERSION}"
        )

    artifacts = _mapping(data["artifacts"], "artifacts")
    _exact_keys(artifacts, frozenset({"package", "packageSignature"}), "artifacts")
    package_artifact = _artifact_pin(artifacts["package"], "artifacts.package")
    signature_artifact = _artifact_pin(
        artifacts["packageSignature"], "artifacts.packageSignature"
    )
    if signature_artifact["filename"] != package_artifact["filename"] + ".sig":
        raise ChromiumRuntimeCandidateError("package signature filename is not derived")

    package = _mapping(data["package"], "package")
    _exact_keys(
        package,
        frozenset(
            {
                "arch",
                "archiveDirectories",
                "archiveEntries",
                "archiveRegularFiles",
                "buildInfo",
                "buildTimestamp",
                "mtree",
                "name",
                "packageInfo",
                "packager",
                "signature",
                "version",
            }
        ),
        "package",
    )
    if package["name"] != "cef" or package["version"] != "151.3.24-1" or package["arch"] != "x86_64":
        raise ChromiumRuntimeCandidateError("package identity is not the closed CEF package")
    _text(package["packager"], "package.packager")
    _integer(package["buildTimestamp"], "package.buildTimestamp")
    for field in ("archiveDirectories", "archiveEntries", "archiveRegularFiles"):
        _integer(package[field], f"package.{field}")
    if (
        package["archiveDirectories"] + package["archiveRegularFiles"]
        != package["archiveEntries"]
    ):
        raise ChromiumRuntimeCandidateError("package archive counters do not reconcile")
    for field, expected_path in (
        ("buildInfo", ".BUILDINFO"),
        ("mtree", ".MTREE"),
        ("packageInfo", ".PKGINFO"),
    ):
        pin = _file_pin(package[field], f"package.{field}")
        if pin["path"] != expected_path or pin["mode"] != "0644":
            raise ChromiumRuntimeCandidateError(f"package.{field} identity is invalid")
    package_signature = _signature_pin(package["signature"], "package.signature")

    packaging = _mapping(data["packaging"], "packaging")
    _exact_keys(
        packaging,
        frozenset(
            {
                "buildFlags",
                "cefCommit",
                "cefGitArchiveSha256",
                "chromiumSourceSha256",
                "commit",
                "files",
                "pkgbuildSha256",
                "repository",
                "tag",
                "tagObject",
                "tagSignature",
                "treeListingSha256",
            }
        ),
        "packaging",
    )
    if tuple(_array(packaging["buildFlags"], "packaging.buildFlags")) != _BUILD_FLAGS:
        raise ChromiumRuntimeCandidateError("packaging.buildFlags is not the reviewed list")
    for field in ("cefCommit", "commit"):
        _digest(packaging[field], f"packaging.{field}", _COMMIT_PATTERN)
    for field in (
        "cefGitArchiveSha256",
        "chromiumSourceSha256",
        "pkgbuildSha256",
        "treeListingSha256",
    ):
        _digest(packaging[field], f"packaging.{field}")
    _digest(packaging["tagObject"], "packaging.tagObject", _COMMIT_PATTERN)
    if packaging["tag"] != package["version"]:
        raise ChromiumRuntimeCandidateError("packaging tag/package version mismatch")
    _https_url(packaging["repository"], "packaging.repository")
    _signature_pin(packaging["tagSignature"], "packaging.tagSignature")
    packaging_files_data = _array(packaging["files"], "packaging.files")
    packaging_files = [
        _packaging_entry_pin(item, f"packaging.files[{index}]")
        for index, item in enumerate(packaging_files_data)
    ]
    packaging_paths = [str(item["path"]) for item in packaging_files]
    if packaging_paths != sorted(packaging_paths) or len(set(packaging_paths)) != len(packaging_paths):
        raise ChromiumRuntimeCandidateError("packaging.files must be unique and sorted")
    packaging_links = {
        (str(item["path"]), str(item["target"]))
        for item in packaging_files
        if item.get("type") == "symlink"
    }
    if packaging_links != {("LICENSES/0BSD.txt", "../LICENSE")}:
        raise ChromiumRuntimeCandidateError("packaging symlink authority is not closed")
    regular_packaging_paths = {
        str(item["path"])
        for item in packaging_files
        if item.get("type") != "symlink"
    }
    for path, target in packaging_links:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
        if resolved not in regular_packaging_paths:
            raise ChromiumRuntimeCandidateError(
                "packaging symlink does not resolve to a locked regular file"
            )
    pkgbuild = next((item for item in packaging_files if item["path"] == "PKGBUILD"), None)
    if pkgbuild is None or pkgbuild["sha256"] != packaging["pkgbuildSha256"]:
        raise ChromiumRuntimeCandidateError("packaging PKGBUILD authority is incomplete")

    shell = _mapping(data["shell"], "shell")
    _exact_keys(shell, frozenset({"sourceFiles", "sourceTreeSha256", "toolchain"}), "shell")
    source_files_data = _array(shell["sourceFiles"], "shell.sourceFiles")
    source_files = [
        _file_pin(item, f"shell.sourceFiles[{index}]")
        for index, item in enumerate(source_files_data)
    ]
    if tuple(item["path"] for item in source_files) != _SHELL_SOURCE_PATHS:
        raise ChromiumRuntimeCandidateError("shell.sourceFiles is not the closed source set")
    source_entries = _tree_entries_from_pins(source_files)
    source_tree = _versioned_tree_sha256(
        source_entries,
        "chromiumShellSourceTreeVersion",
        CHROMIUM_SHELL_SOURCE_TREE_VERSION,
    )
    if not hmac.compare_digest(source_tree, _digest(shell["sourceTreeSha256"], "shell.sourceTreeSha256")):
        raise ChromiumRuntimeCandidateError("shell source tree digest does not match")
    toolchain = _mapping(shell["toolchain"], "shell.toolchain")
    _exact_keys(
        toolchain,
        frozenset(
            {
                *_TOOL_NAMES,
                "parallelJobs",
                "sdkFileCount",
                "sdkTotalBytes",
                "sdkTreeSha256",
                "warningExceptions",
            }
        ),
        "shell.toolchain",
    )
    for name in _TOOL_NAMES:
        _tool_pin(toolchain[name], f"shell.toolchain.{name}")
    if toolchain["parallelJobs"] != 1:
        raise ChromiumRuntimeCandidateError("shell build must use one parallel job")
    _integer(toolchain["sdkFileCount"], "shell.toolchain.sdkFileCount")
    _integer(toolchain["sdkTotalBytes"], "shell.toolchain.sdkTotalBytes")
    _digest(toolchain["sdkTreeSha256"], "shell.toolchain.sdkTreeSha256")
    if toolchain["warningExceptions"] != ["-Wno-undefined-var-template"]:
        raise ChromiumRuntimeCandidateError("shell warning exception is not closed")

    candidate = _mapping(data["candidate"], "candidate")
    _exact_keys(
        candidate,
        frozenset(
            {
                "candidateId",
                "cefVersion",
                "chromiumVersion",
                "executable",
                "fileCount",
                "locale",
                "runtimeFiles",
                "sandbox",
                "target",
                "totalBytes",
                "treeSha256",
            }
        ),
        "candidate",
    )
    if (
        candidate["candidateId"] != "cef-151.3.24-1-linux-x86_64-en-US"
        or candidate["cefVersion"]
        != "151.3.24+g2384915+chromium-151.0.7922.174"
        or candidate["chromiumVersion"] != "151.0.7922.174"
        or candidate["locale"] != "en-US"
        or candidate["target"] != "linux-x86_64"
        or candidate["cefVersion"].split("+g", 1)[1].split("+", 1)[0]
        != str(packaging["cefCommit"])[:7]
    ):
        raise ChromiumRuntimeCandidateError("candidate identity is not the closed CEF v1 shape")
    executable = _file_pin(candidate["executable"], "candidate.executable")
    if executable["path"] != "zen-cef-shell" or executable["mode"] != "0755":
        raise ChromiumRuntimeCandidateError("candidate executable identity is invalid")
    runtime_data = _array(candidate["runtimeFiles"], "candidate.runtimeFiles")
    runtime_files = [
        _archive_file_pin(item, f"candidate.runtimeFiles[{index}]")
        for index, item in enumerate(runtime_data)
    ]
    if tuple(item["archivePath"] for item in runtime_files) != _RUNTIME_ARCHIVE_PATHS:
        raise ChromiumRuntimeCandidateError("candidate runtime archive set is not closed")
    expected_destinations = tuple(path.removeprefix("usr/lib/cef/") for path in _RUNTIME_ARCHIVE_PATHS)
    if tuple(item["path"] for item in runtime_files) != expected_destinations:
        raise ChromiumRuntimeCandidateError("candidate runtime destinations are not closed")
    all_runtime = [executable, *runtime_files]
    entries = _tree_entries_from_pins(all_runtime)
    if candidate["fileCount"] != len(entries) or candidate["totalBytes"] != sum(item.size for item in entries):
        raise ChromiumRuntimeCandidateError("candidate runtime count/bytes do not reconcile")
    if not hmac.compare_digest(
        chromium_runtime_tree_sha256(entries),
        _digest(candidate["treeSha256"], "candidate.treeSha256"),
    ):
        raise ChromiumRuntimeCandidateError("candidate runtime tree digest does not match")
    sandbox = _mapping(candidate["sandbox"], "candidate.sandbox")
    _exact_keys(
        sandbox,
        frozenset({"gid", "installedPath", "mode", "sha256", "size", "uid"}),
        "candidate.sandbox",
    )
    if (
        _absolute_path_text(sandbox["installedPath"], "candidate.sandbox.installedPath")
        != "/usr/lib/cef/chrome-sandbox"
        or sandbox["mode"] != "4755"
        or sandbox["uid"] != 0
        or sandbox["gid"] != 0
    ):
        raise ChromiumRuntimeCandidateError("candidate sandbox authority is not root-owned 4755")
    _integer(sandbox["size"], "candidate.sandbox.size")
    _digest(sandbox["sha256"], "candidate.sandbox.sha256")

    provenance = _mapping(data["provenance"], "provenance")
    _exact_keys(
        provenance,
        frozenset(
            {
                "captureIssue",
                "captureParity",
                "cefSourceBuildStatus",
                "cefSourceReproducibilityStatus",
                "downloadBoundary",
                "permissionBoundary",
                "rtcUsePipeWire",
            }
        ),
        "provenance",
    )
    if (
        provenance["captureParity"] != "blocked-upstream-cef-4053"
        or provenance["cefSourceBuildStatus"] != "not-performed"
        or provenance["cefSourceReproducibilityStatus"] != "not-tested"
        or provenance["downloadBoundary"] != "system-save-picker"
        or provenance["permissionBoundary"] != "deny-all-reference"
        or _https_url(provenance["captureIssue"], "provenance.captureIssue")
        != "https://github.com/chromiumembedded/cef/issues/4053"
        or _boolean(provenance["rtcUsePipeWire"], "provenance.rtcUsePipeWire") is not True
    ):
        raise ChromiumRuntimeCandidateError("provenance claims are not the closed fail-closed set")

    if package_artifact["filename"] != f"cef-{package['version']}-{package['arch']}.pkg.tar.zst":
        raise ChromiumRuntimeCandidateError("package artifact identity mismatch")
    if package_signature["fingerprint"] != packaging["tagSignature"]["fingerprint"]:
        raise ChromiumRuntimeCandidateError("package/tag signer fingerprint mismatch")
    return data


def chromium_runtime_source_lock_from_dict(value: Any) -> ChromiumRuntimeSourceLock:
    data = _validate_lock(value)
    return ChromiumRuntimeSourceLock(_canonical_json_bytes(data))


def chromium_runtime_source_lock_to_dict(lock: ChromiumRuntimeSourceLock) -> dict[str, Any]:
    if not isinstance(lock, ChromiumRuntimeSourceLock):
        raise ChromiumRuntimeCandidateError("Chromium source lock was fabricated")
    data = _parse_json_bytes(lock.canonical, "Chromium runtime source lock")
    _validate_lock(data)
    if not hmac.compare_digest(lock.canonical, _canonical_json_bytes(data)):
        raise ChromiumRuntimeCandidateError("Chromium source lock bytes are not canonical")
    return data


def render_chromium_runtime_source_lock(lock: ChromiumRuntimeSourceLock) -> str:
    chromium_runtime_source_lock_to_dict(lock)
    return lock.canonical.decode("ascii")


def chromium_runtime_source_lock_sha256(lock: ChromiumRuntimeSourceLock) -> str:
    return hashlib.sha256(lock.canonical).hexdigest()


def _read_document(path: os.PathLike[str] | str, label: str, limit: int) -> tuple[str, bytes]:
    try:
        normalized = _absolute_normalized_path(path, label)
        with _open_absolute_file(normalized, label) as (descriptor, initial):
            if initial.st_nlink != 1:
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} must have exactly one hard link"
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            text = _read_bounded_utf8(descriptor, limit, label)
            final = os.fstat(descriptor)
            if _stat_identity(initial) != _stat_identity(final):
                raise ChromiumRuntimeCandidateSafetyError(f"{label} changed while reading")
            return normalized, text.encode("utf-8")
    except ChromiumRuntimeCandidateError:
        raise
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error


def load_chromium_runtime_source_lock(path: os.PathLike[str] | str) -> ChromiumRuntimeSourceLock:
    _normalized, payload = _read_document(path, "Chromium runtime source lock", MAX_LOCK_BYTES)
    lock = chromium_runtime_source_lock_from_dict(
        _parse_json_bytes(payload, "Chromium runtime source lock")
    )
    if not hmac.compare_digest(payload, lock.canonical):
        raise ChromiumRuntimeCandidateError("Chromium runtime source lock is not canonical JSON")
    return lock


def bundled_chromium_runtime_source_lock_path() -> str:
    return os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "runtime_candidates",
            BUNDLED_CHROMIUM_RUNTIME_SOURCE_LOCK,
        )
    )


def bundled_chromium_shell_source_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "chromium_shell"))


def load_bundled_chromium_runtime_source_lock() -> ChromiumRuntimeSourceLock:
    lock = load_chromium_runtime_source_lock(bundled_chromium_runtime_source_lock_path())
    actual = chromium_runtime_source_lock_sha256(lock)
    if not hmac.compare_digest(actual, EXPECTED_BUNDLED_CHROMIUM_RUNTIME_SOURCE_LOCK_SHA256):
        raise ChromiumRuntimeCandidateError("bundled Chromium source lock digest does not match")
    return lock


def _expected_candidate_descriptor(lock: ChromiumRuntimeSourceLock) -> ChromiumRuntimeCandidateDescriptor:
    data = chromium_runtime_source_lock_to_dict(lock)
    candidate = data["candidate"]
    package = data["package"]
    packaging = data["packaging"]
    provenance = data["provenance"]
    artifacts = data["artifacts"]
    descriptor = {
        "boundaries": {
            "captureParity": provenance["captureParity"],
            "downloadBoundary": provenance["downloadBoundary"],
            "permissionBoundary": provenance["permissionBoundary"],
        },
        "chromiumRuntimeCandidateVersion": CHROMIUM_RUNTIME_CANDIDATE_VERSION,
        "identity": {
            "candidateId": candidate["candidateId"],
            "cefVersion": candidate["cefVersion"],
            "chromiumVersion": candidate["chromiumVersion"],
            "locale": candidate["locale"],
            "target": candidate["target"],
        },
        "provenance": {
            "cefSourceBuildStatus": provenance["cefSourceBuildStatus"],
            "cefSourceReproducibilityStatus": provenance[
                "cefSourceReproducibilityStatus"
            ],
            "distribution": "arch-linux-signed-package",
            "packageSha256": artifacts["package"]["sha256"],
            "packageSignatureSha256": artifacts["packageSignature"]["sha256"],
            "packagingCommit": packaging["commit"],
            "packagingTag": packaging["tag"],
            "signerFingerprint": package["signature"]["fingerprint"],
            "sourceLockSha256": chromium_runtime_source_lock_sha256(lock),
        },
        "runtime": {
            "executable": candidate["executable"]["path"],
            "fileCount": candidate["fileCount"],
            "sandbox": dict(candidate["sandbox"]),
            "totalBytes": candidate["totalBytes"],
            "treeSha256": candidate["treeSha256"],
        },
    }
    return chromium_runtime_candidate_descriptor_from_dict(descriptor)


def chromium_runtime_candidate_descriptor_from_dict(value: Any) -> ChromiumRuntimeCandidateDescriptor:
    data = _mapping(value, "Chromium candidate descriptor")
    _exact_keys(
        data,
        frozenset(
            {
                "boundaries",
                "chromiumRuntimeCandidateVersion",
                "identity",
                "provenance",
                "runtime",
            }
        ),
        "Chromium candidate descriptor",
    )
    if data["chromiumRuntimeCandidateVersion"] != CHROMIUM_RUNTIME_CANDIDATE_VERSION:
        raise ChromiumRuntimeCandidateError("Chromium candidate descriptor version is invalid")
    boundaries = _mapping(data["boundaries"], "descriptor.boundaries")
    _exact_keys(
        boundaries,
        frozenset({"captureParity", "downloadBoundary", "permissionBoundary"}),
        "descriptor.boundaries",
    )
    if boundaries != {
        "captureParity": "blocked-upstream-cef-4053",
        "downloadBoundary": "system-save-picker",
        "permissionBoundary": "deny-all-reference",
    }:
        raise ChromiumRuntimeCandidateError("candidate boundaries are not fail-closed")
    identity = _mapping(data["identity"], "descriptor.identity")
    _exact_keys(
        identity,
        frozenset({"candidateId", "cefVersion", "chromiumVersion", "locale", "target"}),
        "descriptor.identity",
    )
    for key in identity:
        _text(identity[key], f"descriptor.identity.{key}")
    provenance = _mapping(data["provenance"], "descriptor.provenance")
    _exact_keys(
        provenance,
        frozenset(
            {
                "cefSourceBuildStatus",
                "cefSourceReproducibilityStatus",
                "distribution",
                "packageSha256",
                "packageSignatureSha256",
                "packagingCommit",
                "packagingTag",
                "signerFingerprint",
                "sourceLockSha256",
            }
        ),
        "descriptor.provenance",
    )
    if (
        provenance["cefSourceBuildStatus"] != "not-performed"
        or provenance["cefSourceReproducibilityStatus"] != "not-tested"
        or provenance["distribution"] != "arch-linux-signed-package"
    ):
        raise ChromiumRuntimeCandidateError("descriptor provenance claims are invalid")
    for field in ("packageSha256", "packageSignatureSha256", "sourceLockSha256"):
        _digest(provenance[field], f"descriptor.provenance.{field}")
    _digest(provenance["packagingCommit"], "descriptor.provenance.packagingCommit", _COMMIT_PATTERN)
    _digest(provenance["signerFingerprint"], "descriptor.provenance.signerFingerprint", _FINGERPRINT_PATTERN)
    _text(provenance["packagingTag"], "descriptor.provenance.packagingTag")
    runtime = _mapping(data["runtime"], "descriptor.runtime")
    _exact_keys(
        runtime,
        frozenset({"executable", "fileCount", "sandbox", "totalBytes", "treeSha256"}),
        "descriptor.runtime",
    )
    _safe_relative_path(runtime["executable"], "descriptor.runtime.executable", basename=True)
    _integer(runtime["fileCount"], "descriptor.runtime.fileCount")
    _integer(runtime["totalBytes"], "descriptor.runtime.totalBytes")
    _digest(runtime["treeSha256"], "descriptor.runtime.treeSha256")
    sandbox = _mapping(runtime["sandbox"], "descriptor.runtime.sandbox")
    _exact_keys(
        sandbox,
        frozenset({"gid", "installedPath", "mode", "sha256", "size", "uid"}),
        "descriptor.runtime.sandbox",
    )
    _absolute_path_text(sandbox["installedPath"], "descriptor.runtime.sandbox.installedPath")
    if sandbox["mode"] != "4755" or sandbox["uid"] != 0 or sandbox["gid"] != 0:
        raise ChromiumRuntimeCandidateError("descriptor sandbox identity is invalid")
    _integer(sandbox["size"], "descriptor.runtime.sandbox.size")
    _digest(sandbox["sha256"], "descriptor.runtime.sandbox.sha256")
    return ChromiumRuntimeCandidateDescriptor(_canonical_json_bytes(data))


def chromium_runtime_candidate_descriptor_to_dict(
    descriptor: ChromiumRuntimeCandidateDescriptor,
) -> dict[str, Any]:
    if not isinstance(descriptor, ChromiumRuntimeCandidateDescriptor):
        raise ChromiumRuntimeCandidateError("Chromium candidate descriptor was fabricated")
    data = _parse_json_bytes(descriptor.canonical, "Chromium candidate descriptor")
    parsed = chromium_runtime_candidate_descriptor_from_dict(data)
    if not hmac.compare_digest(parsed.canonical, descriptor.canonical):
        raise ChromiumRuntimeCandidateError("Chromium candidate descriptor is not canonical")
    return data


def render_chromium_runtime_candidate_descriptor(
    descriptor: ChromiumRuntimeCandidateDescriptor,
) -> str:
    chromium_runtime_candidate_descriptor_to_dict(descriptor)
    return descriptor.canonical.decode("ascii")


def chromium_runtime_candidate_descriptor_sha256(
    descriptor: ChromiumRuntimeCandidateDescriptor,
) -> str:
    return hashlib.sha256(descriptor.canonical).hexdigest()


def _hash_open_file(descriptor: int) -> tuple[str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


@contextmanager
def _open_pinned_artifact_file(
    path: str, label: str
) -> Iterator[tuple[int, os.stat_result]]:
    """Open a pinned artifact through non-writable, no-follow ancestors.

    Pacman's package cache is owned by the unprivileged system ``alpm`` user on
    current Arch systems.  That owner is not an artifact authority: the
    retained regular-file descriptor, root/effective-user ownership, exact
    size/hash, and detached signature are.  Consequently ancestor ownership
    may vary, but no ancestor may be group/world writable.
    """
    common_flags, directory_flags = _platform_flags()
    components = path[1:].split("/")
    descriptors: list[int] = []
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        root_info = os.fstat(current)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_IMODE(root_info.st_mode) & 0o022
        ):
            raise ChromiumRuntimeCandidateSafetyError(
                f"{label} root ancestor is not a non-writable directory"
            )
        traversed = ""
        for component in components[:-1]:
            traversed += "/" + component
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                detail = "is or traverses a symbolic link" if error.errno == errno.ELOOP else str(error)
                raise ChromiumRuntimeCandidateSafetyError(
                    f"cannot open {label} ancestor {traversed}: {detail}"
                ) from error
            descriptors.append(child)
            current = child
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} ancestor is not a non-writable directory: {traversed}"
                )
        try:
            descriptor = os.open(components[-1], common_flags, dir_fd=current)
        except OSError as error:
            detail = "is a symbolic link" if error.errno == errno.ELOOP else str(error)
            raise ChromiumRuntimeCandidateSafetyError(
                f"cannot open {label}: {detail}"
            ) from error
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ChromiumRuntimeCandidateSafetyError(f"{label} is not a regular file")
        yield descriptor, info
    finally:
        primary = sys.exc_info()[0] is not None
        close_errors: list[OSError] = []
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as error:
                close_errors.append(error)
        if close_errors and not primary:
            raise ChromiumRuntimeCandidateSafetyError(
                f"cannot close retained {label} descriptors: {close_errors[0]}"
            ) from close_errors[0]


@contextmanager
def _open_pinned_input(
    path: os.PathLike[str] | str, pin: Mapping[str, Any], label: str
) -> Iterator[_OpenedPinnedInput]:
    try:
        normalized = _absolute_normalized_path(path, label)
        with _open_pinned_artifact_file(normalized, label) as (descriptor, initial):
            if initial.st_nlink != 1:
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} must have exactly one hard link"
                )
            mode = stat.S_IMODE(initial.st_mode)
            if (
                initial.st_uid not in (0, os.geteuid())
                or mode & 0o400 != 0o400
                or mode & 0o133
            ):
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} must be root/effective-user owned, owner-readable, "
                    "non-executable, and non-writable by group/other"
                )
            digest, size = _hash_open_file(descriptor)
            final = os.fstat(descriptor)
            if _stat_identity(initial) != _stat_identity(final):
                raise ChromiumRuntimeCandidateSafetyError(f"{label} changed while hashing")
            if size != pin["size"] or not hmac.compare_digest(digest, str(pin["sha256"])):
                raise ChromiumRuntimeCandidateError(
                    f"{label} size/SHA-256 does not match the source lock"
                )
            yield _OpenedPinnedInput(
                label=label,
                logical_path=normalized,
                descriptor=descriptor,
                initial_identity=_stat_identity(initial),
            )
    except ChromiumRuntimeCandidateError:
        raise
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error


def _revalidate_opened_input(opened: _OpenedPinnedInput) -> None:
    current = os.fstat(opened.descriptor)
    if _stat_identity(current) != opened.initial_identity:
        raise ChromiumRuntimeCandidateSafetyError(
            f"{opened.label} changed during candidate assembly"
        )


def _gpg_status_lines(stdout: str) -> list[str]:
    prefix = "[GNUPG:] "
    return [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]


def _verify_package_signature(
    lock: ChromiumRuntimeSourceLock,
    package_fd: int,
    signature_fd: int,
    *,
    gpg_home: str = PACMAN_GPG_HOME,
) -> None:
    data = chromium_runtime_source_lock_to_dict(lock)
    tool = data["shell"]["toolchain"]["gpg"]
    if gpg_home != PACMAN_GPG_HOME:
        raise ChromiumRuntimeCandidateSafetyError("pacman GnuPG home is not the pinned system path")
    command = (
        tool["path"],
        "--homedir",
        gpg_home,
        "--no-options",
        "--batch",
        "--no-tty",
        "--status-fd=1",
        "--verify",
        f"/proc/self/fd/{signature_fd}",
        f"/proc/self/fd/{package_fd}",
    )
    environment = {
        "GNUPGHOME": gpg_home,
        "HOME": gpg_home,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        result = subprocess.run(
            command,
            env=environment,
            shell=False,
            close_fds=True,
            pass_fds=(package_fd, signature_fd),
            capture_output=True,
            text=False,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ChromiumRuntimeCandidateError(f"cannot verify Arch package signature: {error}") from error
    if (
        len(result.stdout) > MAX_TOOL_OUTPUT_BYTES
        or len(result.stderr) > MAX_TOOL_OUTPUT_BYTES
    ):
        raise ChromiumRuntimeCandidateError("GnuPG verification diagnostics exceeded limit")
    try:
        status_text = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChromiumRuntimeCandidateError("GnuPG status output is not UTF-8") from error
    status = _gpg_status_lines(status_text)
    forbidden = ("BADSIG ", "ERRSIG ", "NO_PUBKEY ", "EXPSIG ", "EXPKEYSIG ", "REVKEYSIG ")
    if result.returncode != 0 or any(line.startswith(forbidden) for line in status):
        detail = result.stderr.decode("utf-8", errors="replace").strip() or f"exit {result.returncode}"
        raise ChromiumRuntimeCandidateError(f"Arch package signature is invalid: {detail}")
    valid = [line.split() for line in status if line.startswith("VALIDSIG ")]
    good = [line.split(maxsplit=2) for line in status if line.startswith("GOODSIG ")]
    trusts = [line.split() for line in status if line.startswith("TRUST_FULLY ")]
    pin = data["package"]["signature"]
    if len(valid) != 1 or len(valid[0]) < 11 or len(good) != 1 or len(good[0]) != 3 or len(trusts) != 1:
        raise ChromiumRuntimeCandidateError("GnuPG did not emit complete GOODSIG/VALIDSIG/TRUST_FULLY")
    fields = valid[0]
    if (
        fields[1] != pin["fingerprint"]
        or fields[2] != pin["signatureDate"]
        or fields[3] != str(pin["signatureTimestamp"])
        or fields[5] != "4"
        or fields[7] != str(pin["publicKeyAlgorithmCode"])
        or fields[8] != str(pin["hashAlgorithmCode"])
        or fields[9] != "00"
        or fields[10] != pin["fingerprint"]
        or good[0][1] != pin["fingerprint"][-16:]
        or good[0][2] != pin["signerUid"]
    ):
        raise ChromiumRuntimeCandidateError("GnuPG package signature identity does not match lock")


def _validated_tar_name(name: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name.startswith("/")
        or "\\" in name
        or len(name.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise ChromiumRuntimeCandidateSafetyError("package contains an unsafe tar path")
    components = name.split("/")
    if any(component in ("", ".", "..") for component in components) or len(components) > MAX_PATH_DEPTH:
        raise ChromiumRuntimeCandidateSafetyError(f"package contains an unsafe tar path: {name}")
    return name


@contextmanager
def _open_package_tar(
    package_fd: int, zstd_path: str
) -> Iterator[tarfile.TarFile]:
    os.lseek(package_fd, 0, os.SEEK_SET)
    environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    try:
        process = subprocess.Popen(
            (zstd_path, "-q", "-d", "-c"),
            env=environment,
            shell=False,
            close_fds=True,
            stdin=package_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ChromiumRuntimeCandidateError(f"cannot start zstd package stream: {error}") from error
    archive: tarfile.TarFile | None = None
    primary = False
    try:
        if process.stdout is None:
            raise ChromiumRuntimeCandidateError("zstd package stream has no stdout")
        archive = tarfile.open(fileobj=process.stdout, mode="r|")
        yield archive
    except tarfile.TarError as error:
        primary = True
        raise ChromiumRuntimeCandidateError(
            f"decompressed CEF package is not a valid tar stream: {error}"
        ) from error
    except BaseException:
        primary = True
        raise
    finally:
        if archive is not None:
            archive.close()
        if process.stdout is not None:
            process.stdout.close()
        try:
            stderr = process.stderr.read(MAX_TOOL_OUTPUT_BYTES + 1) if process.stderr else b""
            if len(stderr) > MAX_TOOL_OUTPUT_BYTES:
                process.kill()
                raise ChromiumRuntimeCandidateError("zstd diagnostics exceeded limit")
            return_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            if not primary:
                raise ChromiumRuntimeCandidateError("zstd package stream did not terminate") from error
        else:
            if return_code != 0 and not primary:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise ChromiumRuntimeCandidateError(
                    f"zstd package stream failed: {detail or return_code}"
                )


def _read_tar_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, *, limit: int | None = None
) -> bytes:
    source = archive.extractfile(member)
    if source is None:
        raise ChromiumRuntimeCandidateError(f"cannot read package member {member.name}")
    maximum = member.size if limit is None else min(member.size, limit)
    payload = source.read(maximum + 1)
    if len(payload) != member.size or (limit is not None and member.size > limit):
        raise ChromiumRuntimeCandidateError(f"package member size is invalid: {member.name}")
    return payload


def _metadata_assignments(payload: bytes, label: str) -> dict[str, list[str]]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ChromiumRuntimeCandidateError(f"{label} is not UTF-8") from error
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" = ")
        if not separator or not key or not value:
            raise ChromiumRuntimeCandidateError(f"{label} contains a malformed assignment")
        result.setdefault(key, []).append(value)
    return result


def _scan_package(lock: ChromiumRuntimeSourceLock, package_fd: int) -> _PackageScan:
    data = chromium_runtime_source_lock_to_dict(lock)
    package = data["package"]
    candidate = data["candidate"]
    toolchain = data["shell"]["toolchain"]
    runtime_pins = {item["archivePath"]: item for item in candidate["runtimeFiles"]}
    sandbox_path = candidate["sandbox"]["installedPath"].removeprefix("/")
    metadata_pins = {
        package[field]["path"]: package[field]
        for field in ("buildInfo", "mtree", "packageInfo")
    }
    required = set(runtime_pins) | {sandbox_path} | set(metadata_pins)
    observed_required: set[str] = set()
    metadata_payloads: dict[str, bytes] = {}
    sdk_entries: list[ChromiumRuntimeTreeEntry] = []
    names: set[str] = set()
    directories = 0
    regular_files = 0
    member_count = 0
    with _open_package_tar(package_fd, toolchain["zstd"]["path"]) as archive:
        for member in archive:
            member_count += 1
            if member_count > MAX_PACKAGE_MEMBERS:
                raise ChromiumRuntimeCandidateSafetyError("package has too many members")
            name = _validated_tar_name(member.name)
            if name in names:
                raise ChromiumRuntimeCandidateSafetyError(f"package repeats member: {name}")
            names.add(name)
            if member.uid != 0 or member.gid != 0:
                raise ChromiumRuntimeCandidateSafetyError(f"package member is not root-owned: {name}")
            if member.isdir():
                directories += 1
                continue
            if not member.isfile():
                raise ChromiumRuntimeCandidateSafetyError(
                    f"package contains a link or special member: {name}"
                )
            regular_files += 1
            selected_sdk = name.startswith(_SDK_PREFIXES) or name in _SDK_EXACT_PATHS
            if name not in required and not selected_sdk:
                continue
            payload = _read_tar_member(
                archive,
                member,
                limit=MAX_PACKAGE_METADATA_BYTES if name in metadata_pins else None,
            )
            digest = hashlib.sha256(payload).hexdigest()
            mode = f"{member.mode & 0o7777:04o}"
            if name in runtime_pins:
                pin = runtime_pins[name]
                if (member.size, mode, digest) != (pin["size"], pin["mode"], pin["sha256"]):
                    raise ChromiumRuntimeCandidateError(f"runtime package member mismatch: {name}")
                observed_required.add(name)
            elif name == sandbox_path:
                pin = candidate["sandbox"]
                if (member.size, mode, digest) != (pin["size"], pin["mode"], pin["sha256"]):
                    raise ChromiumRuntimeCandidateError("package sandbox member mismatch")
                observed_required.add(name)
            elif name in metadata_pins:
                pin = metadata_pins[name]
                if (member.size, mode, digest) != (pin["size"], pin["mode"], pin["sha256"]):
                    raise ChromiumRuntimeCandidateError(f"package metadata mismatch: {name}")
                metadata_payloads[name] = payload
                observed_required.add(name)
            if selected_sdk:
                if mode not in ("0644", "0755"):
                    raise ChromiumRuntimeCandidateSafetyError(f"SDK member mode is unsafe: {name}")
                sdk_entries.append(
                    ChromiumRuntimeTreeEntry(path=name, size=member.size, mode=mode, sha256=digest)
                )
    if (
        member_count != package["archiveEntries"]
        or directories != package["archiveDirectories"]
        or regular_files != package["archiveRegularFiles"]
    ):
        raise ChromiumRuntimeCandidateError("package archive counters do not match lock")
    if observed_required != required:
        raise ChromiumRuntimeCandidateError("package required-member authority is incomplete")
    ordered_sdk = tuple(sorted(sdk_entries, key=lambda item: item.path))
    if (
        len(ordered_sdk) != toolchain["sdkFileCount"]
        or sum(item.size for item in ordered_sdk) != toolchain["sdkTotalBytes"]
        or _versioned_tree_sha256(ordered_sdk, "chromiumSdkTreeVersion", CHROMIUM_SDK_TREE_VERSION)
        != toolchain["sdkTreeSha256"]
    ):
        raise ChromiumRuntimeCandidateError("package SDK tree does not match lock")
    pkginfo = _metadata_assignments(metadata_payloads[".PKGINFO"], ".PKGINFO")
    expected_pkginfo = {
        "pkgname": [package["name"]],
        "pkgver": [package["version"]],
        "arch": [package["arch"]],
        "builddate": [str(package["buildTimestamp"])],
        "packager": [package["packager"]],
    }
    for key, expected in expected_pkginfo.items():
        if pkginfo.get(key) != expected:
            raise ChromiumRuntimeCandidateError(f".PKGINFO {key} does not match lock")
    buildinfo = _metadata_assignments(metadata_payloads[".BUILDINFO"], ".BUILDINFO")
    if buildinfo.get("pkgbuild_sha256sum") != [data["packaging"]["pkgbuildSha256"]]:
        raise ChromiumRuntimeCandidateError(".BUILDINFO does not bind the locked PKGBUILD")
    version_header = next(
        (item for item in ordered_sdk if item.path == "usr/include/cef/include/cef_version.h"),
        None,
    )
    if version_header is None:
        raise ChromiumRuntimeCandidateError("CEF SDK has no version header")
    return _PackageScan(sdk_files=ordered_sdk)


def _open_absolute_directory(
    path: os.PathLike[str] | str, label: str
) -> tuple[str, int, tuple[int, ...]]:
    try:
        rendered = os.fspath(path)
        normalized = "/" if rendered == "/" else _absolute_normalized_path(path, label)
        _common, directory_flags = _platform_flags()
    except (TypeError, RuntimeSafetyError) as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error
    components = [] if normalized == "/" else normalized[1:].split("/")
    descriptors: list[int] = []
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        _safe_owner(os.fstat(current), f"{label} ancestor /")
        traversed = ""
        for component in components:
            traversed += f"/{component}"
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise ChromiumRuntimeCandidateSafetyError(
                        f"{label} is or traverses a symbolic link"
                    ) from error
                raise ChromiumRuntimeCandidateSafetyError(
                    f"cannot open {label} ancestor {traversed}: {error}"
                ) from error
            descriptors.append(child)
            current = child
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode):
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} ancestor is not a directory: {traversed}"
                )
            _safe_owner(info, f"{label} ancestor {traversed}")
        descriptor = descriptors.pop()
        return normalized, descriptor, _stat_identity(os.fstat(descriptor))
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error
    finally:
        for item in reversed(descriptors):
            os.close(item)


def _open_relative_directory(root_fd: int, components: Sequence[str]) -> int:
    _common, directory_flags = _platform_flags()
    current = os.dup(root_fd)
    try:
        for component in components:
            child = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _ensure_relative_directory(
    root_fd: int, components: Sequence[str], created: set[str]
) -> int:
    _common, directory_flags = _platform_flags()
    current = os.dup(root_fd)
    relative: list[str] = []
    try:
        for component in components:
            relative.append(component)
            rendered = "/".join(relative)
            if rendered not in created:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError as error:
                    raise ChromiumRuntimeCandidateSafetyError(
                        f"extraction path collides while creating {rendered}"
                    ) from error
                created.add(rendered)
            child = os.open(component, directory_flags, dir_fd=current)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                os.close(child)
                raise ChromiumRuntimeCandidateSafetyError(
                    f"created directory has unsafe identity: {rendered}"
                )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _write_member_file(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    root_fd: int,
    destination: str,
    mode: int,
) -> ChromiumRuntimeTreeEntry:
    components = destination.split("/")
    created: set[str] = getattr(_write_member_file, "_created", set())
    setattr(_write_member_file, "_created", created)
    parent_fd = _ensure_relative_directory(root_fd, components[:-1], created)
    common_flags, _directory_flags = _platform_flags()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | common_flags
    descriptor = -1
    try:
        descriptor = os.open(components[-1], flags, 0o600, dir_fd=parent_fd)
        source = archive.extractfile(member)
        if source is None:
            raise ChromiumRuntimeCandidateError(f"cannot extract package member {member.name}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > member.size:
                raise ChromiumRuntimeCandidateError(f"package member grew: {member.name}")
            digest.update(chunk)
            write_all(descriptor, chunk)
        if total != member.size:
            raise ChromiumRuntimeCandidateError(f"package member is incomplete: {member.name}")
        os.fchmod(descriptor, mode)
        os.utime(descriptor, ns=(0, 0))
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_size != total
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_mtime_ns != 0
        ):
            raise ChromiumRuntimeCandidateSafetyError(
                f"extracted member normalization failed: {destination}"
            )
        return ChromiumRuntimeTreeEntry(
            path=destination,
            size=total,
            mode=f"{mode:04o}",
            sha256=digest.hexdigest(),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _reset_member_writer() -> None:
    setattr(_write_member_file, "_created", set())


def _extract_package_subset(
    lock: ChromiumRuntimeSourceLock,
    scan: _PackageScan,
    package_fd: int,
    staging_fd: int,
) -> tuple[int, int]:
    data = chromium_runtime_source_lock_to_dict(lock)
    candidate = data["candidate"]
    zstd_path = data["shell"]["toolchain"]["zstd"]["path"]
    runtime_map = {item["archivePath"]: item for item in candidate["runtimeFiles"]}
    sdk_map = {item.path: item for item in scan.sdk_files}
    os.mkdir("runtime", 0o700, dir_fd=staging_fd)
    os.mkdir("sdk", 0o700, dir_fd=staging_fd)
    _common, directory_flags = _platform_flags()
    runtime_fd = os.open("runtime", directory_flags, dir_fd=staging_fd)
    sdk_fd = os.open("sdk", directory_flags, dir_fd=staging_fd)
    observed_runtime: set[str] = set()
    observed_sdk: set[str] = set()
    _reset_member_writer()
    try:
        with _open_package_tar(package_fd, zstd_path) as archive:
            for member in archive:
                name = member.name
                if name in runtime_map:
                    pin = runtime_map[name]
                    entry = _write_member_file(
                        archive,
                        member,
                        runtime_fd,
                        pin["path"],
                        int(pin["mode"], 8),
                    )
                    if (entry.size, entry.mode, entry.sha256) != (
                        pin["size"],
                        pin["mode"],
                        pin["sha256"],
                    ):
                        raise ChromiumRuntimeCandidateError(
                            f"extracted runtime member changed: {name}"
                        )
                    observed_runtime.add(name)
                elif name in sdk_map:
                    pin = sdk_map[name]
                    entry = _write_member_file(
                        archive,
                        member,
                        sdk_fd,
                        name,
                        int(pin.mode, 8),
                    )
                    if entry != pin:
                        raise ChromiumRuntimeCandidateError(
                            f"extracted SDK member changed: {name}"
                        )
                    observed_sdk.add(name)
        if observed_runtime != set(runtime_map) or observed_sdk != set(sdk_map):
            raise ChromiumRuntimeCandidateError("package subset extraction is incomplete")
        return runtime_fd, sdk_fd
    except BaseException:
        os.close(runtime_fd)
        os.close(sdk_fd)
        raise


def _verify_closed_source_tree(
    root_path: os.PathLike[str] | str,
    pins: Sequence[Mapping[str, Any]],
    label: str,
    *,
    allow_git: bool,
) -> dict[str, bytes]:
    normalized, root_fd, root_identity = _open_absolute_directory(root_path, label)
    del normalized
    expected_files = {str(pin["path"]): pin for pin in pins}
    implied_directories: set[str] = set()
    for path in expected_files:
        components = path.split("/")[:-1]
        for index in range(1, len(components) + 1):
            implied_directories.add("/".join(components[:index]))
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    payloads: dict[str, bytes] = {}
    _common, directory_flags = _platform_flags()

    def visit(directory_fd: int, relative: str) -> None:
        names = sorted(os.listdir(directory_fd))
        for name in names:
            path = f"{relative}/{name}".strip("/")
            if not relative and name == ".git" and allow_git:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ChromiumRuntimeCandidateSafetyError(f"{label} .git is not a directory")
                continue
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if path not in implied_directories:
                    raise ChromiumRuntimeCandidateSafetyError(
                        f"{label} contains unknown directory {path}"
                    )
                child = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    observed_directories.add(path)
                    visit(child, path)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                pin = expected_files.get(path)
                if pin is None or pin.get("type") == "symlink":
                    raise ChromiumRuntimeCandidateSafetyError(
                        f"{label} contains unknown regular file {path}"
                    )
                descriptor = os.open(name, _common, dir_fd=directory_fd)
                try:
                    initial = os.fstat(descriptor)
                    digest, size = _hash_open_file(descriptor)
                    final = os.fstat(descriptor)
                    if (
                        _stat_identity(initial) != _stat_identity(final)
                        or initial.st_nlink != 1
                        or stat.S_IMODE(initial.st_mode) != int(pin["mode"], 8)
                        or size != pin["size"]
                        or not hmac.compare_digest(digest, str(pin["sha256"]))
                    ):
                        raise ChromiumRuntimeCandidateError(
                            f"{label} file does not match lock: {path}"
                        )
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    payloads[path] = os.read(descriptor, size + 1)
                finally:
                    os.close(descriptor)
                observed_files.add(path)
            elif stat.S_ISLNK(info.st_mode):
                pin = expected_files.get(path)
                target = os.readlink(name, dir_fd=directory_fd)
                if (
                    pin is None
                    or pin.get("type") != "symlink"
                    or target != pin["target"]
                ):
                    raise ChromiumRuntimeCandidateSafetyError(
                        f"{label} symlink does not match lock: {path}"
                    )
                observed_files.add(path)
            else:
                raise ChromiumRuntimeCandidateSafetyError(
                    f"{label} contains a link or special entry: {path}"
                )

    try:
        visit(root_fd, "")
        if observed_files != set(expected_files) or observed_directories != implied_directories:
            raise ChromiumRuntimeCandidateError(f"{label} closed inventory is incomplete")
        if _stat_identity(os.fstat(root_fd)) != root_identity:
            raise ChromiumRuntimeCandidateSafetyError(f"{label} changed while verifying")
        return payloads
    finally:
        os.close(root_fd)


def _verify_tool(pin: Mapping[str, Any], label: str) -> None:
    try:
        with _open_absolute_file(pin["path"], label) as (descriptor, initial):
            if (
                initial.st_uid != 0
                or not stat.S_ISREG(initial.st_mode)
                or initial.st_nlink != 1
                or stat.S_IMODE(initial.st_mode) & 0o022
                or not stat.S_IMODE(initial.st_mode) & 0o111
            ):
                raise ChromiumRuntimeCandidateSafetyError(f"{label} metadata is unsafe")
            digest, size = _hash_open_file(descriptor)
            if (size, digest) != (pin["size"], pin["sha256"]):
                raise ChromiumRuntimeCandidateError(f"{label} does not match toolchain lock")
            if _stat_identity(initial) != _stat_identity(os.fstat(descriptor)):
                raise ChromiumRuntimeCandidateSafetyError(f"{label} changed while hashing")
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error


def _tool_version(pin: Mapping[str, Any], name: str) -> None:
    command = (pin["path"], "--version")
    try:
        result = subprocess.run(
            command,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            shell=False,
            close_fds=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ChromiumRuntimeCandidateError(f"cannot query {name} version: {error}") from error
    combined = (result.stdout + result.stderr).splitlines()
    if result.returncode != 0 or not combined or combined[0].strip() != pin["version"]:
        raise ChromiumRuntimeCandidateError(f"{name} version does not match toolchain lock")


def _write_bytes_file(root_fd: int, relative: str, payload: bytes, mode: int) -> None:
    components = relative.split("/")
    created: set[str] = getattr(_write_bytes_file, "_created", set())
    setattr(_write_bytes_file, "_created", created)
    parent_fd = _ensure_relative_directory(root_fd, components[:-1], created)
    common, _directory = _platform_flags()
    descriptor = -1
    try:
        descriptor = os.open(
            components[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | common, 0o600, dir_fd=parent_fd
        )
        write_all(descriptor, payload)
        os.fchmod(descriptor, mode)
        os.utime(descriptor, ns=(0, 0))
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _run_build_command(command: Sequence[str], environment: Mapping[str, str], label: str) -> None:
    try:
        result = subprocess.run(
            tuple(command),
            env=dict(environment),
            shell=False,
            close_fds=True,
            capture_output=True,
            check=False,
            timeout=MAX_BUILD_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ChromiumRuntimeCandidateError(f"cannot execute {label}: {error}") from error
    output = result.stdout + result.stderr
    if len(output) > MAX_TOOL_OUTPUT_BYTES:
        raise ChromiumRuntimeCandidateError(f"{label} diagnostics exceeded limit")
    if result.returncode != 0:
        detail = output.decode("utf-8", errors="replace")[-8000:]
        raise ChromiumRuntimeCandidateError(f"{label} failed with {result.returncode}: {detail}")


def _build_shell(
    lock: ChromiumRuntimeSourceLock,
    staging_path: str,
    staging_fd: int,
    runtime_fd: int,
    source_payloads: Mapping[str, bytes],
) -> ChromiumRuntimeTreeEntry:
    data = chromium_runtime_source_lock_to_dict(lock)
    shell = data["shell"]
    toolchain = shell["toolchain"]
    os.mkdir("source", 0o700, dir_fd=staging_fd)
    os.mkdir("build", 0o700, dir_fd=staging_fd)
    os.mkdir("home", 0o700, dir_fd=staging_fd)
    os.mkdir("tmp", 0o700, dir_fd=staging_fd)
    _common, directory_flags = _platform_flags()
    source_fd = os.open("source", directory_flags, dir_fd=staging_fd)
    try:
        setattr(_write_bytes_file, "_created", set())
        for pin in shell["sourceFiles"]:
            _write_bytes_file(source_fd, pin["path"], source_payloads[pin["path"]], int(pin["mode"], 8))
    finally:
        os.close(source_fd)
    source_path = f"{staging_path}/source"
    build_path = f"{staging_path}/build"
    sdk_root = f"{staging_path}/sdk/usr"
    runtime_path = f"{staging_path}/runtime"
    canonical_flags = " ".join(
        (
            "-Wno-undefined-var-template",
            f"-ffile-prefix-map={sdk_root}=/usr",
            f"-fdebug-prefix-map={sdk_root}=/usr",
            f"-fmacro-prefix-map={sdk_root}=/usr",
            f"-ffile-prefix-map={source_path}=/usr/src/zen-cef-shell",
            f"-fdebug-prefix-map={source_path}=/usr/src/zen-cef-shell",
            f"-fmacro-prefix-map={source_path}=/usr/src/zen-cef-shell",
            f"-ffile-prefix-map={build_path}=/usr/src/zen-cef-build",
            f"-fdebug-prefix-map={build_path}=/usr/src/zen-cef-build",
            f"-fmacro-prefix-map={build_path}=/usr/src/zen-cef-build",
        )
    )
    environment = {
        "HOME": f"{staging_path}/home",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "SOURCE_DATE_EPOCH": "0",
        "TMPDIR": f"{staging_path}/tmp",
        "TZ": "UTC",
        "ZERO_AR_DATE": "1",
    }
    configure = (
        toolchain["cmake"]["path"],
        "-S",
        source_path,
        "-B",
        build_path,
        "-G",
        "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_CXX_COMPILER={toolchain['clang']['path']}",
        f"-DCMAKE_MAKE_PROGRAM={toolchain['ninja']['path']}",
        f"-DCMAKE_MODULE_PATH={sdk_root}/share/cmake/Modules",
        f"-DCEF_INCLUDE_DIR={sdk_root}/include/cef",
        f"-DCEF_LIBRARY={runtime_path}/libcef.so",
        f"-DCEF_WRAPPER_SRC_DIR={sdk_root}/src/cef",
        f"-DCMAKE_CXX_FLAGS={canonical_flags}",
        "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
        "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF",
    )
    _run_build_command(configure, environment, "deterministic CMake configure")
    build = (
        toolchain["ninja"]["path"],
        "-C",
        build_path,
        "-j1",
        "zen-cef-shell",
    )
    _run_build_command(build, environment, "single-job Ninja build")
    binary_path = f"{build_path}/zen-cef-shell"
    try:
        with _open_absolute_file(binary_path, "built CEF shell") as (source_fd, initial):
            if initial.st_nlink != 1 or not stat.S_ISREG(initial.st_mode):
                raise ChromiumRuntimeCandidateSafetyError("built CEF shell metadata is unsafe")
            pin = data["candidate"]["executable"]
            digest, size = _hash_open_file(source_fd)
            if (size, digest) != (pin["size"], pin["sha256"]):
                raise ChromiumRuntimeCandidateError(
                    f"built CEF shell does not match lock: {size} bytes {digest}"
                )
            os.lseek(source_fd, 0, os.SEEK_SET)
            common, _directory = _platform_flags()
            destination_fd = os.open(
                pin["path"],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | common,
                0o600,
                dir_fd=runtime_fd,
            )
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    write_all(destination_fd, chunk)
                os.fchmod(destination_fd, 0o755)
                os.utime(destination_fd, ns=(0, 0))
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
            if _stat_identity(initial) != _stat_identity(os.fstat(source_fd)):
                raise ChromiumRuntimeCandidateSafetyError("built CEF shell changed while copying")
            return ChromiumRuntimeTreeEntry(
                path=pin["path"], size=size, mode=pin["mode"], sha256=digest
            )
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error


def _normalize_tree(directory_fd: int) -> None:
    _common, directory_flags = _platform_flags()
    for name in sorted(os.listdir(directory_fd)):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, directory_flags, dir_fd=directory_fd)
            try:
                _normalize_tree(child)
                os.fchmod(child, 0o755)
                os.utime(child, ns=(0, 0))
                os.fsync(child)
            finally:
                os.close(child)
    os.fchmod(directory_fd, 0o755)
    os.utime(directory_fd, ns=(0, 0))
    os.fsync(directory_fd)


def _remove_directory_contents(directory_fd: int) -> None:
    _common, directory_flags = _platform_flags()
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, directory_flags, dir_fd=directory_fd)
            try:
                _remove_directory_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _write_candidate_descriptor(
    root_fd: int, descriptor: ChromiumRuntimeCandidateDescriptor
) -> None:
    common, _directory = _platform_flags()
    file_fd = os.open(
        CHROMIUM_RUNTIME_CANDIDATE_DESCRIPTOR,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | common,
        0o600,
        dir_fd=root_fd,
    )
    try:
        write_all(file_fd, descriptor.canonical)
        os.fchmod(file_fd, 0o644)
        os.utime(file_fd, ns=(0, 0))
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _read_candidate_descriptor(root_fd: int) -> ChromiumRuntimeCandidateDescriptor:
    common, _directory = _platform_flags()
    descriptor = os.open(CHROMIUM_RUNTIME_CANDIDATE_DESCRIPTOR, common, dir_fd=root_fd)
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != 0o644
            or initial.st_mtime_ns != 0
            or initial.st_size > MAX_DESCRIPTOR_BYTES
        ):
            raise ChromiumRuntimeCandidateSafetyError("candidate descriptor metadata is unsafe")
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = os.read(descriptor, MAX_DESCRIPTOR_BYTES + 1)
        if len(payload) != initial.st_size:
            raise ChromiumRuntimeCandidateSafetyError("candidate descriptor read is incomplete")
        parsed = chromium_runtime_candidate_descriptor_from_dict(
            _parse_json_bytes(payload, "Chromium candidate descriptor")
        )
        if not hmac.compare_digest(payload, parsed.canonical):
            raise ChromiumRuntimeCandidateError("candidate descriptor is not canonical JSON")
        if _stat_identity(initial) != _stat_identity(os.fstat(descriptor)):
            raise ChromiumRuntimeCandidateSafetyError("candidate descriptor changed while reading")
        return parsed
    finally:
        os.close(descriptor)


def _hash_runtime_file(
    directory_fd: int, name: str, relative: str, maximum_bytes: int
) -> ChromiumRuntimeTreeEntry:
    common, _directory = _platform_flags()
    descriptor = os.open(name, common, dir_fd=directory_fd)
    try:
        initial = os.fstat(descriptor)
        mode = stat.S_IMODE(initial.st_mode)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or mode not in (0o644, 0o755)
            or initial.st_mtime_ns != 0
            or initial.st_size > maximum_bytes
        ):
            raise ChromiumRuntimeCandidateSafetyError(f"runtime file metadata is unsafe: {relative}")
        digest, size = _hash_open_file(descriptor)
        if size != initial.st_size or _stat_identity(initial) != _stat_identity(os.fstat(descriptor)):
            raise ChromiumRuntimeCandidateSafetyError(f"runtime file changed while hashing: {relative}")
        return ChromiumRuntimeTreeEntry(
            path=relative, size=size, mode=f"{mode:04o}", sha256=digest
        )
    finally:
        os.close(descriptor)


def _verify_runtime_tree(
    runtime_fd: int, lock: ChromiumRuntimeSourceLock
) -> tuple[ChromiumRuntimeTreeEntry, ...]:
    data = chromium_runtime_source_lock_to_dict(lock)
    candidate = data["candidate"]
    entries: list[ChromiumRuntimeTreeEntry] = []
    observed_directories: set[str] = set()
    _common, directory_flags = _platform_flags()

    def visit(directory_fd: int, relative_directory: str) -> None:
        initial = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(initial.st_mode)
            or stat.S_IMODE(initial.st_mode) != 0o755
            or initial.st_mtime_ns != 0
        ):
            raise ChromiumRuntimeCandidateSafetyError("runtime directory is not normalized")
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{relative_directory}/{name}".strip("/")
            _safe_relative_path(relative, "runtime entry")
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    observed_directories.add(relative)
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                entries.append(
                    _hash_runtime_file(
                        directory_fd, name, relative, candidate["totalBytes"]
                    )
                )
            else:
                raise ChromiumRuntimeCandidateSafetyError(
                    f"runtime tree contains a link or special entry: {relative}"
                )
        if _stat_identity(initial) != _stat_identity(os.fstat(directory_fd)):
            raise ChromiumRuntimeCandidateSafetyError("runtime directory changed while verifying")

    visit(runtime_fd, "")
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    implied: set[str] = set()
    for entry in ordered:
        components = entry.path.split("/")[:-1]
        for index in range(1, len(components) + 1):
            implied.add("/".join(components[:index]))
    if observed_directories != implied:
        raise ChromiumRuntimeCandidateSafetyError("runtime tree contains empty/unknown directories")
    expected = _tree_entries_from_pins(
        [candidate["executable"], *candidate["runtimeFiles"]]
    )
    if ordered != expected:
        raise ChromiumRuntimeCandidateError("runtime files do not match source lock")
    if chromium_runtime_tree_sha256(ordered) != candidate["treeSha256"]:
        raise ChromiumRuntimeCandidateError("runtime tree digest does not match source lock")
    return ordered


def _open_verified_sandbox(
    lock: ChromiumRuntimeSourceLock,
) -> tuple[int, tuple[int, ...]]:
    data = chromium_runtime_source_lock_to_dict(lock)
    pin = data["candidate"]["sandbox"]
    path = pin["installedPath"]
    try:
        context = _open_absolute_file(path, "root-owned CEF sandbox")
        descriptor, initial = context.__enter__()
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error
    try:
        mode = stat.S_IMODE(initial.st_mode)
        digest, size = _hash_open_file(descriptor)
        if (
            initial.st_uid != pin["uid"]
            or initial.st_gid != pin["gid"]
            or initial.st_nlink != 1
            or mode != int(pin["mode"], 8)
            or size != pin["size"]
            or not hmac.compare_digest(digest, pin["sha256"])
            or _stat_identity(initial) != _stat_identity(os.fstat(descriptor))
        ):
            raise ChromiumRuntimeCandidateSafetyError(
                "installed CEF sandbox does not match root-owned authority"
            )
        duplicated = os.dup(descriptor)
        identity = _stat_identity(initial)
    finally:
        context.__exit__(None, None, None)
    return duplicated, identity


@contextmanager
def open_verified_chromium_runtime_candidate(
    candidate_path: os.PathLike[str] | str,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> Iterator[OpenVerifiedChromiumRuntimeCandidate]:
    lock = (
        load_bundled_chromium_runtime_source_lock()
        if source_lock_path is None
        else load_chromium_runtime_source_lock(source_lock_path)
    )
    normalized, root_fd, root_identity = _open_absolute_directory(
        candidate_path, "Chromium runtime candidate"
    )
    runtime_fd = -1
    sandbox_fd = -1
    try:
        root_info = os.fstat(root_fd)
        if (
            stat.S_IMODE(root_info.st_mode) != 0o755
            or root_info.st_mtime_ns != 0
        ):
            raise ChromiumRuntimeCandidateSafetyError("candidate root is not normalized")
        if frozenset(os.listdir(root_fd)) != frozenset(
            {CHROMIUM_RUNTIME_CANDIDATE_DESCRIPTOR, "runtime"}
        ):
            raise ChromiumRuntimeCandidateSafetyError("candidate root entries are not exact")
        descriptor = _read_candidate_descriptor(root_fd)
        if descriptor != _expected_candidate_descriptor(lock):
            raise ChromiumRuntimeCandidateError("candidate descriptor does not match source lock")
        _common, directory_flags = _platform_flags()
        runtime_fd = os.open("runtime", directory_flags, dir_fd=root_fd)
        runtime_info = os.fstat(runtime_fd)
        files = _verify_runtime_tree(runtime_fd, lock)
        sandbox_fd, sandbox_identity = _open_verified_sandbox(lock)
        if _stat_identity(root_info) != _stat_identity(os.fstat(root_fd)):
            raise ChromiumRuntimeCandidateSafetyError("candidate root changed while verifying")
        yield OpenVerifiedChromiumRuntimeCandidate(
            logical_path=normalized,
            root_descriptor=root_fd,
            runtime_descriptor=runtime_fd,
            sandbox_descriptor=sandbox_fd,
            root_identity=root_identity,
            runtime_identity=_stat_identity(runtime_info),
            sandbox_identity=sandbox_identity,
            descriptor=descriptor,
            files=files,
        )
    finally:
        if sandbox_fd >= 0:
            os.close(sandbox_fd)
        if runtime_fd >= 0:
            os.close(runtime_fd)
        os.close(root_fd)


def revalidate_open_chromium_runtime_candidate(
    opened: OpenVerifiedChromiumRuntimeCandidate,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> None:
    if not isinstance(opened, OpenVerifiedChromiumRuntimeCandidate):
        raise ChromiumRuntimeCandidateError("open Chromium candidate authority was fabricated")
    lock = (
        load_bundled_chromium_runtime_source_lock()
        if source_lock_path is None
        else load_chromium_runtime_source_lock(source_lock_path)
    )
    root_info = os.fstat(opened.root_descriptor)
    runtime_info = os.fstat(opened.runtime_descriptor)
    sandbox_info = os.fstat(opened.sandbox_descriptor)
    if _stat_identity(root_info) != opened.root_identity:
        raise ChromiumRuntimeCandidateSafetyError("retained candidate root changed")
    if _stat_identity(runtime_info) != opened.runtime_identity:
        raise ChromiumRuntimeCandidateSafetyError("retained runtime root changed")
    if _stat_identity(sandbox_info) != opened.sandbox_identity:
        raise ChromiumRuntimeCandidateSafetyError("retained sandbox changed")
    logical_fd = -1
    sandbox_fd = -1
    try:
        _normalized, logical_fd, logical_identity = _open_absolute_directory(
            opened.logical_path, "candidate logical path"
        )
        if logical_identity != opened.root_identity:
            raise ChromiumRuntimeCandidateSafetyError("candidate logical path was rebound")
        sandbox_fd, sandbox_identity = _open_verified_sandbox(lock)
        if sandbox_identity != opened.sandbox_identity:
            raise ChromiumRuntimeCandidateSafetyError("sandbox logical path was rebound")
    finally:
        if sandbox_fd >= 0:
            os.close(sandbox_fd)
        if logical_fd >= 0:
            os.close(logical_fd)
    entry = os.stat("runtime", dir_fd=opened.root_descriptor, follow_symlinks=False)
    if (entry.st_dev, entry.st_ino) != (runtime_info.st_dev, runtime_info.st_ino):
        raise ChromiumRuntimeCandidateSafetyError("candidate runtime entry was rebound")
    descriptor = _read_candidate_descriptor(opened.root_descriptor)
    if descriptor != opened.descriptor or descriptor != _expected_candidate_descriptor(lock):
        raise ChromiumRuntimeCandidateError("candidate descriptor changed")
    if _verify_runtime_tree(opened.runtime_descriptor, lock) != opened.files:
        raise ChromiumRuntimeCandidateError("candidate runtime tree changed")


def chromium_runtime_candidate_executable_path(
    opened: OpenVerifiedChromiumRuntimeCandidate,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> str:
    revalidate_open_chromium_runtime_candidate(opened, source_lock_path=source_lock_path)
    descriptor = chromium_runtime_candidate_descriptor_to_dict(opened.descriptor)
    return f"/proc/self/fd/{opened.runtime_descriptor}/{descriptor['runtime']['executable']}"


def chromium_runtime_candidate_environment(
    opened: OpenVerifiedChromiumRuntimeCandidate,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> dict[str, str]:
    revalidate_open_chromium_runtime_candidate(opened, source_lock_path=source_lock_path)
    descriptor = chromium_runtime_candidate_descriptor_to_dict(opened.descriptor)
    return {
        "CHROME_DEVEL_SANDBOX": descriptor["runtime"]["sandbox"]["installedPath"]
    }


def render_chromium_shell_config(
    *,
    profile_root: os.PathLike[str] | str,
    start_url: str,
    display_backend: str,
    fixture_mode: bool = False,
    test_auto_activate: bool = False,
) -> bytes:
    try:
        profile = _absolute_normalized_path(profile_root, "CEF profile root")
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error
    if display_backend not in ("wayland", "x11"):
        raise ChromiumRuntimeCandidateError("CEF display backend must be wayland or x11")
    if type(fixture_mode) is not bool or type(test_auto_activate) is not bool:
        raise ChromiumRuntimeCandidateError("CEF fixture flags must be booleans")
    if test_auto_activate and not fixture_mode:
        raise ChromiumRuntimeCandidateError("CEF auto-activation requires fixture mode")
    if (
        not isinstance(start_url, str)
        or not start_url
        or len(start_url.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in start_url)
    ):
        raise ChromiumRuntimeCandidateError("CEF start URL is invalid")
    try:
        parsed = urlsplit(start_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ChromiumRuntimeCandidateError("CEF start URL is malformed") from error
    if (
        parsed.username is not None
        or parsed.password is not None
        or not hostname
        or parsed.fragment
    ):
        raise ChromiumRuntimeCandidateError("CEF start URL must be credential-free and fragment-free")
    if fixture_mode:
        if parsed.scheme != "http" or hostname != "127.0.0.1" or port is None:
            raise ChromiumRuntimeCandidateError("CEF fixture URL must be explicit IPv4 loopback HTTP")
    elif parsed.scheme != "https":
        raise ChromiumRuntimeCandidateError("CEF production URL must use HTTPS")
    fields = (
        "zenCefShellConfigVersion=1",
        f"displayBackend={display_backend}",
        f"fixtureMode={int(fixture_mode)}",
        f"profileRoot={profile}",
        f"startUrl={start_url}",
        f"testAutoActivate={int(test_auto_activate)}",
    )
    payload = ("\n".join(fields) + "\n").encode("utf-8")
    if len(payload) > 16 * 1024 or any(byte < 32 and byte != 10 for byte in payload):
        raise ChromiumRuntimeCandidateError("CEF shell config exceeds closed encoding")
    return payload


def create_chromium_shell_config_descriptor(payload: bytes) -> int:
    if not isinstance(payload, bytes) or not payload or len(payload) > 16 * 1024:
        raise ChromiumRuntimeCandidateError("CEF shell config payload is invalid")
    try:
        descriptor = _create_memfd()
    except RuntimeSafetyError as error:
        raise ChromiumRuntimeCandidateSafetyError(str(error)) from error
    try:
        write_all(descriptor, payload)
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _REQUIRED_SEALS)
        if fcntl.fcntl(descriptor, _F_GET_SEALS) & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise ChromiumRuntimeCandidateSafetyError("CEF shell config sealing failed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def assemble_chromium_runtime_candidate(
    *,
    package_path: os.PathLike[str] | str,
    package_signature_path: os.PathLike[str] | str,
    packaging_source_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    source_lock_path: os.PathLike[str] | str | None = None,
    gpg_home: str = PACMAN_GPG_HOME,
) -> ChromiumRuntimeCandidateDescriptor:
    lock_path = (
        bundled_chromium_runtime_source_lock_path()
        if source_lock_path is None
        else _absolute_normalized_path(source_lock_path, "Chromium source lock")
    )
    lock = (
        load_bundled_chromium_runtime_source_lock()
        if source_lock_path is None
        else load_chromium_runtime_source_lock(lock_path)
    )
    data = chromium_runtime_source_lock_to_dict(lock)
    packaging_payloads = _verify_closed_source_tree(
        packaging_source_path,
        data["packaging"]["files"],
        "Arch CEF packaging source",
        allow_git=True,
    )
    del packaging_payloads
    shell_payloads = _verify_closed_source_tree(
        bundled_chromium_shell_source_path(),
        data["shell"]["sourceFiles"],
        "bundled CEF shell source",
        allow_git=False,
    )
    for name in _TOOL_NAMES:
        pin = data["shell"]["toolchain"][name]
        _verify_tool(pin, f"pinned {name}")
        _tool_version(pin, name)
    output = _absolute_normalized_path(output_path, "Chromium candidate output")
    output_parent, output_name = output.rsplit("/", 1)
    if not output_parent:
        output_parent = "/"
    _parent, parent_fd, _identity = _open_absolute_directory(
        output_parent, "Chromium candidate output parent"
    )
    staging_name = f".chromium-candidate-{secrets.token_hex(16)}"
    staging_fd = -1
    runtime_fd = -1
    sdk_fd = -1
    published = False
    try:
        try:
            os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ChromiumRuntimeCandidateSafetyError("candidate output already exists")
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        _common, directory_flags = _platform_flags()
        staging_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
        staging_path = f"{output_parent.rstrip('/')}/{staging_name}" or f"/{staging_name}"
        with ExitStack() as stack:
            package_input = stack.enter_context(
                _open_pinned_input(package_path, data["artifacts"]["package"], "Arch CEF package")
            )
            signature_input = stack.enter_context(
                _open_pinned_input(
                    package_signature_path,
                    data["artifacts"]["packageSignature"],
                    "Arch CEF package signature",
                )
            )
            _verify_package_signature(
                lock,
                package_input.descriptor,
                signature_input.descriptor,
                gpg_home=gpg_home,
            )
            scan = _scan_package(lock, package_input.descriptor)
            runtime_fd, sdk_fd = _extract_package_subset(
                lock, scan, package_input.descriptor, staging_fd
            )
            _build_shell(lock, staging_path, staging_fd, runtime_fd, shell_payloads)
            _normalize_tree(runtime_fd)
            files = _verify_runtime_tree(runtime_fd, lock)
            if len(files) != data["candidate"]["fileCount"]:
                raise ChromiumRuntimeCandidateError("built runtime file count changed")
            descriptor = _expected_candidate_descriptor(lock)
            _write_candidate_descriptor(staging_fd, descriptor)
            os.close(sdk_fd)
            sdk_fd = -1
            for disposable in ("build", "home", "sdk", "source", "tmp"):
                child = os.open(disposable, directory_flags, dir_fd=staging_fd)
                try:
                    _remove_directory_contents(child)
                finally:
                    os.close(child)
                os.rmdir(disposable, dir_fd=staging_fd)
            _normalize_tree(staging_fd)
            _revalidate_opened_input(package_input)
            _revalidate_opened_input(signature_input)
            with open_verified_chromium_runtime_candidate(
                staging_path, source_lock_path=lock_path
            ) as opened:
                if opened.descriptor != descriptor:
                    raise ChromiumRuntimeCandidateError("staged candidate descriptor changed")
            rename_noreplace(parent_fd, staging_name, output_name)
            published = True
            os.fsync(parent_fd)
        with open_verified_chromium_runtime_candidate(
            output, source_lock_path=lock_path
        ) as opened:
            if opened.descriptor != descriptor:
                raise ChromiumRuntimeCandidateError("published candidate descriptor changed")
        return descriptor
    except (ChromiumRuntimeCandidateError, RuntimeSafetyError):
        raise
    except OSError as error:
        raise ChromiumRuntimeCandidateSafetyError(
            f"cannot assemble Chromium runtime candidate: {error}"
        ) from error
    finally:
        primary = sys.exc_info()[0] is not None
        for descriptor_fd in (runtime_fd, sdk_fd):
            if descriptor_fd >= 0:
                os.close(descriptor_fd)
        if staging_fd >= 0:
            if not published:
                try:
                    _remove_directory_contents(staging_fd)
                except OSError:
                    if not primary:
                        raise
            os.close(staging_fd)
        if not published:
            try:
                os.rmdir(staging_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                if not primary:
                    raise
        os.close(parent_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zen_app_runtime.chromium_runtime_candidate",
        description="Offline assembly and verification of the pinned Chromium/CEF candidate",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--source-lock", default=bundled_chromium_runtime_source_lock_path())
    assemble.add_argument("--package", required=True)
    assemble.add_argument("--package-signature", required=True)
    assemble.add_argument("--packaging-source", required=True)
    assemble.add_argument("--output", required=True)
    assemble.add_argument("--gpg-home", default=PACMAN_GPG_HOME)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--source-lock", default=bundled_chromium_runtime_source_lock_path())
    verify.add_argument("--candidate", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "assemble":
            descriptor = assemble_chromium_runtime_candidate(
                source_lock_path=arguments.source_lock,
                package_path=arguments.package,
                package_signature_path=arguments.package_signature,
                packaging_source_path=arguments.packaging_source,
                output_path=arguments.output,
                gpg_home=arguments.gpg_home,
            )
        else:
            with open_verified_chromium_runtime_candidate(
                arguments.candidate, source_lock_path=arguments.source_lock
            ) as opened:
                descriptor = opened.descriptor
        sys.stdout.write(render_chromium_runtime_candidate_descriptor(descriptor))
        return 0
    except ChromiumRuntimeCandidateError as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
