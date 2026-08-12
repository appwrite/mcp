"""Documentation the SDK docstrings deliberately leave to helper classes.

The spec describes `queries` via each SDK's `Query` class, which is intentional.
An MCP client has no helper — it sends JSON — so the wire format is all it can
act on, and nothing generated states it. This documents how to address the API;
defects belong upstream, where every SDK benefits. Unrelated to `docs_search.py`.
"""

from __future__ import annotations

QUERIES_GUIDANCE = (
    "Each query is its own JSON string, e.g. "
    '{"method":"greaterThanEqual","attribute":"rating","values":[2]}. Filters '
    "take attribute and values: equal, notEqual, lessThan, lessThanEqual, "
    "greaterThan, greaterThanEqual, between, startsWith, endsWith, contains, "
    "search. isNull, isNotNull, orderAsc and orderDesc take attribute only. "
    "limit, offset, cursorAfter and cursorBefore take values only. On list "
    "endpoints a relationship returns only the related ID unless selected: "
    '{"method":"select","values":["*","author.*"]} expands it in the same call, '
    "avoiding one call per row."
)

# Applied to every tool that declares the parameter, keyed by name.
PARAMETER_GUIDANCE: dict[str, str] = {
    "queries": QUERIES_GUIDANCE,
}


def describe_parameter(parameter_name: str, description: str) -> str:
    """Return ``description`` prefixed with this parameter's wire-format notes.

    The guidance leads rather than trails: search output truncates long
    parameter descriptions, and the SDK text is long enough on its own to
    consume the budget.
    """
    guidance = PARAMETER_GUIDANCE.get(parameter_name)
    if guidance is None:
        return description

    base = description.strip()
    if not base:
        return guidance
    return f"{guidance} {base}"
