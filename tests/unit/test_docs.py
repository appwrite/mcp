import json
import unittest

from appwrite_console.query import Query

from mcp_server_appwrite.docs import QUERIES_GUIDANCE, describe_parameter

# Each group as the guidance describes it, checked against what the SDK's Query
# class actually emits. Grouping a value-taking method as attribute-only (or the
# reverse) tells clients to build a query the API does not accept.
TAKES_ATTRIBUTE_AND_VALUES = {
    "equal": Query.equal("a", 1),
    "notEqual": Query.not_equal("a", 1),
    "lessThan": Query.less_than("a", 1),
    "lessThanEqual": Query.less_than_equal("a", 1),
    "greaterThan": Query.greater_than("a", 1),
    "greaterThanEqual": Query.greater_than_equal("a", 1),
    "between": Query.between("a", 1, 2),
    "startsWith": Query.starts_with("a", "x"),
    "endsWith": Query.ends_with("a", "x"),
    "contains": Query.contains("a", "x"),
    "search": Query.search("a", "x"),
}

TAKES_ATTRIBUTE_ONLY = {
    "isNull": Query.is_null("a"),
    "isNotNull": Query.is_not_null("a"),
    "orderAsc": Query.order_asc("a"),
    "orderDesc": Query.order_desc("a"),
}

TAKES_VALUES_ONLY = {
    "limit": Query.limit(10),
    "offset": Query.offset(10),
    "cursorAfter": Query.cursor_after("a"),
    "cursorBefore": Query.cursor_before("a"),
}


class QueryGuidanceTests(unittest.TestCase):
    def test_attribute_and_value_methods_emit_both(self):
        for method, emitted in TAKES_ATTRIBUTE_AND_VALUES.items():
            with self.subTest(method=method):
                query = json.loads(emitted)
                self.assertEqual(query["method"], method)
                self.assertIn("attribute", query)
                self.assertIn("values", query)

    def test_attribute_only_methods_emit_no_values(self):
        for method, emitted in TAKES_ATTRIBUTE_ONLY.items():
            with self.subTest(method=method):
                query = json.loads(emitted)
                self.assertEqual(query["method"], method)
                self.assertIn("attribute", query)
                self.assertNotIn("values", query)

    def test_value_only_methods_emit_no_attribute(self):
        for method, emitted in TAKES_VALUES_ONLY.items():
            with self.subTest(method=method):
                query = json.loads(emitted)
                self.assertEqual(query["method"], method)
                self.assertNotIn("attribute", query)
                self.assertIn("values", query)

    def test_guidance_names_every_method_in_its_correct_group(self):
        attribute_and_values, attribute_only, values_only = (
            QUERIES_GUIDANCE.split("take attribute and values:")[1].split(
                "take attribute only."
            )[0],
            QUERIES_GUIDANCE.split("take attribute only.")[0].rsplit(".", 2)[-1],
            QUERIES_GUIDANCE.split("take values only.")[0].rsplit(".", 1)[-1],
        )

        for method in TAKES_ATTRIBUTE_AND_VALUES:
            self.assertIn(method, attribute_and_values, method)
        for method in TAKES_ATTRIBUTE_ONLY:
            self.assertIn(method, attribute_only, method)
        for method in TAKES_VALUES_ONLY:
            self.assertIn(method, values_only, method)

    def test_examples_match_the_sdk_helper(self):
        # The documented examples must stay identical to the canonical producer.
        self.assertIn(Query.greater_than_equal("rating", 2), QUERIES_GUIDANCE)
        self.assertIn(Query.select(["*", "author.*"]), QUERIES_GUIDANCE)

    def test_unknown_parameter_is_untouched(self):
        self.assertEqual(describe_parameter("search", "Search term."), "Search term.")


if __name__ == "__main__":
    unittest.main()
