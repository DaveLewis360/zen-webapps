"""Offline assembly and retained authority for one pinned Gecko candidate.

The assembler never performs network access.  It consumes operator-supplied,
no-follow input files, verifies Mozilla's detached checksum signature with an
isolated GnuPG home, verifies every pinned size and digest, validates both tar
streams without ``extractall()``, and publishes a normalized missing output
atomically.  The runnable candidate is the exact official Firefox archive;
the matching source archive is provenance, not a claim that a source build was
performed.

A retained directory descriptor prevents pathname replacement from redirecting
execution.  It cannot make effective-user-owned distribution files immutable
after the last hash.  Development candidates therefore require immediate
revalidation and descriptor-relative execution; release integration must use
root-owned, non-user-writable storage or a stronger immutable staging boundary.
"""

from __future__ import annotations

import argparse
import configparser
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit

from ._secure_fs import rename_noreplace, write_all
from .runtime import (
    RuntimeSafetyError,
    _absolute_normalized_path,
    _open_absolute_file,
    _platform_flags,
    _read_bounded_utf8,
    _safe_owner,
    _stat_identity,
)

GECKO_RUNTIME_SOURCE_LOCK_VERSION = 1
GECKO_RUNTIME_TREE_VERSION = 1
GECKO_RUNTIME_CANDIDATE_VERSION = 1
GECKO_RUNTIME_CANDIDATE_DESCRIPTOR = "gecko-runtime-candidate.json"
BUNDLED_GECKO_RUNTIME_SOURCE_LOCK = (
    "gecko-firefox-154.0-linux-x86_64-en-US.lock.json"
)
EXPECTED_BUNDLED_GECKO_RUNTIME_SOURCE_LOCK_SHA256 = (
    "037cd82a1e840ab5734a5fa54263bf62c07bc53e5d058512b2211e1030f9db3b"
)
GPG_EXECUTABLE = "/usr/bin/gpg"
MAX_LOCK_BYTES = 128 * 1024
MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_CHECKSUM_BYTES = 2 * 1024 * 1024
MAX_TAR_MEMBERS = 1_000_000
MAX_RUNTIME_DIRECTORIES = 512
MAX_RELATIVE_PATH_BYTES = 4096
MAX_PATH_DEPTH = 32

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHA512_PATTERN = re.compile(r"^[0-9a-f]{128}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}$")
_BUILD_ID_PATTERN = re.compile(r"^[0-9]{14}$")
_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
_CHECKSUM_LINE_PATTERN = re.compile(r"^([0-9a-f]{64})  ([^\x00-\x1f\x7f]+)$")
_REQUIRED_SOURCE_METADATA = frozenset(
    {
        "browser/config/version.txt",
        "browser/config/version_display.txt",
        "config/milestone.txt",
        "sourcestamp.txt",
    }
)


class GeckoRuntimeCandidateError(ValueError):
    """A Gecko source lock, input, archive, or candidate failed closed."""


class GeckoRuntimeCandidateSafetyError(GeckoRuntimeCandidateError):
    """A filesystem or archive safety invariant failed."""


@dataclass(frozen=True)
class GeckoArtifactPin:
    filename: str
    url: str
    size: int
    sha256: str
    sha512: str | None = None
    checksum_path: str | None = None


@dataclass(frozen=True)
class GeckoSignaturePin:
    hash_algorithm: str
    primary_fingerprint: str
    public_key_algorithm: str
    signature_date: str
    signature_timestamp: int
    signer_uid: str
    signing_fingerprint: str


@dataclass(frozen=True)
class GeckoReleasePin:
    release_root: str
    signature: GeckoSignaturePin
    source_repository: str
    source_stamp: str


@dataclass(frozen=True)
class GeckoExecutablePin:
    mode: str
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class GeckoRuntimeTreePin:
    archive_root: str
    build_id: str
    candidate_id: str
    executable: GeckoExecutablePin
    file_count: int
    locale: str
    product: str
    target: str
    total_bytes: int
    tree_sha256: str
    version: str


@dataclass(frozen=True)
class GeckoSourceMetadataPin:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class GeckoSourcePin:
    archive_root: str
    build_status: str
    metadata: tuple[GeckoSourceMetadataPin, ...]
    reproducibility_status: str
    toolchain_snapshot: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class GeckoRuntimeSourceLock:
    lock_version: int
    checksums: GeckoArtifactPin
    checksums_signature: GeckoArtifactPin
    runtime_archive: GeckoArtifactPin
    signing_key: GeckoArtifactPin
    source_archive: GeckoArtifactPin
    candidate: GeckoRuntimeTreePin
    release: GeckoReleasePin
    source: GeckoSourcePin


@dataclass(frozen=True)
class GeckoRuntimeTreeEntry:
    path: str
    size: int
    mode: str
    sha256: str


@dataclass(frozen=True)
class GeckoRuntimeCandidateDescriptor:
    candidate_version: int
    candidate_id: str
    distribution: str
    runtime_archive_sha256: str
    signed_checksums_sha256: str
    signer_primary_fingerprint: str
    source_archive_sha256: str
    source_build_status: str
    source_lock_sha256: str
    source_reproducibility_status: str
    archive_root: str
    build_id: str
    executable: str
    file_count: int
    locale: str
    product: str
    source_repository: str
    source_stamp: str
    target: str
    total_bytes: int
    tree_sha256: str
    version: str


@dataclass(frozen=True)
class OpenVerifiedGeckoRuntimeCandidate:
    """Candidate authority whose descriptors are owned by its context."""

    logical_path: str
    root_descriptor: int
    runtime_descriptor: int
    root_identity: tuple[int, ...]
    runtime_identity: tuple[int, ...]
    descriptor: GeckoRuntimeCandidateDescriptor
    files: tuple[GeckoRuntimeTreeEntry, ...]


