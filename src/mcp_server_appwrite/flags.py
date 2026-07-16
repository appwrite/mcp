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

FLAGS: tuple[Flag, ...] = (CONSOLE_URL,)


def value(flag: Flag) -> str | None:
    """The flag's current value (normalized), or ``None`` when unset."""
    return os.getenv(flag.env, "").strip().rstrip("/") or None


def register_cli_args(parser: argparse.ArgumentParser) -> None:
    """Add a ``--<name>`` argument per flag, defaulting to its environment
    variable so both spellings behave identically."""
    for flag in FLAGS:
        parser.add_argument(
            f"--{flag.name}",
            default=os.getenv(flag.env, ""),
            help=f"Testing flag: {flag.help} (default ${flag.env}).",
        )


def apply_cli_args(args: argparse.Namespace) -> None:
    """Write parsed CLI flag values back to their environment variables."""
    for flag in FLAGS:
        raw = getattr(args, flag.name.replace("-", "_"), "")
        if raw:
            os.environ[flag.env] = raw
