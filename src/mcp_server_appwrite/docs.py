"""Documentation the SDK docstrings deliberately leave to helper classes.

The Appwrite API spec describes `queries` in terms of the `Query` class each SDK
ships, which is intentional — SDK users should reach for the helper rather than
hand-build query strings. An MCP client has no such helper: it sends JSON over
`appwrite_call_tool`, so the wire format is the only thing it can act on, and
nothing in the generated description states it.

This module supplies that missing half for the MCP surface. It documents how to
address the API, and never corrects a defect: anything wrong upstream belongs
upstream, where every SDK benefits rather than this one client.

Not to be confused with `docs_search.py`, which searches the Appwrite product
documentation for the user.
"""

from __future__ import annotations

QUERIES_GUIDANCE = (
    "Each query is its own JSON string, e.g. "
    '{"method":"greaterThanEqual","attribute":"rating","values":[2]}. Filters '
    "take attribute and values: equal, notEqual, lessThan, lessThanEqual, "
    "greaterThan, greaterThanEqual, between, isNull, isNotNull, startsWith, "
    "endsWith, contains, search. orderAsc and orderDesc take attribute only. "
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