@dataclass(frozen=True)
class _OpenedPinnedInput:
    label: str
    logical_path: str
    descriptor: int
    initial_identity: tuple[int, ...]


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
    raise GeckoRuntimeCandidateError(f"{label} fields: {', '.join(details)}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GeckoRuntimeCandidateError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GeckoRuntimeCandidateError(f"{label} must be nonempty text")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise GeckoRuntimeCandidateError(f"{label} cannot contain control characters")
    return value


def _integer(value: Any, label: str, *, positive: bool = True) -> int:
    if type(value) is not int or (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "nonnegative"
        raise GeckoRuntimeCandidateError(f"{label} must be a {qualifier} integer")
    return value


def _digest(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise GeckoRuntimeCandidateError(f"{label} has an invalid digest")
    return value


def _safe_relative_path(value: Any, label: str, *, basename: bool = False) -> str:
    path = _text(value, label)
    if path.startswith("/") or "\\" in path or len(path.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise GeckoRuntimeCandidateError(f"{label} must be a safe relative POSIX path")
    components = path.split("/")
    if (
        any(component in ("", ".", "..") for component in components)
        or len(components) > MAX_PATH_DEPTH
        or (basename and len(components) != 1)
    ):
        raise GeckoRuntimeCandidateError(f"{label} must be a safe relative POSIX path")
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
        raise GeckoRuntimeCandidateError(f"{label} must be a canonical credential-free HTTPS URL")
    return rendered


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
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


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GeckoRuntimeCandidateError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise GeckoRuntimeCandidateError(f"non-finite JSON number is not permitted: {value}")


def _parse_json_bytes(payload: bytes, label: str) -> Any:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GeckoRuntimeCandidateError(f"{label} must be UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except GeckoRuntimeCandidateError:
        raise
    except json.JSONDecodeError as error:
        raise GeckoRuntimeCandidateError(f"{label} is not strict JSON: {error}") from error


def _parse_artifact(value: Any, label: str, *, archive: bool) -> GeckoArtifactPin:
    data = _mapping(value, label)
    expected = (
        frozenset({"checksumPath", "filename", "sha256", "sha512", "size", "url"})
        if archive
        else frozenset({"filename", "sha256", "size", "url"})
    )
    _exact_keys(data, expected, label)
    filename = _safe_relative_path(data["filename"], f"{label}.filename", basename=True)
    url = _https_url(data["url"], f"{label}.url")
    if urlsplit(url).path.rsplit("/", 1)[-1] != filename:
        raise GeckoRuntimeCandidateError(f"{label}.url does not end in its filename")
    sha512 = None
    checksum_path = None
    if archive:
        sha512 = _digest(data["sha512"], f"{label}.sha512", _SHA512_PATTERN)
        checksum_path = _safe_relative_path(
            data["checksumPath"], f"{label}.checksumPath"
        )
        if checksum_path.rsplit("/", 1)[-1] != filename:
            raise GeckoRuntimeCandidateError(
                f"{label}.checksumPath does not end in its filename"
            )
    return GeckoArtifactPin(
        filename=filename,
        url=url,
        size=_integer(data["size"], f"{label}.size"),
        sha256=_digest(data["sha256"], f"{label}.sha256", _SHA256_PATTERN),
        sha512=sha512,
        checksum_path=checksum_path,
    )


def _artifact_to_dict(pin: GeckoArtifactPin) -> dict[str, Any]:
    result: dict[str, Any] = {
        "filename": pin.filename,
        "sha256": pin.sha256,
        "size": pin.size,
        "url": pin.url,
    }
    if pin.sha512 is not None or pin.checksum_path is not None:
        if pin.sha512 is None or pin.checksum_path is None:
            raise GeckoRuntimeCandidateError("archive artifact pin is incomplete")
        result["checksumPath"] = pin.checksum_path
        result["sha512"] = pin.sha512
    return result


def gecko_runtime_source_lock_from_dict(value: Any) -> GeckoRuntimeSourceLock:
    data = _mapping(value, "Gecko runtime source lock")
    _exact_keys(
        data,
        frozenset(
            {
                "artifacts",
                "candidate",
                "geckoRuntimeSourceLockVersion",
                "release",
                "source",
            }
        ),
        "Gecko runtime source lock",
    )
    version = data["geckoRuntimeSourceLockVersion"]
    if type(version) is not int or version != GECKO_RUNTIME_SOURCE_LOCK_VERSION:
        raise GeckoRuntimeCandidateError(
            f"geckoRuntimeSourceLockVersion must be {GECKO_RUNTIME_SOURCE_LOCK_VERSION}"
        )

    artifacts = _mapping(data["artifacts"], "artifacts")
    _exact_keys(
        artifacts,
        frozenset(
            {
                "checksums",
                "checksumsSignature",
                "runtimeArchive",
                "signingKey",
                "sourceArchive",
            }
        ),
        "artifacts",
    )
    checksums = _parse_artifact(artifacts["checksums"], "artifacts.checksums", archive=False)
    checksums_signature = _parse_artifact(
        artifacts["checksumsSignature"],
        "artifacts.checksumsSignature",
        archive=False,
    )
    runtime_archive = _parse_artifact(
        artifacts["runtimeArchive"], "artifacts.runtimeArchive", archive=True
    )
    signing_key = _parse_artifact(
        artifacts["signingKey"], "artifacts.signingKey", archive=False
    )
    source_archive = _parse_artifact(
        artifacts["sourceArchive"], "artifacts.sourceArchive", archive=True
    )

    candidate_data = _mapping(data["candidate"], "candidate")
    _exact_keys(
        candidate_data,
        frozenset(
            {
                "archiveRoot",
                "buildId",
                "candidateId",
                "executable",
                "fileCount",
                "locale",
                "product",
                "target",
                "totalBytes",
                "treeSha256",
                "version",
            }
        ),
        "candidate",
    )
    executable_data = _mapping(candidate_data["executable"], "candidate.executable")
    _exact_keys(
        executable_data,
        frozenset({"mode", "path", "sha256", "size"}),
        "candidate.executable",
    )
    executable_mode = _text(executable_data["mode"], "candidate.executable.mode")
    if _MODE_PATTERN.fullmatch(executable_mode) is None or executable_mode != "0755":
        raise GeckoRuntimeCandidateError("candidate executable mode must be 0755")
    executable = GeckoExecutablePin(
        mode=executable_mode,
        path=_safe_relative_path(executable_data["path"], "candidate.executable.path"),
        sha256=_digest(
            executable_data["sha256"], "candidate.executable.sha256", _SHA256_PATTERN
        ),
        size=_integer(executable_data["size"], "candidate.executable.size"),
    )
    candidate_version = _text(candidate_data["version"], "candidate.version")
    if _VERSION_PATTERN.fullmatch(candidate_version) is None:
        raise GeckoRuntimeCandidateError("candidate.version is invalid")
    archive_root = _safe_relative_path(
        candidate_data["archiveRoot"], "candidate.archiveRoot", basename=True
    )
    candidate_id = _safe_relative_path(
        candidate_data["candidateId"], "candidate.candidateId", basename=True
    )
    build_id = _text(candidate_data["buildId"], "candidate.buildId")
    if _BUILD_ID_PATTERN.fullmatch(build_id) is None:
        raise GeckoRuntimeCandidateError("candidate.buildId must be fourteen digits")
    locale = _safe_relative_path(candidate_data["locale"], "candidate.locale", basename=True)
    target = _safe_relative_path(candidate_data["target"], "candidate.target", basename=True)
    product = _text(candidate_data["product"], "candidate.product")
    expected_candidate_id = f"firefox-{candidate_version}-{target}-{locale}"
    if candidate_id != expected_candidate_id or product != "Firefox" or archive_root != "firefox":
        raise GeckoRuntimeCandidateError("candidate identity is not the closed Firefox v1 shape")
    candidate = GeckoRuntimeTreePin(
        archive_root=archive_root,
        build_id=build_id,
        candidate_id=candidate_id,
        executable=executable,
        file_count=_integer(candidate_data["fileCount"], "candidate.fileCount"),
        locale=locale,
        product=product,
        target=target,
        total_bytes=_integer(candidate_data["totalBytes"], "candidate.totalBytes"),
        tree_sha256=_digest(
            candidate_data["treeSha256"], "candidate.treeSha256", _SHA256_PATTERN
        ),
        version=candidate_version,
    )

    release_data = _mapping(data["release"], "release")
    _exact_keys(
        release_data,
        frozenset({"releaseRoot", "signature", "sourceRepository", "sourceStamp"}),
        "release",
    )
    signature_data = _mapping(release_data["signature"], "release.signature")
    _exact_keys(
        signature_data,
        frozenset(
            {
                "hashAlgorithm",
                "primaryFingerprint",
                "publicKeyAlgorithm",
                "signatureDate",
                "signatureTimestamp",
                "signerUid",
                "signingFingerprint",
            }
        ),
        "release.signature",
    )
    primary_fingerprint = _digest(
        signature_data["primaryFingerprint"],
        "release.signature.primaryFingerprint",
        _FINGERPRINT_PATTERN,
    )
    signing_fingerprint = _digest(
        signature_data["signingFingerprint"],
        "release.signature.signingFingerprint",
        _FINGERPRINT_PATTERN,
    )
    signature = GeckoSignaturePin(
        hash_algorithm=_text(signature_data["hashAlgorithm"], "release.signature.hashAlgorithm"),
        primary_fingerprint=primary_fingerprint,
        public_key_algorithm=_text(
            signature_data["publicKeyAlgorithm"],
            "release.signature.publicKeyAlgorithm",
        ),
        signature_date=_text(signature_data["signatureDate"], "release.signature.signatureDate"),
        signature_timestamp=_integer(
            signature_data["signatureTimestamp"],
            "release.signature.signatureTimestamp",
        ),
        signer_uid=_text(signature_data["signerUid"], "release.signature.signerUid"),
        signing_fingerprint=signing_fingerprint,
    )
    if signature.hash_algorithm != "SHA512" or signature.public_key_algorithm != "RSA":
        raise GeckoRuntimeCandidateError("release signature algorithms must be RSA/SHA512")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", signature.signature_date) is None:
        raise GeckoRuntimeCandidateError("release signature date must be YYYY-MM-DD")
    release_root = _https_url(release_data["releaseRoot"], "release.releaseRoot")
    if not release_root.endswith("/"):
        raise GeckoRuntimeCandidateError("release.releaseRoot must end with a slash")
    source_repository = _https_url(
        release_data["sourceRepository"], "release.sourceRepository"
    )
    source_stamp = _text(release_data["sourceStamp"], "release.sourceStamp")
    if re.fullmatch(r"[0-9a-f]{40}", source_stamp) is None:
        raise GeckoRuntimeCandidateError("release.sourceStamp must be a lowercase 40-hex revision")
    release = GeckoReleasePin(
        release_root=release_root,
        signature=signature,
        source_repository=source_repository,
        source_stamp=source_stamp,
    )

    source_data = _mapping(data["source"], "source")
    _exact_keys(
        source_data,
        frozenset(
            {
                "archiveRoot",
                "buildStatus",
                "metadata",
                "reproducibilityStatus",
                "toolchainSnapshot",
            }
        ),
        "source",
    )
    metadata_data = source_data["metadata"]
    if not isinstance(metadata_data, list):
        raise GeckoRuntimeCandidateError("source.metadata must be an array")
    metadata: list[GeckoSourceMetadataPin] = []
    for index, item in enumerate(metadata_data):
        item_data = _mapping(item, f"source.metadata[{index}]")
        _exact_keys(
            item_data,
            frozenset({"path", "sha256", "size"}),
            f"source.metadata[{index}]",
        )
        metadata.append(
            GeckoSourceMetadataPin(
                path=_safe_relative_path(item_data["path"], f"source.metadata[{index}].path"),
                sha256=_digest(
                    item_data["sha256"],
                    f"source.metadata[{index}].sha256",
                    _SHA256_PATTERN,
                ),
                size=_integer(item_data["size"], f"source.metadata[{index}].size"),
            )
        )
    if [item.path for item in metadata] != sorted(item.path for item in metadata):
        raise GeckoRuntimeCandidateError("source.metadata must be sorted by path")
    if frozenset(item.path for item in metadata) != _REQUIRED_SOURCE_METADATA:
        raise GeckoRuntimeCandidateError("source.metadata is not the closed required set")
    toolchain_data = _mapping(source_data["toolchainSnapshot"], "source.toolchainSnapshot")
    toolchain_keys = frozenset({"clang", "cmake", "ninja", "purpose", "rustc", "target"})
    _exact_keys(toolchain_data, toolchain_keys, "source.toolchainSnapshot")
    toolchain = tuple(
        (key, _text(toolchain_data[key], f"source.toolchainSnapshot.{key}"))
        for key in sorted(toolchain_keys)
    )
    toolchain_mapping = dict(toolchain)
    build_status = _text(source_data["buildStatus"], "source.buildStatus")
    reproducibility_status = _text(
        source_data["reproducibilityStatus"], "source.reproducibilityStatus"
    )
    source_archive_root = _safe_relative_path(
        source_data["archiveRoot"], "source.archiveRoot", basename=True
    )
    if (
        source_archive_root != f"firefox-{candidate.version}"
        or build_status != "not-performed"
        or reproducibility_status != "not-tested"
        or toolchain_mapping["purpose"] != "future-source-build-input-snapshot"
    ):
        raise GeckoRuntimeCandidateError("source provenance status is not the closed v1 shape")
    source = GeckoSourcePin(
        archive_root=source_archive_root,
        build_status=build_status,
        metadata=tuple(metadata),
        reproducibility_status=reproducibility_status,
        toolchain_snapshot=toolchain,
    )

    expected_urls = (
        (checksums.url, release.release_root + "SHA256SUMS"),
        (checksums_signature.url, release.release_root + "SHA256SUMS.asc"),
        (signing_key.url, release.release_root + "KEY"),
        (runtime_archive.url, release.release_root + str(runtime_archive.checksum_path)),
        (source_archive.url, release.release_root + str(source_archive.checksum_path)),
    )
    if any(actual != expected for actual, expected in expected_urls):
        raise GeckoRuntimeCandidateError("artifact URL is not derived from release.releaseRoot")
    return GeckoRuntimeSourceLock(
        lock_version=version,
        checksums=checksums,
        checksums_signature=checksums_signature,
        runtime_archive=runtime_archive,
        signing_key=signing_key,
        source_archive=source_archive,
        candidate=candidate,
        release=release,
        source=source,
    )


def gecko_runtime_source_lock_to_dict(lock: GeckoRuntimeSourceLock) -> dict[str, Any]:
    if not isinstance(lock, GeckoRuntimeSourceLock):
        raise GeckoRuntimeCandidateError("Gecko runtime source lock was fabricated")
    data = {
        "artifacts": {
            "checksums": _artifact_to_dict(lock.checksums),
            "checksumsSignature": _artifact_to_dict(lock.checksums_signature),
            "runtimeArchive": _artifact_to_dict(lock.runtime_archive),
            "signingKey": _artifact_to_dict(lock.signing_key),
            "sourceArchive": _artifact_to_dict(lock.source_archive),
        },
        "candidate": {
            "archiveRoot": lock.candidate.archive_root,
            "buildId": lock.candidate.build_id,
            "candidateId": lock.candidate.candidate_id,
            "executable": {
                "mode": lock.candidate.executable.mode,
                "path": lock.candidate.executable.path,
                "sha256": lock.candidate.executable.sha256,
                "size": lock.candidate.executable.size,
            },
            "fileCount": lock.candidate.file_count,
            "locale": lock.candidate.locale,
            "product": lock.candidate.product,
            "target": lock.candidate.target,
            "totalBytes": lock.candidate.total_bytes,
            "treeSha256": lock.candidate.tree_sha256,
            "version": lock.candidate.version,
        },
        "geckoRuntimeSourceLockVersion": lock.lock_version,
        "release": {
            "releaseRoot": lock.release.release_root,
            "signature": {
                "hashAlgorithm": lock.release.signature.hash_algorithm,
                "primaryFingerprint": lock.release.signature.primary_fingerprint,
                "publicKeyAlgorithm": lock.release.signature.public_key_algorithm,
                "signatureDate": lock.release.signature.signature_date,
                "signatureTimestamp": lock.release.signature.signature_timestamp,
                "signerUid": lock.release.signature.signer_uid,
                "signingFingerprint": lock.release.signature.signing_fingerprint,
            },
            "sourceRepository": lock.release.source_repository,
            "sourceStamp": lock.release.source_stamp,
        },
        "source": {
            "archiveRoot": lock.source.archive_root,
            "buildStatus": lock.source.build_status,
            "metadata": [
                {"path": item.path, "sha256": item.sha256, "size": item.size}
                for item in lock.source.metadata
            ],
            "reproducibilityStatus": lock.source.reproducibility_status,
            "toolchainSnapshot": dict(lock.source.toolchain_snapshot),
        },
    }
    gecko_runtime_source_lock_from_dict(data)
    return data


def render_gecko_runtime_source_lock(lock: GeckoRuntimeSourceLock) -> str:
    return _canonical_json_bytes(gecko_runtime_source_lock_to_dict(lock)).decode("ascii")


def gecko_runtime_source_lock_sha256(lock: GeckoRuntimeSourceLock) -> str:
    return hashlib.sha256(render_gecko_runtime_source_lock(lock).encode("ascii")).hexdigest()


def _read_document(path: os.PathLike[str] | str, label: str, limit: int) -> tuple[str, bytes]:
    try:
        normalized = _absolute_normalized_path(path, label)
        with _open_absolute_file(normalized, label) as (descriptor, initial):
            if initial.st_nlink != 1:
                raise GeckoRuntimeCandidateSafetyError(f"{label} must have exactly one hard link")
            os.lseek(descriptor, 0, os.SEEK_SET)
            text = _read_bounded_utf8(descriptor, limit, label)
            final = os.fstat(descriptor)
            if _stat_identity(initial) != _stat_identity(final):
                raise GeckoRuntimeCandidateSafetyError(f"{label} changed while reading")
            return normalized, text.encode("utf-8")
    except GeckoRuntimeCandidateError:
        raise
    except RuntimeSafetyError as error:
        raise GeckoRuntimeCandidateSafetyError(str(error)) from error


def load_gecko_runtime_source_lock(
    lock_path: os.PathLike[str] | str,
) -> GeckoRuntimeSourceLock:
    _normalized, payload = _read_document(lock_path, "Gecko runtime source lock", MAX_LOCK_BYTES)
    data = _parse_json_bytes(payload, "Gecko runtime source lock")
    lock = gecko_runtime_source_lock_from_dict(data)
    canonical = render_gecko_runtime_source_lock(lock).encode("ascii")
    if not hmac.compare_digest(payload, canonical):
        raise GeckoRuntimeCandidateError("Gecko runtime source lock is not canonical JSON")
    return lock


def bundled_gecko_runtime_source_lock_path() -> str:
    return os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "runtime_candidates",
            BUNDLED_GECKO_RUNTIME_SOURCE_LOCK,
        )
    )


def load_bundled_gecko_runtime_source_lock() -> GeckoRuntimeSourceLock:
    lock = load_gecko_runtime_source_lock(bundled_gecko_runtime_source_lock_path())
    actual = gecko_runtime_source_lock_sha256(lock)
    if not hmac.compare_digest(
        actual, EXPECTED_BUNDLED_GECKO_RUNTIME_SOURCE_LOCK_SHA256
    ):
        raise GeckoRuntimeCandidateError("bundled Gecko source lock digest does not match")
    return lock


def _tree_entry_to_dict(entry: GeckoRuntimeTreeEntry) -> dict[str, Any]:
    return {
        "mode": entry.mode,
        "path": entry.path,
        "sha256": entry.sha256,
        "size": entry.size,
    }


def gecko_runtime_tree_sha256(entries: Sequence[GeckoRuntimeTreeEntry]) -> str:
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    if len({item.path for item in ordered}) != len(ordered):
        raise GeckoRuntimeCandidateError("Gecko runtime tree contains duplicate paths")
    payload = _canonical_json_bytes(
        {
            "files": [_tree_entry_to_dict(item) for item in ordered],
            "geckoRuntimeTreeVersion": GECKO_RUNTIME_TREE_VERSION,
        }
    )
    return hashlib.sha256(payload).hexdigest()


def _expected_candidate_descriptor(
    lock: GeckoRuntimeSourceLock,
) -> GeckoRuntimeCandidateDescriptor:
    return GeckoRuntimeCandidateDescriptor(
        candidate_version=GECKO_RUNTIME_CANDIDATE_VERSION,
        candidate_id=lock.candidate.candidate_id,
        distribution="official-mozilla-release-archive",
        runtime_archive_sha256=lock.runtime_archive.sha256,
        signed_checksums_sha256=lock.checksums.sha256,
        signer_primary_fingerprint=lock.release.signature.primary_fingerprint,
        source_archive_sha256=lock.source_archive.sha256,
        source_build_status=lock.source.build_status,
        source_lock_sha256=gecko_runtime_source_lock_sha256(lock),
        source_reproducibility_status=lock.source.reproducibility_status,
        archive_root=lock.candidate.archive_root,
        build_id=lock.candidate.build_id,
        executable=lock.candidate.executable.path,
        file_count=lock.candidate.file_count,
        locale=lock.candidate.locale,
        product=lock.candidate.product,
        source_repository=lock.release.source_repository,
        source_stamp=lock.release.source_stamp,
        target=lock.candidate.target,
        total_bytes=lock.candidate.total_bytes,
        tree_sha256=lock.candidate.tree_sha256,
        version=lock.candidate.version,
    )


def gecko_runtime_candidate_descriptor_to_dict(
    descriptor: GeckoRuntimeCandidateDescriptor,
) -> dict[str, Any]:
    if not isinstance(descriptor, GeckoRuntimeCandidateDescriptor):
        raise GeckoRuntimeCandidateError("Gecko runtime candidate descriptor was fabricated")
    data = {
        "candidateId": descriptor.candidate_id,
        "geckoRuntimeCandidateVersion": descriptor.candidate_version,
        "provenance": {
            "distribution": descriptor.distribution,
            "runtimeArchiveSha256": descriptor.runtime_archive_sha256,
            "signedChecksumsSha256": descriptor.signed_checksums_sha256,
            "signerPrimaryFingerprint": descriptor.signer_primary_fingerprint,
            "sourceArchiveSha256": descriptor.source_archive_sha256,
            "sourceBuildStatus": descriptor.source_build_status,
            "sourceLockSha256": descriptor.source_lock_sha256,
            "sourceReproducibilityStatus": descriptor.source_reproducibility_status,
        },
        "runtime": {
            "archiveRoot": descriptor.archive_root,
            "buildId": descriptor.build_id,
            "executable": descriptor.executable,
            "fileCount": descriptor.file_count,
            "locale": descriptor.locale,
            "product": descriptor.product,
            "sourceRepository": descriptor.source_repository,
            "sourceStamp": descriptor.source_stamp,
            "target": descriptor.target,
            "totalBytes": descriptor.total_bytes,
            "treeSha256": descriptor.tree_sha256,
            "version": descriptor.version,
        },
    }
    gecko_runtime_candidate_descriptor_from_dict(data)
    return data


def gecko_runtime_candidate_descriptor_from_dict(
    value: Any,
) -> GeckoRuntimeCandidateDescriptor:
    data = _mapping(value, "Gecko runtime candidate descriptor")
    _exact_keys(
        data,
        frozenset(
            {
                "candidateId",
                "geckoRuntimeCandidateVersion",
                "provenance",
                "runtime",
            }
        ),
        "Gecko runtime candidate descriptor",
    )
    version = data["geckoRuntimeCandidateVersion"]
    if type(version) is not int or version != GECKO_RUNTIME_CANDIDATE_VERSION:
        raise GeckoRuntimeCandidateError(
            f"geckoRuntimeCandidateVersion must be {GECKO_RUNTIME_CANDIDATE_VERSION}"
        )
    provenance = _mapping(data["provenance"], "candidate provenance")
    _exact_keys(
        provenance,
        frozenset(
            {
                "distribution",
                "runtimeArchiveSha256",
                "signedChecksumsSha256",
                "signerPrimaryFingerprint",
                "sourceArchiveSha256",
                "sourceBuildStatus",
                "sourceLockSha256",
                "sourceReproducibilityStatus",
            }
        ),
        "candidate provenance",
    )
    runtime = _mapping(data["runtime"], "candidate runtime")
    _exact_keys(
        runtime,
        frozenset(
            {
                "archiveRoot",
                "buildId",
                "executable",
                "fileCount",
                "locale",
                "product",
                "sourceRepository",
                "sourceStamp",
                "target",
                "totalBytes",
                "treeSha256",
                "version",
            }
        ),
        "candidate runtime",
    )
    build_id = _text(runtime["buildId"], "candidate runtime.buildId")
    source_stamp = _text(runtime["sourceStamp"], "candidate runtime.sourceStamp")
    signer = _digest(
        provenance["signerPrimaryFingerprint"],
        "candidate provenance.signerPrimaryFingerprint",
        _FINGERPRINT_PATTERN,
    )
    candidate_id = _safe_relative_path(data["candidateId"], "candidateId", basename=True)
    candidate_version = _text(runtime["version"], "candidate runtime.version")
    target = _safe_relative_path(runtime["target"], "candidate runtime.target", basename=True)
    locale = _safe_relative_path(runtime["locale"], "candidate runtime.locale", basename=True)
    if (
        _BUILD_ID_PATTERN.fullmatch(build_id) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_stamp) is None
        or _VERSION_PATTERN.fullmatch(candidate_version) is None
        or candidate_id != f"firefox-{candidate_version}-{target}-{locale}"
        or _text(runtime["product"], "candidate runtime.product") != "Firefox"
        or _text(provenance["distribution"], "candidate provenance.distribution")
        != "official-mozilla-release-archive"
        or _text(provenance["sourceBuildStatus"], "candidate provenance.sourceBuildStatus")
        != "not-performed"
        or _text(
            provenance["sourceReproducibilityStatus"],
            "candidate provenance.sourceReproducibilityStatus",
        )
        != "not-tested"
    ):
        raise GeckoRuntimeCandidateError("candidate descriptor identity/status is invalid")
    return GeckoRuntimeCandidateDescriptor(
        candidate_version=version,
        candidate_id=candidate_id,
        distribution="official-mozilla-release-archive",
        runtime_archive_sha256=_digest(
            provenance["runtimeArchiveSha256"],
            "candidate provenance.runtimeArchiveSha256",
            _SHA256_PATTERN,
        ),
        signed_checksums_sha256=_digest(
            provenance["signedChecksumsSha256"],
            "candidate provenance.signedChecksumsSha256",
            _SHA256_PATTERN,
        ),
        signer_primary_fingerprint=signer,
        source_archive_sha256=_digest(
            provenance["sourceArchiveSha256"],
            "candidate provenance.sourceArchiveSha256",
            _SHA256_PATTERN,
        ),
        source_build_status="not-performed",
        source_lock_sha256=_digest(
            provenance["sourceLockSha256"],
            "candidate provenance.sourceLockSha256",
            _SHA256_PATTERN,
        ),
        source_reproducibility_status="not-tested",
        archive_root=_safe_relative_path(
            runtime["archiveRoot"], "candidate runtime.archiveRoot", basename=True
        ),
        build_id=build_id,
        executable=_safe_relative_path(
            runtime["executable"], "candidate runtime.executable"
        ),
        file_count=_integer(runtime["fileCount"], "candidate runtime.fileCount"),
        locale=locale,
        product="Firefox",
        source_repository=_https_url(
            runtime["sourceRepository"], "candidate runtime.sourceRepository"
        ),
        source_stamp=source_stamp,
        target=target,
        total_bytes=_integer(runtime["totalBytes"], "candidate runtime.totalBytes"),
        tree_sha256=_digest(
            runtime["treeSha256"], "candidate runtime.treeSha256", _SHA256_PATTERN
        ),
        version=candidate_version,
    )


def render_gecko_runtime_candidate_descriptor(
    descriptor: GeckoRuntimeCandidateDescriptor,
) -> str:
    return _canonical_json_bytes(
        gecko_runtime_candidate_descriptor_to_dict(descriptor)
    ).decode("ascii")


def _hash_open_file(descriptor: int) -> tuple[str, str, int]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    sha256 = hashlib.sha256()
    sha512 = hashlib.sha512()
    size = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        sha256.update(chunk)
        sha512.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return sha256.hexdigest(), sha512.hexdigest(), size


@contextmanager
def _open_pinned_input(
    path: os.PathLike[str] | str,
    pin: GeckoArtifactPin,
    label: str,
) -> Iterator[_OpenedPinnedInput]:
    try:
        normalized = _absolute_normalized_path(path, label)
        with _open_absolute_file(normalized, label) as (descriptor, initial):
            if initial.st_nlink != 1:
                raise GeckoRuntimeCandidateSafetyError(
                    f"{label} must have exactly one hard link"
                )
            mode = stat.S_IMODE(initial.st_mode)
            if mode & 0o400 != 0o400 or mode & 0o111:
                raise GeckoRuntimeCandidateSafetyError(
                    f"{label} must be owner-readable and non-executable"
                )
            sha256, sha512, size = _hash_open_file(descriptor)
            final = os.fstat(descriptor)
            if _stat_identity(initial) != _stat_identity(final):
                raise GeckoRuntimeCandidateSafetyError(f"{label} changed while hashing")
            if size != pin.size:
                raise GeckoRuntimeCandidateError(f"{label} size does not match the source lock")
            if not hmac.compare_digest(sha256, pin.sha256):
                raise GeckoRuntimeCandidateError(f"{label} SHA-256 does not match the source lock")
            if pin.sha512 is not None and not hmac.compare_digest(sha512, pin.sha512):
                raise GeckoRuntimeCandidateError(f"{label} SHA-512 does not match the source lock")
            yield _OpenedPinnedInput(
                label=label,
                logical_path=normalized,
                descriptor=descriptor,
                initial_identity=_stat_identity(initial),
            )
    except GeckoRuntimeCandidateError:
        raise
    except RuntimeSafetyError as error:
        raise GeckoRuntimeCandidateSafetyError(str(error)) from error


def _revalidate_opened_input(opened: _OpenedPinnedInput) -> None:
    try:
        current = os.fstat(opened.descriptor)
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot revalidate {opened.label}: {error}"
        ) from error
    if _stat_identity(current) != opened.initial_identity:
        raise GeckoRuntimeCandidateSafetyError(
            f"{opened.label} changed during candidate assembly"
        )


def _gpg_status_lines(stdout: str) -> list[str]:
    prefix = "[GNUPG:] "
    return [line[len(prefix):] for line in stdout.splitlines() if line.startswith(prefix)]


def _run_gpg(command: Sequence[str], *, home: str, pass_fds: Sequence[int]) -> subprocess.CompletedProcess[str]:
    environment = {
        "GNUPGHOME": home,
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        return subprocess.run(
            tuple(command),
            env=environment,
            shell=False,
            close_fds=True,
            pass_fds=tuple(pass_fds),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GeckoRuntimeCandidateError(f"cannot execute isolated GnuPG verification: {error}") from error


def _verify_signed_checksums(
    lock: GeckoRuntimeSourceLock,
    signing_key_fd: int,
    signature_fd: int,
    checksums_fd: int,
) -> None:
    if not os.path.isfile(GPG_EXECUTABLE) or not os.access(GPG_EXECUTABLE, os.X_OK):
        raise GeckoRuntimeCandidateError(f"required GnuPG executable is unavailable: {GPG_EXECUTABLE}")
    for descriptor in (signing_key_fd, signature_fd, checksums_fd):
        os.lseek(descriptor, 0, os.SEEK_SET)
    with tempfile.TemporaryDirectory(prefix="zen-gecko-gpg-", dir="/tmp") as home:
        os.chmod(home, 0o700)
        common = (
            GPG_EXECUTABLE,
            "--homedir",
            home,
            "--no-options",
            "--batch",
            "--no-tty",
            "--status-fd=1",
        )
        imported = _run_gpg(
            (*common, "--import-options", "import-minimal", "--import", f"/proc/self/fd/{signing_key_fd}"),
            home=home,
            pass_fds=(signing_key_fd,),
        )
        import_status = _gpg_status_lines(imported.stdout)
        imported_fingerprints = {
            line.split()[2]
            for line in import_status
            if line.startswith("IMPORT_OK ") and len(line.split()) >= 3
        }
        if imported.returncode != 0 or imported_fingerprints != {
            lock.release.signature.primary_fingerprint
        }:
            detail = imported.stderr.strip() or f"exit {imported.returncode}"
            raise GeckoRuntimeCandidateError(
                f"Mozilla signing key import did not yield the pinned fingerprint: {detail}"
            )

        verified = _run_gpg(
            (
                *common,
                "--verify",
                f"/proc/self/fd/{signature_fd}",
                f"/proc/self/fd/{checksums_fd}",
            ),
            home=home,
            pass_fds=(signature_fd, checksums_fd),
        )
        status = _gpg_status_lines(verified.stdout)
        forbidden = ("BADSIG ", "ERRSIG ", "NO_PUBKEY ", "EXPSIG ", "EXPKEYSIG ", "REVKEYSIG ")
        if verified.returncode != 0 or any(line.startswith(forbidden) for line in status):
            detail = verified.stderr.strip() or f"exit {verified.returncode}"
            raise GeckoRuntimeCandidateError(f"Mozilla checksum signature is invalid: {detail}")
        valid = [line.split() for line in status if line.startswith("VALIDSIG ")]
        good = [line.split(maxsplit=2) for line in status if line.startswith("GOODSIG ")]
        if len(valid) != 1 or len(good) != 1 or len(valid[0]) < 11 or len(good[0]) != 3:
            raise GeckoRuntimeCandidateError("GnuPG did not emit one complete GOODSIG/VALIDSIG authority")
        fields = valid[0]
        signature = lock.release.signature
        expected_key_id = signature.signing_fingerprint[-16:]
        if (
            fields[1] != signature.signing_fingerprint
            or fields[2] != signature.signature_date
            or fields[3] != str(signature.signature_timestamp)
            or fields[5] != "4"
            or fields[7] != "1"
            or fields[8] != "10"
            or fields[9] != "00"
            or fields[10] != signature.primary_fingerprint
            or good[0][1] != expected_key_id
            or good[0][2] != signature.signer_uid
        ):
            raise GeckoRuntimeCandidateError("GnuPG signature identity does not match the source lock")


def _read_fd_bytes(descriptor: int, limit: int, label: str) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise GeckoRuntimeCandidateError(f"{label} exceeds its size limit")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _verify_checksum_authority(lock: GeckoRuntimeSourceLock, checksums_fd: int) -> None:
    payload = _read_fd_bytes(checksums_fd, MAX_CHECKSUM_BYTES, "SHA256SUMS")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GeckoRuntimeCandidateError("SHA256SUMS is not UTF-8") from error
    if not text.endswith("\n"):
        raise GeckoRuntimeCandidateError("SHA256SUMS must end with one line feed")
    entries: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = _CHECKSUM_LINE_PATTERN.fullmatch(line)
        if match is None:
            raise GeckoRuntimeCandidateError(f"SHA256SUMS line {number} is malformed")
        digest, path = match.groups()
        if path in entries:
            raise GeckoRuntimeCandidateError(f"SHA256SUMS contains duplicate path {path}")
        entries[path] = digest
    for pin, label in (
        (lock.runtime_archive, "runtime archive"),
        (lock.source_archive, "source archive"),
    ):
        assert pin.checksum_path is not None
        if entries.get(pin.checksum_path) != pin.sha256:
            raise GeckoRuntimeCandidateError(
                f"signed SHA256SUMS does not authorize the pinned {label}"
            )


def _validated_tar_relative_name(name: str, root: str, label: str) -> tuple[str, ...]:
    if (
        not isinstance(name, str)
        or not name
        or name.startswith("/")
        or "\\" in name
        or len(name.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise GeckoRuntimeCandidateSafetyError(f"{label} contains an unsafe tar path")
    components = name.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise GeckoRuntimeCandidateSafetyError(f"{label} contains an unsafe tar path: {name}")
    if len(components) > MAX_PATH_DEPTH or components[0] != root or len(components) < 2:
        raise GeckoRuntimeCandidateSafetyError(f"{label} contains a path outside {root}: {name}")
    return tuple(components[1:])


def _open_tar_stream(descriptor: int, label: str) -> tuple[Any, tarfile.TarFile]:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        stream = os.fdopen(os.dup(descriptor), "rb", closefd=True)
        archive = tarfile.open(fileobj=stream, mode="r|*")
        return stream, archive
    except (OSError, tarfile.TarError) as error:
        raise GeckoRuntimeCandidateError(f"cannot open {label}: {error}") from error


def _validate_source_archive(lock: GeckoRuntimeSourceLock, descriptor: int) -> None:
    expected = {item.path: item for item in lock.source.metadata}
    observed: dict[str, bytes] = {}
    root_marker_seen = False
    member_count = 0
    stream, archive = _open_tar_stream(descriptor, "source archive")
    try:
        for member in archive:
            member_count += 1
            if member_count > MAX_TAR_MEMBERS:
                raise GeckoRuntimeCandidateSafetyError("source archive has too many members")
            if member.name == ".":
                if root_marker_seen or not member.isdir():
                    raise GeckoRuntimeCandidateSafetyError("source archive root marker is invalid")
                root_marker_seen = True
                continue
            relative = "/".join(
                _validated_tar_relative_name(
                    member.name, lock.source.archive_root, "source archive"
                )
            )
            if not (member.isdir() or member.isfile()):
                raise GeckoRuntimeCandidateSafetyError(
                    f"source archive contains a link or special member: {member.name}"
                )
            if member.isfile() and relative in expected:
                if relative in observed:
                    raise GeckoRuntimeCandidateSafetyError(
                        f"source archive repeats metadata path: {relative}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise GeckoRuntimeCandidateError(
                        f"cannot read source metadata member: {relative}"
                    )
                payload = source.read(expected[relative].size + 1)
                pin = expected[relative]
                if len(payload) != pin.size or member.size != pin.size:
                    raise GeckoRuntimeCandidateError(
                        f"source metadata size does not match: {relative}"
                    )
                if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), pin.sha256):
                    raise GeckoRuntimeCandidateError(
                        f"source metadata digest does not match: {relative}"
                    )
                observed[relative] = payload
    except (OSError, tarfile.TarError) as error:
        raise GeckoRuntimeCandidateError(f"cannot validate source archive: {error}") from error
    finally:
        archive.close()
        stream.close()
    if not root_marker_seen:
        raise GeckoRuntimeCandidateSafetyError("source archive has no safe root marker")
    if frozenset(observed) != _REQUIRED_SOURCE_METADATA:
        raise GeckoRuntimeCandidateError("source archive metadata authority is incomplete")
    try:
        source_stamp = observed["sourcestamp.txt"].decode("utf-8", errors="strict")
        version = observed["browser/config/version.txt"].decode("utf-8", errors="strict")
        version_display = observed["browser/config/version_display.txt"].decode(
            "utf-8", errors="strict"
        )
        milestone = observed["config/milestone.txt"].decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GeckoRuntimeCandidateError("source metadata is not UTF-8") from error
    expected_stamp = (
        f"{lock.candidate.build_id}\n"
        f"{lock.release.source_repository}/rev/{lock.release.source_stamp}"
    )
    milestone_values = [
        line.strip()
        for line in milestone.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if (
        source_stamp != expected_stamp
        or version != f"{lock.candidate.version}\n"
        or version_display != f"{lock.candidate.version}\n"
        or milestone_values != [lock.candidate.version]
    ):
        raise GeckoRuntimeCandidateError(
            "source archive metadata does not bind the locked build/revision/version"
        )


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
    root_fd: int,
    components: Sequence[str],
    created: set[str],
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
                    raise GeckoRuntimeCandidateSafetyError(
                        f"runtime archive path collides while creating {rendered}"
                    ) from error
                created.add(rendered)
            child = os.open(component, directory_flags, dir_fd=current)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                os.close(child)
                raise GeckoRuntimeCandidateSafetyError(
                    f"created runtime directory has unsafe identity: {rendered}"
                )
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _normalize_directory(root_fd: int, components: Sequence[str]) -> None:
    descriptor = _open_relative_directory(root_fd, components)
    try:
        os.fchmod(descriptor, 0o755)
        os.utime(descriptor, ns=(0, 0))
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o755 or info.st_mtime_ns != 0:
            raise GeckoRuntimeCandidateSafetyError("candidate directory normalization failed")
    finally:
        os.close(descriptor)


def _write_tar_file(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    parent_fd: int,
    name: str,
    mode: int,
) -> tuple[str, bytes | None]:
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if close_on_exec is None or nofollow is None:
        raise GeckoRuntimeCandidateSafetyError("candidate extraction requires O_CLOEXEC/O_NOFOLLOW")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | close_on_exec | nofollow
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot exclusively create runtime member {member.name}: {error}"
        ) from error
    capture = bytearray() if member.name.endswith("/application.ini") else None
    digest = hashlib.sha256()
    total = 0
    try:
        source = archive.extractfile(member)
        if source is None:
            raise GeckoRuntimeCandidateError(f"cannot read runtime member {member.name}")
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > member.size:
                raise GeckoRuntimeCandidateError(
                    f"runtime member exceeds declared size: {member.name}"
                )
            digest.update(chunk)
            write_all(descriptor, chunk)
            if capture is not None:
                if len(capture) + len(chunk) > MAX_DESCRIPTOR_BYTES:
                    raise GeckoRuntimeCandidateError("runtime application.ini is oversized")
                capture.extend(chunk)
        if total != member.size:
            raise GeckoRuntimeCandidateError(
                f"runtime member size is incomplete: {member.name}"
            )
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
            raise GeckoRuntimeCandidateSafetyError(
                f"runtime member normalization failed: {member.name}"
            )
        return digest.hexdigest(), bytes(capture) if capture is not None else None
    finally:
        os.close(descriptor)


def _verify_runtime_application_ini(lock: GeckoRuntimeSourceLock, payload: bytes) -> None:
    try:
        text = payload.decode("utf-8", errors="strict")
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(text)
    except (UnicodeDecodeError, configparser.Error) as error:
        raise GeckoRuntimeCandidateError(f"runtime application.ini is invalid: {error}") from error
    expected = {
        "Name": lock.candidate.product,
        "Version": lock.candidate.version,
        "BuildID": lock.candidate.build_id,
        "SourceRepository": lock.release.source_repository,
        "SourceStamp": lock.release.source_stamp,
    }
    try:
        actual = {key: parser["App"][key] for key in expected}
        minimum = parser["Gecko"]["MinVersion"]
        maximum = parser["Gecko"]["MaxVersion"]
    except KeyError as error:
        raise GeckoRuntimeCandidateError("runtime application.ini identity is incomplete") from error
    if actual != expected or minimum != lock.candidate.version or maximum != lock.candidate.version:
        raise GeckoRuntimeCandidateError("runtime application.ini does not match the source lock")


def _extract_runtime_archive(
    lock: GeckoRuntimeSourceLock,
    archive_fd: int,
    candidate_root_fd: int,
) -> tuple[GeckoRuntimeTreeEntry, ...]:
    entries: list[GeckoRuntimeTreeEntry] = []
    seen: set[str] = set()
    created_directories: set[str] = set()
    total_bytes = 0
    application_ini: bytes | None = None
    stream, archive = _open_tar_stream(archive_fd, "runtime archive")
    try:
        for member in archive:
            if len(entries) >= lock.candidate.file_count:
                raise GeckoRuntimeCandidateSafetyError("runtime archive has too many members")
            relative_components = _validated_tar_relative_name(
                member.name, lock.candidate.archive_root, "runtime archive"
            )
            if not member.isfile():
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime archive contains a directory, link, or special member: {member.name}"
                )
            relative = "/".join(relative_components)
            if relative in seen:
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime archive repeats path: {relative}"
                )
            seen.add(relative)
            mode = member.mode & 0o777
            if mode not in (0o644, 0o755) or member.mode != mode:
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime member has unsupported mode: {member.name}"
                )
            if member.size < 0 or member.size > lock.candidate.total_bytes - total_bytes:
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime member size exceeds the locked tree: {member.name}"
                )
            parent_fd = _ensure_relative_directory(
                candidate_root_fd,
                (lock.candidate.archive_root, *relative_components[:-1]),
                created_directories,
            )
            try:
                digest, captured = _write_tar_file(
                    archive,
                    member,
                    parent_fd,
                    relative_components[-1],
                    mode,
                )
            finally:
                os.close(parent_fd)
            if captured is not None:
                if relative != "application.ini" or application_ini is not None:
                    raise GeckoRuntimeCandidateSafetyError(
                        "runtime application.ini authority is ambiguous"
                    )
                application_ini = captured
            entries.append(
                GeckoRuntimeTreeEntry(
                    path=relative,
                    size=member.size,
                    mode=f"{mode:04o}",
                    sha256=digest,
                )
            )
            total_bytes += member.size
    except (OSError, tarfile.TarError) as error:
        raise GeckoRuntimeCandidateError(f"cannot extract runtime archive: {error}") from error
    finally:
        archive.close()
        stream.close()
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    if len(ordered) != lock.candidate.file_count:
        raise GeckoRuntimeCandidateError("runtime archive file count does not match the source lock")
    if total_bytes != lock.candidate.total_bytes:
        raise GeckoRuntimeCandidateError("runtime archive byte count does not match the source lock")
    tree_digest = gecko_runtime_tree_sha256(ordered)
    if not hmac.compare_digest(tree_digest, lock.candidate.tree_sha256):
        raise GeckoRuntimeCandidateError("runtime archive canonical tree digest does not match")
    executable = next(
        (item for item in ordered if item.path == lock.candidate.executable.path), None
    )
    expected_executable = lock.candidate.executable
    if executable is None or (
        executable.mode,
        executable.size,
        executable.sha256,
    ) != (
        expected_executable.mode,
        expected_executable.size,
        expected_executable.sha256,
    ):
        raise GeckoRuntimeCandidateError("runtime executable does not match the source lock")
    if application_ini is None:
        raise GeckoRuntimeCandidateError("runtime archive has no application.ini")
    _verify_runtime_application_ini(lock, application_ini)
    for relative in sorted(created_directories, key=lambda item: (-item.count("/"), item)):
        _normalize_directory(candidate_root_fd, relative.split("/"))
    return ordered


