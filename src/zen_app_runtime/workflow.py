"""High-level, versioned app workflow over existing authority APIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from .app_shell import (
    AppShellError,
    adapter_capability_to_dict,
    app_shell_policy_sha256,
    app_shell_policy_to_dict,
    build_app_shell_policy,
    load_adapter_capability,
)
from .browser_adapter import (
    BrowserAdapterError,
    browser_adapter_to_dict,
    bundled_adapter_capability,
    open_bundled_browser_adapter,
)
from .launch import (
    LaunchError,
    inspect_launch,
    launch_ticket_to_dict,
    resolve_display_backend,
)
from .manifest import AppManifest
from .onboarding import (
    WebAppOnboardingPlan,
    web_app_onboarding_result,
)
from .profile import (
    ProfileError,
    manifest_sha256,
    profile_provision_result_to_dict,
    provision_profile,
    verify_profile_binding,
)
from .runtime import (
    RuntimeError as RuntimeVerificationError,
    RuntimeIdentity,
    open_verified_runtime,
    probe_sealed_runtime_support,
    runtime_identity_to_dict,
    verify_runtime,
)
from .store import AppAlreadyExists, AppNotFound, AppStore

DOCTOR_REPORT_VERSION = 1
WORKFLOW_STATUS_VERSION = 1
APP_SHELL_WORKFLOW_STATUS_VERSION = 2
BUNDLED_APP_SHELL_WORKFLOW_STATUS_VERSION = 3
APP_SHELL_INSPECTION_VERSION = 1
BUNDLED_APP_SHELL_INSPECTION_VERSION = 2
APP_INSTALL_RESULT_VERSION = 1
APP_SHELL_INSTALL_RESULT_VERSION = 2
BUNDLED_APP_SHELL_INSTALL_RESULT_VERSION = 3
APP_REPAIR_RESULT_VERSION = 1
APP_SHELL_REPAIR_RESULT_VERSION = 2
BUNDLED_APP_SHELL_REPAIR_RESULT_VERSION = 3


class WorkflowError(RuntimeError):
    """A high-level app workflow contract could not be completed safely."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"


def _error_text(error: BaseException) -> str:
    rendered = str(error)
    return rendered if rendered else type(error).__name__


def _proc_self_fd_status() -> dict[str, str]:
    try:
        info = os.stat("/proc/self/fd")
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("/proc/self/fd is not a directory")
        descriptor = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        os.close(descriptor)
        return {"status": "available"}
    except (AttributeError, OSError, TypeError) as error:
        return {"error": _error_text(error), "status": "unavailable"}


def _sealed_snapshot_status() -> dict[str, str]:
    try:
        probe_sealed_runtime_support()
        return {"status": "available"}
    except RuntimeVerificationError as error:
        return {"error": _error_text(error), "status": "unavailable"}


