import inspect
import re
from enum import Enum
from types import UnionType
from typing import Any, Dict, List, Union, get_args, get_origin, get_type_hints

from appwrite.input_file import InputFile
from docstring_parser import parse
from mcp.types import Tool


class Service:
    """Base class for all Appwrite services"""

    _IGNORED_PARAMETERS = {"on_progress"}

    def __init__(self, service_instance, service_name: str):
        self.service = service_instance
        self.service_name = service_name
        self._method_name_overrides = self.get_method_name_overrides()

    def get_method_name_overrides(self) -> Dict[str, str]:
        """
        Override this method to provide method name mappings.
        Returns a dictionary where:
        - key: original method name
        - value: new method name to be used
        """
        return {}

    def _unwrap_optional_type(self, py_type: Any) -> Any:
        """Remove `None` from optional type hints."""
        origin = get_origin(py_type)
        if origin not in (Union, UnionType):
            return py_type

        args = [arg for arg in get_args(py_type) if arg is not type(None)]
        if len(args) == 1:
            return args[0]

        return py_type

    def _input_file_schema(self) -> dict:
        return {
            "oneOf": [
                {
                    "type": "string",
                    "description": "Path to a local file on the machine running the MCP server.",
                },
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to a local file on the machine running the MCP server.",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Filename to associate with inline content uploads.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Inline file content. Use `encoding` to choose UTF-8 text or base64 bytes.",
                        },
                        "encoding": {
                            "type": "string",
                            "enum": ["utf-8", "base64"],
                            "description": "Encoding used for `content`. Defaults to `utf-8`.",
                        },
                        "mime_type": {
                            "type": "string",
                            "description": "Optional MIME type for inline content uploads.",
                        },
                    },
                    "additionalProperties": False,
                },
            ]
        }

    def _clean_description(self, description: str) -> str:
        cleaned_description = description
        for ignored_parameter in self._IGNORED_PARAMETERS:
            cleaned_description = re.sub(
                rf"\n\s*{re.escape(ignored_parameter)}\s*:.*",
                "",
                cleaned_description,
                flags=re.DOTALL,
            )
        return cleaned_description.strip()

    def python_type_to_json_schema(self, py_type: Any) -> dict:
        """Converts Python type hints to JSON Schema types."""
        type_mapping = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
            list: "array",
            dict: "object",
        }

        py_type = self._unwrap_optional_type(py_type)

        if py_type is Any:
            return {}

        # Handle basic types
        if py_type in type_mapping:
            return {"type": type_mapping[py_type]}

        if inspect.isclass(py_type) and issubclass(py_type, Enum):
            enum_values = [member.value for member in py_type]
            value_types = {type(value) for value in enum_values}
            schema = {"enum": enum_values}
            if len(value_types) == 1 and next(iter(value_types)) in type_mapping:
                schema["type"] = type_mapping[next(iter(value_types))]
            return schema

        if py_type is InputFile:
            return self._input_file_schema()

        # Handle List, Dict, and other generic types
        origin = get_origin(py_type)
        args = get_args(py_type)
        if origin:
            # Handle List[T]
            if origin is list or origin is List:
                if args:
                    item_schema = self.python_type_to_json_schema(args[0])
                    return {"type": "array", "items": item_schema}
                return {"type": "array"}

            # Handle Dict[K, V]
            if origin is dict or origin is Dict:
                if len(args) >= 2:
                    value_schema = self.python_type_to_json_schema(args[1])
                    return {"type": "object", "additionalProperties": value_schema}
                return {"type": "object"}

        # Default to string for unknown types
        return {"type": "string"}

    def list_tools(self) -> Dict[str, Dict]:
        """Lists all available tools for this service"""
        tools = {}

        for name, func in inspect.getmembers(self.service, predicate=inspect.ismethod):
            if name.startswith("_"):  # Skip private methods
                continue

            original_func = func.__func__

            # Skip if not from the service's module
            if original_func.__module__ != self.service.__class__.__module__:
                continue

            # Get the overridden name if it exists
            tool_name = self._method_name_overrides.get(
                name, f"{self.service_name}_{name}"
            )

            docstring = parse(original_func.__doc__ or "")
            signature = inspect.signature(original_func)
            type_hints = get_type_hints(original_func)

            properties = {}
            required = []

            for param_name, param in signature.parameters.items():
                if param_name == "self" or param_name in self._IGNORED_PARAMETERS:
                    continue

                param_type = type_hints.get(param_name, str)
                properties[param_name] = self.python_type_to_json_schema(param_type)
                properties[param_name]["description"] = f"Parameter '{param_name}'"

                for doc_param in docstring.params:
                    if doc_param.arg_name == param_name:
                        properties[param_name]["description"] = self._clean_description(
                            doc_param.description
                        )

                if param.default is param.empty:
                    required.append(param_name)

            tool_definition = Tool(
                name=tool_name,
                description=docstring.short_description or "No description available",
                inputSchema={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )

            tools[tool_name] = {
                "definition": tool_definition,
                # Store the service + method identity rather than a bound method so
                # execution can re-bind to a per-request Appwrite client (HTTP/OAuth
                # mode) instead of the credential-less client used for introspection.
                "service_name": self.service_name,
                "method_name": name,
                "parameter_types": {
                    param_name: type_hints[param_name]
                    for param_name in signature.parameters
                    if param_name != "self"
                    and param_name in type_hints
                    and param_name not in self._IGNORED_PARAMETERS
                },
            }

        return tools
