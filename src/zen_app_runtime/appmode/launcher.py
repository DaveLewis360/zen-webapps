"""Focus-or-launch for app-mode apps."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

from .definition import AppDefinition
from .paths import AppModePaths, desktop_id
from .zen import ZenRuntime

# Variables that must not leak a previous app's (or the browser's) identity.
_DROP_ENV = (
    "MOZ_NO_REMOTE",
    "MOZ_LAUNCHED_CHILD",
    "MOZ_APP_REMOTINGNAME",
    "MOZ_DESKTOP_FILE_NAME",
    "XRE_PROFILE_PATH",
    "XRE_PROFILE_LOCAL_PATH",
    "XRE_PROFILE_NAME",
    "XRE_START_OFFLINE",
    "XRE_RESTARTED_BY_PROFILE_MANAGER",
    "ZEN_APP_ID",
    "PULSE_PROP_OVERRIDE",
)


@dataclass(frozen=True)
class LaunchOutcome:
    action: str  # "focused" | "started" | "forwarded"
    pid: int | None = None


def running_pid(profile: Path) -> int | None:
    """PID of the Zen process holding the profile lock, if it is alive."""
    lock = profile / "lock"
    try:
        target = os.readlink(lock)
    except OSError:
        return None
    _, _, pid_text = target.rpartition("+")
    if not pid_text.isdigit():
        return None
    pid = int(pid_text)
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    return pid if str(profile).encode() in cmdline else None


def _hyprctl_focus(window_class: str) -> bool:
    hyprctl = shutil.which("hyprctl")
    if not hyprctl or not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return False
    try:
        clients = json.loads(
            subprocess.run([hyprctl, "-j", "clients"], capture_output=True, text=True, timeout=3, check=False).stdout
            or "[]"
        )
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    if not any(client.get("class") == window_class for client in clients if isinstance(client, dict)):
        return False
    for command in (
        ["dispatch", f'hl.dsp.focus({{ window = "class:{window_class}" }})'],
        ["dispatch", "focuswindow", f"class:^({window_class})$"],
    ):
        try:
            result = subprocess.run([hyprctl, *command], capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.stdout.strip().lower().startswith("ok"):
            return True
    return False


def process_link(link: Path, zen: ZenRuntime) -> Path:
    """``link`` → the Zen binary (created or re-pointed atomically); Zen itself if that fails."""
    target = str(zen.binary)
    try:
        if os.readlink(link) == target:
            return link
    except OSError:
        pass
    temporary = link.with_name(f".{link.name}.{os.getpid()}")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        os.symlink(target, temporary)
        os.replace(temporary, link)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return zen.binary
    return link


def process_executable(paths: AppModePaths, app_id: str, zen: ZenRuntime) -> Path:
    """The file an app is started through: ``<app dir>/zen-app-<id>`` → Zen.

    Linux names a process after the file it executes, so the app runs as
    ``zen-app-<id>`` (the kernel keeps 15 characters) instead of ``zen-bin``:
    ``killall zen-bin`` aimed at the browser leaves every app running, and
    ``killall zen-app-<id>`` stops just that app. Gecko resolves the link to
    find its installation, so nothing else changes. Falls back to the Zen
    binary itself if the link cannot be made.
    """
    return process_link(paths.app_dir(app_id) / desktop_id(app_id), zen)


def identity_argv(executable: Path, profile: Path, identity: str, extra: list[str]) -> list[str]:
    return [str(executable), "--name", identity, "--class", identity, "--profile", str(profile), *extra]


def zen_argv(executable: Path, profile: Path, app_id: str, extra: list[str]) -> list[str]:
    return identity_argv(executable, profile, desktop_id(app_id), extra)


def pulse_properties(name: str, icon_name: str | None = None) -> str:
    """``PULSE_PROP_OVERRIDE`` naming the app's sound streams (libpulse proplist syntax).

    Gecko names its audio after the browser brand, read before any add-on
    starts; libpulse (and PipeWire's pulse server) apply this variable over
    whatever the application sets, so mixers show "Spotify" instead of "Zen".
    """

    def quoted(value: str) -> str:
        clean = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in value).strip()
        return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'

    properties = [f"application.name={quoted(name)}"]
    if icon_name:
        properties.append(f"application.icon_name={quoted(icon_name)}")
    return " ".join(properties)


def identity_environment(identity: str, *, app_id: str, name: str, icon_name: str | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _DROP_ENV}
    environment["MOZ_APP_REMOTINGNAME"] = identity
    environment["MOZ_DESKTOP_FILE_NAME"] = identity
    environment["ZEN_APP_ID"] = app_id
    environment["PULSE_PROP_OVERRIDE"] = pulse_properties(name, icon_name or identity)
    return environment


def app_environment(app_id: str, name: str | None = None, icon_name: str | None = None) -> dict[str, str]:
    return identity_environment(desktop_id(app_id), app_id=app_id, name=name or app_id, icon_name=icon_name)


def rendered_icon_name(profile: Path) -> str | None:
    """Icon name recorded in the rendered profile (zen-app/app.json), if any."""
    try:
        config = json.loads((profile / "zen-app" / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    icon = config.get("icon") if isinstance(config, dict) else None
    name = icon.get("name") if isinstance(icon, dict) else None
    return name if isinstance(name, str) and name else None


def launch_profile(
    *,
    identity: str,
    profile: Path,
    executable: Path,
    environment: dict[str, str],
    log_path: Path,
    first_url: str,
    url: str | None = None,
    action: str | None = None,
    write_pending: bool = False,
    popen=subprocess.Popen,
) -> LaunchOutcome:
    """Focus, forward to, or start the Zen instance that owns ``profile``."""
    pid = running_pid(profile)
    if pid is not None:
        focused = _hyprctl_focus(identity)
        if not url and not action and focused:
            return LaunchOutcome("focused", pid)
        extra: list[str] = []
        if action:
            extra += ["--zen-app-action", action]
        if url:
            extra += ["--zen-app-open", url]
        # A second process with the same profile forwards its command line to
        # the running instance (Gecko remoting) and exits.
        popen(
            identity_argv(executable, profile, identity, extra),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return LaunchOutcome("forwarded", pid)

    if write_pending:
        from .render import write_pending_action

        write_pending_action(profile, action or "open", url)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as log:
        os.chmod(log_path, 0o600)
        process = popen(
            identity_argv(executable, profile, identity, [first_url]),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    return LaunchOutcome("started", getattr(process, "pid", None))


def launch(
    definition: AppDefinition,
    paths: AppModePaths,
    zen: ZenRuntime,
    *,
    url: str | None = None,
    action: str | None = None,
    icon_name: str | None = None,
    popen=subprocess.Popen,
) -> LaunchOutcome:
    profile = paths.profile_dir(definition.id)
    executable = process_executable(paths, definition.id, zen)
    first_url = "about:blank" if definition.needs_onboarding else (url or definition.start_url)
    return launch_profile(
        identity=desktop_id(definition.id),
        profile=profile,
        executable=executable,
        environment=app_environment(definition.id, definition.name, icon_name or rendered_icon_name(profile)),
        log_path=paths.log_path(definition.id),
        first_url=first_url,
        url=url,
        action=action,
        write_pending=bool(action or (url and definition.needs_onboarding)),
        popen=popen,
    )
