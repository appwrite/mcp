import unittest

from jsonschema import Draft202012Validator

from mcp_server_appwrite.events import errors
from mcp_server_appwrite.events.catalog import (
    CATALOG,
    EVENTS,
    Status,
    StatusFilter,
    lookup,
)
from mcp_server_appwrite.events.errors import EventsError

EXPECTED_PATTERNS = {
    "functions.execution.failed": (
        {"project_id": "p1", "function_id": "fn1"},
        [
            "functions.fn1.executions.*.update",
            "functions.fn1.executions.*.create",
        ],
    ),
    "functions.deployment.completed": (
        {"project_id": "p1", "function_id": "fn1", "status": "ready"},
        ["functions.fn1.deployments.*.update"],
    ),
    "sites.deployment.completed": (
        {"project_id": "p1", "site_id": "site-1"},
        ["sites.site-1.deployments.*.update"],
    ),
    "tablesdb.row.created": (
        {"project_id": "p1", "database_id": "db_1", "table_id": "tickets"},
        ["tablesdb.db_1.tables.tickets.rows.*.create"],
    ),
    "storage.file.created": (
        {"project_id": "p1", "bucket_id": "uploads"},
        ["buckets.uploads.files.*.create"],
    ),
    "users.user.created": ({"project_id": "p1"}, ["users.*.create"]),
}

# Values that would widen an Appwrite event pattern or are not Appwrite IDs.
BAD_IDS = ["*", "a*", "a.b", "fn1.executions", "", "-lead", "_lead", "a/b", "a b"]


def _arguments(name: str) -> dict:
    return dict(EXPECTED_PATTERNS[name][0])


class CatalogShapeTests(unittest.TestCase):
    def test_catalog_is_the_v1_set_in_order(self):
        self.assertEqual(
            [event.name for event in EVENTS], list(EXPECTED_PATTERNS.keys())
        )
        self.assertEqual(list(CATALOG), list(EXPECTED_PATTERNS.keys()))

    def test_schemas_are_valid_json_schema(self):
        for event in EVENTS:
            with self.subTest(event=event.name):
                Draft202012Validator.check_schema(event.input_schema)
                Draft202012Validator.check_schema(event.payload_schema)

    def test_every_event_requires_project_id(self):
        for event in EVENTS:
            with self.subTest(event=event.name):
                schema = event.input_schema
                self.assertIn("project_id", schema["required"])
                self.assertFalse(schema["additionalProperties"])

    def test_payload_timestamps_are_nullable(self):
        for event in EVENTS:
            for name, field in event.payload_schema["properties"].items():
                if field.get("format") == "date-time":
                    with self.subTest(event=event.name, field=name):
                        self.assertEqual(field["type"], ["string", "null"])

    def test_payloads_carry_ids_and_metadata_only(self):
        # Appwrite sends whole rows and users; nothing that could hold their
        # content or PII may be declared for delivery.
        forbidden = {"email", "phone", "name", "prefs", "data", "body", "logs"}
        for event in EVENTS:
            with self.subTest(event=event.name):
                self.assertFalse(forbidden & set(event.payload_schema["properties"]))
        self.assertEqual(
            list(lookup("users.user.created").payload_schema["properties"]),
            ["user_id", "created_at"],
        )

    def test_status_filters_are_declared_as_data(self):
        failed = lookup("functions.execution.failed").status
        self.assertEqual(failed, StatusFilter(accepted=(Status.FAILED,)))
        for name in ("functions.deployment.completed", "sites.deployment.completed"):
            with self.subTest(event=name):
                event = lookup(name)
                assert event.status is not None
                self.assertEqual(event.status.accepted, (Status.READY, Status.FAILED))
                self.assertEqual(event.status.argument, "status")
                self.assertIn("status", event.input_schema["properties"])
        for name in (
            "tablesdb.row.created",
            "storage.file.created",
            "users.user.created",
        ):
            self.assertIsNone(lookup(name).status)


class StatusFilterTests(unittest.TestCase):
    def test_execution_failed_accepts_only_failures(self):
        status = lookup("functions.execution.failed").status
        assert status is not None
        self.assertTrue(status.matches("failed", {}))
        for value in ("completed", "waiting", "processing", None):
            self.assertFalse(status.matches(value, {}))

    def test_deployment_filter_honours_the_status_argument(self):
        status = lookup("functions.deployment.completed").status
        assert status is not None
        self.assertTrue(status.matches("ready", {}))
        self.assertTrue(status.matches("failed", {}))
        self.assertFalse(status.matches("building", {}))
        self.assertTrue(status.matches("failed", {"status": "failed"}))
        self.assertFalse(status.matches("ready", {"status": "failed"}))


class PatternTests(unittest.TestCase):
    def test_patterns_per_event(self):
        for name, (arguments, expected) in EXPECTED_PATTERNS.items():
            with self.subTest(event=name):
                self.assertEqual(lookup(name).patterns(arguments), expected)

    def test_patterns_never_contain_project_or_status(self):
        for name, (arguments, _) in EXPECTED_PATTERNS.items():
            for pattern in lookup(name).patterns(arguments):
                self.assertNotIn("p1", pattern.split("."))
                self.assertNotIn("ready", pattern.split("."))


