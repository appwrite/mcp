import base64
import os
import unittest
from unittest import mock

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mcp_server_appwrite.events import envelope as module
from mcp_server_appwrite.events.envelope import (
    ENVELOPE_BUDGET,
    KEYS_ENV,
    NONCE_BYTES,
    SUBSCRIPTION_ID_MAX,
    TAG_BYTES,
    Envelope,
    EnvelopeError,
    EnvelopeFailure,
    EnvelopeTooLarge,
    Keyring,
    KeyringError,
    Principal,
    SealingKey,
    Subscription,
    appwrite_signature,
    canonical_json,
    subscription_id,
    valid_subscription_id,
    verify_appwrite_signature,
)

PROJECT = "6630f1a2b3c4d5e6f7a8"
OTHER_PROJECT = "7740f1a2b3c4d5e6f7a8"
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


def subscription(
    callback: str = "https://chatgpt.com/backend-api/mcp/events/abc",
    secrets: tuple[str, ...] | None = None,
    expires: int = EXPIRES,
) -> Subscription:
    return Subscription.create(
        project=PROJECT,
        name="tablesdb.row.created",
        arguments={
            "project_id": PROJECT,
            "database_id": "main",
            "table_id": "support_tickets",
        },
        callback=callback,
        secrets=secrets or (secret(32),),
        expires=expires,
        principal=PRINCIPAL,
    )


class CanonicalJsonTests(unittest.TestCase):
    def test_sorted_compact_utf8(self):
        self.assertEqual(
            canonical_json({"b": 1, "a": ["é", None]}),
            '{"a":["é",null],"b":1}'.encode("utf-8"),
        )

    def test_rejects_nan(self):
        with self.assertRaises(ValueError):
            canonical_json({"a": float("nan")})


