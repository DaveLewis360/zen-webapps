"""Minimal command-line App Manager."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from .app_shell import AppShellError
from .desktop import DesktopError, build_desktop_preview, render_desktop_preview
from .launch import (
    DISPLAY_BACKENDS,
    LaunchError,
    execute_launch,
    inspect_launch,
    render_launch_ticket,
)
from .launch_plan import LaunchPlanError, build_launch_plan, render_launch_plan
from .manifest import MODES, ManifestError, create_manifest, manifest_to_dict
from .onboarding import (
    OnboardingError,
    build_web_app_onboarding_plan,
    render_web_app_onboarding_plan,
    render_web_app_onboarding_result,
)
from .profile import (
    ProfileError,
    provision_profile,
    render_profile_provision_result,
)
from .runtime import RuntimeError, render_runtime_identity, verify_runtime
from .store import AppStore, StoreError
from .workflow import (
    WorkflowError,
    complete_web_app_onboarding,
    doctor_report,
    inspect_app_shell,
    install_app,
    render_app_shell_inspection,
    render_doctor_report,
    render_install_result,
    render_repair_result,
    render_workflow_status,
    repair_app,
    report_exit_code,
    workflow_status,
)


def default_store_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "zen-app-runtime" / "apps"


def default_profile_root_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "zen-app-runtime"


def _add_plan_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("app_id")
    parser.add_argument("--runtime", type=Path, required=True, help="absolute proposed runtime path")
    parser.add_argument("--launcher", type=Path, required=True, help="absolute launcher path")
    parser.add_argument("--profile-root", type=Path, required=True, help="absolute profile root (not created)")


def _add_launch_paths(
    parser: argparse.ArgumentParser,
    *,
    app_shell: bool = False,
) -> None:
    parser.add_argument("app_id")
    parser.add_argument("--profile-root", required=True, help="absolute managed profile root")
    parser.add_argument("--runtime-lock", required=True, help="absolute runtime lock path")
    parser.add_argument(
        "--display-backend",
        choices=DISPLAY_BACKENDS,
        help="explicit display policy; omission preserves launch-ticket v1",
    )
    if app_shell:
        app_shell_authority = parser.add_mutually_exclusive_group()
        app_shell_authority.add_argument(
            "--app-shell-capability",
            help="absolute runtime-bound external adapter capability path",
        )
        app_shell_authority.add_argument(
            "--bundled-app-shell",
            action="store_true",
            help="use the pinned package-bundled privileged Gecko App Shell",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zen-app", description="Manage Zen app definitions")
    parser.add_argument(
        "--store",
        default=str(default_store_path()),
        help="absolute definition store (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="inspect platform, display, and optional runtime readiness")
    doctor.add_argument("--runtime-lock", help="optional absolute runtime lock path")
    doctor.add_argument(
        "--display-backend",
        choices=DISPLAY_BACKENDS,
        default="auto",
        help="display policy to validate (default: %(default)s)",
    )

    app = commands.add_parser("app", help="run a high-level managed app workflow")
    app_commands = app.add_subparsers(dest="app_command", required=True)
    app_onboard = app_commands.add_parser(
        "onboard",
        help="preview or confirm creation of a launchable web app from one HTTPS URL",
    )
    app_onboard.add_argument("url", help="absolute credential-free HTTPS URL")
    app_onboard.add_argument("--app-id", help="optional lowercase app slug; derived when omitted")
    app_onboard.add_argument("--name", help="optional display name; derived when omitted")
    app_onboard.add_argument("--mode", choices=sorted(MODES), default="Opaque")
    app_onboard.add_argument("--with-stylus", action="store_true")
    app_onboard.add_argument(
        "--profile-root",
        default=str(default_profile_root_path()),
        help="absolute managed profile root (default: %(default)s)",
    )
    app_onboard.add_argument("--runtime-lock", required=True, help="absolute runtime lock path")
    app_onboard.add_argument(
        "--display-backend",
        choices=DISPLAY_BACKENDS,
        default="auto",
        help="display policy for optional launch (default: %(default)s)",
    )
    app_onboard.add_argument(
        "--bundled-app-shell",
        action="store_true",
        help="install/launch with the pinned in-browser App Shell and onboarding",
    )
    app_onboard.add_argument(
        "--launch",
        action="store_true",
        help="launch the installed app after onboarding",
    )
    app_onboard.add_argument(
        "--yes",
        action="store_true",
        help="confirm definition/profile installation and optional process execution",
    )
    app_install = app_commands.add_parser(
        "install",
        help="idempotently create an exact definition and provision its profile",
    )
    app_install.add_argument("app_id")
    app_install.add_argument("--name", required=True)
    app_install.add_argument("--url", required=True, dest="start_url")
    app_install.add_argument("--mode", choices=sorted(MODES), default="Opaque")
    app_install.add_argument("--with-stylus", action="store_true")
    app_install.add_argument("--profile-root", required=True, help="absolute managed profile root")
    app_install.add_argument("--runtime-lock", required=True, help="absolute runtime lock path")
    app_install_shell = app_install.add_mutually_exclusive_group()
    app_install_shell.add_argument(
        "--app-shell-capability",
        help="absolute runtime-bound external adapter capability path",
    )
    app_install_shell.add_argument(
        "--bundled-app-shell",
        action="store_true",
        help="verify and bind the pinned package-bundled App Shell",
    )

    app_status = app_commands.add_parser("status", help="inspect read-only app readiness")
    _add_launch_paths(app_status, app_shell=True)
    app_inspect = app_commands.add_parser("inspect", help="render the exact verified launch ticket")
    _add_launch_paths(app_inspect, app_shell=True)
    app_launch = app_commands.add_parser("launch", help="execute the ready app under supervision")
    _add_launch_paths(app_launch, app_shell=True)
    app_launch.add_argument("--yes", action="store_true", help="confirm process execution")
    app_repair = app_commands.add_parser(
        "repair",
        help="idempotently provision an existing exact definition/runtime binding",
    )
    app_repair.add_argument("app_id")
    app_repair.add_argument("--profile-root", required=True, help="absolute managed profile root")
    app_repair.add_argument("--runtime-lock", required=True, help="absolute runtime lock path")
    app_repair_shell = app_repair.add_mutually_exclusive_group()
    app_repair_shell.add_argument(
        "--app-shell-capability",
        help="absolute runtime-bound external adapter capability path",
    )
    app_repair_shell.add_argument(
        "--bundled-app-shell",
        action="store_true",
        help="verify and bind the pinned package-bundled App Shell",
    )

    create = commands.add_parser("create", help="create an app definition without overwriting")
    create.add_argument("app_id")
    create.add_argument("--name", required=True)
    create.add_argument("--url", required=True, dest="start_url")
    create.add_argument("--mode", choices=sorted(MODES), default="Opaque")
    create.add_argument("--with-stylus", action="store_true")

    commands.add_parser("list", help="list validated app IDs")

    show = commands.add_parser("show", help="show a validated app definition")
    show.add_argument("app_id")

    plan = commands.add_parser("plan", help="render an inert launch plan as JSON")
    _add_plan_paths(plan)

    desktop = commands.add_parser("desktop", help="render inert desktop integration previews")
    desktop_commands = desktop.add_subparsers(dest="desktop_command", required=True)
    desktop_render = desktop_commands.add_parser("render", help="render a plan-only .desktop preview")
    _add_plan_paths(desktop_render)

    runtime = commands.add_parser("runtime", help="verify an explicitly pinned local runtime")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_verify = runtime_commands.add_parser("verify", help="verify a runtime lock without execution")
    runtime_verify.add_argument("--lock", required=True, help="absolute runtime lock path")

    profile = commands.add_parser("profile", help="manage isolated sentinel-marked profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_provision = profile_commands.add_parser("provision", help="provision a bound isolated profile")
    profile_provision.add_argument("app_id")
    profile_provision.add_argument("--profile-root", required=True, help="absolute managed root")
    profile_provision.add_argument("--runtime-lock", required=True, help="absolute runtime lock path")

    launch = commands.add_parser("launch", help="inspect or execute a verified app launch")
    launch_commands = launch.add_subparsers(dest="launch_command", required=True)
    launch_inspect = launch_commands.add_parser(
        "inspect",
        help="render a read-only verified launch ticket",
    )
    _add_launch_paths(launch_inspect)
    launch_execute = launch_commands.add_parser(
        "execute",
        help="execute and supervise the verified runtime",
    )
    _add_launch_paths(launch_execute)
    launch_execute.add_argument("--yes", action="store_true", help="confirm process execution")

    remove = commands.add_parser("remove", help="remove a validated app definition")
    remove.add_argument("app_id")
    remove.add_argument("--yes", action="store_true", help="confirm permanent removal")
    _add_app_mode_commands(commands)
    return parser


APP_MODE_COMMANDS = {"new", "run", "settings", "apps", "presets", "rm", "setup", "refresh", "styles", "glass", "css"}


def _add_app_mode_commands(commands) -> None:
    """App mode: native-feeling apps on the package-managed Zen runtime."""
    new = commands.add_parser(
        "new",
        help="app mode: create an app from a preset (spotify, discord, messenger, ...) or an https URL",
    )
    new.add_argument("source", help="preset key or https:// URL")
    new.add_argument("--id", dest="new_app_id", help="app id (default: preset key or derived from the URL)")
    new.add_argument("--name", dest="new_name", help="display name")
    appearance = new.add_mutually_exclusive_group()
    appearance.add_argument("--transparent", dest="appearance", action="store_const", const="transparent")
    appearance.add_argument("--opaque", dest="appearance", action="store_const", const="opaque")
    new.add_argument("--no-launch", action="store_true", help="only create it; do not start it")

    run = commands.add_parser("run", help="app mode: start an app or focus its window")
    run.add_argument("app_id")
    run.add_argument("url", nargs="?", help="optional deep link to open inside the app")

    settings = commands.add_parser("settings", help="app mode: open an app's settings")
    settings.add_argument("app_id")

    apps = commands.add_parser("apps", help="app mode: list apps")
    apps.add_argument("--json", action="store_true")

    commands.add_parser("presets", help="app mode: list built-in presets")

    rm = commands.add_parser("rm", help="app mode: remove an app and its launcher")
    rm.add_argument("app_id")
    rm.add_argument("--purge", action="store_true", help="also delete the profile (logins, data)")
    rm.add_argument("--yes", action="store_true", help="confirm removal")

    commands.add_parser("setup", help="app mode: install the zen-app launcher shim and refresh all apps")

    refresh = commands.add_parser("refresh", help="app mode: re-render profiles and launchers")
    refresh.add_argument("app_id", nargs="?")

    styles = commands.add_parser("styles", help="app mode: Zen Internet site styles (show / update)")
    styles.add_argument("styles_action", choices=("show", "update"))
    styles.add_argument("target", nargs="?", help="app id or hostname (show)")

    commands.add_parser("glass", help="app mode: show desktop glass sources (Ghostty, Vesktop, Zen)")

    css = commands.add_parser(
        "css",
        help="app mode: an app's own CSS: show it, set it from a file, --on/--off, --remove",
    )
    css.add_argument("app_id")
    css.add_argument("css_file", nargs="?", metavar="FILE", help="CSS file to use (plain CSS or a Stylus userstyle)")
    css_state = css.add_mutually_exclusive_group()
    css_state.add_argument("--on", dest="css_state", action="store_const", const="on", help="apply it")
    css_state.add_argument("--off", dest="css_state", action="store_const", const="off", help="keep it but do not apply it")
    css_state.add_argument("--remove", dest="css_state", action="store_const", const="remove", help="delete it")


def _run_app_mode(arguments: argparse.Namespace) -> int:
    from .appmode import commands as app_mode

    command = arguments.command
    if command == "new":
        return app_mode.cmd_new(
            arguments.source,
            app_id=arguments.new_app_id,
            name=arguments.new_name,
            appearance=arguments.appearance,
            launch_after=not arguments.no_launch,
        )
    if command == "run":
        outcome = app_mode.cmd_run(arguments.app_id, url=arguments.url)
        return 0 if outcome.action in ("started", "focused", "forwarded") else 1
    if command == "settings":
        app_mode.cmd_run(arguments.app_id, action="settings")
        return 0
    if command == "apps":
        return app_mode.cmd_apps(as_json=arguments.json)
    if command == "presets":
        return app_mode.cmd_presets()
    if command == "rm":
        return app_mode.cmd_rm(arguments.app_id, purge=arguments.purge, yes=arguments.yes)
    if command == "setup":
        return app_mode.cmd_setup()
    if command == "refresh":
        return app_mode.cmd_refresh(arguments.app_id)
    if command == "styles":
        return app_mode.cmd_styles(arguments.styles_action, arguments.target)
    if command == "glass":
        return app_mode.cmd_glass()
    if command == "css":
        return app_mode.cmd_css(arguments.app_id, arguments.css_file, arguments.css_state)
    raise AssertionError(command)  # pragma: no cover


def _build_plan(arguments: argparse.Namespace, store: AppStore):
    manifest = store.get_read_only(arguments.app_id)
    return build_launch_plan(
        manifest,
        runtime_path=arguments.runtime,
        launcher_path=arguments.launcher,
        profile_root=arguments.profile_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    store = AppStore(arguments.store)
    if arguments.command in APP_MODE_COMMANDS:
        from .appmode.errors import AppModeError

        try:
            return _run_app_mode(arguments)
        except AppModeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        except OSError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    try:
        if arguments.command == "doctor":
            report = doctor_report(
                runtime_lock=arguments.runtime_lock,
                display_backend=arguments.display_backend,
            )
            sys.stdout.write(render_doctor_report(report))
            return report_exit_code(report)
        if arguments.command == "app" and arguments.app_command == "onboard":
            plan = build_web_app_onboarding_plan(
                arguments.url,
                arguments.profile_root,
                arguments.runtime_lock,
                app_id=arguments.app_id,
                name=arguments.name,
                mode=arguments.mode,
                include_stylus=arguments.with_stylus,
                display_backend=arguments.display_backend,
                launch_requested=arguments.launch,
                use_bundled_app_shell=arguments.bundled_app_shell,
            )
            if not arguments.yes:
                sys.stdout.write(render_web_app_onboarding_plan(plan))
                return 0
            result = complete_web_app_onboarding(store, plan)
            sys.stdout.write(render_web_app_onboarding_result(result))
            sys.stdout.flush()
            if plan.launch_requested:
                return execute_launch(
                    plan.profile_root,
                    plan.manifest,
                    plan.runtime_lock,
                    confirmed=True,
                    display_backend=plan.display_backend,
                    use_bundled_app_shell=plan.use_bundled_app_shell,
                )
        elif arguments.command == "app" and arguments.app_command == "install":
            manifest = create_manifest(
                app_id=arguments.app_id,
                name=arguments.name,
                start_url=arguments.start_url,
                mode=arguments.mode,
                include_stylus=arguments.with_stylus,
            )
            result = install_app(
                store,
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                app_shell_capability=arguments.app_shell_capability,
                use_bundled_app_shell=arguments.bundled_app_shell,
            )
            sys.stdout.write(render_install_result(result))
        elif arguments.command == "app" and arguments.app_command == "status":
            manifest = store.get_read_only(arguments.app_id)
            status = workflow_status(
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                display_backend=arguments.display_backend,
                app_shell_capability=arguments.app_shell_capability,
                use_bundled_app_shell=arguments.bundled_app_shell,
            )
            sys.stdout.write(render_workflow_status(status))
            return report_exit_code(status)
        elif arguments.command == "app" and arguments.app_command == "inspect":
            manifest = store.get_read_only(arguments.app_id)
            if (
                arguments.app_shell_capability is None
                and not arguments.bundled_app_shell
            ):
                ticket = inspect_launch(
                    arguments.profile_root,
                    manifest,
                    arguments.runtime_lock,
                    display_backend=arguments.display_backend,
                )
                sys.stdout.write(render_launch_ticket(ticket))
            else:
                inspection = inspect_app_shell(
                    arguments.profile_root,
                    manifest,
                    arguments.runtime_lock,
                    arguments.app_shell_capability,
                    display_backend=arguments.display_backend,
                    use_bundled_app_shell=arguments.bundled_app_shell,
                )
                sys.stdout.write(render_app_shell_inspection(inspection))
        elif arguments.command == "app" and arguments.app_command == "launch":
            if not arguments.yes:
                raise LaunchError("launch execution requires --yes; no process was started")
            manifest = store.get_read_only(arguments.app_id)
            return execute_launch(
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                confirmed=True,
                display_backend=arguments.display_backend,
                app_shell_capability=arguments.app_shell_capability,
                use_bundled_app_shell=arguments.bundled_app_shell,
            )
        elif arguments.command == "app" and arguments.app_command == "repair":
            manifest = store.get_read_only(arguments.app_id)
            result = repair_app(
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                app_shell_capability=arguments.app_shell_capability,
                use_bundled_app_shell=arguments.bundled_app_shell,
            )
            sys.stdout.write(render_repair_result(result))
        elif arguments.command == "create":
            manifest = create_manifest(
                app_id=arguments.app_id,
                name=arguments.name,
                start_url=arguments.start_url,
                mode=arguments.mode,
                include_stylus=arguments.with_stylus,
            )
            store.create(manifest)
        elif arguments.command == "list":
            for app_id in store.list_ids():
                print(app_id)
        elif arguments.command == "show":
            print(json.dumps(manifest_to_dict(store.get(arguments.app_id)), indent=2, sort_keys=True))
        elif arguments.command == "plan":
            sys.stdout.write(render_launch_plan(_build_plan(arguments, store)))
        elif arguments.command == "desktop" and arguments.desktop_command == "render":
            plan = _build_plan(arguments, store)
            preview = build_desktop_preview(plan, arguments.store)
            sys.stdout.write(render_desktop_preview(preview))
        elif arguments.command == "runtime" and arguments.runtime_command == "verify":
            sys.stdout.write(render_runtime_identity(verify_runtime(arguments.lock)))
        elif arguments.command == "profile" and arguments.profile_command == "provision":
            manifest = store.get_read_only(arguments.app_id)
            result = provision_profile(arguments.profile_root, manifest, arguments.runtime_lock)
            sys.stdout.write(render_profile_provision_result(result))
        elif arguments.command == "launch" and arguments.launch_command == "inspect":
            manifest = store.get_read_only(arguments.app_id)
            ticket = inspect_launch(
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                display_backend=arguments.display_backend,
            )
            sys.stdout.write(render_launch_ticket(ticket))
        elif arguments.command == "launch" and arguments.launch_command == "execute":
            if not arguments.yes:
                raise LaunchError("launch execution requires --yes; no process was started")
            manifest = store.get_read_only(arguments.app_id)
            return execute_launch(
                arguments.profile_root,
                manifest,
                arguments.runtime_lock,
                confirmed=True,
                display_backend=arguments.display_backend,
            )
        elif arguments.command == "remove":
            if not arguments.yes:
                raise StoreError("removal requires --yes; no definition was changed")
            store.remove(arguments.app_id)
        else:  # pragma: no cover - argparse guarantees this branch is unreachable.
            parser.error("unknown command")
        return 0
    except (
        AppShellError,
        DesktopError,
        LaunchError,
        LaunchPlanError,
        ManifestError,
        OnboardingError,
        ProfileError,
        RuntimeError,
        StoreError,
        WorkflowError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
