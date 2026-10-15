"""MCP Events discovery and ``events/list`` against the real hosted server.

A client connects the way ChatGPT does, discovers the events capability and
reads the catalog. The published schemas are checked with ``jsonschema`` as a
client would use them, so the wire is the contract under test.
"""

import base64
import unittest

import httpx
from jsonschema import Draft202012Validator
from support import Server

EXTENSION = "io.modelcontextprotocol/events"

CATALOG = [
    "functions.execution.failed",
    "functions.deployment.completed",
    "sites.deployment.completed",
    "tablesdb.row.created",
    "storage.file.created",
    "users.user.created",
]

VALID_ARGUMENTS = {
    "functions.execution.failed": {"project_id": "p1", "function_id": "fn_1"},
    "functions.deployment.completed": {
        "project_id": "p1",
        "function_id": "fn_1",
        "status": "ready",
    },
    "sites.deployment.completed": {"project_id": "p1", "site_id": "site-1"},
    "tablesdb.row.created": {
        "project_id": "p1",
        "database_id": "main",
        "table_id": "tickets",
    },
    "storage.file.created": {"project_id": "p1", "bucket_id": "uploads"},
    "users.user.created": {"project_id": "p1"},
}

# With the flag on, the server mounts the ingress, which needs sealing keys.
SEALING_KEYS = "k1:" + base64.b64encode(bytes(range(32))).decode()

# Content and PII that Appwrite models carry and an event must never declare.
FORBIDDEN_PAYLOAD_FIELDS = {"email", "phone", "name", "prefs", "data", "body", "logs"}


class EventsEnabledFlow(unittest.TestCase):
    """A client discovers events on a server with the flag on."""

    environment = {"MCP_EVENTS": "1", "MCP_EVENTS_SEALING_KEYS": SEALING_KEYS}

    def test_client_discovers_and_lists_events(self):
        with Server(self.environment) as server, server.client() as client:
            for path in ("/", "/mcp"):
                with self.subTest(step="server/discover", path=path):
                    status, message = client.call("server/discover", path=path)
                    self.assertEqual(status, 200, message)
                    capabilities = message["result"]["capabilities"]
                    self.assertEqual(capabilities["events"], {})
                    self.assertEqual(
                        capabilities["extensions"][EXTENSION], {"listChanged": False}
                    )
                    # The SDK-derived capabilities survive the patch.
                    self.assertIn("tools", capabilities)
                    self.assertIn("resources", capabilities)

            with self.subTest(step="initialize"):
                status, message = client.initialize(path="/mcp")
                self.assertEqual(status, 200, message)
                capabilities = message["result"]["capabilities"]
                self.assertEqual(capabilities["events"], {})
                self.assertEqual(
                    capabilities["extensions"][EXTENSION], {"listChanged": False}
                )
                self.assertIn("tools", capabilities)

            with self.subTest(step="events/list"):
                status, message = client.call("events/list")
                self.assertEqual(status, 200, message)
                self.check_catalog(message["result"])

            with self.subTest(step="events/list with a foreign cursor"):
                status, message = client.call("events/list", {"cursor": "abc"})
                self.assertEqual(status, 400, message)
                self.assertEqual(message["error"]["code"], -32602)

    def check_catalog(self, result):
        self.assertEqual(result["resultType"], "complete")
        self.assertNotIn("nextCursor", result)
        self.assertEqual([event["name"] for event in result["events"]], CATALOG)
        for event in result["events"]:
            name = event["name"]
            with self.subTest(event=name):
                self.assertEqual(
                    set(event),
                    {"name", "description", "delivery", "inputSchema", "payloadSchema"},
                )
                self.assertTrue(event["description"])
                self.assertEqual(event["delivery"], ["webhook"])

                inputs = event["inputSchema"]
                payload = event["payloadSchema"]
                Draft202012Validator.check_schema(inputs)
                Draft202012Validator.check_schema(payload)

                self.assertIn("project_id", inputs["required"])
                self.assertFalse(inputs["additionalProperties"])
                arguments = Draft202012Validator(inputs)
                self.assertTrue(arguments.is_valid(VALID_ARGUMENTS[name]))
                self.assertFalse(arguments.is_valid({}))
                self.assertFalse(
                    arguments.is_valid({**VALID_ARGUMENTS[name], "extra": "x"})
                )
                # A wildcard or a dot in an ID would widen the Appwrite
                # event pattern beyond the named resource.
                for argument in VALID_ARGUMENTS[name]:
                    if argument == "status":
                        continue
                    for bad in ("*", "a.b", "-lead", "a" * 37):
                        self.assertFalse(
                            arguments.is_valid(
                                {**VALID_ARGUMENTS[name], argument: bad}
                            ),
                            (argument, bad),
                        )

                fields = payload["properties"]
                self.assertFalse(FORBIDDEN_PAYLOAD_FIELDS & set(fields))
                for field, schema in fields.items():
                    if schema.get("format") == "date-time":
                        self.assertEqual(schema["type"], ["string", "null"], field)

        deployments = {
            event["name"]: event["inputSchema"]
            for event in result["events"]
            if event["name"].endswith("deployment.completed")
        }
        for name, schema in deployments.items():
            with self.subTest(event=name, argument="status"):
                validator = Draft202012Validator(schema)
                base = VALID_ARGUMENTS[name]
                self.assertTrue(validator.is_valid({**base, "status": "failed"}))
                self.assertFalse(validator.is_valid({**base, "status": "building"}))
        users = next(e for e in result["events"] if e["name"] == "users.user.created")
        self.assertEqual(
            list(users["payloadSchema"]["properties"]), ["user_id", "created_at"]
        )


class EventsDisabledFlow(unittest.TestCase):
    """Without the flag, nothing about events is visible."""

    def test_nothing_is_advertised_or_served(self):
        for value in (None, "0", "false"):
            with (
                self.subTest(flag=value),
                Server({"MCP_EVENTS": value}) as server,
                server.client() as client,
            ):
                status, message = client.call("server/discover")
                self.assertEqual(status, 200, message)
                capabilities = message["result"]["capabilities"]
                self.assertNotIn("events", capabilities)
                self.assertNotIn(EXTENSION, capabilities.get("extensions") or {})

                status, message = client.initialize()
                self.assertEqual(status, 200, message)
                self.assertNotIn("events", message["result"]["capabilities"])

                for method in ("events/list", "events/subscribe", "events/unsubscribe"):
                    _, message = client.call(method)
                    self.assertEqual(message["error"]["code"], -32601, method)

                # The Appwrite webhook ingress is not mounted either.
                response = httpx.post(
                    f"{server.url}/appwrite/webhooks/sub_0123456789abcdef0123456789abcdef"
                )
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
