import unittest
from enum import Enum
from typing import Any, Dict, List

from appwrite_console.input_file import InputFile

from mcp_server_appwrite.service import Service


class ExampleEnum(Enum):
    FIRST = "first"
    SECOND = "second"


class ExampleService:
    def create(
        self,
        name: str,
        mode: ExampleEnum,
        metadata: Dict[str, Any],
        points: List[Any],
        file: InputFile,
        optional_flag: bool = False,
        model_type=None,
        on_progress=None,
    ) -> Dict[str, Any]:
        """
        Create example resource.

        Parameters
        ----------
        name : str
            Resource name.
        mode : ExampleEnum
            Execution mode.
        metadata : Dict[str, Any]
            Arbitrary metadata.
        points : List[Any]
            Collection of loosely typed points.
        file : InputFile
            File input.
        optional_flag : bool
            Optional boolean flag.
        model_type : type, optional
            Internal response model selector.
        on_progress : callable, optional
            Ignored callback.
        """

        return {"ok": True}


class ServiceSchemaTests(unittest.TestCase):
    def test_generates_enum_and_input_file_schema(self):
        tools = Service(ExampleService(), "example").list_tools()
        tool = tools["example_create"]
        schema = tool["definition"].input_schema

        self.assertEqual(tool["definition"].description, "Create example resource.")
        self.assertNotIn("on_progress", schema["properties"])
        self.assertNotIn("model_type", schema["properties"])
        self.assertEqual(schema["properties"]["mode"]["enum"], ["first", "second"])
        self.assertEqual(schema["properties"]["mode"]["type"], "string")
        self.assertEqual(schema["properties"]["points"]["type"], "array")
        self.assertEqual(schema["properties"]["points"]["items"], {})
        self.assertIn("oneOf", schema["properties"]["file"])
        self.assertIn("file", schema["required"])
        self.assertTrue(schema["additionalProperties"] is False)

    def test_filters_methods_and_carries_context_metadata(self):
        tools = Service(
            ExampleService(),
            "example",
            allowed_methods=frozenset(),
            context_scope="project",
        ).list_tools()
        self.assertEqual(tools, {})

        tools = Service(
            ExampleService(),
            "example",
            allowed_methods=frozenset({"create"}),
            context_scope="project",
        ).list_tools()
        self.assertEqual(tools["example_create"]["context_scope"], "project")


if __name__ == "__main__":
    unittest.main()