def _require_directory(info: os.stat_result, label: str, *, normalized: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise GeckoRuntimeCandidateSafetyError(f"{label} is not a directory")
    try:
        _safe_owner(info, label)
    except RuntimeSafetyError as error:
        raise GeckoRuntimeCandidateSafetyError(str(error)) from error
    if stat.S_IMODE(info.st_mode) != 0o755:
        raise GeckoRuntimeCandidateSafetyError(f"{label} mode must be 0755")
    if normalized and info.st_mtime_ns != 0:
        raise GeckoRuntimeCandidateSafetyError(f"{label} mtime is not normalized")


def _open_absolute_directory(path: os.PathLike[str] | str, label: str) -> tuple[str, int, tuple[int, ...]]:
    try:
        rendered = os.fspath(path)
        if rendered == "/":
            normalized = "/"
        else:
            normalized = _absolute_normalized_path(path, label)
        _common, directory_flags = _platform_flags()
    except (TypeError, RuntimeSafetyError) as error:
        raise GeckoRuntimeCandidateSafetyError(str(error)) from error
    components = [] if normalized == "/" else normalized[1:].split("/")
    descriptors: list[int] = []
    try:
        current = os.open("/", directory_flags)
        descriptors.append(current)
        root = os.fstat(current)
        if not stat.S_ISDIR(root.st_mode):
            raise GeckoRuntimeCandidateSafetyError("filesystem root is not a directory")
        _safe_owner(root, f"{label} ancestor /")
        traversed = ""
        for component in components:
            traversed += f"/{component}"
            try:
                child = os.open(component, directory_flags, dir_fd=current)
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise GeckoRuntimeCandidateSafetyError(
                        f"{label} is or traverses a symbolic link"
                    ) from error
                raise GeckoRuntimeCandidateSafetyError(
                    f"cannot open {label} ancestor {traversed}: {error}"
                ) from error
            descriptors.append(child)
            current = child
            info = os.fstat(current)
            if not stat.S_ISDIR(info.st_mode):
                raise GeckoRuntimeCandidateSafetyError(
                    f"{label} ancestor is not a directory: {traversed}"
                )
            _safe_owner(info, f"{label} ancestor {traversed}")
        descriptor = descriptors.pop()
        return normalized, descriptor, _stat_identity(os.fstat(descriptor))
    except RuntimeSafetyError as error:
        raise GeckoRuntimeCandidateSafetyError(str(error)) from error
    finally:
        for descriptor_to_close in reversed(descriptors):
            os.close(descriptor_to_close)


def _read_candidate_descriptor(root_fd: int) -> tuple[GeckoRuntimeCandidateDescriptor, tuple[int, ...]]:
    common_flags, _directory_flags = _platform_flags()
    try:
        descriptor = os.open(GECKO_RUNTIME_CANDIDATE_DESCRIPTOR, common_flags, dir_fd=root_fd)
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot open candidate descriptor: {error}"
        ) from error
    try:
        initial = os.fstat(descriptor)
        try:
            _safe_owner(initial, "candidate descriptor")
        except RuntimeSafetyError as error:
            raise GeckoRuntimeCandidateSafetyError(str(error)) from error
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or stat.S_IMODE(initial.st_mode) != 0o644
            or initial.st_mtime_ns != 0
            or initial.st_size > MAX_DESCRIPTOR_BYTES
        ):
            raise GeckoRuntimeCandidateSafetyError(
                "candidate descriptor metadata is not normalized/safe"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = _read_fd_bytes(descriptor, MAX_DESCRIPTOR_BYTES, "candidate descriptor")
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final):
            raise GeckoRuntimeCandidateSafetyError("candidate descriptor changed while reading")
        parsed = gecko_runtime_candidate_descriptor_from_dict(
            _parse_json_bytes(payload, "candidate descriptor")
        )
        canonical = render_gecko_runtime_candidate_descriptor(parsed).encode("ascii")
        if not hmac.compare_digest(payload, canonical):
            raise GeckoRuntimeCandidateError("candidate descriptor is not canonical JSON")
        return parsed, _stat_identity(initial)
    finally:
        os.close(descriptor)


