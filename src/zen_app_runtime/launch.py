"""Verified, supervised launch of an explicitly pinned local runtime.

Inspection is read-only and deterministic for identical manifest, lock, profile,
and environment inputs. Execution requires explicit confirmation, holds an
exclusive profile lock, and executes the exact descriptor that was verified.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterator, Literal, Mapping

from ._secure_fs import same_inode
from .app_shell import (
    MAX_HANDSHAKE_BYTES,
    AppShellError,
    AppShellPolicy,
    app_shell_policy_sha256,
    build_app_shell_policy,
    load_adapter_capability,
    parse_app_shell_handshake_payload,
    render_app_shell_policy,
)
from .browser_adapter import (
    BrowserAdapterError,
    OpenVerifiedBrowserAdapter,
    browser_adapter_application_path,
    bundled_adapter_capability,
    open_bundled_browser_adapter,
    revalidate_open_browser_adapter,
)
from .manifest import APP_ID_PATTERN, AppManifest
from .profile import (
    OpenVerifiedProfile,
    ProfileError,
    open_verified_profile_binding,
    revalidate_open_profile_binding,
)
from .runtime import (
    RuntimeSafetyError,
    _absolute_normalized_path,
    _create_memfd,
    _platform_flags,
    reverify_open_runtime,
    runtime_identity_to_dict,
)

LAUNCH_TICKET_VERSION = 1
DISPLAY_LAUNCH_TICKET_VERSION = 2
DISPLAY_BACKENDS = ("auto", "wayland", "x11")
DisplayBackend = Literal["auto", "wayland", "x11"]
ResolvedDisplayBackend = Literal["wayland", "x11"]
PROFILE_LOCK_NAME = ".zen-app-runtime-launch.lock"
RUNTIME_STATE_DIRECTORY = ".zen-app-runtime"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FIXED_ENVIRONMENT_PATHS = {
    "HOME": "home",
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "data",
    "XDG_STATE_HOME": "state",
    "TMPDIR": "tmp",
}
_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "AT_SPI_BUS_ADDRESS",
        "DBUS_SESSION_BUS_ADDRESS",
        "DESKTOP_STARTUP_ID",
        "DISPLAY",
        "GDMSESSION",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "PIPEWIRE_REMOTE",
        "PULSE_SERVER",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_ACTIVATION_TOKEN",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_DESKTOP",
        "XDG_SESSION_TYPE",
    }
)
_FORWARDED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)
_GROUP_TERM_SECONDS = 2.0
_GROUP_KILL_SECONDS = 5.0
_GROUP_POLL_SECONDS = 0.02
_APP_SHELL_HANDSHAKE_SECONDS = 5.0
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_F_SEAL_SEAL = getattr(fcntl, "F_SEAL_SEAL", 0x0001)
_F_SEAL_SHRINK = getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
_F_SEAL_GROW = getattr(fcntl, "F_SEAL_GROW", 0x0004)
_F_SEAL_WRITE = getattr(fcntl, "F_SEAL_WRITE", 0x0008)
_APP_SHELL_POLICY_SEALS = (
    _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
)


class LaunchError(RuntimeError):
    """Base error for launch inspection and execution failures."""


class LaunchSafetyError(LaunchError):
    """A launch input or managed filesystem object failed closed validation."""


class LaunchBusyError(LaunchError):
    """The app profile is already held by another supervised launch."""


class LaunchExecutionError(LaunchError):
    """The verified runtime could not be spawned or supervised safely."""


@dataclass(frozen=True)
class LaunchTicket:
    launch_ticket_version: int
    app_id: str
    manifest_schema_version: int
    manifest_sha256: str
    runtime_identity: Any
    profile_path: str
    start_url: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DisplayLaunchTicket:
    launch_ticket_version: int
    app_id: str
    manifest_schema_version: int
    manifest_sha256: str
    runtime_identity: Any
    profile_path: str
    start_url: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    requested_display_backend: DisplayBackend
    resolved_display_backend: ResolvedDisplayBackend


LaunchTicketModel = LaunchTicket | DisplayLaunchTicket


def _absolute_path(value: os.PathLike[str] | str, label: str) -> str:
    try:
        return _absolute_normalized_path(value, label)
    except RuntimeSafetyError as error:
        raise LaunchSafetyError(str(error)) from error


def _environment_for_profile(
    profile_path: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source_environment = os.environ if source is None else source
    state_root = f"{profile_path}/{RUNTIME_STATE_DIRECTORY}"
    environment = {
        name: f"{state_root}/{relative}"
        for name, relative in _FIXED_ENVIRONMENT_PATHS.items()
    }
    environment["PATH"] = SAFE_PATH
    for name, value in source_environment.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise LaunchSafetyError("environment names and values must be text")
        if name in _ALLOWED_ENVIRONMENT_NAMES or name.startswith("LC_"):
            if "\x00" in name or "=" in name or "\x00" in value:
                raise LaunchSafetyError(f"allowed environment variable is invalid: {name!r}")
            environment[name] = value
    return dict(sorted(environment.items()))


def _environment_snapshot(source: Mapping[str, str] | None) -> dict[str, str]:
    source_environment = os.environ if source is None else source
    try:
        items = tuple(source_environment.items())
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise LaunchSafetyError("environment must be a stable text mapping") from error
    snapshot: dict[str, str] = {}
    for name, value in items:
        if not isinstance(name, str) or not isinstance(value, str):
            raise LaunchSafetyError("environment names and values must be text")
        if not name or "=" in name or "\x00" in name or "\x00" in value:
            raise LaunchSafetyError("environment contains an invalid name or value")
        snapshot[name] = value
    return snapshot


def _display_value(snapshot: Mapping[str, str], name: str) -> str | None:
    value = snapshot.get(name)
    if value is None:
        return None
    if (
        not value
        or len(value) > 4096
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        raise LaunchSafetyError(f"{name} is not a valid display-session value")
    return value


def _wayland_declaration(snapshot: Mapping[str, str]) -> bool:
    display = _display_value(snapshot, "WAYLAND_DISPLAY")
    runtime_dir = _display_value(snapshot, "XDG_RUNTIME_DIR")
    if display is None:
        return False
    if runtime_dir is None:
        raise LaunchSafetyError("Wayland requires XDG_RUNTIME_DIR")
    _absolute_path(runtime_dir, "XDG_RUNTIME_DIR")
    if display in (".", "..") or "/" in display or not re.fullmatch(r"[A-Za-z0-9._-]+", display):
        raise LaunchSafetyError("WAYLAND_DISPLAY must be a safe socket basename")
    return True


def _x11_declaration(snapshot: Mapping[str, str]) -> bool:
    display = _display_value(snapshot, "DISPLAY")
    if display is None:
        return False
    authority = _display_value(snapshot, "XAUTHORITY")
    if authority is not None:
        _absolute_path(authority, "XAUTHORITY")
    runtime_dir = _display_value(snapshot, "XDG_RUNTIME_DIR")
    if runtime_dir is not None:
        _absolute_path(runtime_dir, "XDG_RUNTIME_DIR")
    return True


def _resolve_display_backend(
    requested: str,
    snapshot: Mapping[str, str],
) -> ResolvedDisplayBackend:
    if requested not in DISPLAY_BACKENDS:
        raise LaunchSafetyError(
            f"display backend must be one of {list(DISPLAY_BACKENDS)}"
        )
    if requested == "wayland":
        if not _wayland_declaration(snapshot):
            raise LaunchSafetyError("Wayland backend requires WAYLAND_DISPLAY")
        return "wayland"
    if requested == "x11":
        if not _x11_declaration(snapshot):
            raise LaunchSafetyError("X11 backend requires DISPLAY")
        return "x11"
    if "WAYLAND_DISPLAY" in snapshot:
        if _wayland_declaration(snapshot):
            return "wayland"
    if "DISPLAY" in snapshot:
        if _x11_declaration(snapshot):
            return "x11"
    raise LaunchSafetyError("auto display backend requires a valid Wayland or X11 session")


def resolve_display_backend(
    requested: str,
    environment: Mapping[str, str] | None = None,
) -> ResolvedDisplayBackend:
    """Resolve an explicit display policy from one stable environment snapshot."""
    return _resolve_display_backend(requested, _environment_snapshot(environment))


def _environment_for_display_backend(
    profile_path: str,
    requested: str,
    snapshot: Mapping[str, str],
) -> tuple[ResolvedDisplayBackend, dict[str, str]]:
    resolved = _resolve_display_backend(requested, snapshot)
    environment = _environment_for_profile(profile_path, snapshot)
    if resolved == "wayland":
        environment.pop("DISPLAY", None)
        environment.pop("XAUTHORITY", None)
        environment["GDK_BACKEND"] = "wayland"
        environment["MOZ_ENABLE_WAYLAND"] = "1"
    else:
        environment.pop("WAYLAND_DISPLAY", None)
        environment["GDK_BACKEND"] = "x11"
        environment.pop("MOZ_ENABLE_WAYLAND", None)
    return resolved, dict(sorted(environment.items()))


def _ticket_from_opened_profile(
    manifest: AppManifest,
    opened: OpenVerifiedProfile,
    source_environment: Mapping[str, str] | None,
    display_backend: str | None,
) -> LaunchTicketModel:
    profile_path = _absolute_path(opened.binding.path, "profile")
    runtime_path = opened.runtime.identity.path
    argv = (
        runtime_path,
        "--no-remote",
        "--new-instance",
        "--profile",
        profile_path,
        manifest.start_url,
    )
    snapshot = _environment_snapshot(source_environment)
    common = {
        "app_id": manifest.app_id,
        "manifest_schema_version": manifest.schema_version,
        "manifest_sha256": opened.binding.manifest_sha256,
        "runtime_identity": opened.runtime.identity,
        "profile_path": profile_path,
        "start_url": manifest.start_url,
        "argv": argv,
    }
    if display_backend is None:
        return LaunchTicket(
            launch_ticket_version=LAUNCH_TICKET_VERSION,
            environment=tuple(_environment_for_profile(profile_path, snapshot).items()),
            **common,
        )
    resolved, environment = _environment_for_display_backend(
        profile_path,
        display_backend,
        snapshot,
    )
    return DisplayLaunchTicket(
        launch_ticket_version=DISPLAY_LAUNCH_TICKET_VERSION,
        environment=tuple(environment.items()),
        requested_display_backend=display_backend,
        resolved_display_backend=resolved,
        **common,
    )


def _ticket_common_to_dict(
    ticket: LaunchTicketModel,
    expected_version: int,
    extra_environment_names: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if (
        type(ticket.launch_ticket_version) is not int
        or ticket.launch_ticket_version != expected_version
    ):
        raise LaunchSafetyError(f"launchTicketVersion must be {expected_version}")
    if not isinstance(ticket.app_id, str) or not APP_ID_PATTERN.fullmatch(ticket.app_id):
        raise LaunchSafetyError("launch ticket app ID is unsafe")
    if type(ticket.manifest_schema_version) is not int or ticket.manifest_schema_version != 1:
        raise LaunchSafetyError("launch ticket manifest schema version must be 1")
    if not isinstance(ticket.manifest_sha256, str) or not _SHA256_PATTERN.fullmatch(ticket.manifest_sha256):
        raise LaunchSafetyError("launch ticket manifest sha256 is invalid")
    profile_path = _absolute_path(ticket.profile_path, "profile")
    if profile_path.rsplit("/", 1)[-1] != ticket.app_id:
        raise LaunchSafetyError("launch ticket profile path/app ID mismatch")
    try:
        runtime_identity = runtime_identity_to_dict(ticket.runtime_identity)
    except RuntimeSafetyError as error:
        raise LaunchSafetyError(f"launch ticket runtime identity is invalid: {error}") from error
    expected_argv_prefix = (
        runtime_identity["path"],
        "--no-remote",
        "--new-instance",
        "--profile",
        profile_path,
    )
    if (
        not isinstance(ticket.argv, tuple)
        or len(ticket.argv) != 6
        or ticket.argv[:5] != expected_argv_prefix
        or not isinstance(ticket.start_url, str)
        or ticket.argv[5] != ticket.start_url
        or any(ord(character) <= 32 or ord(character) == 127 for character in ticket.start_url)
        or "\\" in ticket.start_url
    ):
        raise LaunchSafetyError("launch ticket argv does not match the fixed runtime contract")
    if not isinstance(ticket.environment, tuple) or any(
        not isinstance(item, tuple) or len(item) != 2
        for item in ticket.environment
    ):
        raise LaunchSafetyError("launch ticket environment must be key/value pairs")
    environment = dict(ticket.environment)
    if len(environment) != len(ticket.environment):
        raise LaunchSafetyError("launch ticket environment contains duplicate names")
    if tuple(sorted(environment.items())) != ticket.environment:
        raise LaunchSafetyError("launch ticket environment must be sorted")
    for name, value in environment.items():
        if not isinstance(name, str) or not isinstance(value, str) or not name or "=" in name:
            raise LaunchSafetyError("launch ticket environment is invalid")
        if "\x00" in name or "\x00" in value:
            raise LaunchSafetyError("launch ticket environment cannot contain NUL")
        if (
            name not in _FIXED_ENVIRONMENT_PATHS
            and name != "PATH"
            and name not in _ALLOWED_ENVIRONMENT_NAMES
            and name not in extra_environment_names
            and not name.startswith("LC_")
        ):
            raise LaunchSafetyError(
                f"launch ticket environment variable is not allowlisted: {name}"
            )
    expected_fixed = _environment_for_profile(profile_path, {})
    for name, value in expected_fixed.items():
        if environment.get(name) != value:
            raise LaunchSafetyError(f"launch ticket environment has an invalid fixed value: {name}")
    return {
        "appId": ticket.app_id,
        "argv": list(ticket.argv),
        "environment": environment,
        "launchTicketVersion": ticket.launch_ticket_version,
        "manifest": {
            "schemaVersion": ticket.manifest_schema_version,
            "sha256": ticket.manifest_sha256,
        },
        "profilePath": profile_path,
        "runtimeIdentity": runtime_identity,
    }


def _display_launch_ticket_to_dict(ticket: DisplayLaunchTicket) -> dict[str, Any]:
    data = _ticket_common_to_dict(
        ticket,
        DISPLAY_LAUNCH_TICKET_VERSION,
        frozenset({"GDK_BACKEND", "MOZ_ENABLE_WAYLAND"}),
    )
    if ticket.requested_display_backend not in DISPLAY_BACKENDS:
        raise LaunchSafetyError("launch ticket requested display backend is invalid")
    if ticket.resolved_display_backend not in ("wayland", "x11"):
        raise LaunchSafetyError("launch ticket resolved display backend is invalid")
    expected_resolved, expected_environment = _environment_for_display_backend(
        ticket.profile_path,
        ticket.requested_display_backend,
        data["environment"],
    )
    if expected_resolved != ticket.resolved_display_backend:
        raise LaunchSafetyError("launch ticket display backend resolution mismatch")
    if tuple(expected_environment.items()) != ticket.environment:
        raise LaunchSafetyError("launch ticket display environment policy mismatch")
    data["displayBackend"] = {
        "requested": ticket.requested_display_backend,
        "resolved": ticket.resolved_display_backend,
    }
    return data


def launch_ticket_to_dict(ticket: LaunchTicketModel) -> dict[str, Any]:
    if type(ticket) is LaunchTicket:
        return _ticket_common_to_dict(ticket, LAUNCH_TICKET_VERSION)
    if type(ticket) is DisplayLaunchTicket:
        return _display_launch_ticket_to_dict(ticket)
    raise LaunchSafetyError("unsupported launch ticket model")


def render_launch_ticket(ticket: LaunchTicketModel) -> str:
    return json.dumps(launch_ticket_to_dict(ticket), indent=2, sort_keys=True) + "\n"


def inspect_launch(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    *,
    environment: Mapping[str, str] | None = None,
    display_backend: str | None = None,
) -> LaunchTicketModel:
    """Build a read-only launch ticket from current verified state."""
    try:
        with open_verified_profile_binding(profile_root, manifest, runtime_lock) as opened:
            return _ticket_from_opened_profile(
                manifest,
                opened,
                environment,
                display_backend,
            )
    except ProfileError as error:
        raise LaunchSafetyError(str(error)) from error


def _require_managed_directory(info: os.stat_result, label: str) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise LaunchSafetyError(f"{label} is not a directory")
    if info.st_uid != os.geteuid():
        raise LaunchSafetyError(f"{label} must be owned by the effective user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise LaunchSafetyError(f"{label} must have mode 0700")


def _open_or_create_managed_directory(parent_fd: int, name: str, label: str) -> int:
    try:
        _, directory_flags = _platform_flags()
    except RuntimeSafetyError as error:
        raise LaunchSafetyError(str(error)) from error
    created = False
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(name, directory_flags, dir_fd=parent_fd)
        if created:
            os.fchmod(descriptor, 0o700)
        held_info = os.fstat(descriptor)
        current_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not same_inode(held_info, current_info):
            raise LaunchSafetyError(f"{label} directory entry changed")
        _require_managed_directory(held_info, label)
        return descriptor
    except LaunchError:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except OSError as error:
        if "descriptor" in locals():
            try:
                os.close(descriptor)
            except OSError:
                pass
        if error.errno == errno.ELOOP:
            raise LaunchSafetyError(f"{label} is a symbolic link") from error
        raise LaunchSafetyError(f"cannot prepare {label}: {error}") from error


def _ensure_runtime_directories(profile_fd: int) -> None:
    state_fd = _open_or_create_managed_directory(
        profile_fd,
        RUNTIME_STATE_DIRECTORY,
        "managed runtime state",
    )
    try:
        for relative in _FIXED_ENVIRONMENT_PATHS.values():
            child_fd = _open_or_create_managed_directory(
                state_fd,
                relative,
                f"managed runtime {relative}",
            )
            try:
                pass
            finally:
                os.close(child_fd)
        os.fsync(state_fd)
        os.fsync(profile_fd)
    except LaunchError:
        raise
    except OSError as error:
        raise LaunchSafetyError(f"cannot synchronize managed runtime directories: {error}") from error
    finally:
        primary_active = sys.exc_info()[0] is not None
        try:
            os.close(state_fd)
        except OSError as error:
            if not primary_active:
                raise LaunchSafetyError(
                    f"managed runtime state descriptor could not be closed: {error}"
                ) from error


def _require_lock_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise LaunchSafetyError("profile launch lock is not a regular file")
    if info.st_uid != os.geteuid():
        raise LaunchSafetyError("profile launch lock must be owned by the effective user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise LaunchSafetyError("profile launch lock must have mode 0600")
    if info.st_nlink != 1:
        raise LaunchSafetyError("profile launch lock must have exactly one hard link")


def _require_active_profile_lock(profile_fd: int, descriptor: int) -> None:
    try:
        held_info = os.fstat(descriptor)
        current_info = os.stat(
            PROFILE_LOCK_NAME,
            dir_fd=profile_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise LaunchSafetyError(f"cannot revalidate profile launch lock: {error}") from error
    _require_lock_file(held_info)
    if not same_inode(held_info, current_info):
        raise LaunchSafetyError("profile launch lock entry changed while held")
    _require_lock_file(current_info)


@contextmanager
def _acquire_profile_lock(profile_fd: int) -> Iterator[int]:
    try:
        common_flags, _ = _platform_flags()
    except RuntimeSafetyError as error:
        raise LaunchSafetyError(str(error)) from error
    flags = (common_flags & ~os.O_RDONLY) | os.O_RDWR
    descriptor: int | None = None
    acquired = False
    try:
        try:
            descriptor = os.open(
                PROFILE_LOCK_NAME,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=profile_fd,
            )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            os.fsync(profile_fd)
        except FileExistsError:
            descriptor = os.open(PROFILE_LOCK_NAME, flags, dir_fd=profile_fd)
        held_info = os.fstat(descriptor)
        current_info = os.stat(PROFILE_LOCK_NAME, dir_fd=profile_fd, follow_symlinks=False)
        if not same_inode(held_info, current_info):
            raise LaunchSafetyError("profile launch lock entry changed")
        _require_lock_file(held_info)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise LaunchBusyError("app profile is already running") from error
            raise LaunchSafetyError(f"cannot lock app profile: {error}") from error
        acquired = True
        _require_active_profile_lock(profile_fd, descriptor)
        yield descriptor
    except LaunchError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LaunchSafetyError("profile launch lock is a symbolic link") from error
        raise LaunchSafetyError(f"cannot open profile launch lock: {error}") from error
    finally:
        primary_active = sys.exc_info()[0] is not None
        cleanup_errors: list[OSError] = []
        if descriptor is not None:
            if acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError as error:
                    cleanup_errors.append(error)
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        if cleanup_errors and not primary_active:
            detail = "; ".join(str(error) for error in cleanup_errors)
            raise LaunchExecutionError(f"profile lock cleanup failed: {detail}") from cleanup_errors[0]


def _verified_proc_executable(opened: OpenVerifiedProfile) -> str:
    try:
        reverify_open_runtime(opened.runtime)
    except RuntimeSafetyError as error:
        raise LaunchExecutionError(f"runtime re-verification failed: {error}") from error
    proc_path = f"/proc/self/fd/{opened.runtime.descriptor}"
    try:
        held_info = os.fstat(opened.runtime.descriptor)
        proc_info = os.stat(proc_path)
    except OSError as error:
        raise LaunchExecutionError(f"cannot address verified runtime descriptor: {error}") from error
    if not same_inode(held_info, proc_info) or not stat.S_ISREG(proc_info.st_mode):
        raise LaunchExecutionError("verified runtime descriptor identity changed")
    return proc_path


def _verified_proc_profile(opened: OpenVerifiedProfile) -> str:
    proc_path = f"/proc/self/fd/{opened.profile_descriptor}"
    try:
        held_info = os.fstat(opened.profile_descriptor)
        proc_info = os.stat(proc_path)
    except OSError as error:
        raise LaunchExecutionError(f"cannot address verified profile descriptor: {error}") from error
    if not same_inode(held_info, proc_info):
        raise LaunchExecutionError("verified profile descriptor identity changed")
    _require_managed_directory(proc_info, "verified profile")
    return proc_path


def _execution_contract(
    ticket: LaunchTicketModel,
    profile_proc_path: str,
) -> tuple[tuple[str, ...], dict[str, str]]:
    child_argv = list(ticket.argv)
    child_argv[4] = profile_proc_path
    child_environment = dict(ticket.environment)
    logical_state = f"{ticket.profile_path}/{RUNTIME_STATE_DIRECTORY}"
    execution_state = f"{profile_proc_path}/{RUNTIME_STATE_DIRECTORY}"
    for name, relative in _FIXED_ENVIRONMENT_PATHS.items():
        expected = f"{logical_state}/{relative}"
        if child_environment.get(name) != expected:
            raise LaunchSafetyError(
                f"launch ticket environment has an invalid fixed value: {name}"
            )
        child_environment[name] = f"{execution_state}/{relative}"
    return tuple(child_argv), child_environment


def _normalize_child_status(status: int) -> int:
    return status if status >= 0 else 128 + (-status)


def _live_launch_session_groups(session_id: int) -> set[int]:
    groups: set[int] = set()
    try:
        entries = os.scandir("/proc")
    except OSError as error:
        raise LaunchExecutionError(f"cannot inspect runtime process session: {error}") from error
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/stat", encoding="utf-8") as stat_file:
                    rendered = stat_file.read()
                fields = rendered[rendered.rfind(")") + 2 :].split()
                state = fields[0]
                process_group = int(fields[2])
                process_session = int(fields[3])
            except (OSError, IndexError, ValueError):
                continue
            if process_session == session_id and state != "Z":
                groups.add(process_group)
    return groups


def _signal_launch_session(
    session_id: int,
    signum: int,
    *,
    include_leader_group: bool,
) -> list[OSError]:
    errors: list[OSError] = []
    groups = _live_launch_session_groups(session_id)
    if include_leader_group:
        groups.add(session_id)
    for process_group in groups:
        try:
            os.killpg(process_group, signum)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(error)
    return errors


def _wait_for_launch_session_exit(
    process: subprocess.Popen[Any],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        leader_running = process.poll() is None
        if not leader_running and not _live_launch_session_groups(process.pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_GROUP_POLL_SECONDS)


def _terminate_launch_session(process: subprocess.Popen[Any]) -> None:
    errors = _signal_launch_session(
        process.pid,
        signal.SIGTERM,
        include_leader_group=process.poll() is None,
    )
    if _wait_for_launch_session_exit(process, _GROUP_TERM_SECONDS):
        if errors:
            raise LaunchExecutionError(f"cannot terminate runtime process session: {errors[0]}")
        return
    errors.extend(
        _signal_launch_session(
            process.pid,
            signal.SIGKILL,
            include_leader_group=process.poll() is None,
        )
    )
    if not _wait_for_launch_session_exit(process, _GROUP_KILL_SECONDS):
        raise LaunchExecutionError("runtime process session did not exit after SIGKILL")
    if errors:
        raise LaunchExecutionError(f"cannot terminate runtime process session: {errors[0]}")


def _drain_remaining_launch_session(process: subprocess.Popen[Any]) -> None:
    if not _live_launch_session_groups(process.pid):
        return
    errors = _signal_launch_session(
        process.pid,
        signal.SIGTERM,
        include_leader_group=False,
    )
    deadline = time.monotonic() + _GROUP_TERM_SECONDS
    while _live_launch_session_groups(process.pid) and time.monotonic() < deadline:
        time.sleep(_GROUP_POLL_SECONDS)
    if _live_launch_session_groups(process.pid):
        errors.extend(
            _signal_launch_session(
                process.pid,
                signal.SIGKILL,
                include_leader_group=False,
            )
        )
        deadline = time.monotonic() + _GROUP_KILL_SECONDS
        while _live_launch_session_groups(process.pid) and time.monotonic() < deadline:
            time.sleep(_GROUP_POLL_SECONDS)
    if _live_launch_session_groups(process.pid):
        raise LaunchExecutionError("runtime process session retained live members after SIGKILL")
    if errors:
        raise LaunchExecutionError(f"cannot drain runtime process session: {errors[0]}")


def _write_pipe_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - os.write contract.
            raise LaunchExecutionError("cannot write app-shell policy authority")
        view = view[written:]


def _create_app_shell_policy_descriptor(payload: bytes) -> int:
    """Create a seekable immutable policy authority for Gecko and fake adapters."""
    descriptor: int | None = None
    try:
        descriptor = _create_memfd()
        _write_pipe_all(descriptor, payload)
        os.fchmod(descriptor, 0o400)
        fcntl.fcntl(descriptor, _F_ADD_SEALS, _APP_SHELL_POLICY_SEALS)
        seals = fcntl.fcntl(descriptor, _F_GET_SEALS)
        if seals & _APP_SHELL_POLICY_SEALS != _APP_SHELL_POLICY_SEALS:
            raise LaunchExecutionError("app-shell policy memfd seal set is incomplete")
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(payload):
            raise LaunchExecutionError("app-shell policy memfd identity is invalid")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except LaunchError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, TypeError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise LaunchExecutionError(
            f"cannot create sealed app-shell policy authority: {error}"
        ) from error


def _await_app_shell_handshake(
    process: subprocess.Popen[Any],
    descriptor: int,
    policy: AppShellPolicy,
) -> None:
    deadline = time.monotonic() + _APP_SHELL_HANDSHAKE_SECONDS
    payload = bytearray()
    expected_digest = app_shell_policy_sha256(policy)
    os.set_blocking(descriptor, False)
    while True:
        if process.poll() is not None:
            raise LaunchExecutionError(
                "app-shell adapter exited before its ready-before-site-content handshake"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LaunchExecutionError(
                "app-shell adapter did not complete its startup handshake"
            )
        readable, _, _ = select.select(
            (descriptor,),
            (),
            (),
            min(remaining, _GROUP_POLL_SECONDS),
        )
        if not readable:
            continue
        chunk = os.read(descriptor, min(4096, MAX_HANDSHAKE_BYTES + 1 - len(payload)))
        if not chunk:
            raise LaunchExecutionError(
                "app-shell adapter closed its startup handshake without readiness"
            )
        payload.extend(chunk)
        if len(payload) > MAX_HANDSHAKE_BYTES:
            raise LaunchExecutionError("app-shell startup handshake exceeds the size limit")
        if b"\n" not in payload:
            continue
        document, trailing = bytes(payload).split(b"\n", 1)
        if trailing:
            raise LaunchExecutionError(
                "app-shell startup handshake contains trailing data"
            )
        try:
            parse_app_shell_handshake_payload(
                document,
                expected_policy_sha256=expected_digest,
            )
        except AppShellError as error:
            raise LaunchExecutionError(str(error)) from error
        return


def _spawn_and_supervise(
    opened: OpenVerifiedProfile,
    ticket: LaunchTicketModel,
    lock_descriptor: int,
    app_shell_policy: AppShellPolicy | None = None,
    browser_adapter: OpenVerifiedBrowserAdapter | None = None,
) -> int:
    runtime_proc_path = _verified_proc_executable(opened)
    profile_proc_path = _verified_proc_profile(opened)
    _require_active_profile_lock(opened.profile_descriptor, lock_descriptor)
    child_argv, child_environment = _execution_contract(ticket, profile_proc_path)
    policy_read: int | None = None
    handshake_read: int | None = None
    handshake_write: int | None = None
    inherited_descriptors = (opened.runtime.descriptor, opened.profile_descriptor)
    if browser_adapter is not None and app_shell_policy is None:
        raise LaunchSafetyError("bundled browser adapter requires an app-shell policy")
    if app_shell_policy is not None:
        try:
            policy_payload = render_app_shell_policy(app_shell_policy).encode("utf-8")
        except AppShellError as error:
            raise LaunchSafetyError(str(error)) from error
        pipe2 = getattr(os, "pipe2", None)
        close_on_exec = getattr(os, "O_CLOEXEC", None)
        if pipe2 is None or close_on_exec is None:
            raise LaunchExecutionError(
                "app-shell execution requires close-on-exec pipe support"
            )
        try:
            policy_read = _create_app_shell_policy_descriptor(policy_payload)
            handshake_read, handshake_write = pipe2(close_on_exec)
        except LaunchError:
            for descriptor in (handshake_write, handshake_read, policy_read):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise
        except OSError as error:
            for descriptor in (handshake_write, handshake_read, policy_read):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise LaunchExecutionError(
                f"cannot create app-shell startup authority channels: {error}"
            ) from error
        authority_arguments = (
            "--zen-app-runtime-app-shell-policy-fd",
            str(policy_read),
            "--zen-app-runtime-app-shell-handshake-fd",
            str(handshake_write),
        )
        if browser_adapter is None:
            child_argv = child_argv[:-1] + authority_arguments
        else:
            try:
                application_path = browser_adapter_application_path(browser_adapter)
            except BrowserAdapterError as error:
                raise LaunchSafetyError(str(error)) from error
            child_argv = (
                child_argv[0],
                "-app",
                application_path,
                *child_argv[1:-1],
                *authority_arguments,
            )
            child_environment["ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD"] = str(policy_read)
            child_environment["ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD"] = str(
                handshake_write
            )
            inherited_descriptors = (
                *inherited_descriptors,
                browser_adapter.descriptor,
            )
        inherited_descriptors = (
            *inherited_descriptors,
            policy_read,
            handshake_write,
        )
    process: subprocess.Popen[Any] | None = None
    old_handlers: dict[signal.Signals, Any] = {}
    installed_handlers: list[signal.Signals] = []
    pending_signals: list[int] = []
    forwarding_errors: list[OSError] = []

    def forward(signum: int, _frame: Any) -> None:
        if process is None:
            pending_signals.append(signum)
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        except OSError as error:
            forwarding_errors.append(error)

    try:
        try:
            for signum in _FORWARDED_SIGNALS:
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, forward)
                installed_handlers.append(signum)
        except (OSError, ValueError) as error:
            raise LaunchExecutionError(f"cannot install launch signal handlers: {error}") from error

        try:
            if browser_adapter is not None:
                try:
                    revalidate_open_browser_adapter(browser_adapter)
                except BrowserAdapterError as error:
                    raise LaunchSafetyError(str(error)) from error
            process = subprocess.Popen(
                child_argv,
                executable=runtime_proc_path,
                shell=False,
                close_fds=True,
                pass_fds=inherited_descriptors,
                start_new_session=True,
                env=child_environment,
            )
        except OSError as error:
            raise LaunchExecutionError(f"cannot spawn verified runtime: {error}") from error

        _require_active_profile_lock(opened.profile_descriptor, lock_descriptor)
        if app_shell_policy is not None:
            if policy_read is None or handshake_read is None or handshake_write is None:
                raise LaunchExecutionError("app-shell startup authority channel is incomplete")
            os.close(policy_read)
            policy_read = None
            os.close(handshake_write)
            handshake_write = None
            _await_app_shell_handshake(process, handshake_read, app_shell_policy)
            os.close(handshake_read)
            handshake_read = None
        for signum in pending_signals:
            forward(signum, None)
        try:
            status = process.wait()
            _drain_remaining_launch_session(process)
        except BaseException:
            _terminate_launch_session(process)
            raise
        if forwarding_errors:
            raise LaunchExecutionError(
                f"could not forward a signal to the runtime process session: {forwarding_errors[0]}"
            ) from forwarding_errors[0]
        return _normalize_child_status(status)
    except LaunchError:
        if process is not None and (
            process.poll() is None or _live_launch_session_groups(process.pid)
        ):
            _terminate_launch_session(process)
        raise
    except (OSError, ValueError) as error:
        if process is not None and (
            process.poll() is None or _live_launch_session_groups(process.pid)
        ):
            _terminate_launch_session(process)
        raise LaunchExecutionError(f"cannot supervise verified runtime: {error}") from error
    finally:
        for descriptor in (handshake_write, handshake_read, policy_read):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        for signum in reversed(installed_handlers):
            try:
                signal.signal(signum, old_handlers[signum])
            except (OSError, ValueError):
                pass


def execute_launch(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    *,
    confirmed: bool,
    environment: Mapping[str, str] | None = None,
    display_backend: str | None = None,
    app_shell_capability: os.PathLike[str] | str | None = None,
    use_bundled_app_shell: bool = False,
) -> int:
    """Execute legacy, claimed-adapter, or verified bundled App Shell launch."""
    if not confirmed:
        raise LaunchSafetyError("launch execution requires --yes; no process was started")
    if not isinstance(use_bundled_app_shell, bool):
        raise LaunchSafetyError("use_bundled_app_shell must be boolean")
    if app_shell_capability is not None and use_bundled_app_shell:
        raise LaunchSafetyError(
            "external app-shell capability and bundled App Shell are mutually exclusive"
        )
    try:
        with open_verified_profile_binding(profile_root, manifest, runtime_lock) as opened:
            ticket = _ticket_from_opened_profile(
                manifest,
                opened,
                environment,
                display_backend,
            )
            with ExitStack() as resources:
                app_shell_policy: AppShellPolicy | None = None
                browser_adapter: OpenVerifiedBrowserAdapter | None = None
                if use_bundled_app_shell:
                    try:
                        browser_adapter = resources.enter_context(
                            open_bundled_browser_adapter()
                        )
                        bundled_adapter_capability(
                            opened.runtime.identity,
                            browser_adapter,
                        )
                        app_shell_policy = build_app_shell_policy(manifest)
                    except (AppShellError, BrowserAdapterError) as error:
                        raise LaunchSafetyError(str(error)) from error
                elif app_shell_capability is not None:
                    try:
                        load_adapter_capability(
                            app_shell_capability,
                            opened.runtime.identity,
                        )
                        app_shell_policy = build_app_shell_policy(manifest)
                    except AppShellError as error:
                        raise LaunchSafetyError(str(error)) from error
                with _acquire_profile_lock(opened.profile_descriptor) as lock_descriptor:
                    revalidate_open_profile_binding(opened, manifest)
                    _ensure_runtime_directories(opened.profile_descriptor)
                    revalidate_open_profile_binding(opened, manifest)
                    return _spawn_and_supervise(
                        opened,
                        ticket,
                        lock_descriptor,
                        app_shell_policy,
                        browser_adapter,
                    )
    except LaunchError:
        raise
    except ProfileError as error:
        raise LaunchSafetyError(str(error)) from error
