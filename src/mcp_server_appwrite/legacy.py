"""Launch flags from releases before the operator surface.

Those releases switched each Appwrite service on with its own flag
(``--users``, ``--databases``, ``--all``...). The full catalog is always
registered now, so the flags do nothing, but plugins and client configs written
for those releases still pass them. Rejecting them made argparse exit before
the MCP handshake, which clients report only as a failed connection, and the
agent lost the server. They are accepted and ignored instead, and the session
tells the agent the launch config is outdated: first in the initialize
instructions, and in ``appwrite_get_context``, which agents call first. The
notice asks the agent to tell the user, so a person decides whether to fix the
config or report it.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

SERVICE_FLAGS: tuple[str, ...] = (
    "tablesdb",
    "users",
    "teams",
    "storage",
    "functions",
    "messaging",
    "locale",
    "avatars",
    "sites",
    "databases",
    "all",
)

DESTINATION = "legacy_flags"

ISSUES_URL = "https://github.com/appwrite/mcp/issues"


def register_cli_args(parser: argparse.ArgumentParser) -> None:
    """Accept every legacy flag as a hidden no-op, collecting the ones passed
    into ``args.legacy_flags``."""
    parser.set_defaults(**{DESTINATION: []})
    for name in SERVICE_FLAGS:
        parser.add_argument(
            f"--{name}",
            action="append_const",
            const=f"--{name}",
            dest=DESTINATION,
            help=argparse.SUPPRESS,
        )


def passed(args: argparse.Namespace) -> tuple[str, ...]:
    """The legacy flags on the command line, deduplicated, in order."""
    return tuple(dict.fromkeys(getattr(args, DESTINATION, [])))


def notice(flags: Sequence[str]) -> str | None:
    """What the agent is told when the server was launched with ``flags``."""
    if not flags:
        return None
    return (
        f"This Appwrite MCP server was launched with deprecated flags "
        f"({' '.join(flags)}). They are ignored: every Appwrite service is "
        "available without them. The plugin or MCP client config that starts "
        "this server is outdated. Tell the user, and suggest updating or "
        "reinstalling it, or removing these flags from its launch command. If "
        f"the config comes from an Appwrite plugin, offer to report it at "
        f"{ISSUES_URL}; ask the user before opening an issue."
    )
