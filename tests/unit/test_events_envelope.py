"""Envelope properties that no e2e flow can establish.

Sealing, opening, tampering (401), project and webhook binding, key rotation
across server restarts and expiry are covered end to end through the ingress in
``tests/e2e/test_events_ingress.py``. Subscription ids (deterministic, bound to
the principal, independent of argument order, valid Appwrite custom ids) and
the derived webhook secret are covered through ``events/subscribe`` in
``tests/e2e/test_events_subscribe.py``. What stays here:

* The Appwrite signature against a vector computed by PHP, the way Appwrite's
  webhooks worker does it. The e2e harness signs deliveries with its own
  implementation of the same formula, so only this vector ties both to PHP.
* The envelope size budget that appwrite/appwrite#14293 must accommodate:
  worst-case inputs that no realistic flow would send.
"""

import base64
import os
import unittest

from mcp_server_appwrite.events.envelope import (
    ENVELOPE_BUDGET,
    EnvelopeTooLarge,
    Keyring,
    Principal,
    Subscription,
    appwrite_signature,
    verify_appwrite_signature,
)

APPWRITE_ID = "a" * 36
EXPIRES = 1_760_000_000_000


def key(seed: int) -> bytes:
    return bytes((seed + index * 7) % 256 for index in range(32))


def ring(*entries: tuple[str, bytes]) -> Keyring:
    return Keyring.parse(
        ",".join(
            f"{name}:{base64.b64encode(material).decode()}"
            for name, material in entries
        )
    )


def secret(size: int) -> str:
    return "whsec_" + base64.b64encode(os.urandom(size)).decode()


PRINCIPAL = Principal(
    issuer="https://cloud.appwrite.io/v1/oauth2/console",
    subject="66b1c2d3e4f5a6b7c8d9",
    client="chatgpt-connector",
).digest


class AppwriteSignatureTests(unittest.TestCase):
    # Produced with PHP 8.5, the way Appwrite's webhooks worker signs
    # (src/Appwrite/Platform/Workers/Webhooks.php):
    #   php -r '$url="https://mcp.appwrite.io/appwrite/webhooks/sub_0123456789abcdef0123456789abcdef";
    #           $payload="{\"\$id\":\"6630f1a2b3c4d5e6f7a8\",\"status\":\"failed\"}";
    #           $key="appwrite-signing-key-for-tests";
    #           echo base64_encode(hash_hmac("sha1", $url . $payload, $key, true));'
    URL = (
        "https://mcp.appwrite.io/appwrite/webhooks/sub_0123456789abcdef0123456789abcdef"
    )
    BODY = b'{"$id":"6630f1a2b3c4d5e6f7a8","status":"failed"}'
    KEY = "appwrite-signing-key-for-tests"
    SIGNATURE = "SgM0XNLzyHAwzlgQA2iZykDQI8M="

    def test_known_vector(self):
        self.assertEqual(
            appwrite_signature(self.URL, self.BODY, self.KEY), self.SIGNATURE
        )
        self.assertTrue(
            verify_appwrite_signature(self.URL, self.BODY, self.SIGNATURE, self.KEY)
        )

    def test_rejects_changes(self):
        cases = (
            (self.URL + "x", self.BODY, self.SIGNATURE, self.KEY),
            (self.URL, self.BODY + b" ", self.SIGNATURE, self.KEY),
            (self.URL, self.BODY, self.SIGNATURE, self.KEY + "x"),
            (self.URL, self.BODY, "AgM0XNLzyHAwzlgQA2iZykDQI8M=", self.KEY),
            (self.URL, self.BODY, "", self.KEY),
            (self.URL, self.BODY, "é", self.KEY),
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(verify_appwrite_signature(*case))


class SizeBudgetTests(unittest.TestCase):
    """Envelope lengths for realistic cases. The worst case must fit the budget
    that appwrite/appwrite#14293 has to let `authPassword` hold."""

    def setUp(self):
        self.keyring = ring(("k1", key(1)))

    def seal(
        self, name: str, arguments: dict[str, str], url: int, secrets: tuple[int, ...]
    ) -> int:
        callback = "https://" + "x" * (url - len("https://"))
        sealed = Subscription.create(
            project=APPWRITE_ID,
            name=name,
            arguments=arguments,
            callback=callback,
            secrets=tuple(secret(size) for size in secrets),
            expires=EXPIRES,
            principal=PRINCIPAL,
        )
        return len(self.keyring.seal(sealed))

    def test_worst_cases_fit_the_budget(self):
        rows = {
            "project_id": APPWRITE_ID,
            "database_id": APPWRITE_ID,
            "table_id": APPWRITE_ID,
        }
        deployments = {
            "project_id": APPWRITE_ID,
            "function_id": APPWRITE_ID,
            "status": "failed",
        }
        cases = {
            "users.user.created, 100-char URL, one 32-byte secret": (
                "users.user.created",
                {"project_id": APPWRITE_ID},
                100,
                (32,),
            ),
            "tablesdb.row.created, 150-char URL, one 32-byte secret": (
                "tablesdb.row.created",
                rows,
                150,
                (32,),
            ),
            "tablesdb.row.created, 300-char URL, two 64-byte secrets": (
                "tablesdb.row.created",
                rows,
                300,
                (64, 64),
            ),
            "functions.deployment.completed, 300-char URL, two 64-byte secrets": (
                "functions.deployment.completed",
                deployments,
                300,
                (64, 64),
            ),
        }
        for label, (name, arguments, url, secrets) in cases.items():
            with self.subTest(case=label):
                self.assertLessEqual(
                    self.seal(name, arguments, url, secrets), ENVELOPE_BUDGET
                )

    def test_seal_refuses_oversized_envelopes(self):
        with self.assertRaises(EnvelopeTooLarge):
            self.seal(
                "users.user.created",
                {"project_id": APPWRITE_ID},
                ENVELOPE_BUDGET,
                (32,),
            )


if __name__ == "__main__":
    unittest.main()
