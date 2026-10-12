"""Push delivery (``events/stream``) through real hosted servers.

Two server instances share one real Redis, the way replicas behind the gateway
do. A client opens a stream on one, Appwrite's webhooks worker
(:meth:`support.Appwrite.fire`) delivers to the other from the webhook the
stream stored in :class:`support.Cloud`, and the event arrives on the stream:
the cross-replica hop this feature exists for. The flows then cover what a
stream owns over its life: one webhook with a short lease, renewed while the
stream is open and deleted when it closes, heartbeats, immediate errors for
invalid subscriptions, termination when access goes away, recovery from a bus
outage, a clean close on shutdown and a lease that runs out after a crash.

The servers run with short clocks (:data:`SETTINGS`) so renewals and
heartbeats happen within seconds.
"""

import base64
import json
import re
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from support import (
    EXPIRES,
    OTHER_TOKEN,
    PUBLIC_URL,
    SUBJECTLESS_TOKEN,
    SUBJECTS,
    TOKEN,
    Appwrite,
    Client,
    Cloud,
    Project,
    Server,
    Stream,
    collector,
    redis,
)

from mcp_server_appwrite.auth import public_base_url
from mcp_server_appwrite.events.bus import RedisBus
from mcp_server_appwrite.events.egress import Egress
from mcp_server_appwrite.events.envelope import Context, Keyring, Mode
from mcp_server_appwrite.events.ingress import Ingress
from mcp_server_appwrite.events.streams import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "appwrite"
KEYS = "k1:" + base64.b64encode(bytes(range(64, 96))).decode()

SETTINGS = Settings(heartbeat=0.5, renew=1.0, retry=0.3, lease=30.0, appwrite=0.5)
PRINCIPAL_STREAMS = 2
"""Streams one user may hold per server in these flows."""

MAIN = "6630f1a2b3c4d5e6f7a8"
FREE = "7740f1a2b3c4d5e6f7a8"
"""A Free-plan project whose two webhooks are taken."""

REVOKED = "e2e-stream-revoked"
"""A user whose access to the project is revoked mid-stream."""
EXPIRING = "e2e-stream-expiring"
"""A user whose token expires while the stream is open."""

ROWS = {"project_id": MAIN, "database_id": "main", "table_id": "support_tickets"}
ROW_EVENT = "tablesdb.main.tables.support_tickets.rows.row_8f2a.create"
ROW_DATA = {
    "database_id": "main",
    "table_id": "support_tickets",
    "row_id": "row_8f2a",
    "created_at": "2026-10-09T10:00:00.250Z",
}

LABEL = re.compile(
    r"MCP events push \| tablesdb\.row\.created \| until "
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \| owner [A-Za-z0-9_-]{8}"
)
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

ACTIVE = "notifications/events/active"
EVENT = "notifications/events/event"
HEARTBEAT = "notifications/events/heartbeat"
ERROR = "notifications/events/error"
TERMINATED = "notifications/events/terminated"

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
NOT_FOUND = -32011
FORBIDDEN = -32012
RESOURCE_EXHAUSTED = -32013
UNSUPPORTED = -32014

cloud: Cloud


def setUpModule() -> None:
    global cloud
    cloud = Cloud()
    SUBJECTS[REVOKED] = "cc17c8d9e0f1a2b3c4d5"
    SUBJECTS[EXPIRING] = "dd28d9e0f1a2b3c4d5e6"
    cloud.add(Project(MAIN, tables={("main", "support_tickets")}))
    cloud.add(Project(FREE, tables={("main", "tickets")}, webhook_limit=2))
    for id in ("console_hook_1", "console_hook_2"):
        cloud.store(FREE, {"$id": id, "name": "Mine", "url": "https://example.com"})
    cloud.grant(TOKEN, {MAIN, FREE})
    cloud.grant(OTHER_TOKEN, {MAIN})
    cloud.grant(EXPIRING, {MAIN})


