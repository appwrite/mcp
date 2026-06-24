from typing import Dict, List

from mcp.types import Tool

from .service import Service


class ToolManager:
    def __init__(self):
        self.services: List[Service] = []
        self.tools_registry = {}

    def register_service(self, service: Service):
        """Register a new service and its tools"""
        self.services.append(service)
        self.tools_registry.update(service.list_tools())

    def get_all_tools(self) -> List[Tool]:
        """Get all tool definitions"""
        return [tool_info["definition"] for tool_info in self.tools_registry.values()]

    def get_tool(self, name: str) -> Dict:
        """Get a specific tool by name"""
        return self.tools_registry.get(name)