def _display_status(
    requested: str,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    try:
        resolved = resolve_display_backend(requested, environment)
        return {
            "requested": requested,
            "resolved": resolved,
            "status": "available",
        }
    except LaunchError as error:
        return {
            "error": _error_text(error),
            "requested": requested,
            "status": "unavailable",
        }


def doctor_report(
    *,
    runtime_lock: os.PathLike[str] | str | None = None,
    display_backend: str = "auto",
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a read-only capability report; blocked capabilities are data."""
    proc_status = _proc_self_fd_status()
    sealed_status = _sealed_snapshot_status()
    display_status = _display_status(display_backend, environment)
    runtime_status: dict[str, Any]
    if runtime_lock is None:
        runtime_status = {"status": "not-requested"}
    else:
        try:
            with open_verified_runtime(runtime_lock) as opened:
                runtime_status = {
                    "executionAuthority": (
                        "sealed-snapshot" if opened.sealed_snapshot else "trusted-root-file"
                    ),
                    "identity": runtime_identity_to_dict(opened.identity),
                    "status": "verified",
                }
        except RuntimeVerificationError as error:
            runtime_status = {
                "error": _error_text(error),
                "status": "invalid",
            }
    ready = (
        proc_status["status"] == "available"
        and sealed_status["status"] == "available"
        and display_status["status"] == "available"
        and runtime_status["status"] in ("not-requested", "verified")
    )
    return {
        "display": display_status,
        "doctorReportVersion": DOCTOR_REPORT_VERSION,
        "overallStatus": "ready" if ready else "blocked",
        "platform": {
            "procSelfFd": proc_status,
            "sealedRuntimeSnapshots": sealed_status,
        },
        "runtime": runtime_status,
    }


def render_doctor_report(report: Mapping[str, Any]) -> str:
    return _canonical_json(report)


def report_exit_code(report: Mapping[str, Any]) -> int:
    return 0 if report.get("overallStatus") == "ready" else 1


def _logical_profile_path(
    profile_root: os.PathLike[str] | str,
    app_id: str,
) -> str:
    rendered = os.fspath(profile_root)
    if not isinstance(rendered, str):
        raise WorkflowError("profile root path must be text")
    return str(Path(rendered) / app_id)


def _app_shell_readiness(
    manifest: AppManifest,
    runtime_identity: RuntimeIdentity,
    app_shell_capability: os.PathLike[str] | str | None,
    use_bundled_app_shell: bool,
) -> dict[str, Any]:
    if not isinstance(use_bundled_app_shell, bool):
        raise WorkflowError("use_bundled_app_shell must be boolean")
    if app_shell_capability is not None and use_bundled_app_shell:
        raise WorkflowError(
            "external app-shell capability and bundled App Shell are mutually exclusive"
        )
    if app_shell_capability is None and not use_bundled_app_shell:
        raise WorkflowError("an App Shell authority was not selected")
    policy = build_app_shell_policy(manifest)
    if use_bundled_app_shell:
        try:
            with open_bundled_browser_adapter() as opened:
                capability = bundled_adapter_capability(runtime_identity, opened)
                adapter = browser_adapter_to_dict(opened)
        except BrowserAdapterError as error:
            raise WorkflowError(str(error)) from error
    else:
        capability = load_adapter_capability(
            app_shell_capability,
            runtime_identity,
        )
        adapter = None
    result = {
        "adapterCapability": adapter_capability_to_dict(capability),
        "policySha256": app_shell_policy_sha256(policy),
        "status": "ready-for-startup-handshake",
    }
    if adapter is not None:
        result["browserAdapter"] = adapter
    return result


def workflow_status(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    *,
    display_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    app_shell_capability: os.PathLike[str] | str | None = None,
    use_bundled_app_shell: bool = False,
) -> dict[str, Any]:
    """Inspect whether an existing app binding is ready without mutation."""
    logical_profile = _logical_profile_path(profile_root, manifest.app_id)
    verified_identity = None
    runtime_status: dict[str, Any]
    profile_status: dict[str, Any]
    try:
        binding = verify_profile_binding(profile_root, manifest, runtime_lock)
    except ProfileError as profile_error:
        try:
            identity = verify_runtime(runtime_lock)
        except RuntimeVerificationError as runtime_error:
            runtime_status = {
                "error": _error_text(runtime_error),
                "status": "invalid",
            }
            profile_status = {
                "path": logical_profile,
                "status": "unchecked",
            }
        else:
            verified_identity = identity
            runtime_status = {
                "identity": runtime_identity_to_dict(identity),
                "status": "verified",
            }
            profile_status = {
                "error": _error_text(profile_error),
                "path": logical_profile,
                "status": "blocked",
            }
    else:
        verified_identity = binding.runtime_identity
        runtime_status = {
            "identity": runtime_identity_to_dict(binding.runtime_identity),
            "status": "verified",
        }
        profile_status = {
            "manifestSha256": binding.manifest_sha256,
            "path": str(binding.path),
            "status": "ready",
        }

    if display_backend is None:
        display_status: dict[str, Any] = {"status": "legacy-v1"}
    else:
        display_status = _display_status(display_backend, environment)

    app_shell_requested = app_shell_capability is not None or use_bundled_app_shell
    app_shell_status: dict[str, Any] | None = None
    if app_shell_requested:
        if verified_identity is None:
            app_shell_status = {"status": "unchecked"}
        else:
            try:
                app_shell_status = _app_shell_readiness(
                    manifest,
                    verified_identity,
                    app_shell_capability,
                    use_bundled_app_shell,
                )
            except (AppShellError, WorkflowError) as error:
                app_shell_status = {
                    "error": _error_text(error),
                    "status": "blocked",
                }

    ready = (
        runtime_status["status"] == "verified"
        and profile_status["status"] == "ready"
        and display_status["status"] in ("legacy-v1", "available")
        and (
            app_shell_status is None
            or app_shell_status["status"] == "ready-for-startup-handshake"
        )
    )
    result = {
        "appId": manifest.app_id,
        "definition": {
            "manifestSha256": manifest_sha256(manifest),
            "status": "present",
        },
        "display": display_status,
        "overallStatus": "ready" if ready else "blocked",
        "profile": profile_status,
        "runtime": runtime_status,
        "workflowStatusVersion": (
            WORKFLOW_STATUS_VERSION
            if app_shell_status is None
            else (
                BUNDLED_APP_SHELL_WORKFLOW_STATUS_VERSION
                if use_bundled_app_shell
                else APP_SHELL_WORKFLOW_STATUS_VERSION
            )
        ),
    }
    if app_shell_status is not None:
        result["appShell"] = app_shell_status
    return result


def render_workflow_status(status: Mapping[str, Any]) -> str:
    return _canonical_json(status)


def inspect_app_shell(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    capability_path: os.PathLike[str] | str | None = None,
    *,
    display_backend: str | None = None,
    environment: Mapping[str, str] | None = None,
    use_bundled_app_shell: bool = False,
) -> dict[str, Any]:
    """Inspect exact launch authority plus the selected App Shell policy."""
    ticket = inspect_launch(
        profile_root,
        manifest,
        runtime_lock,
        display_backend=display_backend,
        environment=environment,
    )
    readiness = _app_shell_readiness(
        manifest,
        ticket.runtime_identity,
        capability_path,
        use_bundled_app_shell,
    )
    policy = build_app_shell_policy(manifest)
    result = {
        "adapterCapability": readiness["adapterCapability"],
        "appShellInspectionVersion": (
            BUNDLED_APP_SHELL_INSPECTION_VERSION
            if use_bundled_app_shell
            else APP_SHELL_INSPECTION_VERSION
        ),
        "launchTicket": launch_ticket_to_dict(ticket),
        "policy": app_shell_policy_to_dict(policy),
        "policySha256": app_shell_policy_sha256(policy),
        "status": "ready-for-startup-handshake",
    }
    if "browserAdapter" in readiness:
        result["browserAdapter"] = readiness["browserAdapter"]
    return result


def render_app_shell_inspection(inspection: Mapping[str, Any]) -> str:
    return _canonical_json(inspection)


def _ensure_definition(store: AppStore, manifest: AppManifest) -> str:
    try:
        existing = store.get_read_only(manifest.app_id)
    except AppNotFound:
        try:
            store.create(manifest)
            return "created"
        except AppAlreadyExists:
            existing = store.get_read_only(manifest.app_id)
    if existing != manifest:
        raise WorkflowError(
            f"app definition already exists with different content: {manifest.app_id}"
        )
    return "existing"


def install_app(
    store: AppStore,
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    *,
    app_shell_capability: os.PathLike[str] | str | None = None,
    use_bundled_app_shell: bool = False,
) -> dict[str, Any]:
    """Install definition then profile, retaining a repairable definition on failure."""
    identity = verify_runtime(runtime_lock)
    app_shell_requested = app_shell_capability is not None or use_bundled_app_shell
    app_shell_status: dict[str, Any] | None = None
    if app_shell_requested:
        app_shell_status = _app_shell_readiness(
            manifest,
            identity,
            app_shell_capability,
            use_bundled_app_shell,
        )
    definition_status = _ensure_definition(store, manifest)
    profile_result = provision_profile(profile_root, manifest, runtime_lock)
    result = {
        "appId": manifest.app_id,
        "appInstallResultVersion": (
            APP_INSTALL_RESULT_VERSION
            if app_shell_status is None
            else (
                BUNDLED_APP_SHELL_INSTALL_RESULT_VERSION
                if use_bundled_app_shell
                else APP_SHELL_INSTALL_RESULT_VERSION
            )
        ),
        "definitionStatus": definition_status,
        "overallStatus": "ready",
        "profile": profile_provision_result_to_dict(profile_result),
        "runtimeIdentity": runtime_identity_to_dict(identity),
    }
    if app_shell_status is not None:
        result["appShell"] = app_shell_status
    return result


def render_install_result(result: Mapping[str, Any]) -> str:
    return _canonical_json(result)


def complete_web_app_onboarding(
    store: AppStore,
    plan: WebAppOnboardingPlan,
) -> dict[str, Any]:
    """Install one URL-derived app through the existing authoritative workflow."""
    installation = install_app(
        store,
        plan.profile_root,
        plan.manifest,
        plan.runtime_lock,
        use_bundled_app_shell=plan.use_bundled_app_shell,
    )
    return web_app_onboarding_result(plan, installation)


def repair_app(
    profile_root: os.PathLike[str] | str,
    manifest: AppManifest,
    runtime_lock: os.PathLike[str] | str,
    *,
    app_shell_capability: os.PathLike[str] | str | None = None,
    use_bundled_app_shell: bool = False,
) -> dict[str, Any]:
    """Idempotently provision the exact existing definition/runtime binding."""
    identity = verify_runtime(runtime_lock)
    app_shell_requested = app_shell_capability is not None or use_bundled_app_shell
    app_shell_status: dict[str, Any] | None = None
    if app_shell_requested:
        app_shell_status = _app_shell_readiness(
            manifest,
            identity,
            app_shell_capability,
            use_bundled_app_shell,
        )
    profile_result = provision_profile(profile_root, manifest, runtime_lock)
    result = {
        "appId": manifest.app_id,
        "appRepairResultVersion": (
            APP_REPAIR_RESULT_VERSION
            if app_shell_status is None
            else (
                BUNDLED_APP_SHELL_REPAIR_RESULT_VERSION
                if use_bundled_app_shell
                else APP_SHELL_REPAIR_RESULT_VERSION
            )
        ),
        "overallStatus": "ready",
        "profile": profile_provision_result_to_dict(profile_result),
        "runtimeIdentity": runtime_identity_to_dict(identity),
    }
    if app_shell_status is not None:
        result["appShell"] = app_shell_status
    return result


def render_repair_result(result: Mapping[str, Any]) -> str:
    return _canonical_json(result)
