"""Sentry error monitoring for the hosted Appwrite MCP server.

Sentry is intentionally separate from the OpenTelemetry metrics module: metrics
stay aggregate and Sentry receives only publishable error events. Like hosted
metrics, this is disabled for stdio so self-hosted local processes do not phone
home.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from appwrite.exception import AppwriteException

_enabled = False

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
    "x_appwrite_key",
}


def _log(message: str) -> None:
    print(f"[appwrite-mcp][sentry] {message}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    return _enabled


def init_error_monitoring(transport: str, version: str) -> bool:
    """Configure Sentry for hosted HTTP deployments.

    Returns True when Sentry is enabled. A no-op unless ``SENTRY_DSN`` is set and
    the server runs the hosted HTTP transport.
    """
    global _enabled
    if _enabled:
        return True

    if transport != "http":
        return False

    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        _log("disabled: no SENTRY_DSN configured")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            release=os.getenv("SENTRY_RELEASE") or version,
            environment=os.getenv("SENTRY_ENVIRONMENT"),
            integrations=[StarletteIntegration()],
            send_default_pii=False,
            traces_sample_rate=0.0,
            profiles_sample_rate=0.0,
            before_send=_before_send,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log(f"disabled: failed to initialize ({exc})")
        return False

    _enabled = True
    _log("enabled")
    return True


def capture_exception(
    exc: BaseException,
    *,
    tags: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    transaction: str | None = None,
) -> bool:
    """Capture an unexpected exception if monitoring is enabled.

    Expected user/input errors and already-captured exception chains are ignored.
    The helper is exception-safe because monitoring must never affect request
    behavior.
    """
    if not _enabled or not _should_capture(exc):
        return False

    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for key, value in (tags or {}).items():
                if value is not None:
                    scope.set_tag(key, str(value))
            if transaction:
                setter = getattr(scope, "set_transaction_name", None)
                if callable(setter):
                    setter(transaction)
            if context:
                scope.set_context("appwrite_mcp", _sanitize(context))
            sentry_sdk.capture_exception(exc)
        _mark_captured(exc)
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def capture_appwrite_exception(
    exc: AppwriteException,
    *,
    service: str,
    action: str,
    classification: str,
    project_id: str | None = None,
    organization_id: str | None = None,
) -> bool:
    """Capture publishable Appwrite API failures.

    Appwrite 4xx responses are expected user/API outcomes and remain metrics-only.
    Unknown or 5xx responses are sent to Sentry with low-cardinality tags.
    """
    tags = {
        "appwrite.service": service or "unknown",
        "appwrite.action": action or "unknown",
        "appwrite.classification": classification or "unknown",
        "appwrite.error_code": getattr(exc, "code", None),
        "appwrite.error_type": getattr(exc, "type", None),
        "appwrite.project_id": project_id,
        "appwrite.organization_id": organization_id,
    }
    return capture_exception(
        exc,
        tags=tags,
        context={
            "appwrite": {
                "service": service or "unknown",
                "action": action or "unknown",
                "classification": classification or "unknown",
                "error_code": getattr(exc, "code", None),
                "error_type": getattr(exc, "type", None),
                "project_id": project_id,
                "organization_id": organization_id,
            },
        },
        transaction=f"appwrite.{service or 'unknown'}.{action or 'unknown'}",
    )


def _should_capture(exc: BaseException) -> bool:
    if _already_captured(exc):
        return False
    if isinstance(exc, ValueError):
        return False
    appwrite_error = _find_appwrite_exception(exc)
    if appwrite_error is not None and _is_appwrite_client_error(appwrite_error):
        return False
    return True


def _find_appwrite_exception(exc: BaseException) -> AppwriteException | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AppwriteException):
            return current
        current = current.__cause__ or current.__context__
    return None


def _is_appwrite_client_error(exc: AppwriteException) -> bool:
    try:
        code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        return False
    return 400 <= code < 500


def _already_captured(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "_appwrite_mcp_sentry_captured", False):
            return True
        current = current.__cause__ or current.__context__
    return False


def _mark_captured(exc: BaseException) -> None:
    try:
        setattr(exc, "_appwrite_mcp_sentry_captured", True)
    except Exception:  # pragma: no cover - defensive
        pass


def _before_send(event: Any, hint: Any) -> Any:
    return _sanitize(event)


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in {"data", "body", "arguments"}:
                sanitized[key] = "[Filtered]"
            elif any(secret in normalized for secret in _SENSITIVE_KEYS):
                sanitized[key] = "[Filtered]"
            else:
                sanitized[key] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    return value
