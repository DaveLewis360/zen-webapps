"""Pure, deterministic launch-plan models and JSON rendering.

A launch plan describes a requested future browser invocation. It does not
create profiles, inspect executables, launch processes, or enforce capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .manifest import APP_ID_PATTERN, AppManifest, CURRENT_SCHEMA_VERSION, MODES, manifest_to_dict

CURRENT_PLAN_VERSION = 1
REQUESTED_NOT_ENFORCED = "requested-not-enforced"
UNSUPPORTED = "unsupported"
_CAPABILITY_STATUSES = frozenset({REQUESTED_NOT_ENFORCED, UNSUPPORTED})
_FIXED_LINK_POLICY = {
    "allowUserOverride": False,
    "defaultAction": "external",
    "externalTarget": "system-default-browser",
    "internalNavigation": "same-origin",
}


class LaunchPlanError(ValueError):
    """Raised when a launch plan would be ambiguous or unsafe to render."""


@dataclass(frozen=True)
class Capability:
    name: str
    requested: Any
    status: str


@dataclass(frozen=True)
class LaunchPlan:
    plan_version: int
    manifest_schema_version: int
    app_id: str
    name: str
    start_url: str
    runtime_path: str
    launcher_path: str
    profile_root: str
    profile_path: str
    argv: tuple[str, ...]
    requested_extensions: tuple[tuple[str, str], ...]
    capabilities: tuple[Capability, ...]


def _absolute_path(value: os.PathLike[str] | str, label: str) -> str:
    if isinstance(value, bytes):
        raise LaunchPlanError(f"{label} path must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise LaunchPlanError(f"{label} path must be path-like text") from error
    if not isinstance(rendered, str):
        raise LaunchPlanError(f"{label} path must be text")
    if not Path(rendered).is_absolute():
        raise LaunchPlanError(f"{label} path must be absolute")
    if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
        raise LaunchPlanError(f"{label} path cannot contain control characters")
    return rendered


def build_launch_plan(
    manifest: AppManifest,
    runtime_path: os.PathLike[str] | str,
    launcher_path: os.PathLike[str] | str,
    profile_root: os.PathLike[str] | str,
) -> LaunchPlan:
    """Build an inert plan from a validated schema-v1 manifest and explicit paths."""
    manifest_data = manifest_to_dict(manifest)
    runtime = _absolute_path(runtime_path, "runtime")
    launcher = _absolute_path(launcher_path, "launcher")
    root = _absolute_path(profile_root, "profile root")
    profile = str(Path(root) / manifest.app_id)
    requested_extensions = tuple((item.id, item.selection) for item in manifest.extensions)

    capabilities = (
        Capability("presentation-mode", manifest.configuration.mode, REQUESTED_NOT_ENFORCED),
        Capability("profile-isolation", profile, REQUESTED_NOT_ENFORCED),
        Capability("link-policy", dict(manifest_data["linkPolicy"]), REQUESTED_NOT_ENFORCED),
        *(
            Capability(f"extension:{extension_id}", selection, UNSUPPORTED)
            for extension_id, selection in requested_extensions
        ),
    )
    return LaunchPlan(
        plan_version=CURRENT_PLAN_VERSION,
        manifest_schema_version=manifest.schema_version,
        app_id=manifest.app_id,
        name=manifest.name,
        start_url=manifest.start_url,
        runtime_path=runtime,
        launcher_path=launcher,
        profile_root=root,
        profile_path=profile,
        argv=(runtime, "--profile", profile, manifest.start_url),
        requested_extensions=requested_extensions,
        capabilities=capabilities,
    )


def _validate_capabilities(plan: LaunchPlan, expected_profile: str) -> None:
    if plan.requested_extensions not in (
        (("zen-internet", "bundled"),),
        (("zen-internet", "bundled"), ("stylus", "optional")),
    ):
        raise LaunchPlanError("capabilities do not represent approved requested extensions")
    if len(plan.capabilities) != 3 + len(plan.requested_extensions):
        raise LaunchPlanError("capabilities are incomplete")

    presentation, profile, link_policy, *extensions = plan.capabilities
    if (
        presentation.name != "presentation-mode"
        or presentation.requested not in MODES
        or presentation.status != REQUESTED_NOT_ENFORCED
        or profile != Capability("profile-isolation", expected_profile, REQUESTED_NOT_ENFORCED)
        or link_policy != Capability("link-policy", _FIXED_LINK_POLICY, REQUESTED_NOT_ENFORCED)
    ):
        raise LaunchPlanError("capabilities are incomplete or mislabeled")
    expected_extensions = tuple(
        Capability(f"extension:{extension_id}", selection, UNSUPPORTED)
        for extension_id, selection in plan.requested_extensions
    )
    if tuple(extensions) != expected_extensions:
        raise LaunchPlanError("capabilities are incomplete or mislabeled")
    if any(capability.status not in _CAPABILITY_STATUSES for capability in plan.capabilities):
        raise LaunchPlanError("capability status is invalid")


def launch_plan_to_dict(plan: LaunchPlan) -> dict[str, Any]:
    """Serialize a complete plan after validating its independent version and shape."""
    if plan.plan_version != CURRENT_PLAN_VERSION:
        raise LaunchPlanError(f"planVersion must be {CURRENT_PLAN_VERSION}")
    if plan.manifest_schema_version != CURRENT_SCHEMA_VERSION:
        raise LaunchPlanError(f"manifestSchemaVersion must be {CURRENT_SCHEMA_VERSION}")
    if not isinstance(plan.app_id, str) or not APP_ID_PATTERN.fullmatch(plan.app_id):
        raise LaunchPlanError("appId must remain a schema-v1 lowercase ASCII slug")
    runtime = _absolute_path(plan.runtime_path, "runtime")
    launcher = _absolute_path(plan.launcher_path, "launcher")
    root = _absolute_path(plan.profile_root, "profile root")
    expected_profile = str(Path(root) / plan.app_id)
    if plan.profile_path != expected_profile:
        raise LaunchPlanError("profilePath must be derived from profileRoot and appId")
    expected_argv = (runtime, "--profile", expected_profile, plan.start_url)
    if plan.argv != expected_argv or not all(isinstance(token, str) for token in plan.argv):
        raise LaunchPlanError("argv must be the ordered launch token sequence")
    _validate_capabilities(plan, expected_profile)

    # LaunchPlan is a public frozen dataclass, so callers can construct one
    # without going through build_launch_plan(). Revalidate every field that
    # claims schema-v1 provenance before serializing such an instance.
    from .manifest import ManifestError, create_manifest

    try:
        create_manifest(
            plan.app_id,
            plan.name,
            plan.start_url,
            mode=plan.capabilities[0].requested,
            include_stylus=len(plan.requested_extensions) == 2,
        )
    except ManifestError as error:
        raise LaunchPlanError("plan contains invalid schema-v1 manifest fields") from error

    return {
        "planVersion": plan.plan_version,
        "manifestSchemaVersion": plan.manifest_schema_version,
        "appId": plan.app_id,
        "name": plan.name,
        "startUrl": plan.start_url,
        "runtimePath": runtime,
        "launcherPath": launcher,
        "profileRoot": root,
        "profilePath": expected_profile,
        "argv": list(plan.argv),
        "capabilities": [
            {
                "name": capability.name,
                "requested": capability.requested,
                "status": capability.status,
            }
            for capability in plan.capabilities
        ],
    }


def render_launch_plan(plan: LaunchPlan) -> str:
    """Render deterministic, sorted, indented JSON with exactly one trailing LF."""
    return json.dumps(launch_plan_to_dict(plan), indent=2, sort_keys=True) + "\n"
