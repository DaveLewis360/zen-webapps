"""Versioned, network-free onboarding for creating a web app from one HTTPS URL.

Version 1 preserves the terminal/legacy launch contract. Version 2 opts into the
pinned privileged browser adapter, whose own onboarding remains ahead of site
content and requires the policy-fd pre-content handshake during execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .manifest import AppManifest, create_manifest, manifest_to_dict

WEB_APP_ONBOARDING_PLAN_VERSION = 1
WEB_APP_ONBOARDING_APP_SHELL_PLAN_VERSION = 2
WEB_APP_ONBOARDING_RESULT_VERSION = 1
WEB_APP_ONBOARDING_APP_SHELL_RESULT_VERSION = 2
ONBOARDING_DISPLAY_BACKENDS = frozenset({"auto", "wayland", "x11"})


class OnboardingError(ValueError):
    """A URL-to-webapp onboarding request violated its closed contract."""


@dataclass(frozen=True)
class WebAppOnboardingPlan:
    plan_version: int
    manifest: AppManifest
    profile_root: str
    runtime_lock: str
    display_backend: str
    launch_requested: bool
    use_bundled_app_shell: bool = False


def _absolute_normalized_path(value: os.PathLike[str] | str, label: str) -> str:
    if isinstance(value, bytes):
        raise OnboardingError(f"{label} path must be text")
    try:
        rendered = os.fspath(value)
    except TypeError as error:
        raise OnboardingError(f"{label} path must be path-like text") from error
    if not isinstance(rendered, str):
        raise OnboardingError(f"{label} path must be text")
    if not rendered.startswith("/"):
        raise OnboardingError(f"{label} path must be absolute")
    if rendered == "/" or rendered.startswith("//"):
        raise OnboardingError(f"{label} path must name an absolute normalized path")
    if any(component in ("", ".", "..") for component in rendered[1:].split("/")):
        raise OnboardingError(f"{label} path must be absolute and normalized")
    if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
        raise OnboardingError(f"{label} path cannot contain control characters")
    return rendered


def canonicalize_web_app_url(value: str) -> str:
    """Canonicalize only URL syntax; never fetch metadata or follow redirects."""
    if not isinstance(value, str):
        raise OnboardingError("web app URL must be text")
    if (
        not value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise OnboardingError(
            "web app URL cannot contain whitespace, control characters, or backslashes"
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise OnboardingError(f"web app URL is invalid: {error}") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OnboardingError("web app URL must be an absolute credential-free HTTPS URL")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise OnboardingError("web app URL hostname is invalid") from error
    if not host or "%" in host:
        raise OnboardingError("web app URL hostname is invalid")
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host + (f":{port}" if port not in (None, 443) else "")
    canonical = urlunsplit(("https", authority, parsed.path or "/", parsed.query, parsed.fragment))
    try:
        create_manifest("web-app", "Web App", canonical)
    except ValueError as error:
        raise OnboardingError(str(error)) from error
    return canonical


def default_web_app_name(canonical_url: str) -> str:
    """Derive a short local display name without trusting remote page content."""
    canonical = canonicalize_web_app_url(canonical_url)
    host = urlsplit(canonical).hostname or ""
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        return "Web App"
    labels = host.split(".")
    if labels[0] == "www" and len(labels) > 1:
        labels = labels[1:]
    label = labels[0]
    try:
        label = label.encode("ascii").decode("idna")
    except (UnicodeError, UnicodeDecodeError):
        pass
    words = [word for word in re.split(r"[-_]+", label) if word]
    name = " ".join(word[:1].upper() + word[1:] for word in words).strip()
    return (name or "Web App")[:80].rstrip()


def default_web_app_id(canonical_url: str) -> str:
    """Derive a stable collision-resistant app ID from the complete canonical URL."""
    canonical = canonicalize_web_app_url(canonical_url)
    host = urlsplit(canonical).hostname or "web-app"
    base = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    if base.startswith("www-"):
        base = base[4:]
    base = (base or "web-app")[:53].rstrip("-") or "web-app"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


def build_web_app_onboarding_plan(
    start_url: str,
    profile_root: os.PathLike[str] | str,
    runtime_lock: os.PathLike[str] | str,
    *,
    app_id: str | None = None,
    name: str | None = None,
    mode: str = "Opaque",
    include_stylus: bool = False,
    display_backend: str = "auto",
    launch_requested: bool = False,
    use_bundled_app_shell: bool = False,
) -> WebAppOnboardingPlan:
    """Build a deterministic side-effect-free onboarding plan from one link."""
    canonical_url = canonicalize_web_app_url(start_url)
    manifest = create_manifest(
        app_id=default_web_app_id(canonical_url) if app_id is None else app_id,
        name=default_web_app_name(canonical_url) if name is None else name,
        start_url=canonical_url,
        mode=mode,
        include_stylus=include_stylus,
    )
    if display_backend not in ONBOARDING_DISPLAY_BACKENDS:
        raise OnboardingError(
            f"display backend must be one of {sorted(ONBOARDING_DISPLAY_BACKENDS)}"
        )
    if not isinstance(launch_requested, bool):
        raise OnboardingError("launch_requested must be boolean")
    if not isinstance(use_bundled_app_shell, bool):
        raise OnboardingError("use_bundled_app_shell must be boolean")
    return WebAppOnboardingPlan(
        plan_version=(
            WEB_APP_ONBOARDING_APP_SHELL_PLAN_VERSION
            if use_bundled_app_shell
            else WEB_APP_ONBOARDING_PLAN_VERSION
        ),
        manifest=manifest,
        profile_root=_absolute_normalized_path(profile_root, "profile root"),
        runtime_lock=_absolute_normalized_path(runtime_lock, "runtime lock"),
        display_backend=display_backend,
        launch_requested=launch_requested,
        use_bundled_app_shell=use_bundled_app_shell,
    )


def web_app_onboarding_plan_to_dict(plan: WebAppOnboardingPlan) -> dict[str, Any]:
    if not isinstance(plan.use_bundled_app_shell, bool):
        raise OnboardingError("onboarding bundled App Shell request must be boolean")
    expected_version = (
        WEB_APP_ONBOARDING_APP_SHELL_PLAN_VERSION
        if plan.use_bundled_app_shell
        else WEB_APP_ONBOARDING_PLAN_VERSION
    )
    if plan.plan_version != expected_version:
        raise OnboardingError(
            f"webAppOnboardingPlanVersion must be {expected_version}"
        )
    if plan.display_backend not in ONBOARDING_DISPLAY_BACKENDS:
        raise OnboardingError("onboarding display backend is invalid")
    if not isinstance(plan.launch_requested, bool):
        raise OnboardingError("onboarding launch request must be boolean")
    browser_experience: dict[str, Any]
    if plan.use_bundled_app_shell:
        browser_experience = {
            "inBrowserAppShell": True,
            "mode": "bundled-privileged-app-shell",
            "startupAuthority": "policy-fd-and-pre-content-handshake",
        }
    else:
        browser_experience = {
            "inBrowserAppShell": False,
            "mode": "legacy-isolated-browser",
        }
    return {
        "actions": {
            "installDefinitionAndProfile": True,
            "launchAfterInstall": plan.launch_requested,
        },
        "browserExperience": browser_experience,
        "confirmation": "required",
        "displayBackend": plan.display_backend,
        "manifest": manifest_to_dict(plan.manifest),
        "profileRoot": _absolute_normalized_path(plan.profile_root, "profile root"),
        "runtimeLock": _absolute_normalized_path(plan.runtime_lock, "runtime lock"),
        "webAppOnboardingPlanVersion": expected_version,
    }


def render_web_app_onboarding_plan(plan: WebAppOnboardingPlan) -> str:
    return json.dumps(
        web_app_onboarding_plan_to_dict(plan),
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def web_app_onboarding_result(
    plan: WebAppOnboardingPlan,
    installation: Mapping[str, Any],
) -> dict[str, Any]:
    """Wrap the authoritative legacy or bundled-adapter installation result."""
    plan_data = web_app_onboarding_plan_to_dict(plan)
    if not isinstance(installation, Mapping):
        raise OnboardingError("onboarding installation result must be an object")
    if installation.get("appId") != plan.manifest.app_id:
        raise OnboardingError("onboarding installation result belongs to another app")
    if installation.get("overallStatus") != "ready":
        raise OnboardingError("onboarding installation did not reach ready state")
    result_version = (
        WEB_APP_ONBOARDING_APP_SHELL_RESULT_VERSION
        if plan.use_bundled_app_shell
        else WEB_APP_ONBOARDING_RESULT_VERSION
    )
    return {
        "appId": plan.manifest.app_id,
        "browserExperience": plan_data["browserExperience"],
        "installation": dict(installation),
        "launch": {
            "displayBackend": plan.display_backend,
            "status": "starting" if plan.launch_requested else "not-requested",
        },
        "onboardingStatus": "completed",
        "startUrl": plan.manifest.start_url,
        "webAppOnboardingResultVersion": result_version,
    }


def render_web_app_onboarding_result(result: Mapping[str, Any]) -> str:
    return json.dumps(result, allow_nan=False, indent=2, sort_keys=True) + "\n"
