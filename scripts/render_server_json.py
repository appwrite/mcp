from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PACKAGE_IDENTIFIER = "mcp-server-appwrite"


def render_server_metadata(
    version: str,
    *,
    template_path: Path = Path("server.template.json"),
    output_path: Path = Path("server.json"),
) -> None:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"version must be MAJOR.MINOR.PATCH, got {version!r}")

    data: dict[str, Any] = json.loads(template_path.read_text())
    data["version"] = version

    for package in data.get("packages", []):
        if package.get("identifier") == PACKAGE_IDENTIFIER:
            package["version"] = version
            break
    else:
        raise ValueError(f"{PACKAGE_IDENTIFIER!r} package entry not found")

    output_path.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render MCP Registry metadata.")
    parser.add_argument("version", help="Release version without leading v.")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("server.template.json"),
        help="Path to the server metadata template.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("server.json"),
        help="Path to write rendered metadata.",
    )
    args = parser.parse_args()

    render_server_metadata(
        args.version,
        template_path=args.template,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
