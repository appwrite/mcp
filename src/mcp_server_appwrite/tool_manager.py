from __future__ import annotations

from mcp.types import Tool

from .service import Service


class ToolManager:
    def __init__(self):
        self.services: list[Service] = []
        self.tools_registry: dict[str, dict] = {}

    def register_service(self, service: Service):
        """Register a new service and its tools"""
        self.services.append(service)
        self.tools_registry.update(service.list_tools())

    def get_all_tools(self) -> list[Tool]:
        """Get all tool definitions"""
        return [tool_info["definition"] for tool_info in self.tools_registry.values()]

    def get_tool(self, name: str) -> dict | None:
        """Get a specific tool by name, or None if unregistered"""
        return self.tools_registry.get(name)
