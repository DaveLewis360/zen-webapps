"""Closed App Shell permission, download, and portal mediation contracts.

The persisted documents are deliberately privacy bounded: they bind an opaque
request token to the exact policy, origin, document incarnation, navigation
epoch, closed request kinds, timing, and one-shot decision.  They never carry a
URL path, filename, device identifier/label, message content, or portal restore
token.  Runtime callbacks and selected files/devices remain process-local.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

APP_SHELL_MEDIATION_REQUEST_VERSION = 1
APP_SHELL_MEDIATION_DECISION_VERSION = 1
MEDIATION_REQUEST_TTL_MS = 30_000
MEDIATION_REQUEST_KINDS = (
    "camera",
    "clipboard-read",
    "clipboard-write",
    "download",
    "file-picker",
    "microphone",
    "notifications",
    "screen-capture",
    "speaker-selection",
)
MEDIATION_DECISION_OUTCOMES = (
    "allow-once",
    "continue-to-portal",
    "deny",
)
MEDIATION_INVALIDATION_REASONS = (
    "navigation",
    "process-exit",
    "reload",
    "request-expired",
    "window-close",
)

_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_ACTIVATION_KINDS = frozenset(MEDIATION_REQUEST_KINDS) - {"download"}
_SYSTEM_PICKER_KINDS = frozenset({"download", "file-picker"})
_DOWNSTREAM_AUTHORITIES = frozenset(
    {
        "none",
        "system-file-picker-required",
        "xdg-desktop-portal-required",
    }
)


class AppShellMediationError(ValueError):
    """A mediation document or one-shot state transition failed closed."""


@dataclass(frozen=True)
class AppShellMediationContext:
    origin: str
    document_id: int
    navigation_epoch: int


@dataclass(frozen=True)
class AppShellMediationRequest:
    request_version: int
    request_id: str
    policy_sha256: str
    context: AppShellMediationContext
    kinds: tuple[str, ...]
    requested_at_monotonic_ms: int
    expires_at_monotonic_ms: int
    content_user_activation: bool
    portal_required: bool
    system_picker_required: bool


@dataclass(frozen=True)
class AppShellMediationDecision:
    decision_version: int
    request_id: str
    request_sha256: str
    outcome: str
    decided_at_monotonic_ms: int
    privileged_user_activation: bool
    session_only: bool
    persistence: str
    downstream_authority: str
    claims_downstream_consent: bool


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise AppShellMediationError(f"{label} must be an object with text keys")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise AppShellMediationError(f"{label} fields: {', '.join(details)}")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AppShellMediationError(f"{label} must be an integer at least {minimum}")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise AppShellMediationError(f"{label} must be a boolean")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise AppShellMediationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _request_id(value: Any) -> str:
    if not isinstance(value, str) or not _REQUEST_ID_PATTERN.fullmatch(value):
        raise AppShellMediationError("requestId must be 128-bit lowercase hexadecimal")
    return value


def _canonical_origin(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or "\\" in value
    ):
        raise AppShellMediationError("mediation origin is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise AppShellMediationError("mediation origin must be a credential-free HTTPS origin")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (UnicodeError, ValueError) as error:
        raise AppShellMediationError(f"mediation origin is invalid: {error}") from error
    rendered_host = f"[{host}]" if ":" in host else host
    canonical = f"https://{rendered_host}" + (
        f":{port}" if port not in (None, 443) else ""
    )
    if not host or value != canonical:
        raise AppShellMediationError("mediation origin must be canonical")
    return canonical


def _context_to_dict(context: AppShellMediationContext) -> dict[str, Any]:
    data = {
        "documentId": context.document_id,
        "navigationEpoch": context.navigation_epoch,
        "origin": context.origin,
    }
    _context_from_dict(data)
    return data


def _context_from_dict(value: Any) -> AppShellMediationContext:
    data = _mapping(value, "mediation context")
    _exact_keys(
        data,
        frozenset({"documentId", "navigationEpoch", "origin"}),
        "mediation context",
    )
    return AppShellMediationContext(
        origin=_canonical_origin(data["origin"]),
        document_id=_integer(data["documentId"], "documentId", minimum=1),
        navigation_epoch=_integer(
            data["navigationEpoch"],
            "navigationEpoch",
            minimum=0,
        ),
    )


def _kinds(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AppShellMediationError("kinds must be a non-empty array")
    if not all(isinstance(item, str) for item in value):
        raise AppShellMediationError("kinds must contain only strings")
    kinds = tuple(value)
    if kinds != tuple(sorted(set(kinds))):
        raise AppShellMediationError("kinds must be sorted and duplicate-free")
    unknown = set(kinds) - set(MEDIATION_REQUEST_KINDS)
    if unknown:
        raise AppShellMediationError(f"unknown mediation kinds: {sorted(unknown)}")
    exclusive = {"download", "file-picker", "screen-capture"}
    if len(kinds) > 1 and exclusive.intersection(kinds):
        raise AppShellMediationError(
            "download, file-picker, and screen-capture requests must be exclusive"
        )
    return kinds


def _validate_request(request: AppShellMediationRequest) -> None:
    if request.request_version != APP_SHELL_MEDIATION_REQUEST_VERSION:
        raise AppShellMediationError(
            f"mediationRequestVersion must be {APP_SHELL_MEDIATION_REQUEST_VERSION}"
        )
    _request_id(request.request_id)
    _digest(request.policy_sha256, "policySha256")
    _context_to_dict(request.context)
    kinds = _kinds(list(request.kinds))
    requested = _integer(
        request.requested_at_monotonic_ms,
        "requestedAtMonotonicMs",
    )
    expires = _integer(
        request.expires_at_monotonic_ms,
        "expiresAtMonotonicMs",
    )
    if expires != requested + MEDIATION_REQUEST_TTL_MS:
        raise AppShellMediationError(
            f"expiresAtMonotonicMs must equal request time plus {MEDIATION_REQUEST_TTL_MS}"
        )
    activation = _boolean(
        request.content_user_activation,
        "contentUserActivation",
    )
    if _CONTENT_ACTIVATION_KINDS.intersection(kinds) and not activation:
        raise AppShellMediationError(
            "this mediation kind requires content user activation"
        )
    portal_required = _boolean(request.portal_required, "portalRequired")
    if portal_required != ("screen-capture" in kinds):
        raise AppShellMediationError(
            "portalRequired must be true exactly for screen-capture"
        )
    system_picker_required = _boolean(
        request.system_picker_required,
        "systemPickerRequired",
    )
    if system_picker_required != bool(_SYSTEM_PICKER_KINDS.intersection(kinds)):
        raise AppShellMediationError(
            "systemPickerRequired must match download or file-picker kinds"
        )


def build_app_shell_mediation_request(
    *,
    request_id: str,
    policy_sha256: str,
    context: AppShellMediationContext,
    kinds: tuple[str, ...],
    requested_at_ms: int,
    content_user_activation: bool,
) -> AppShellMediationRequest:
    """Build one closed request whose derived boundaries cannot be fabricated."""
    request = AppShellMediationRequest(
        request_version=APP_SHELL_MEDIATION_REQUEST_VERSION,
        request_id=request_id,
        policy_sha256=policy_sha256,
        context=context,
        kinds=tuple(sorted(kinds)),
        requested_at_monotonic_ms=requested_at_ms,
        expires_at_monotonic_ms=requested_at_ms + MEDIATION_REQUEST_TTL_MS,
        content_user_activation=content_user_activation,
        portal_required="screen-capture" in kinds,
        system_picker_required=bool(_SYSTEM_PICKER_KINDS.intersection(kinds)),
    )
    _validate_request(request)
    return request


def app_shell_mediation_request_to_dict(
    request: AppShellMediationRequest,
) -> dict[str, Any]:
    _validate_request(request)
    return {
        "contentUserActivation": request.content_user_activation,
        "context": _context_to_dict(request.context),
        "expiresAtMonotonicMs": request.expires_at_monotonic_ms,
        "kinds": list(request.kinds),
        "mediationRequestVersion": request.request_version,
        "policySha256": request.policy_sha256,
        "portalRequired": request.portal_required,
        "requestId": request.request_id,
        "requestedAtMonotonicMs": request.requested_at_monotonic_ms,
        "systemPickerRequired": request.system_picker_required,
    }


def app_shell_mediation_request_from_dict(value: Any) -> AppShellMediationRequest:
    data = _mapping(value, "App Shell mediation request")
    _exact_keys(
        data,
        frozenset(
            {
                "contentUserActivation",
                "context",
                "expiresAtMonotonicMs",
                "kinds",
                "mediationRequestVersion",
                "policySha256",
                "portalRequired",
                "requestId",
                "requestedAtMonotonicMs",
                "systemPickerRequired",
            }
        ),
        "App Shell mediation request",
    )
    request = AppShellMediationRequest(
        request_version=_integer(
            data["mediationRequestVersion"],
            "mediationRequestVersion",
            minimum=1,
        ),
        request_id=_request_id(data["requestId"]),
        policy_sha256=_digest(data["policySha256"], "policySha256"),
        context=_context_from_dict(data["context"]),
        kinds=_kinds(data["kinds"]),
        requested_at_monotonic_ms=_integer(
            data["requestedAtMonotonicMs"],
            "requestedAtMonotonicMs",
        ),
        expires_at_monotonic_ms=_integer(
            data["expiresAtMonotonicMs"],
            "expiresAtMonotonicMs",
        ),
        content_user_activation=_boolean(
            data["contentUserActivation"],
            "contentUserActivation",
        ),
        portal_required=_boolean(data["portalRequired"], "portalRequired"),
        system_picker_required=_boolean(
            data["systemPickerRequired"],
            "systemPickerRequired",
        ),
    )
    _validate_request(request)
    return request


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise AppShellMediationError(
            f"mediation document is not canonical JSON data: {error}"
        ) from error


def render_app_shell_mediation_request(request: AppShellMediationRequest) -> str:
    return _canonical_json(app_shell_mediation_request_to_dict(request))


def app_shell_mediation_request_sha256(request: AppShellMediationRequest) -> str:
    return hashlib.sha256(render_app_shell_mediation_request(request).encode("ascii")).hexdigest()


def _validate_decision(decision: AppShellMediationDecision) -> None:
    if decision.decision_version != APP_SHELL_MEDIATION_DECISION_VERSION:
        raise AppShellMediationError(
            f"mediationDecisionVersion must be {APP_SHELL_MEDIATION_DECISION_VERSION}"
        )
    _request_id(decision.request_id)
    _digest(decision.request_sha256, "requestSha256")
    if decision.outcome not in MEDIATION_DECISION_OUTCOMES:
        raise AppShellMediationError("mediation outcome is not in the closed vocabulary")
    _integer(decision.decided_at_monotonic_ms, "decidedAtMonotonicMs")
    privileged = _boolean(
        decision.privileged_user_activation,
        "privilegedUserActivation",
    )
    if decision.outcome != "deny" and not privileged:
        raise AppShellMediationError(
            "allowing mediation requires privileged user activation"
        )
    if not _boolean(decision.session_only, "sessionOnly"):
        raise AppShellMediationError("mediation decisions must be session-only")
    if decision.persistence != "none":
        raise AppShellMediationError("mediation persistence must be none")
    if decision.downstream_authority not in _DOWNSTREAM_AUTHORITIES:
        raise AppShellMediationError("downstreamAuthority is invalid")
    if decision.outcome == "deny" and decision.downstream_authority != "none":
        raise AppShellMediationError("denial cannot delegate downstream authority")
    if (
        decision.outcome == "continue-to-portal"
        and decision.downstream_authority != "xdg-desktop-portal-required"
    ):
        raise AppShellMediationError(
            "continue-to-portal must require xdg-desktop-portal"
        )
    if decision.outcome == "allow-once" and decision.downstream_authority not in (
        "none",
        "system-file-picker-required",
    ):
        raise AppShellMediationError(
            "allow-once cannot claim xdg-desktop-portal authority"
        )
    if decision.claims_downstream_consent is not False:
        raise AppShellMediationError(
            "the shell decision cannot claim downstream picker or portal consent"
        )


def app_shell_mediation_decision_to_dict(
    decision: AppShellMediationDecision,
) -> dict[str, Any]:
    _validate_decision(decision)
    return {
        "claimsDownstreamConsent": decision.claims_downstream_consent,
        "decidedAtMonotonicMs": decision.decided_at_monotonic_ms,
        "downstreamAuthority": decision.downstream_authority,
        "mediationDecisionVersion": decision.decision_version,
        "outcome": decision.outcome,
        "persistence": decision.persistence,
        "privilegedUserActivation": decision.privileged_user_activation,
        "requestId": decision.request_id,
        "requestSha256": decision.request_sha256,
        "sessionOnly": decision.session_only,
    }


def app_shell_mediation_decision_from_dict(value: Any) -> AppShellMediationDecision:
    data = _mapping(value, "App Shell mediation decision")
    _exact_keys(
        data,
        frozenset(
            {
                "claimsDownstreamConsent",
                "decidedAtMonotonicMs",
                "downstreamAuthority",
                "mediationDecisionVersion",
                "outcome",
                "persistence",
                "privilegedUserActivation",
                "requestId",
                "requestSha256",
                "sessionOnly",
            }
        ),
        "App Shell mediation decision",
    )
    if not isinstance(data["outcome"], str):
        raise AppShellMediationError("outcome must be a string")
    if not isinstance(data["persistence"], str):
        raise AppShellMediationError("persistence must be a string")
    if not isinstance(data["downstreamAuthority"], str):
        raise AppShellMediationError("downstreamAuthority must be a string")
    decision = AppShellMediationDecision(
        decision_version=_integer(
            data["mediationDecisionVersion"],
            "mediationDecisionVersion",
            minimum=1,
        ),
        request_id=_request_id(data["requestId"]),
        request_sha256=_digest(data["requestSha256"], "requestSha256"),
        outcome=data["outcome"],
        decided_at_monotonic_ms=_integer(
            data["decidedAtMonotonicMs"],
            "decidedAtMonotonicMs",
        ),
        privileged_user_activation=_boolean(
            data["privilegedUserActivation"],
            "privilegedUserActivation",
        ),
        session_only=_boolean(data["sessionOnly"], "sessionOnly"),
        persistence=data["persistence"],
        downstream_authority=data["downstreamAuthority"],
        claims_downstream_consent=_boolean(
            data["claimsDownstreamConsent"],
            "claimsDownstreamConsent",
        ),
    )
    _validate_decision(decision)
    return decision


def render_app_shell_mediation_decision(decision: AppShellMediationDecision) -> str:
    return _canonical_json(app_shell_mediation_decision_to_dict(decision))


def app_shell_mediation_decision_sha256(decision: AppShellMediationDecision) -> str:
    return hashlib.sha256(render_app_shell_mediation_decision(decision).encode("ascii")).hexdigest()


class AppShellMediationAuthority:
    """One-session, one-outstanding-request, replay-resistant authority."""

    def __init__(
        self,
        *,
        policy_sha256: str,
        initial_context: AppShellMediationContext,
    ) -> None:
        self._policy_sha256 = _digest(policy_sha256, "policySha256")
        _context_to_dict(initial_context)
        self._context = initial_context
        self._pending: AppShellMediationRequest | None = None
        self._consumed: set[str] = set()

    @property
    def pending_request(self) -> AppShellMediationRequest | None:
        return self._pending

    @property
    def context(self) -> AppShellMediationContext:
        return self._context

    def open_request(
        self,
        request: AppShellMediationRequest,
        *,
        now_ms: int,
    ) -> None:
        app_shell_mediation_request_to_dict(request)
        now = _integer(now_ms, "nowMs")
        digest = app_shell_mediation_request_sha256(request)
        if digest in self._consumed:
            raise AppShellMediationError("mediation request was replayed")
        if self._pending is not None:
            raise AppShellMediationError("another mediation request is outstanding")
        if request.policy_sha256 != self._policy_sha256:
            raise AppShellMediationError("mediation request is bound to another policy")
        if request.context != self._context:
            raise AppShellMediationError("mediation request context does not match")
        if now < request.requested_at_monotonic_ms:
            raise AppShellMediationError("mediation request is from the future")
        if now > request.expires_at_monotonic_ms:
            self._consumed.add(digest)
            raise AppShellMediationError("mediation request expired")
        self._pending = request

    def decide(
        self,
        *,
        request_id: str,
        request_sha256: str,
        outcome: str,
        now_ms: int,
        current_context: AppShellMediationContext,
        privileged_user_activation: bool,
    ) -> AppShellMediationDecision:
        token = _request_id(request_id)
        digest = _digest(request_sha256, "requestSha256")
        now = _integer(now_ms, "nowMs")
        _context_to_dict(current_context)
        privileged = _boolean(
            privileged_user_activation,
            "privilegedUserActivation",
        )
        if digest in self._consumed:
            raise AppShellMediationError("mediation request was replayed")
        request = self._pending
        if request is None:
            raise AppShellMediationError("no mediation request is outstanding")
        actual_digest = app_shell_mediation_request_sha256(request)
        if now > request.expires_at_monotonic_ms:
            self._pending = None
            self._consumed.add(actual_digest)
            raise AppShellMediationError("mediation request expired")
        if token != request.request_id or digest != actual_digest:
            raise AppShellMediationError("mediation decision does not bind the pending request")
        if current_context != self._context or current_context != request.context:
            raise AppShellMediationError("mediation decision context does not match")
        if outcome not in MEDIATION_DECISION_OUTCOMES:
            raise AppShellMediationError("mediation outcome is not in the closed vocabulary")
        if outcome != "deny" and not privileged:
            raise AppShellMediationError(
                "allowing mediation requires privileged user activation"
            )
        if request.portal_required:
            if outcome not in ("continue-to-portal", "deny"):
                raise AppShellMediationError(
                    "screen capture can only deny or continue-to-portal"
                )
        elif outcome == "continue-to-portal":
            raise AppShellMediationError(
                "continue-to-portal is valid only for screen capture"
            )

        if outcome == "deny":
            downstream = "none"
        elif request.portal_required:
            downstream = "xdg-desktop-portal-required"
        elif request.system_picker_required:
            downstream = "system-file-picker-required"
        else:
            downstream = "none"
        decision = AppShellMediationDecision(
            decision_version=APP_SHELL_MEDIATION_DECISION_VERSION,
            request_id=request.request_id,
            request_sha256=actual_digest,
            outcome=outcome,
            decided_at_monotonic_ms=now,
            privileged_user_activation=privileged,
            session_only=True,
            persistence="none",
            downstream_authority=downstream,
            claims_downstream_consent=False,
        )
        app_shell_mediation_decision_to_dict(decision)
        self._pending = None
        self._consumed.add(actual_digest)
        return decision

    def invalidate(
        self,
        reason: str,
        *,
        next_context: AppShellMediationContext | None = None,
    ) -> AppShellMediationRequest | None:
        if reason not in MEDIATION_INVALIDATION_REASONS:
            raise AppShellMediationError("mediation invalidation reason is invalid")
        if next_context is not None:
            _context_to_dict(next_context)
            self._context = next_context
        pending = self._pending
        self._pending = None
        if pending is not None:
            self._consumed.add(app_shell_mediation_request_sha256(pending))
        return pending