class ArgumentValidationTests(unittest.TestCase):
    def assertInvalid(self, event_name: str, arguments: object) -> EventsError:
        with self.assertRaises(EventsError) as caught:
            lookup(event_name).patterns(arguments)
        self.assertEqual(caught.exception.code, errors.INVALID_PARAMS)
        return caught.exception

    def test_rejects_wildcards_and_dots_in_every_id(self):
        for name in EXPECTED_PATTERNS:
            event = lookup(name)
            for argument in event.parameters:
                if argument.choices:
                    continue
                for bad in BAD_IDS:
                    with self.subTest(event=name, argument=argument.name, value=bad):
                        arguments = {**_arguments(name), argument.name: bad}
                        self.assertInvalid(name, arguments)

    def test_rejects_ids_longer_than_36(self):
        arguments = {"project_id": "p1", "bucket_id": "a" * 37}
        self.assertInvalid("storage.file.created", arguments)
        arguments["bucket_id"] = "a" * 36
        self.assertEqual(
            lookup("storage.file.created").patterns(arguments),
            [f"buckets.{'a' * 36}.files.*.create"],
        )

    def test_rejects_missing_unknown_and_mistyped_arguments(self):
        self.assertInvalid("users.user.created", {})
        self.assertInvalid("users.user.created", None)
        self.assertInvalid("users.user.created", ["p1"])
        self.assertInvalid("users.user.created", {"project_id": 1})
        self.assertInvalid("tablesdb.row.created", {"project_id": "p1"})
        error = self.assertInvalid(
            "users.user.created", {"project_id": "p1", "user_id": "u1"}
        )
        self.assertIn("user_id", error.message)

    def test_status_argument_must_be_a_terminal_status(self):
        base = {"project_id": "p1", "site_id": "s1"}
        for bad in ("building", "READY", None, "*"):
            with self.subTest(value=bad):
                self.assertInvalid(
                    "sites.deployment.completed", {**base, "status": bad}
                )
        self.assertEqual(
            lookup("sites.deployment.completed").validate({**base, "status": "failed"}),
            {"project_id": "p1", "site_id": "s1", "status": "failed"},
        )

    def test_validate_agrees_with_the_published_input_schema(self):
        cases = [{}, {"project_id": "p1"}, {"project_id": "*"}, {"extra": "x"}]
        for name in EXPECTED_PATTERNS:
            event = lookup(name)
            validator = Draft202012Validator(event.input_schema)
            samples = [*cases, _arguments(name)]
            samples += [
                {**_arguments(name), argument.name: bad}
                for argument in event.parameters
                for bad in [*BAD_IDS, None, 7]
            ]
            for sample in samples:
                with self.subTest(event=name, sample=sample):
                    schema_accepts = validator.is_valid(sample)
                    try:
                        event.validate(sample)
                        accepts = True
                    except EventsError:
                        accepts = False
                    self.assertEqual(accepts, schema_accepts)


class LookupTests(unittest.TestCase):
    def test_lookup_returns_the_event(self):
        self.assertIs(lookup("users.user.created"), CATALOG["users.user.created"])

    def test_unknown_event_is_not_found_with_kind(self):
        for name in ("users.user.deleted", "", None, 3):
            with self.subTest(name=name):
                with self.assertRaises(EventsError) as caught:
                    lookup(name)
                self.assertEqual(caught.exception.code, errors.NOT_FOUND)
                self.assertEqual(caught.exception.data, {"kind": "event"})


class ErrorTests(unittest.TestCase):
    def test_codes_match_the_chatgpt_values(self):
        self.assertEqual(
            (
                errors.INVALID_PARAMS,
                errors.NOT_FOUND,
                errors.FORBIDDEN,
                errors.RESOURCE_EXHAUSTED,
                errors.UNSUPPORTED,
                errors.CALLBACK_ENDPOINT,
            ),
            (-32602, -32011, -32012, -32013, -32014, -32015),
        )

    def test_constructors_carry_spec_data(self):
        exhausted = EventsError.resource_exhausted(
            "Upgrade to Pro", limit="webhooks", maximum=2
        )
        self.assertEqual(exhausted.code, errors.RESOURCE_EXHAUSTED)
        self.assertEqual(exhausted.data, {"limit": "webhooks", "max": 2})
        callback = EventsError.callback_endpoint(
            "Callback timed out", reason=errors.CallbackFailure.TIMEOUT
        )
        self.assertEqual(callback.code, errors.CALLBACK_ENDPOINT)
        self.assertEqual(callback.data, {"reason": "timeout"})
        self.assertEqual(EventsError.forbidden("no").error.data, None)
        self.assertEqual(EventsError.unsupported("no").code, errors.UNSUPPORTED)

    def test_callback_reasons(self):
        self.assertEqual(
            {reason.value for reason in errors.CallbackFailure},
            {
                "challenge_failed",
                "timeout",
                "connection_refused",
                "tls_error",
                "http_4xx",
                "http_5xx",
            },
        )


if __name__ == "__main__":
    unittest.main()