def tearDownModule() -> None:
    cloud.close()
    SUBJECTS.pop(REVOKED, None)
    SUBJECTS.pop(EXPIRING, None)
    EXPIRES.clear()


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def environment(**extra: str | None) -> dict[str, str | None]:
    return {
        "MCP_EVENTS": "1",
        "MCP_EVENTS_PUSH": "1",
        "MCP_EVENTS_SEALING_KEYS": KEYS,
        "MCP_EVENTS_REDIS_URL": redis().url,
        "APPWRITE_ENDPOINT": cloud.endpoint,
        **extra,
    }


def server(settings: Settings = SETTINGS) -> Server:
    """A push server. Its bus pings Redis every 0.3 s, so a dead connection
    shows within a second."""

    def build() -> dict[str, Any]:
        bus = RedisBus(redis().url, ping=0.3, pong=0.5)
        ingress = Ingress(Keyring.from_env(), Egress(), base=public_base_url(), bus=bus)
        return {"settings": settings, "ingress": ingress}

    return Server(environment(), build)


def params(arguments: dict[str, str] = ROWS, **extra: Any) -> dict[str, Any]:
    """``events/stream`` params as a push client sends them."""
    return {
        "name": "tablesdb.row.created",
        "arguments": dict(arguments),
        "cursor": None,
        **extra,
    }


