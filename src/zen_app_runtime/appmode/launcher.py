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


def zen_argv(zen: ZenRuntime, profile: Path, app_id: str, extra: list[str]) -> list[str]:
    identity = desktop_id(app_id)
    return [str(zen.binary), "--name", identity, "--class", identity, "--profile", str(profile), *extra]


def app_environment(app_id: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in _DROP_ENV}
    identity = desktop_id(app_id)
    environment["MOZ_APP_REMOTINGNAME"] = identity
    environment["MOZ_DESKTOP_FILE_NAME"] = identity
    environment["ZEN_APP_ID"] = app_id
    return environment


def launch(
    definition: AppDefinition,
    paths: AppModePaths,
    zen: ZenRuntime,
    *,
    url: str | None = None,
    action: str | None = None,
    popen=subprocess.Popen,
) -> LaunchOutcome:
    profile = paths.profile_dir(definition.id)
    identity = desktop_id(definition.id)
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
            zen_argv(zen, profile, definition.id, extra),
            env=app_environment(definition.id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return LaunchOutcome("forwarded", pid)

    first_url = "about:blank" if definition.needs_onboarding else (url or definition.start_url)
    if action or (url and definition.needs_onboarding):
        from .render import write_pending_action

        write_pending_action(profile, action or "open", url)
    log_path = paths.log_path(definition.id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab", buffering=0) as log:
        os.chmod(log_path, 0o600)
        process = popen(
            zen_argv(zen, profile, definition.id, [first_url]),
            env=app_environment(definition.id),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    return LaunchOutcome("started", getattr(process, "pid", None))
