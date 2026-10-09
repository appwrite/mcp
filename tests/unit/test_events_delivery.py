import base64
import hashlib
import hmac
import json
import unittest

import httpx

from mcp_server_appwrite.events import delivery, egress

# Official Standard Webhooks signing vector, shared by every reference library,
# e.g. libraries/python/tests/test_webhooks.py::test_sign_function in
# https://github.com/standard-webhooks/standard-webhooks
VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
VECTOR_TIMESTAMP = 1614265330
VECTOR_BODY = b'{"test": 2432232314}'
VECTOR_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

URL = "https://hooks.example.com/mcp"
SUBSCRIPTION = "sub_0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
ROTATED = "whsec_" + base64.b64encode(bytes(range(100, 132))).decode()
EVENT = delivery.Event(
    id="evt_1",
    name="tablesdb.row.created",
    timestamp="2026-10-09T12:00:00Z",
    data={"row_id": "r1", "table_id": "tickets"},
)


def verify(secret: str, headers: httpx.Headers, body: bytes, now: int) -> bool:
    """Receiver-side Standard Webhooks check, written from the spec text."""
    key = base64.b64decode(secret.split("_", 1)[1])
    message_id = headers["webhook-id"]
    timestamp = headers["webhook-timestamp"]
    if abs(now - int(timestamp)) > 300:
        return False
    expected = base64.b64encode(
        hmac.new(
            key, message_id.encode() + b"." + timestamp.encode() + b"." + body, "sha256"
        ).digest()
    )
    for entry in headers["webhook-signature"].split(" "):
        version, _, signature = entry.partition(",")
        if version == "v1" and hmac.compare_digest(signature.encode(), expected):
            return True
    return False


