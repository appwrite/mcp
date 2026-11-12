"""Keeps secrets out of tool results.

A tool result goes into the model's context and from there into transcripts,
subagent logs and caches, so a secret Appwrite returns (an API key created or
updated, a webhook's signing key, a user's password hash) would leak the moment
the call returns. Appwrite's API spec marks every such response field with
``format: password``, and the Console SDK carries that on each model field, so
the result is redacted from the SDK model before it is serialized, unless the
call sets ``reveal_secrets``.

A redacted value is replaced with ``[redacted]``. A string also gets a sibling
``<field>Fingerprint``, a short SHA-256 of the value, so the caller can still
tell which secret it got and check a stored copy against it. Empty values are
kept: they only say a secret is unset, or that Appwrite already hid it.

A payload the SDK could not hydrate into its model has no field types to go
by, so it is redacted by name: every field name some SDK model marks secret.
Rows from a ``select`` query are not redacted at all: they are the caller's own
data, not Appwrite's secrets, and a column may share a secret field's name.
"""

import hashlib
from functools import cache
from typing import Any

import appwrite_console.models
from pydantic import BaseModel
from pydantic.fields import FieldInfo

REDACTED = "[redacted]"

SECRET_FORMAT = "password"
"""The OpenAPI format Appwrite's spec marks secret response fields with."""


def redact(result: Any) -> tuple[Any, list[str]]:
    """The result as JSON-ready data with every secret field redacted, and the
    names of the fields that were, sorted."""
    fields: set[str] = set()
    return _clean(result, fields), sorted(fields)


def redact_payload(payload: Any) -> tuple[Any, list[str]]:
    """A raw response payload with every field named like a secret field
    redacted, and the names that were, sorted."""
    fields: set[str] = set()
    return _clean_payload(payload, fields), sorted(fields)


def fingerprint(value: str) -> str:
    """A short SHA-256 of a secret: enough to tell secrets apart, not to recover one."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def note(fields: list[str]) -> str:
    """Tells the model what was redacted and how to see it if the user asks."""
    return (
        f"Secrets redacted from this result: {', '.join(fields)}. Each redacted "
        "string has a <field>Fingerprint (a SHA-256 prefix) to compare against a "
        "stored copy. Only if the user explicitly asks to see a secret in this "
        "conversation, read it again with reveal_secrets=true (do not repeat a "
        "create, which would make a new secret)."
    )


def _clean(value: Any, fields: set[str]) -> Any:
    if isinstance(value, BaseModel):
        return _clean_model(value, fields)
    if isinstance(value, (list, tuple)):
        return [_clean(item, fields) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, fields) for key, item in value.items()}
    return value


def _clean_model(model: BaseModel, fields: set[str]) -> dict[str, Any]:
    data = model.model_dump(by_alias=True, mode="json", exclude_unset=True)
    for name, field in type(model).model_fields.items():
        key = field.alias or name
        if key not in data:
            continue
        if _secret(field):
            _mask(data, key, fields)
            continue
        item = getattr(model, name)
        if _holds_model(item):
            data[key] = _clean(item, fields)
    return data


def _clean_payload(value: Any, fields: set[str]) -> Any:
    if isinstance(value, list):
        return [_clean_payload(item, fields) for item in value]
    if not isinstance(value, dict):
        return value
    data = dict(value)
    for key, item in value.items():
        if key in _secret_names():
            _mask(data, key, fields)
        else:
            data[key] = _clean_payload(item, fields)
    return data


def _mask(data: dict[str, Any], key: str, fields: set[str]) -> None:
    """Replace a secret value in place. Empty values stay: they only say the
    secret is unset, or that Appwrite already hid it."""
    value = data[key]
    if value in ("", [], None):
        return
    fields.add(key)
    if isinstance(value, str):
        data[f"{key}Fingerprint"] = fingerprint(value)
    data[key] = REDACTED


@cache
def _secret_names() -> frozenset[str]:
    """Every field name an SDK model marks secret."""
    names: set[str] = set()
    for model in vars(appwrite_console.models).values():
        if isinstance(model, type) and issubclass(model, BaseModel):
            for name, field in model.model_fields.items():
                if _secret(field):
                    names.add(field.alias or name)
    return frozenset(names)


def _secret(field: FieldInfo) -> bool:
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get("format") == SECRET_FORMAT


def _holds_model(value: Any) -> bool:
    if isinstance(value, BaseModel):
        return True
    if isinstance(value, (list, tuple)):
        return any(_holds_model(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_model(item) for item in value.values())
    return False
