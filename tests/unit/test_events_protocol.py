"""Events stay off on stdio.

Everything else about the protocol layer is covered end to end against the
hosted server in ``tests/e2e/test_events_protocol.py``. The stdio transport
cannot be booted there: it validates an Appwrite API key against a live project
at startup, which only the credentialed integration suite has.
"""

import os
import unittest
from unittest import mock

from mcp_server_appwrite import flags
from mcp_server_appwrite.events import protocol


class StdioTests(unittest.TestCase):
    def test_stdio_never_serves_events_even_with_the_flag_on(self):
        with mock.patch.dict(os.environ, {flags.EVENTS.env: "1"}):
            self.assertFalse(protocol.enabled("stdio"))
            self.assertTrue(protocol.enabled("http"))


if __name__ == "__main__":
    unittest.main()
