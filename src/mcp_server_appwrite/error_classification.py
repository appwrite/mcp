"""Classify public tool failures into bounded operational categories.

The public operator wraps several distinct failure modes before they reach the
telemetry and error-monitoring layers.  Keep the classification logic here so
Prometheus and Sentry agree about which failures are expected and actionable.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

from appwrite_console.exception import AppwriteException

ErrorCategory = Literal[
    "write_confirmation",
    "appwrite_4xx",
    "appwrite_5xx",
    "sdk_validation",
    "internal",
]

ERROR_CATEGORIES: frozenset[str] = frozenset(
    {
        "write_confirmation",
        "appwrite_4xx",
        "appwrite_5xx",
        "sdk_validation",
        "internal",
    }
)


class WriteConfirmationRequired(RuntimeError):
    """A mutating hidden tool was called without explicit confirmation."""


def classify_tool_error(exc: BaseException) -> ErrorCategory:
    """Return the bounded operational category for an exception chain."""
    chain = tuple(_exception_chain(exc))

    if any(isinstance(item, WriteConfirmationRequired) for item in chain):
        return "write_confirmation"

    if any(_is_sdk_validation_error(item) for item in chain):
        return "sdk_validation"

    appwrite_error = next(
        (item for item in chain if isinstance(item, AppwriteException)), None
    )
    if appwrite_error is not None:
        code = _appwrite_status_code(appwrite_error)
        if code is not None and 400 <= code < 500:
            return "appwrite_4xx"
        if code is not None and 500 <= code < 600:
            return "appwrite_5xx"

    return "internal"


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walk causes and contexts defensively, including malformed cycles."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current

        # Push context first so an explicit cause is inspected first.
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)


def is_response_parse_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the SDK failing to deserialize an Appwrite response.

    The request itself reached Appwrite and was accepted, so the caller must not
    present this as a failed operation (see ``_format_appwrite_error``).
    """
    return any(_is_sdk_validation_error(item) for item in _exception_chain(exc))


def _is_sdk_validation_error(exc: BaseException) -> bool:
    error_type = type(exc)
    if error_type.__name__ == "ValidationError" and error_type.__module__.startswith(
        "pydantic"
    ):
        return True

    # The console SDK normally chains the Pydantic error, but retain a narrow
    # fallback for SDK versions that only preserve their parse-error message.
    return isinstance(exc, AppwriteException) and str(exc).startswith(
        "Unable to parse response into "
    )


def _appwrite_status_code(exc: AppwriteException) -> int | None:
    raw_code = getattr(exc, "code", None)
    if raw_code is None:
        return None
    try:
        return int(raw_code)
    except (TypeError, ValueError):
        return None
