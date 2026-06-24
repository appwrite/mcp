import logging
import unittest

from mcp_server_appwrite.http_app import HealthzAccessLogFilter


class HealthzAccessLogFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = HealthzAccessLogFilter()

    def _record(self, args):
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=args,
            exc_info=None,
        )

    def test_filters_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/healthz", "1.1", 200))

        self.assertFalse(self.filter.filter(record))

    def test_filters_healthz_access_logs_with_query_string(self):
        record = self._record(
            ("127.0.0.1:12345", "GET", "/healthz?ready=1", "1.1", 200)
        )

        self.assertFalse(self.filter.filter(record))

    def test_keeps_non_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/mcp", "1.1", 401))

        self.assertTrue(self.filter.filter(record))


if __name__ == "__main__":
    unittest.main()