class SubscriptionIdTests(unittest.TestCase):
    def test_deterministic(self):
        first = subscription_id(PRINCIPAL, "https://a.test/x", "users.user.created", {})
        second = subscription_id(
            PRINCIPAL, "https://a.test/x", "users.user.created", {}
        )
        self.assertEqual(first, second)

    def test_argument_order_does_not_matter(self):
        forward = subscription_id(
            PRINCIPAL,
            "https://a.test/x",
            "tablesdb.row.created",
            {"project_id": "p", "database_id": "d", "table_id": "t"},
        )
        backward = subscription_id(
            PRINCIPAL,
            "https://a.test/x",
            "tablesdb.row.created",
            {"table_id": "t", "database_id": "d", "project_id": "p"},
        )
        self.assertEqual(forward, backward)

    def test_every_input_changes_the_id(self):
        base = ("principal", "https://a.test/x", "users.user.created", {"a": "1"})
        variants = (
            ("other", *base[1:]),
            (base[0], "https://a.test/y", *base[2:]),
            (*base[:2], "storage.file.created", base[3]),
            (*base[:3], {"a": "2"}),
        )
        original = subscription_id(*base)
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(subscription_id(*variant), original)

    def test_is_a_valid_appwrite_custom_id(self):
        # Mirrors utopia-php/database Key + Appwrite CustomId: at most 36 chars
        # of [A-Za-z0-9._-], not starting with "_", "." or "-".
        for index in range(200):
            value = subscription_id(f"principal-{index}", "https://a.test", "e", {})
            with self.subTest(value=value):
                self.assertLessEqual(len(value), SUBSCRIPTION_ID_MAX)
                self.assertRegex(value, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
                self.assertTrue(valid_subscription_id(value))

    def test_rejects_foreign_ids(self):
        for value in (
            "unique()",
            "_sub",
            "sub_short",
            "x" * 36,
            "sub_" + "g" * 31 + "!",
        ):
            with self.subTest(value=value):
                self.assertFalse(valid_subscription_id(value))

    def test_subscription_rejects_mismatched_id(self):
        with self.assertRaises(ValueError):
            Subscription(
                id="sub_" + "0" * 32,
                project=PROJECT,
                name="users.user.created",
                arguments={},
                callback="https://a.test",
                secrets=(secret(32),),
                expires=EXPIRES,
                principal=PRINCIPAL,
            )

    def test_principal_digest_is_stable_and_short(self):
        again = Principal(
            issuer="https://cloud.appwrite.io/v1/oauth2/console",
            subject="66b1c2d3e4f5a6b7c8d9",
            client="chatgpt-connector",
        ).digest
        other = Principal(
            issuer="https://cloud.appwrite.io/v1/oauth2/console",
            subject="66b1c2d3e4f5a6b7c8d9",
            client="another-client",
        ).digest
        self.assertEqual(PRINCIPAL, again)
        self.assertNotEqual(PRINCIPAL, other)
        self.assertEqual(len(PRINCIPAL), 22)


class SealTests(unittest.TestCase):
    def setUp(self):
        self.keyring = ring(("k1", key(1)))

    def test_round_trip(self):
        original = subscription(secrets=(secret(32), secret(24)))
        envelope = self.keyring.seal(original)
        self.assertTrue(envelope.startswith("v1.k1."))
        opened = self.keyring.open(envelope, original.id, PROJECT)
        self.assertEqual(opened, original)
        self.assertEqual(
            list(opened.arguments), ["database_id", "project_id", "table_id"]
        )

    def test_nonce_is_random(self):
        original = subscription()
        self.assertNotEqual(self.keyring.seal(original), self.keyring.seal(original))

    def test_envelope_is_basic_auth_safe(self):
        envelope = self.keyring.seal(subscription())
        self.assertRegex(envelope, r"^[A-Za-z0-9._-]+$")

    def test_tampering_each_region_is_rejected(self):
        original = subscription()
        envelope = Envelope.parse(self.keyring.seal(original))
        ciphertext_end = len(envelope.payload) - TAG_BYTES
        regions = {
            "nonce": range(0, NONCE_BYTES),
            "ciphertext": range(NONCE_BYTES, ciphertext_end),
            "tag": range(ciphertext_end, len(envelope.payload)),
        }
        for region, offsets in regions.items():
            for offset in (offsets[0], offsets[len(offsets) // 2], offsets[-1]):
                with self.subTest(region=region, offset=offset):
                    payload = bytearray(envelope.payload)
                    payload[offset] ^= 0x01
                    tampered = Envelope(envelope.version, envelope.key, bytes(payload))
                    with self.assertRaises(EnvelopeError) as caught:
                        self.keyring.open(str(tampered), original.id, PROJECT)
                    self.assertEqual(
                        caught.exception.failure, EnvelopeFailure.AUTHENTICATION
                    )

    def test_truncated_and_extended_payloads_are_rejected(self):
        original = subscription()
        envelope = Envelope.parse(self.keyring.seal(original))
        for payload in (envelope.payload[:-1], envelope.payload + b"\x00"):
            with self.subTest(length=len(payload)):
                changed = Envelope(envelope.version, envelope.key, payload)
                with self.assertRaises(EnvelopeError) as caught:
                    self.keyring.open(str(changed), original.id, PROJECT)
                self.assertEqual(
                    caught.exception.failure, EnvelopeFailure.AUTHENTICATION
                )

    def test_wrong_version(self):
        original = subscription()
        envelope = "v2" + self.keyring.seal(original)[2:]
        with self.assertRaises(EnvelopeError) as caught:
            self.keyring.open(envelope, original.id, PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.VERSION)

    def test_malformed(self):
        original = subscription()
        for envelope in (
            "",
            "v1",
            "v1.k1",
            "v1.k1.a.b",
            "v1.k1.!!!!",
            "v1.k1.AAAA",
            "v1.k 1.AAAA",
        ):
            with self.subTest(envelope=envelope):
                with self.assertRaises(EnvelopeError) as caught:
                    self.keyring.open(envelope, original.id, PROJECT)
                self.assertEqual(caught.exception.failure, EnvelopeFailure.MALFORMED)

    def test_bound_to_subscription_id(self):
        original = subscription()
        other = subscription(
            callback="https://chatgpt.com/backend-api/mcp/events/other"
        )
        envelope = self.keyring.seal(original)
        with self.assertRaises(EnvelopeError) as caught:
            self.keyring.open(envelope, other.id, PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.AUTHENTICATION)

    def test_bound_to_project(self):
        original = subscription()
        envelope = self.keyring.seal(original)
        with self.assertRaises(EnvelopeError) as caught:
            self.keyring.open(envelope, original.id, OTHER_PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.AUTHENTICATION)

    def test_contents_must_hash_to_the_id(self):
        # An envelope sealed by a buggy writer under a mismatched id still opens
        # (the AEAD binding holds) but its contents do not hash to that id.
        original = subscription()
        forged_id = "sub_" + "f" * 32
        keyring = self.keyring
        material = keyring.active
        sealed = AESGCM(material.encryption).encrypt(
            b"\x00" * NONCE_BYTES,
            module._plaintext(original),
            module._associated(material.id, forged_id, PROJECT),
        )
        forged = str(Envelope("v1", material.id, b"\x00" * NONCE_BYTES + sealed))
        with self.assertRaises(EnvelopeError) as caught:
            keyring.open(forged, forged_id, PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.BINDING)


class RotationTests(unittest.TestCase):
    def test_old_key_opens_new_key_seals(self):
        old = ring(("k1", key(1)))
        rotated = ring(("k2", key(2)), ("k1", key(1)))
        original = subscription()
        legacy = old.seal(original)
        self.assertEqual(rotated.open(legacy, original.id, PROJECT), original)
        fresh = rotated.seal(original)
        self.assertTrue(fresh.startswith("v1.k2."))
        self.assertEqual(rotated.open(fresh, original.id, PROJECT), original)

    def test_unknown_key(self):
        original = subscription()
        envelope = ring(("k2", key(2))).seal(original)
        with self.assertRaises(EnvelopeError) as caught:
            ring(("k1", key(1))).open(envelope, original.id, PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.KEY)

    def test_key_id_swap_is_rejected(self):
        # Same material under two ids: the key id is bound as associated data.
        keyring = ring(("k1", key(1)), ("k2", key(1)))
        original = subscription()
        envelope = keyring.seal(original)
        swapped = envelope.replace("v1.k1.", "v1.k2.", 1)
        with self.assertRaises(EnvelopeError) as caught:
            keyring.open(swapped, original.id, PROJECT)
        self.assertEqual(caught.exception.failure, EnvelopeFailure.AUTHENTICATION)


class KeyringTests(unittest.TestCase):
    def test_from_env(self):
        encoded = base64.b64encode(key(3)).decode()
        with mock.patch.dict(
            os.environ, {KEYS_ENV: f" k3:{encoded} , k1:{encoded[:-1]}x= "}
        ):
            with self.assertRaises(KeyringError):
                Keyring.from_env()
        other = base64.urlsafe_b64encode(key(4)).decode().rstrip("=")
        with mock.patch.dict(os.environ, {KEYS_ENV: f"k3:{encoded},k4:{other}"}):
            keyring = Keyring.from_env()
        self.assertEqual([entry.id for entry in keyring.keys], ["k3", "k4"])
        self.assertEqual(keyring.active.material, key(3))
        self.assertEqual(keyring.get("k4").material, key(4))

    def test_missing(self):
        with mock.patch.dict(os.environ, {KEYS_ENV: ""}):
            with self.assertRaisesRegex(KeyringError, KEYS_ENV):
                Keyring.from_env()

    def test_rejects_short_long_and_weak_keys(self):
        for material in (
            os.urandom(16),
            os.urandom(31),
            os.urandom(33),
            bytes(32),
            b"a" * 32,
        ):
            with self.subTest(length=len(material)):
                with self.assertRaises(KeyringError):
                    SealingKey(id="k1", material=material)

    def test_rejects_bad_entries(self):
        good = base64.b64encode(key(1)).decode()
        for text in (
            "",
            good,
            f"k.1:{good}",
            f":{good}",
            "k1:not base64!",
            f"k1:{good},k1:{base64.b64encode(key(2)).decode()}",
        ):
            with self.subTest(text=text):
                with self.assertRaises(KeyringError):
                    Keyring.parse(text)

    def test_signing_key_is_derived_and_valid_for_appwrite(self):
        keyring = ring(("k2", key(2)), ("k1", key(1)))
        first = keyring.signing_key("sub_" + "0" * 32)
        self.assertEqual(first, keyring.signing_key("sub_" + "0" * 32, "k2"))
        self.assertNotEqual(first, keyring.signing_key("sub_" + "1" * 32))
        self.assertNotEqual(first, keyring.signing_key("sub_" + "0" * 32, "k1"))
        # Appwrite's webhook `secret` param is Text(256, 8).
        self.assertTrue(8 <= len(first) <= 256)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        with self.assertRaises(EnvelopeError):
            keyring.signing_key("sub_" + "0" * 32, "k9")


class ExpiryTests(unittest.TestCase):
    def test_expired(self):
        current = subscription(expires=EXPIRES)
        self.assertFalse(current.expired(EXPIRES - 1))
        self.assertTrue(current.expired(EXPIRES))
        self.assertTrue(current.expired(EXPIRES + 1))

    def test_defaults_to_now(self):
        self.assertTrue(subscription(expires=1).expired())
        self.assertFalse(subscription(expires=2**53).expired())

    def test_expired_envelope_still_opens(self):
        keyring = ring(("k1", key(1)))
        stale = subscription(expires=1)
        opened = keyring.open(keyring.seal(stale), stale.id, PROJECT)
        self.assertTrue(opened.expired())


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

    def test_with_derived_key(self):
        keyring = ring(("k1", key(1)))
        signing = keyring.signing_key(self.URL.rsplit("/", 1)[1])
        signature = appwrite_signature(self.URL, self.BODY, signing)
        self.assertTrue(
            verify_appwrite_signature(self.URL, self.BODY, signature, signing)
        )


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
