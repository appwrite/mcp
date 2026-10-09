"""The production retry schedule, whose real-time waits (30 s, 2 min, 8 min)
are too long to run end to end.

Everything else about delivery is covered end to end. Signing (checked by the
official ``standardwebhooks`` library), the delivery body and headers,
re-signed retries with a short injected policy, 410/413 not retried, dual
signing and the 256 KiB cap go through the ingress in
``tests/e2e/test_events_ingress.py``. ``whsec_`` validation and every outcome
of the verification handshake go through ``events/subscribe`` in
``tests/e2e/test_events_subscribe.py``.
"""

import base64
import unittest

import httpx

from mcp_server_appwrite.events import delivery, egress
from mcp_server_appwrite.events.errors import CallbackFailure

URL = "https://hooks.example.com/mcp"
SUBSCRIPTION = "sub_0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
EVENT = delivery.Event(
    id="evt_1",
    name="tablesdb.row.created",
    timestamp="2026-10-09T12:00:00Z",
    data={"row_id": "r1", "table_id": "tickets"},
)


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


class RetryScheduleTest(unittest.IsolatedAsyncioTestCase):
    """The production schedule spans ~12 minutes, too long to wait for in e2e,
    which runs a short injected policy instead."""

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

    async def test_abandons_after_bounded_attempts(self) -> None:
        recorder = Recorder(httpx.Response(500))
        result = await self.run_deliver(recorder, sample=0.999999)
        self.assertEqual(
            result,
            delivery.Result(
                delivery.Outcome.ABANDONED, 4, 500, CallbackFailure.HTTP_5XX
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


if __name__ == "__main__":
    unittest.main()