class Clock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class Recorder:
    """MockTransport handler replaying scripted responses and keeping requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


class SigningTest(unittest.TestCase):
    def test_matches_standard_webhooks_vector(self) -> None:
        key = delivery.decode_secret(VECTOR_SECRET)
        signature = delivery.sign([key], VECTOR_ID, VECTOR_TIMESTAMP, VECTOR_BODY)
        self.assertEqual(signature, VECTOR_SIGNATURE)

    def test_vector_round_trips_through_independent_verifier(self) -> None:
        headers = httpx.Headers(
            {
                "webhook-id": VECTOR_ID,
                "webhook-timestamp": str(VECTOR_TIMESTAMP),
                "webhook-signature": VECTOR_SIGNATURE,
            }
        )
        self.assertTrue(verify(VECTOR_SECRET, headers, VECTOR_BODY, VECTOR_TIMESTAMP))
        self.assertFalse(
            verify(VECTOR_SECRET, headers, VECTOR_BODY + b" ", VECTOR_TIMESTAMP)
        )

    def test_signature_uses_the_decoded_key(self) -> None:
        key = bytes(range(32))
        content = b"m.1." + b"{}"
        expected = base64.b64encode(
            hmac.new(key, content, hashlib.sha256).digest()
        ).decode()
        self.assertEqual(delivery.sign([key], "m", 1, b"{}"), "v1," + expected)


class SecretTest(unittest.TestCase):
    def test_accepts_bounds(self) -> None:
        for size in (24, 32, 64):
            with self.subTest(size=size):
                secret = "whsec_" + base64.b64encode(b"k" * size).decode()
                self.assertEqual(delivery.decode_secret(secret), b"k" * size)

    def test_rejects_invalid_secrets(self) -> None:
        cases = [
            "",
            "whsec_",
            "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
            "WHSEC_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
            "whsec_not*base64!",
            "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaS",  # bad padding
            "whsec_" + base64.b64encode(b"k" * 23).decode(),
            "whsec_" + base64.b64encode(b"k" * 65).decode(),
            "whsec_ " + base64.b64encode(b"k" * 32).decode(),
            None,
            42,
        ]
        for secret in cases:
            with self.subTest(secret=secret):
                with self.assertRaises(delivery.InvalidSecretError):
                    delivery.decode_secret(secret)  # type: ignore[arg-type]

    def test_callback_requires_valid_secrets(self) -> None:
        with self.assertRaises(delivery.InvalidSecretError):
            delivery.Callback(URL, SUBSCRIPTION, ())
        with self.assertRaises(delivery.InvalidSecretError):
            delivery.Callback(URL, SUBSCRIPTION, ("whsec_short",))


class VerificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.callback = delivery.Callback(URL, SUBSCRIPTION, (SECRET,))
        self.clock = Clock()

    async def run_verify(self, handler) -> None:
        transport = httpx.MockTransport(handler)
        async with egress.Egress(transport=transport) as client:
            dispatcher = delivery.Dispatcher(client, clock=self.clock)
            await dispatcher.verify(self.callback)

    async def assert_reason(self, handler, reason: delivery.Reason) -> None:
        with self.assertRaises(delivery.CallbackError) as raised:
            await self.run_verify(handler)
        self.assertEqual(raised.exception.reason, reason)

    async def test_echoed_challenge_succeeds(self) -> None:
        seen: list[httpx.Request] = []

        def echo(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            payload = json.loads(request.content)
            return httpx.Response(200, json={"challenge": payload["challenge"]})

        await self.run_verify(echo)
        request = seen[0]
        payload = json.loads(request.content)
        self.assertEqual(payload["type"], "verification")
        self.assertGreaterEqual(len(payload["challenge"]), 32)
        self.assertTrue(request.headers["webhook-id"].startswith("msg_verification_"))
        self.assertEqual(request.headers["x-mcp-subscription-id"], SUBSCRIPTION)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertTrue(
            verify(SECRET, request.headers, request.content, int(self.clock.now))
        )

    async def test_challenges_are_single_use(self) -> None:
        challenges: list[str] = []

        def echo(request: httpx.Request) -> httpx.Response:
            challenges.append(json.loads(request.content)["challenge"])
            return httpx.Response(200, json={"challenge": challenges[-1]})

        await self.run_verify(echo)
        await self.run_verify(echo)
        self.assertNotEqual(challenges[0], challenges[1])

    async def test_mismatched_challenge_fails(self) -> None:
        await self.assert_reason(
            lambda request: httpx.Response(200, json={"challenge": "wrong"}),
            delivery.Reason.CHALLENGE_FAILED,
        )

    async def test_malformed_bodies_fail(self) -> None:
        for content in [b"", b"not json", b"[]", b'{"challenge": 1}', b"{}"]:
            with self.subTest(content=content):
                await self.assert_reason(
                    lambda request, content=content: httpx.Response(
                        200, content=content
                    ),
                    delivery.Reason.CHALLENGE_FAILED,
                )

    async def test_non_2xx_echo_fails(self) -> None:
        def echo(status: int):
            def handler(request: httpx.Request) -> httpx.Response:
                challenge = json.loads(request.content)["challenge"]
                return httpx.Response(status, json={"challenge": challenge})

            return handler

        await self.assert_reason(echo(404), delivery.Reason.HTTP_4XX)
        await self.assert_reason(echo(503), delivery.Reason.HTTP_5XX)

    async def test_redirect_is_not_followed(self) -> None:
        recorder = Recorder(
            httpx.Response(307, headers={"Location": "https://169.254.169.254/"})
        )
        await self.assert_reason(recorder, delivery.Reason.CHALLENGE_FAILED)
        self.assertEqual(len(recorder.requests), 1)

    async def test_transport_failures_are_categorized(self) -> None:
        await self.assert_reason(
            Recorder(httpx.ReadTimeout("slow")), delivery.Reason.TIMEOUT
        )
        await self.assert_reason(
            Recorder(httpx.ConnectError("refused")),
            delivery.Reason.CONNECTION_REFUSED,
        )

    async def test_forbidden_destination_is_connection_refused(self) -> None:
        self.callback = delivery.Callback(
            "http://hooks.example.com/", SUBSCRIPTION, (SECRET,)
        )
        recorder = Recorder(httpx.Response(200))
        await self.assert_reason(recorder, delivery.Reason.CONNECTION_REFUSED)
        self.assertEqual(recorder.requests, [])


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.callback = delivery.Callback(URL, SUBSCRIPTION, (SECRET,))
        self.clock = Clock()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.now += seconds

    async def run_deliver(
        self,
        recorder: Recorder,
        event: delivery.Event = EVENT,
        sample: float = 0.5,
    ) -> delivery.Result:
        transport = httpx.MockTransport(recorder)
        async with egress.Egress(transport=transport) as client:
            dispatcher = delivery.Dispatcher(
                client, sleep=self.sleep, clock=self.clock, sample=lambda: sample
            )
            return await dispatcher.deliver(self.callback, event)

    async def test_delivers_one_signed_event(self) -> None:
        recorder = Recorder(httpx.Response(200))
        result = await self.run_deliver(recorder)
        self.assertEqual(result, delivery.Result(delivery.Outcome.DELIVERED, 1, 200))
        request = recorder.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.content,
            b'{"eventId":"evt_1","name":"tablesdb.row.created",'
            b'"timestamp":"2026-10-09T12:00:00Z",'
            b'"data":{"row_id":"r1","table_id":"tickets"},"cursor":null}',
        )
        self.assertEqual(request.headers["webhook-id"], "evt_1")
        self.assertEqual(request.headers["x-mcp-subscription-id"], SUBSCRIPTION)
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertTrue(
            verify(SECRET, request.headers, request.content, int(self.clock.now))
        )
        self.assertEqual(self.sleeps, [])

    async def test_retries_with_backoff_and_re_signs(self) -> None:
        recorder = Recorder(
            httpx.Response(503),
            httpx.Response(429),
            httpx.Response(202),
        )
        result = await self.run_deliver(recorder)
        self.assertEqual(result, delivery.Result(delivery.Outcome.DELIVERED, 3, 202))
        self.assertEqual(self.sleeps, [30.0, 120.0])
        timestamps = [
            request.headers["webhook-timestamp"] for request in recorder.requests
        ]
        self.assertEqual(len(set(timestamps)), 3)
        signatures = {
            request.headers["webhook-signature"] for request in recorder.requests
        }
        self.assertEqual(len(signatures), 3)
        self.assertEqual(
            {r.headers["webhook-id"] for r in recorder.requests}, {"evt_1"}
        )
        for request in recorder.requests:
            sent = int(request.headers["webhook-timestamp"])
            self.assertTrue(verify(SECRET, request.headers, request.content, sent))

    async def test_abandons_after_bounded_attempts(self) -> None:
        recorder = Recorder(httpx.Response(500))
        result = await self.run_deliver(recorder, sample=0.999999)
        self.assertEqual(
            result,
            delivery.Result(
                delivery.Outcome.ABANDONED, 4, 500, delivery.Reason.HTTP_5XX
            ),
        )
        self.assertEqual(len(recorder.requests), 4)
        self.assertEqual(len(self.sleeps), 3)
        # Worst-case jitter plus four full egress timeouts stays within 15 min.
        worst = sum(self.sleeps) + 4 * egress.TIMEOUT_SECONDS
        self.assertLessEqual(worst, 15 * 60)

    async def test_jitter_bounds(self) -> None:
        policy = delivery.RetryPolicy()
        self.assertAlmostEqual(policy.delay(0, 0.0), 24.0)
        self.assertAlmostEqual(policy.delay(0, 1.0), 36.0)
        self.assertEqual(policy.attempts, 4)
        with self.assertRaises(ValueError):
            delivery.RetryPolicy(jitter=1.0)
        with self.assertRaises(ValueError):
            delivery.RetryPolicy(delays=(-1.0,))

    async def test_gone_and_too_large_are_final(self) -> None:
        for status in (410, 413):
            with self.subTest(status=status):
                self.sleeps.clear()
                recorder = Recorder(httpx.Response(status))
                result = await self.run_deliver(recorder)
                self.assertEqual(
                    result,
                    delivery.Result(
                        delivery.Outcome.REJECTED, 1, status, delivery.Reason.HTTP_4XX
                    ),
                )
                self.assertEqual(len(recorder.requests), 1)
                self.assertEqual(self.sleeps, [])

    async def test_transport_errors_are_retried(self) -> None:
        recorder = Recorder(
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.Response(200),
        )
        result = await self.run_deliver(recorder)
        self.assertEqual(result, delivery.Result(delivery.Outcome.DELIVERED, 3, 200))

    async def test_last_error_category_is_reported(self) -> None:
        result = await self.run_deliver(Recorder(httpx.ReadTimeout("slow")))
        self.assertEqual(result.outcome, delivery.Outcome.ABANDONED)
        self.assertEqual(result.reason, delivery.Reason.TIMEOUT)
        self.assertIsNone(result.status)

    async def test_forbidden_destination_is_not_retried(self) -> None:
        self.callback = delivery.Callback(
            "http://hooks.example.com/", SUBSCRIPTION, (SECRET,)
        )
        recorder = Recorder(httpx.Response(200))
        result = await self.run_deliver(recorder)
        self.assertEqual(
            result,
            delivery.Result(
                delivery.Outcome.REJECTED,
                1,
                reason=delivery.Reason.CONNECTION_REFUSED,
            ),
        )
        self.assertEqual(recorder.requests, [])

    async def test_dual_signs_during_rotation(self) -> None:
        self.callback = delivery.Callback(URL, SUBSCRIPTION, (ROTATED, SECRET))
        recorder = Recorder(httpx.Response(200))
        await self.run_deliver(recorder)
        request = recorder.requests[0]
        headers, body = request.headers, request.content
        self.assertEqual(len(headers["webhook-signature"].split(" ")), 2)
        now = int(self.clock.now)
        self.assertTrue(verify(SECRET, headers, body, now))
        self.assertTrue(verify(ROTATED, headers, body, now))
        other = "whsec_" + base64.b64encode(b"\x07" * 32).decode()
        self.assertFalse(verify(other, headers, body, now))

    async def test_payload_size_cap(self) -> None:
        overhead = len(
            delivery.Event(EVENT.id, EVENT.name, EVENT.timestamp, {"blob": ""}).encode()
        )
        fits = delivery.Event(
            EVENT.id,
            EVENT.name,
            EVENT.timestamp,
            {"blob": "a" * (delivery.MAX_BODY_BYTES - overhead)},
        )
        self.assertEqual(len(fits.encode()), delivery.MAX_BODY_BYTES)
        result = await self.run_deliver(Recorder(httpx.Response(200)), fits)
        self.assertEqual(result.outcome, delivery.Outcome.DELIVERED)

        too_large = delivery.Event(
            EVENT.id,
            EVENT.name,
            EVENT.timestamp,
            {"blob": "a" * (delivery.MAX_BODY_BYTES - overhead + 1)},
        )
        recorder = Recorder(httpx.Response(200))
        with self.assertRaises(delivery.PayloadTooLargeError):
            await self.run_deliver(recorder, too_large)
        self.assertEqual(recorder.requests, [])


if __name__ == "__main__":
    unittest.main()
