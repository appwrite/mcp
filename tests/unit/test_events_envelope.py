"""Envelope properties that no e2e flow can establish.

Sealing, opening, tampering (401), server, project and webhook binding, key
rotation across server restarts and expiry are covered end to end through the
ingress in ``tests/e2e/test_events_ingress.py``. Subscription ids
(deterministic, bound to the principal, independent of argument order, valid
Appwrite custom ids) and the derived webhook secret are covered through
``events/subscribe`` in ``tests/e2e/test_events_subscribe.py``. What stays here:

* The published ``mcp-subscription+jwe`` conformance vector from
  mcp-event-subscriptions, opened through this adapter. Every envelope in the
  e2e flows is sealed and opened by this server, so only the vector ties the
  format to the profile.
* The Appwrite signature against a vector computed by PHP, the way Appwrite's
  webhooks worker does it. The e2e harness signs deliveries with its own
  implementation of the same formula, so only this vector ties both to PHP.
* The envelope size budget that appwrite/appwrite#14293 must accommodate:
  worst-case inputs that no realistic flow would send.
"""

import base64
import json
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mcp_server_appwrite.events.envelope import (
    ENVELOPE_BUDGET,
    Context,
    EnvelopeError,
    EnvelopeFailure,
    EnvelopeTooLarge,
    Keyring,
    Principal,
    Subscription,
    appwrite_signature,
    milliseconds,
    verify_appwrite_signature,
)

VECTOR = Path(__file__).parent / "fixtures" / "jwe-v1.json"
"""``test-vectors/encryption/jwe-v1.json`` from
https://github.com/ChiragAgg5k/mcp-event-subscriptions (0.1.0a1), unchanged."""

APPWRITE_ID = "a" * 36
SERVER = "https://mcp.appwrite.io"
EXPIRES = 1_760_000_000_000
REFRESH = EXPIRES - 360_000


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


class ProfileVectorTests(unittest.TestCase):
    def test_opens_the_published_vector(self):
        vector = json.loads(VECTOR.read_text())
        record = vector["plaintext"]["subscription"]
        keyring = ring(
            (vector["protected"]["kid"], bytes.fromhex(vector["keyHex"])),
            ("k1", key(1)),
        )
        context = Context(**vector["context"])
        # The vector's lease ends in 2030; pin the clock before it, as the
        # package's own conformance tests do.
        now = datetime(2029, 1, 1, tzinfo=UTC)

        self.assertEqual(keyring.record(vector["token"], context, now=now), record)
        # The vector's id is not one subscription_id() derives, so the adapter
        # refuses it only for binding, after the profile accepted it.
        with self.assertRaises(EnvelopeError) as raised:
            keyring.open(vector["token"], context, now=now)
        self.assertEqual(raised.exception.failure, EnvelopeFailure.BINDING)

        # The vector's record is exactly what this adapter seals, given an id
        # derived from its contents.
        ours = Subscription.create(
            project=context.tenant,
            name=record["event"]["name"],
            arguments=record["event"]["arguments"],
            callback=record["delivery"]["url"],
            secret=record["delivery"]["secret"],
            expires=milliseconds(record["lease"]["expiresAt"]),
            refresh=milliseconds(record["lease"]["refreshBefore"]),
            principal=record["principal"],
        )
        self.assertEqual(
            Subscription.from_record({**record, "id": ours.id}, context.tenant), ours
        )
        # Logging a subscription never shows its secret.
        self.assertNotIn(record["delivery"]["secret"], repr(ours))


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
    """The worst realistic case must fit the budget that appwrite/appwrite#14293
    has to let `authPassword` hold, and nothing larger is ever sealed."""

    def seal(self, url: int, size: int) -> int:
        sealed = Subscription.create(
            project=APPWRITE_ID,
            name="functions.deployment.completed",
            arguments={
                "project_id": APPWRITE_ID,
                "function_id": APPWRITE_ID,
                "status": "failed",
            },
            callback="https://" + "x" * (url - len("https://")),
            secret=secret(size),
            expires=EXPIRES,
            refresh=REFRESH,
            principal=PRINCIPAL,
        )
        return len(ring(("k1", key(1))).seal(sealed, SERVER))

    def test_worst_case_fits_and_larger_is_refused(self):
        # 300-char callback URL, 64-byte secret, the longest catalog arguments.
        self.assertLessEqual(self.seal(300, 64), ENVELOPE_BUDGET)
        with self.assertRaises(EnvelopeTooLarge):
            self.seal(ENVELOPE_BUDGET, 32)


if __name__ == "__main__":
    unittest.main()
