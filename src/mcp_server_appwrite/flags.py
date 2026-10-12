"""Central registry for tester/feature flags.

A flag is an opt-in behavior override for testing (for example, pointing OAuth
login at a pre-release console). Every flag is declared once here and gets, for
free, a ``--<name>`` CLI argument and a ``<env>`` environment variable — the CLI
argument simply writes through to the environment, which is the single runtime
source of truth (modules read flags per request via :func:`value`, so tests can
toggle them with ``mock.patch.dict(os.environ, ...)``).

To add a flag:

1. Add a ``Flag`` entry to ``FLAGS`` below.
2. Read it where needed with ``flags.value(flags.MY_FLAG)``.
3. Document how to enable and test it in ``docs/flags.md``.

Flags are for testing overrides only — permanent configuration belongs in
``constants.py`` or a plain environment variable.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Flag:
    name: str
    """Kebab-case CLI name, exposed as ``--<name>``."""

    env: str
    """Environment variable backing the flag; the CLI argument writes to it."""

    help: str
    """One-line description shown in ``--help`` and docs."""


CONSOLE_URL = Flag(
    name="console-url",
    env="MCP_CONSOLE_URL",
    help=(
        "Base URL of an alternative Appwrite Console to use for OAuth "
        "login/consent (e.g. https://new.appwrite.io). HTTP transport only."
    ),
)

EVENTS = Flag(
    name="events",
    env="MCP_EVENTS",
    help=(
        "Set to 1 to advertise MCP Events and serve the events/* methods. "
        "HTTP transport only."
    ),
)

FLAGS: tuple[Flag, ...] = (CONSOLE_URL, EVENTS)

ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})
"""Values that switch an on/off flag on (case-insensitive)."""


def value(flag: Flag) -> str | None:
    """The flag's current value (normalized), or ``None`` when unset."""
    return os.getenv(flag.env, "").strip().rstrip("/") or None


def enabled(flag: Flag) -> bool:
    """Whether an on/off flag is switched on. Anything outside
    :data:`ENABLED_VALUES`, including unset, means off."""
    return (value(flag) or "").lower() in ENABLED_VALUES


def register_cli_args(parser: argparse.ArgumentParser) -> None:
    """Add a ``--<name>`` argument per flag. The default is ``None`` (not the
    environment variable) so an explicit ``--<name> ""`` is distinguishable
    from "not provided" and can clear a flag exported in the shell."""
    for flag in FLAGS:
        parser.add_argument(
            f"--{flag.name}",
            default=None,
            help=f"Testing flag: {flag.help} (default ${flag.env}).",
        )


def apply_cli_args(args: argparse.Namespace) -> None:
    """Write parsed CLI flag values back to their environment variables.

    A flag not provided on the CLI leaves its environment variable untouched;
    an explicit empty value (``--<name> ""``) clears it."""
    for flag in FLAGS:
        raw = getattr(args, flag.name.replace("-", "_"), None)
        if raw is not None:
            os.environ[flag.env] = raw