def wait(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class StreamFlow(unittest.TestCase):
    """Streams on one replica receive what Appwrite delivers to another."""

    a: Server
    b: Server
    appwrite: Appwrite

    @classmethod
    def setUpClass(cls) -> None:
        settings = replace(SETTINGS, principal_streams=PRINCIPAL_STREAMS)
        cls.a = server(settings).__enter__()
        cls.b = server(settings).__enter__()
        cls.appwrite = Appwrite(cls.b)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.appwrite.close()
        cls.b.__exit__(None, None, None)
        cls.a.__exit__(None, None, None)

    def ours(self, project: str = MAIN) -> dict[str, dict[str, Any]]:
        return {
            id: document
            for id, document in cloud.documents(project).items()
            if LABEL.fullmatch(document["name"])
        }

    @contextmanager
    def opened(
        self, client: Client, request: dict[str, Any] | None = None, **options: Any
    ) -> Iterator[tuple[Stream, str]]:
        """A stream once ``active`` arrived, and the id of its webhook."""
        before = set(self.ours())
        with client.stream(request or params(), **options) as stream:
            yield stream, self.activated(stream, before)

    def activated(self, stream: Stream, before: set[str]) -> str:
        active = stream.next()
        self.assertEqual(stream.status, 200)
        self.assertTrue(stream.content_type.startswith("text/event-stream"))
        self.assertEqual(active["method"], ACTIVE, active)
        self.assertEqual(
            active["params"],
            {"cursor": None, "truncated": False, "_meta": {SUBSCRIPTION_ID: stream.id}},
        )
        created = set(self.ours()) - before
        self.assertEqual(len(created), 1, created)
        return created.pop()

    def refused(self, stream: Stream, code: int) -> dict[str, Any]:
        """An invalid stream: an immediate JSON error, no SSE, no webhook."""
        message = stream.next()
        self.assertTrue(stream.content_type.startswith("application/json"), message)
        self.assertEqual(message["id"], stream.id)
        self.assertEqual(message["error"]["code"], code, message)
        return message["error"]

    def gone(self, id: str, project: str = MAIN) -> None:
        self.assertTrue(
            wait(lambda: id not in cloud.documents(project)), f"{id} still stored"
        )

    def test_stream_receives_events_delivered_to_another_replica(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.receive(legacy)

    def receive(self, legacy: bool) -> None:
        metrics = collector()
        streams = metrics.count("mcp.events.streams", event="tablesdb.row.created")
        delivered = metrics.count(
            "mcp.events.deliveries", mode="push", outcome="delivered"
        )
        with self.a.client() as client, self.opened(client, legacy=legacy) as opened:
            stream, id = opened
            document = cloud.documents(MAIN)[id]
            self.assertEqual(document["url"], f"{PUBLIC_URL}/appwrite/webhooks/{id}")
            self.assertEqual(
                document["events"],
                ["tablesdb.main.tables.support_tickets.rows.*.create"],
            )
            self.assertEqual(document["authUsername"], "mcp-events")
            subscription = Keyring.parse(KEYS).open(
                document["httpPass"], Context(PUBLIC_URL, MAIN, id)
            )
            self.assertIs(subscription.mode, Mode.PUSH)
            self.assertIsNone(subscription.callback)
            self.assertAlmostEqual(
                subscription.expires / 1000, time.time() + SETTINGS.lease, delta=5
            )
            self.assertEqual(
                metrics.wait(
                    "mcp.events.streams", streams + 1, event=subscription.name
                ),
                streams + 1,
            )

            [answer] = self.appwrite.fire(
                cloud, MAIN, ROW_EVENT, fixture("row_created")
            )
            self.assertEqual(answer.status_code, 200, answer.text)
            self.assertEqual(answer.json()["status"], "accepted", answer.text)
            event = stream.until(EVENT)
            self.assertEqual(
                event["params"],
                {
                    "eventId": answer.json()["eventId"],
                    "name": "tablesdb.row.created",
                    "timestamp": "2026-10-09T10:00:00.250Z",
                    "data": ROW_DATA,
                    "cursor": None,
                    "_meta": {SUBSCRIPTION_ID: stream.id},
                },
            )
            self.assertEqual(
                metrics.wait(
                    "mcp.events.deliveries",
                    delivered + 1,
                    mode="push",
                    outcome="delivered",
                ),
                delivered + 1,
            )

            heartbeat = stream.next(SETTINGS.heartbeat * 4)
            self.assertEqual(
                heartbeat,
                {
                    "jsonrpc": "2.0",
                    "method": HEARTBEAT,
                    "params": {"cursor": None, "_meta": {SUBSCRIPTION_ID: stream.id}},
                },
            )

            # The lease is renewed while the stream is open.
            renewals = cloud.calls("PUT", f"/v1/webhooks/{id}")
            self.assertTrue(
                wait(lambda: cloud.calls("PUT", f"/v1/webhooks/{id}") > renewals)
            )
            renewed = Keyring.parse(KEYS).open(
                cloud.documents(MAIN)[id]["httpPass"], Context(PUBLIC_URL, MAIN, id)
            )
            self.assertGreater(renewed.expires, subscription.expires)
            webhook = next(w for w in cloud.webhooks(MAIN) if w.id == id)

        # Aborting the request ends the subscription and deletes its webhook.
        self.gone(id)
        self.assertEqual(
            metrics.wait("mcp.events.streams", streams, event="tablesdb.row.created"),
            streams,
        )
        # A delivery Appwrite was already sending finds no stream: 200, dropped.
        dropped = metrics.count("mcp.events.ingress", reason="no_listener")
        late = self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
        self.assertEqual(late.status_code, 200, late.text)
        self.assertEqual(late.json(), {"status": "dropped", "reason": "no_listener"})
        self.assertEqual(
            metrics.wait("mcp.events.ingress", dropped + 1, reason="no_listener"),
            dropped + 1,
        )

    def test_each_stream_is_its_own_subscription(self):
        with self.a.client() as client, self.b.client() as other:
            with (
                self.opened(client) as (first, one),
                self.opened(other) as (
                    second,
                    two,
                ),
            ):
                ids = {one, two}
                self.assertEqual(len(ids), 2, ids)
                answers = self.appwrite.fire(
                    cloud, MAIN, ROW_EVENT, fixture("row_created")
                )
                self.assertEqual(len(answers), 2)
                for stream in (first, second):
                    self.assertEqual(stream.until(EVENT)["params"]["data"], ROW_DATA)
        for id in ids:
            self.gone(id)

    def test_invalid_streams_are_refused_before_any_stream_opens(self):
        before = cloud.documents(MAIN)
        cases = [
            ("unknown event", TOKEN, {**params(), "name": "rows.deleted"}, NOT_FOUND),
            (
                "bad arguments",
                TOKEN,
                params({**ROWS, "table_id": "tickets.*"}),
                INVALID_PARAMS,
            ),
            ("bad cursor", TOKEN, params(cursor=7), INVALID_PARAMS),
            ("bad maxAgeMs", TOKEN, params(maxAgeMs=-1), INVALID_PARAMS),
            ("missing table", TOKEN, params({**ROWS, "table_id": "orders"}), NOT_FOUND),
            ("no user", SUBJECTLESS_TOKEN, params(), FORBIDDEN),
            (
                "no access",
                OTHER_TOKEN,
                params({**ROWS, "project_id": FREE, "table_id": "tickets"}),
                FORBIDDEN,
            ),
        ]
        for name, token, request, code in cases:
            with self.subTest(case=name), Client(self.a.url, token) as client:
                with client.stream(request) as stream:
                    self.refused(stream, code)
        self.assertEqual(cloud.documents(MAIN), before)

        with self.subTest(case="plan limit"), self.a.client() as client:
            with client.stream(
                params({**ROWS, "project_id": FREE, "table_id": "tickets"})
            ) as stream:
                error = self.refused(stream, RESOURCE_EXHAUSTED)
            self.assertEqual(error["data"], {"limit": "webhooks", "max": 2})

        with self.subTest(case="stream cap"), Client(self.a.url, OTHER_TOKEN) as client:
            held = [client.stream(params()) for _ in range(PRINCIPAL_STREAMS)]
            try:
                for stream in held:
                    stream.__enter__()
                    self.assertEqual(stream.next()["method"], ACTIVE)
                with client.stream(params()) as refused:
                    error = self.refused(refused, RESOURCE_EXHAUSTED)
                self.assertEqual(
                    error["data"], {"limit": "streams", "max": PRINCIPAL_STREAMS}
                )
            finally:
                for stream in held:
                    stream.close()
            # The other replica keeps its own count.
            with Client(self.b.url, OTHER_TOKEN) as elsewhere:
                with elsewhere.stream(params()) as stream:
                    self.assertEqual(stream.next()["method"], ACTIVE)

        with self.subTest(case="push through events/subscribe"):
            with self.a.client() as client:
                _, message = client.call(
                    "events/subscribe",
                    {**params(), "delivery": {"mode": "push"}},
                )
            self.assertEqual(message["error"]["code"], UNSUPPORTED, message)
            self.assertEqual(
                message["error"]["data"], {"feature": "deliveryMode", "value": "push"}
            )

    def terminated(self, stream: Stream, code: int) -> dict[str, Any]:
        """``terminated``, then the final result, then the end of the stream."""
        message = stream.until(TERMINATED)
        self.assertEqual(message["params"]["_meta"], {SUBSCRIPTION_ID: stream.id})
        error = message["params"]["error"]
        self.assertEqual(error["code"], code, message)
        final = stream.next()
        self.assertEqual(final["id"], stream.id, final)
        self.assertIn("result", final)
        self.assertTrue(stream.ended())
        return error

    def test_revoked_access_terminates_the_stream(self):
        cloud.grant(REVOKED, {MAIN})
        with Client(self.a.url, REVOKED) as client, self.opened(client) as (stream, id):
            cloud.grant(REVOKED, set())
            self.terminated(stream, FORBIDDEN)
        # Its webhook cannot be deleted without access; its lease runs out.
        self.assertIn(id, cloud.documents(MAIN))
        cloud.remove(MAIN, id)

    def test_deleted_webhook_terminates_the_stream(self):
        with self.a.client() as client, self.opened(client) as (stream, id):
            cloud.remove(MAIN, id)
            error = self.terminated(stream, NOT_FOUND)
        self.assertEqual(error["data"], {"kind": "subscription"})

    def test_failed_renewal_is_reported_and_retried(self):
        with self.a.client() as client, self.opened(client) as (stream, id):
            cloud.fail("PUT", f"/v1/webhooks/{id}")
            error = stream.until(ERROR, timeout=SETTINGS.renew * 4)
            self.assertEqual(error["params"]["error"]["code"], INTERNAL_ERROR, error)
            renewals = cloud.calls("PUT", f"/v1/webhooks/{id}")
            self.assertTrue(
                wait(lambda: cloud.calls("PUT", f"/v1/webhooks/{id}") > renewals)
            )
            self.assertEqual(stream.until(HEARTBEAT)["method"], HEARTBEAT)
            [answer] = self.appwrite.fire(
                cloud, MAIN, ROW_EVENT, fixture("row_created")
            )
            self.assertEqual(answer.json()["status"], "accepted", answer.text)
            self.assertEqual(stream.until(EVENT)["params"]["data"], ROW_DATA)
        self.gone(id)

    def test_bus_outage_is_reported_then_marked_as_a_gap(self):
        bus = redis()
        with self.a.client() as client, self.opened(client) as (stream, id):
            bus.stop()
            try:
                error = stream.until(ERROR)
                self.assertEqual(error["params"]["error"]["code"], INTERNAL_ERROR)
                # Appwrite's delivery meanwhile is acknowledged and dropped.
                [answer] = self.appwrite.fire(
                    cloud, MAIN, ROW_EVENT, fixture("row_created")
                )
                self.assertEqual(
                    answer.json(), {"status": "dropped", "reason": "bus_error"}
                )
            finally:
                bus.start()
            active = stream.until(ACTIVE)
            self.assertEqual(
                active["params"],
                {
                    "cursor": None,
                    "truncated": True,
                    "_meta": {SUBSCRIPTION_ID: stream.id},
                },
            )
            [answer] = self.appwrite.fire(
                cloud, MAIN, ROW_EVENT, fixture("row_created")
            )
            self.assertEqual(answer.json()["status"], "accepted", answer.text)
            self.assertEqual(stream.until(EVENT)["params"]["data"], ROW_DATA)
        self.gone(id)

    def test_stream_ends_cleanly_before_its_token_expires(self):
        EXPIRES[EXPIRING] = int(time.time()) + 3
        try:
            with (
                Client(self.a.url, EXPIRING) as client,
                self.opened(client) as (
                    stream,
                    id,
                ),
            ):
                # No terminated: the client is to reconnect with a new token,
                # not drop the subscription.
                final = stream.next(6.0)
                while final.get("method") == HEARTBEAT:
                    final = stream.next(6.0)
                self.assertEqual(final["id"], stream.id, final)
                self.assertIn("result", final)
                self.assertTrue(stream.ended())
            self.gone(id)
        finally:
            EXPIRES.pop(EXPIRING, None)

    def test_hung_renewal_is_reported_and_retried(self):
        table = "/v1/tablesdb/main/tables/support_tickets"
        with self.a.client() as client, self.opened(client) as (stream, id):
            cloud.before("GET", table, lambda: time.sleep(SETTINGS.appwrite * 3))
            error = stream.until(ERROR, timeout=SETTINGS.renew * 4)
            self.assertEqual(error["params"]["error"]["code"], INTERNAL_ERROR, error)
            renewals = cloud.calls("PUT", f"/v1/webhooks/{id}")
            self.assertTrue(
                wait(lambda: cloud.calls("PUT", f"/v1/webhooks/{id}") > renewals)
            )
            self.assertEqual(stream.until(HEARTBEAT)["method"], HEARTBEAT)
        self.gone(id)

    def test_frozen_bus_is_noticed(self):
        bus = redis()
        with self.a.client() as client, self.opened(client) as (stream, id):
            bus.pause()
            try:
                error = stream.until(ERROR, timeout=5.0)
                self.assertEqual(error["params"]["error"]["code"], INTERNAL_ERROR)
            finally:
                bus.resume()
            active = stream.until(ACTIVE, timeout=10.0)
            self.assertTrue(active["params"]["truncated"])
            [answer] = self.appwrite.fire(
                cloud, MAIN, ROW_EVENT, fixture("row_created")
            )
            self.assertEqual(answer.json()["status"], "accepted", answer.text)
            self.assertEqual(stream.until(EVENT)["params"]["data"], ROW_DATA)
        self.gone(id)

    def test_abort_while_the_webhook_is_created_leaves_none(self):
        before = set(self.ours())
        creates = cloud.calls("POST", "/v1/webhooks")
        cloud.before("POST", "/v1/webhooks", lambda: time.sleep(1.0))
        with self.a.client() as client:
            stream = client.stream(params())
            time.sleep(0.5)
            stream.close()
        self.assertTrue(wait(lambda: cloud.calls("POST", "/v1/webhooks") > creates))
        self.assertTrue(wait(lambda: set(self.ours()) == before), self.ours())


class LifecycleFlow(unittest.TestCase):
    """How a stream's webhook goes away when its server stops or dies."""

    def ours(self) -> set[str]:
        return {
            id
            for id, document in cloud.documents(MAIN).items()
            if LABEL.fullmatch(document["name"])
        }

    def test_shutdown_closes_streams_and_deletes_their_webhooks(self):
        before = self.ours()
        instance = server().__enter__()
        try:
            client = instance.client()
            stream = client.stream(params()).__enter__()
            self.assertEqual(stream.next()["method"], ACTIVE)
            [id] = self.ours() - before
        except BaseException:
            instance.__exit__(None, None, None)
            raise
        stopping = threading.Thread(target=instance.__exit__, args=(None, None, None))
        stopping.start()
        try:
            final = stream.next()
            while "method" in final:
                final = stream.next()
            self.assertEqual(final["id"], stream.id, final)
            self.assertIn("result", final)
            self.assertTrue(stream.ended())
        finally:
            stopping.join(30.0)
            stream.close()
            client.__exit__(None, None, None)
        self.assertFalse(stopping.is_alive(), "the server did not stop")
        self.assertNotIn(id, cloud.documents(MAIN))

    def test_a_crashed_stream_leaves_a_lease_that_runs_out(self):
        crashing = Settings(heartbeat=5.0, renew=60.0, retry=5.0, lease=1.0)
        before = self.ours()
        with server(crashing) as instance:
            appwrite = Appwrite(instance)
            try:
                # The replica dies before it can delete the webhook.
                cloud.fail("DELETE", r"/v1/webhooks/sub_[0-9a-f]{32}")
                with instance.client() as client, client.stream(params()) as stream:
                    self.assertEqual(stream.next()["method"], ACTIVE)
                    [id] = self.ours() - before
                self.assertTrue(
                    wait(lambda: cloud.calls("DELETE", f"/v1/webhooks/{id}") == 1)
                )
                self.assertIn(id, cloud.documents(MAIN))

                # Past its lease, Appwrite's deliveries are dropped as expired.
                time.sleep(crashing.lease + 1.1)
                webhook = next(w for w in cloud.webhooks(MAIN) if w.id == id)
                answer = appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
                self.assertEqual(
                    answer.json(), {"status": "dropped", "reason": "expired"}
                )

                # A minute past its lease (not the ten minutes a webhook
                # subscription gets), the owner's next stream removes it.
                document = cloud.documents(MAIN)[id]
                stamp = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 65)
                )
                cloud.store(
                    MAIN,
                    {
                        **document,
                        "$id": id,
                        "name": re.sub(
                            r"until \S+", f"until {stamp}", document["name"]
                        ),
                    },
                )
                with instance.client() as client, client.stream(params()) as stream:
                    self.assertEqual(stream.next()["method"], ACTIVE)
                    self.assertNotIn(id, cloud.documents(MAIN))
            finally:
                appwrite.close()


class PushOffFlow(unittest.TestCase):
    """Without push, nothing about it is advertised or served."""

    def test_push_needs_its_flag_and_a_bus(self):
        for name, extra in (
            ("flag off", {"MCP_EVENTS_PUSH": None}),
            ("no bus", {"MCP_EVENTS_REDIS_URL": None}),
        ):
            with self.subTest(case=name), Server(environment(**extra)) as instance:
                with instance.client() as client:
                    _, listing = client.call("events/list")
                    for event in listing["result"]["events"]:
                        self.assertEqual(event["delivery"], ["webhook"], event)
                    with client.stream(params()) as stream:
                        message = stream.next()
                    self.assertEqual(message["error"]["code"], METHOD_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