def _hash_runtime_file(
    directory_fd: int,
    name: str,
    relative: str,
    expected_total: int,
) -> tuple[GeckoRuntimeTreeEntry, bytes | None]:
    common_flags, _directory_flags = _platform_flags()
    try:
        descriptor = os.open(name, common_flags, dir_fd=directory_fd)
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot open runtime file {relative}: {error}"
        ) from error
    try:
        initial = os.fstat(descriptor)
        try:
            _safe_owner(initial, f"runtime file {relative}")
        except RuntimeSafetyError as error:
            raise GeckoRuntimeCandidateSafetyError(str(error)) from error
        mode = stat.S_IMODE(initial.st_mode)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or mode not in (0o644, 0o755)
            or initial.st_mtime_ns != 0
            or initial.st_size > expected_total
        ):
            raise GeckoRuntimeCandidateSafetyError(
                f"runtime file metadata is unsafe: {relative}"
            )
        digest = hashlib.sha256()
        total = 0
        capture = bytearray() if relative == "application.ini" else None
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > initial.st_size:
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime file grew while hashing: {relative}"
                )
            digest.update(chunk)
            if capture is not None:
                if len(capture) + len(chunk) > MAX_DESCRIPTOR_BYTES:
                    raise GeckoRuntimeCandidateError("runtime application.ini is oversized")
                capture.extend(chunk)
        final = os.fstat(descriptor)
        if _stat_identity(initial) != _stat_identity(final) or total != initial.st_size:
            raise GeckoRuntimeCandidateSafetyError(
                f"runtime file changed while hashing: {relative}"
            )
        return (
            GeckoRuntimeTreeEntry(
                path=relative,
                size=total,
                mode=f"{mode:04o}",
                sha256=digest.hexdigest(),
            ),
            bytes(capture) if capture is not None else None,
        )
    finally:
        os.close(descriptor)


