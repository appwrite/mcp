"""Server-side argument validation of the events catalog.

Nothing reaches ``Event.patterns`` or ``lookup`` over HTTP until
``events/subscribe`` lands (PR 5 of #127). The published schemas are covered
end to end in ``tests/e2e/test_events_protocol.py``; replace this module with
the subscribe e2e flow once it exists.
"""

import unittest

from mcp_server_appwrite.events import errors
from mcp_server_appwrite.events.catalog import EVENTS, lookup
from mcp_server_appwrite.events.errors import EventsError

VALID = {
    "functions.execution.failed": {"function_id": "fn1"},
    "functions.deployment.completed": {"function_id": "fn1"},
    "sites.deployment.completed": {"site_id": "site-1"},
    "tablesdb.row.created": {"database_id": "db_1", "table_id": "tickets"},
    "storage.file.created": {"bucket_id": "uploads"},
    "users.user.created": {},
}

# Values that would widen an Appwrite event pattern or are not Appwrite IDs.
BAD_IDS = ["*", "a*", "a.b", "fn1.executions", "", "-lead", "_lead", "a b", "a" * 37]


class ArgumentValidationTests(unittest.TestCase):
    def test_ids_can_never_widen_a_pattern(self):
        for event in EVENTS:
            arguments = {"project_id": "p1", **VALID[event.name]}
            patterns = event.patterns(arguments)
            self.assertTrue(patterns)
            for pattern in patterns:
                self.assertNotIn("p1", pattern.split("."))
            for argument in event.parameters:
                if argument.choices:
                    continue
                for bad in BAD_IDS:
                    with self.subTest(event=event.name, argument=argument.name):
                        with self.assertRaises(EventsError) as caught:
                            event.patterns({**arguments, argument.name: bad})
                        self.assertEqual(caught.exception.code, errors.INVALID_PARAMS)

    def test_unknown_event_is_not_found(self):
        with self.assertRaises(EventsError) as caught:
            lookup("users.user.deleted")
        self.assertEqual(caught.exception.code, errors.NOT_FOUND)
        self.assertEqual(caught.exception.data, {"kind": "event"})


if __name__ == "__main__":
    unittest.main()
