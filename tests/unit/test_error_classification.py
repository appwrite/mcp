import unittest

from appwrite_console.exception import AppwriteException
from pydantic import BaseModel, ValidationError

from mcp_server_appwrite.error_classification import (
    HostedBinaryResponseTooLarge,
    WriteConfirmationRequired,
    classify_tool_error,
    is_response_parse_error,
)


class ErrorClassificationTests(unittest.TestCase):
    def test_write_confirmation(self):
        self.assertEqual(
            classify_tool_error(WriteConfirmationRequired("confirm_write=true")),
            "write_confirmation",
        )

    def test_hosted_binary_response_too_large(self):
        error = HostedBinaryResponseTooLarge("storage_get_file_download", 1024)
        self.assertEqual(classify_tool_error(error), "response_too_large")

    def test_wrapped_appwrite_4xx(self):
        for code in (400, 401, 404, 409, 429, 499):
            with self.subTest(code=code):
                appwrite_error = AppwriteException("client error", str(code), None)
                wrapped = RuntimeError("wrapped")
                wrapped.__cause__ = appwrite_error

                self.assertEqual(classify_tool_error(wrapped), "appwrite_4xx")

    def test_appwrite_5xx(self):
        for code in (500, 503, 599):
            with self.subTest(code=code):
                self.assertEqual(
                    classify_tool_error(
                        AppwriteException("upstream failed", code, "server_error")
                    ),
                    "appwrite_5xx",
                )

    def test_sdk_input_validation(self):
        error = AppwriteException(
            'Invalid parameter: "filters" must be an array of strings',
            type="sdk_input_validation",
        )
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = error

        for failure in (error, wrapped):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(classify_tool_error(failure), "sdk_input_validation")

    def test_sdk_input_validation_type_does_not_hide_responses(self):
        for code, response, expected in (
            (503, None, "appwrite_5xx"),
            (0, {}, "internal"),
            (0, "", "internal"),
        ):
            with self.subTest(code=code, response=response):
                error = AppwriteException(
                    "upstream failed", code, "sdk_input_validation", response
                )

                self.assertEqual(classify_tool_error(error), expected)

    def test_sdk_validation_takes_precedence_over_code(self):
        class Provider(BaseModel):
            options: dict

        try:
            Provider.model_validate({"options": []})
        except ValidationError as validation_error:
            appwrite_error = AppwriteException(
                "Unable to parse response into Provider", 0, "sdk_input_validation"
            )
            appwrite_error.__cause__ = validation_error
        else:  # pragma: no cover - defensive
            self.fail("Expected Pydantic validation to fail")

        self.assertEqual(classify_tool_error(appwrite_error), "sdk_validation")

    def test_sdk_validation_message_fallback(self):
        error = AppwriteException(
            "Unable to parse response into ProviderList: invalid model", 0, None
        )
        self.assertEqual(classify_tool_error(error), "sdk_validation")

    def test_code_less_appwrite_and_unexpected_errors_are_internal(self):
        self.assertEqual(
            classify_tool_error(AppwriteException("network down", 0, None)),
            "internal",
        )
        self.assertEqual(classify_tool_error(TypeError("boom")), "internal")

    def test_exception_cycle_is_safe(self):
        first = RuntimeError("first")
        second = AppwriteException("not found", 404, "not_found")
        first.__cause__ = second
        second.__context__ = first

        self.assertEqual(classify_tool_error(first), "appwrite_4xx")

    def test_recognizes_response_parse_errors_through_the_chain(self):
        parse_failure = AppwriteException("Unable to parse response into Project: ...")
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = parse_failure

        self.assertTrue(is_response_parse_error(parse_failure))
        self.assertTrue(is_response_parse_error(wrapped))
        self.assertFalse(
            is_response_parse_error(AppwriteException("not found", 404, "not_found"))
        )


if __name__ == "__main__":
    unittest.main()