def _verify_runtime_tree(
    runtime_fd: int,
    lock: GeckoRuntimeSourceLock,
) -> tuple[GeckoRuntimeTreeEntry, ...]:
    entries: list[GeckoRuntimeTreeEntry] = []
    observed_directories: set[str] = set()
    application_ini: bytes | None = None
    total_bytes = 0
    _common_flags, directory_flags = _platform_flags()

    def visit(directory_fd: int, relative_directory: str) -> None:
        nonlocal application_ini, total_bytes
        label = "runtime root" if not relative_directory else f"runtime directory {relative_directory}"
        initial = os.fstat(directory_fd)
        _require_directory(initial, label, normalized=True)
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise GeckoRuntimeCandidateSafetyError(f"cannot list {label}: {error}") from error
        if not all(isinstance(name, str) for name in names):
            raise GeckoRuntimeCandidateSafetyError(f"{label} contains a non-text entry")
        for name in sorted(names):
            if name in ("", ".", "..") or "/" in name or "\x00" in name:
                raise GeckoRuntimeCandidateSafetyError(f"{label} contains an unsafe entry")
            relative = f"{relative_directory}/{name}".strip("/")
            if len(relative.encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
                raise GeckoRuntimeCandidateSafetyError("runtime tree path is oversized")
            try:
                entry_info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError as error:
                raise GeckoRuntimeCandidateSafetyError(
                    f"cannot inspect runtime entry {relative}: {error}"
                ) from error
            if stat.S_ISDIR(entry_info.st_mode):
                if len(observed_directories) >= MAX_RUNTIME_DIRECTORIES:
                    raise GeckoRuntimeCandidateSafetyError("runtime tree has too many directories")
                try:
                    child = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError as error:
                    raise GeckoRuntimeCandidateSafetyError(
                        f"cannot open runtime directory {relative}: {error}"
                    ) from error
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (entry_info.st_dev, entry_info.st_ino):
                        raise GeckoRuntimeCandidateSafetyError(
                            f"runtime directory rebound while opening: {relative}"
                        )
                    observed_directories.add(relative)
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry_info.st_mode):
                if len(entries) >= lock.candidate.file_count:
                    raise GeckoRuntimeCandidateSafetyError("runtime tree has too many files")
                entry, captured = _hash_runtime_file(
                    directory_fd, name, relative, lock.candidate.total_bytes
                )
                entries.append(entry)
                total_bytes += entry.size
                if total_bytes > lock.candidate.total_bytes:
                    raise GeckoRuntimeCandidateSafetyError("runtime tree exceeds locked bytes")
                if captured is not None:
                    application_ini = captured
            else:
                raise GeckoRuntimeCandidateSafetyError(
                    f"runtime tree contains a link or special entry: {relative}"
                )
        final = os.fstat(directory_fd)
        if _stat_identity(initial) != _stat_identity(final):
            raise GeckoRuntimeCandidateSafetyError(f"{label} changed while verifying")

    visit(runtime_fd, "")
    ordered = tuple(sorted(entries, key=lambda item: item.path))
    implied_directories: set[str] = set()
    for entry in ordered:
        components = entry.path.split("/")[:-1]
        for index in range(1, len(components) + 1):
            implied_directories.add("/".join(components[:index]))
    if observed_directories != implied_directories:
        raise GeckoRuntimeCandidateSafetyError(
            "runtime tree contains an empty or unrepresented directory"
        )
    if len(ordered) != lock.candidate.file_count or total_bytes != lock.candidate.total_bytes:
        raise GeckoRuntimeCandidateError("runtime tree count/size does not match the source lock")
    actual_tree = gecko_runtime_tree_sha256(ordered)
    if not hmac.compare_digest(actual_tree, lock.candidate.tree_sha256):
        raise GeckoRuntimeCandidateError("runtime tree digest does not match the source lock")
    executable = next(
        (item for item in ordered if item.path == lock.candidate.executable.path), None
    )
    pin = lock.candidate.executable
    if executable is None or (executable.mode, executable.size, executable.sha256) != (
        pin.mode,
        pin.size,
        pin.sha256,
    ):
        raise GeckoRuntimeCandidateError("candidate runtime executable does not match")
    if application_ini is None:
        raise GeckoRuntimeCandidateError("candidate runtime has no application.ini")
    _verify_runtime_application_ini(lock, application_ini)
    return ordered


@contextmanager
def open_verified_gecko_runtime_candidate(
    candidate_path: os.PathLike[str] | str,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> Iterator[OpenVerifiedGeckoRuntimeCandidate]:
    """Open, hash, and retain an exact candidate distribution root."""
    lock_path = (
        bundled_gecko_runtime_source_lock_path()
        if source_lock_path is None
        else os.fspath(source_lock_path)
    )
    lock = (
        load_bundled_gecko_runtime_source_lock()
        if source_lock_path is None
        else load_gecko_runtime_source_lock(lock_path)
    )
    normalized, root_fd, root_identity = _open_absolute_directory(
        candidate_path, "Gecko runtime candidate"
    )
    runtime_fd = -1
    try:
        root_info = os.fstat(root_fd)
        _require_directory(root_info, "Gecko runtime candidate root", normalized=True)
        entries = frozenset(os.listdir(root_fd))
        expected_entries = frozenset(
            {GECKO_RUNTIME_CANDIDATE_DESCRIPTOR, lock.candidate.archive_root}
        )
        if entries != expected_entries:
            raise GeckoRuntimeCandidateSafetyError(
                "candidate root does not contain the exact descriptor/runtime entries"
            )
        descriptor, _descriptor_identity = _read_candidate_descriptor(root_fd)
        expected_descriptor = _expected_candidate_descriptor(lock)
        if descriptor != expected_descriptor:
            raise GeckoRuntimeCandidateError(
                "candidate descriptor does not match the exact source lock"
            )
        _common_flags, directory_flags = _platform_flags()
        try:
            runtime_fd = os.open(
                lock.candidate.archive_root,
                directory_flags,
                dir_fd=root_fd,
            )
        except OSError as error:
            raise GeckoRuntimeCandidateSafetyError(
                f"cannot open candidate runtime root: {error}"
            ) from error
        runtime_info = os.fstat(runtime_fd)
        _require_directory(runtime_info, "candidate runtime root", normalized=True)
        files = _verify_runtime_tree(runtime_fd, lock)
        final_root = os.fstat(root_fd)
        if _stat_identity(root_info) != _stat_identity(final_root):
            raise GeckoRuntimeCandidateSafetyError(
                "candidate root changed while verifying"
            )
        yield OpenVerifiedGeckoRuntimeCandidate(
            logical_path=normalized,
            root_descriptor=root_fd,
            runtime_descriptor=runtime_fd,
            root_identity=root_identity,
            runtime_identity=_stat_identity(runtime_info),
            descriptor=descriptor,
            files=files,
        )
    finally:
        if runtime_fd >= 0:
            os.close(runtime_fd)
        os.close(root_fd)


def revalidate_open_gecko_runtime_candidate(
    opened: OpenVerifiedGeckoRuntimeCandidate,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> None:
    """Re-hash retained authority and reject logical-path or entry rebinding."""
    if not isinstance(opened, OpenVerifiedGeckoRuntimeCandidate):
        raise GeckoRuntimeCandidateError("open Gecko runtime candidate authority was fabricated")
    lock = (
        load_bundled_gecko_runtime_source_lock()
        if source_lock_path is None
        else load_gecko_runtime_source_lock(source_lock_path)
    )
    try:
        root_info = os.fstat(opened.root_descriptor)
        runtime_info = os.fstat(opened.runtime_descriptor)
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot revalidate retained candidate descriptors: {error}"
        ) from error
    if _stat_identity(root_info) != opened.root_identity:
        raise GeckoRuntimeCandidateSafetyError("retained candidate root changed")
    if _stat_identity(runtime_info) != opened.runtime_identity:
        raise GeckoRuntimeCandidateSafetyError("retained runtime root changed")
    logical_fd = -1
    try:
        _normalized, logical_fd, logical_identity = _open_absolute_directory(
            opened.logical_path, "candidate logical path"
        )
        if logical_identity != opened.root_identity:
            raise GeckoRuntimeCandidateSafetyError("candidate logical path was rebound")
    finally:
        if logical_fd >= 0:
            os.close(logical_fd)
    entry = os.stat(
        lock.candidate.archive_root,
        dir_fd=opened.root_descriptor,
        follow_symlinks=False,
    )
    if (entry.st_dev, entry.st_ino) != (runtime_info.st_dev, runtime_info.st_ino):
        raise GeckoRuntimeCandidateSafetyError("candidate runtime directory entry was rebound")
    descriptor, _identity = _read_candidate_descriptor(opened.root_descriptor)
    if descriptor != opened.descriptor or descriptor != _expected_candidate_descriptor(lock):
        raise GeckoRuntimeCandidateError("candidate descriptor changed after verification")
    files = _verify_runtime_tree(opened.runtime_descriptor, lock)
    if files != opened.files:
        raise GeckoRuntimeCandidateError("candidate runtime tree changed after verification")


def gecko_runtime_candidate_executable_path(
    opened: OpenVerifiedGeckoRuntimeCandidate,
    *,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> str:
    """Return an fd-relative executable path after immediate full revalidation."""
    revalidate_open_gecko_runtime_candidate(
        opened, source_lock_path=source_lock_path
    )
    return f"/proc/self/fd/{opened.runtime_descriptor}/{opened.descriptor.executable}"


def _write_candidate_descriptor(root_fd: int, descriptor: GeckoRuntimeCandidateDescriptor) -> None:
    payload = render_gecko_runtime_candidate_descriptor(descriptor).encode("ascii")
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if close_on_exec is None or nofollow is None:
        raise GeckoRuntimeCandidateSafetyError("candidate publication requires no-follow flags")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | close_on_exec | nofollow
    file_fd = os.open(GECKO_RUNTIME_CANDIDATE_DESCRIPTOR, flags, 0o600, dir_fd=root_fd)
    try:
        write_all(file_fd, payload)
        os.fchmod(file_fd, 0o644)
        os.utime(file_fd, ns=(0, 0))
        os.fsync(file_fd)
    finally:
        os.close(file_fd)


def _remove_directory_contents(directory_fd: int) -> None:
    _common_flags, directory_flags = _platform_flags()
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


def assemble_gecko_runtime_candidate(
    *,
    checksums_path: os.PathLike[str] | str,
    checksums_signature_path: os.PathLike[str] | str,
    signing_key_path: os.PathLike[str] | str,
    runtime_archive_path: os.PathLike[str] | str,
    source_archive_path: os.PathLike[str] | str,
    output_path: os.PathLike[str] | str,
    source_lock_path: os.PathLike[str] | str | None = None,
) -> GeckoRuntimeCandidateDescriptor:
    """Assemble one archive-exact candidate without network or overwrite."""
    lock_path = (
        bundled_gecko_runtime_source_lock_path()
        if source_lock_path is None
        else _absolute_normalized_path(source_lock_path, "Gecko source lock")
    )
    lock = (
        load_bundled_gecko_runtime_source_lock()
        if source_lock_path is None
        else load_gecko_runtime_source_lock(lock_path)
    )
    output = _absolute_normalized_path(output_path, "candidate output")
    output_parent, output_name = output.rsplit("/", 1)
    if not output_parent:
        output_parent = "/"
    _parent_path, parent_fd, _parent_identity = _open_absolute_directory(
        output_parent, "candidate output parent"
    )
    staging_name = f".gecko-candidate-{secrets.token_hex(16)}"
    staging_fd = -1
    published = False
    try:
        try:
            os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise GeckoRuntimeCandidateSafetyError("candidate output already exists")
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        _common_flags, directory_flags = _platform_flags()
        staging_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
        staging_path = f"{output_parent.rstrip('/')}/{staging_name}" or f"/{staging_name}"

        with ExitStack() as stack:
            checksums = stack.enter_context(
                _open_pinned_input(checksums_path, lock.checksums, "Mozilla SHA256SUMS")
            )
            signature = stack.enter_context(
                _open_pinned_input(
                    checksums_signature_path,
                    lock.checksums_signature,
                    "Mozilla SHA256SUMS signature",
                )
            )
            signing_key = stack.enter_context(
                _open_pinned_input(signing_key_path, lock.signing_key, "Mozilla signing key")
            )
            runtime_archive = stack.enter_context(
                _open_pinned_input(
                    runtime_archive_path, lock.runtime_archive, "Firefox runtime archive"
                )
            )
            source_archive = stack.enter_context(
                _open_pinned_input(
                    source_archive_path, lock.source_archive, "Firefox source archive"
                )
            )
            opened_inputs = (
                checksums,
                signature,
                signing_key,
                runtime_archive,
                source_archive,
            )
            _verify_signed_checksums(
                lock,
                signing_key.descriptor,
                signature.descriptor,
                checksums.descriptor,
            )
            _verify_checksum_authority(lock, checksums.descriptor)
            _validate_source_archive(lock, source_archive.descriptor)
            _extract_runtime_archive(lock, runtime_archive.descriptor, staging_fd)
            candidate_descriptor = _expected_candidate_descriptor(lock)
            _write_candidate_descriptor(staging_fd, candidate_descriptor)
            os.fchmod(staging_fd, 0o755)
            os.utime(staging_fd, ns=(0, 0))
            os.fsync(staging_fd)
            for opened_input in opened_inputs:
                _revalidate_opened_input(opened_input)

            with open_verified_gecko_runtime_candidate(
                staging_path,
                source_lock_path=lock_path,
            ) as opened:
                if opened.descriptor != candidate_descriptor:
                    raise GeckoRuntimeCandidateError(
                        "staged candidate descriptor changed before publication"
                    )

            rename_noreplace(parent_fd, staging_name, output_name)
            published = True
            os.fsync(parent_fd)

        with open_verified_gecko_runtime_candidate(
            output,
            source_lock_path=lock_path,
        ) as opened:
            if opened.descriptor != candidate_descriptor:
                raise GeckoRuntimeCandidateError(
                    "published candidate descriptor changed"
                )
        return candidate_descriptor
    except (GeckoRuntimeCandidateError, RuntimeSafetyError):
        raise
    except OSError as error:
        raise GeckoRuntimeCandidateSafetyError(
            f"cannot assemble Gecko runtime candidate: {error}"
        ) from error
    finally:
        primary_active = sys.exc_info()[0] is not None
        if staging_fd >= 0:
            if not published:
                try:
                    _remove_directory_contents(staging_fd)
                except OSError:
                    if not primary_active:
                        raise
            os.close(staging_fd)
        if not published:
            try:
                os.rmdir(staging_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                if not primary_active:
                    raise
        os.close(parent_fd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m zen_app_runtime.gecko_runtime_candidate",
        description="Offline assembly and verification of the pinned Gecko candidate",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--source-lock", default=bundled_gecko_runtime_source_lock_path())
    assemble.add_argument("--checksums", required=True)
    assemble.add_argument("--checksums-signature", required=True)
    assemble.add_argument("--signing-key", required=True)
    assemble.add_argument("--runtime-archive", required=True)
    assemble.add_argument("--source-archive", required=True)
    assemble.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--source-lock", default=bundled_gecko_runtime_source_lock_path())
    verify.add_argument("--candidate", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "assemble":
            descriptor = assemble_gecko_runtime_candidate(
                source_lock_path=arguments.source_lock,
                checksums_path=arguments.checksums,
                checksums_signature_path=arguments.checksums_signature,
                signing_key_path=arguments.signing_key,
                runtime_archive_path=arguments.runtime_archive,
                source_archive_path=arguments.source_archive,
                output_path=arguments.output,
            )
        else:
            with open_verified_gecko_runtime_candidate(
                arguments.candidate,
                source_lock_path=arguments.source_lock,
            ) as opened:
                descriptor = opened.descriptor
        sys.stdout.write(render_gecko_runtime_candidate_descriptor(descriptor))
        return 0
    except GeckoRuntimeCandidateError as error:
        print(f"gecko runtime candidate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
