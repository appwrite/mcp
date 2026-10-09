"""Appwrite webhooks in, Standard Webhooks out, through the real hosted server.

Each flow plays the three parties around the server on localhost: Appwrite's
webhooks worker (:class:`support.Appwrite`) signs and posts recorded-shape
bodies to the real ingress, and the server's real dispatcher and SSRF-checked
egress deliver to an HTTPS receiver (:class:`support.Receiver`) that verifies
every request with the official ``standardwebhooks`` library. Counters are read
from the OTLP metrics the server exports (:class:`support.Collector`).

Until ``events/subscribe`` exists (PR 5 of #127), each test creates the webhook
the way subscribe will: the subscription sealed with the server's sealing keys
into ``authPassword``, the signing key derived from them as the webhook
``secret``, the event's Appwrite patterns as its events. Replace
:meth:`IngressFlow.subscribe` with a real ``events/subscribe`` call then.

The servers under test use the production egress with two test settings,
injected through ``build_app(ingress=...)``: loopback callbacks are allowed and
the receiver's self-signed certificate is trusted. Retries use a short policy
(0.3 s, 0.3 s, 0.6 s) and a 1 s request timeout so failure paths finish in
seconds; the production schedule is checked in the delivery unit tests.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from support import (
    PUBLIC_URL,
    Appwrite,
    Receiver,
    Reply,
    Server,
    Webhook,
    collector,
    whsec,
)

from mcp_server_appwrite.auth import public_base_url
from mcp_server_appwrite.events.catalog import lookup
from mcp_server_appwrite.events.delivery import Dispatcher, RetryPolicy
from mcp_server_appwrite.events.egress import Egress
from mcp_server_appwrite.events.envelope import (
    Keyring,
    KeyringError,
    Principal,
    Subscription,
)
from mcp_server_appwrite.events.ingress import Ingress

FIXTURES = Path(__file__).parent / "fixtures" / "appwrite"
PROJECT = "6630f1a2b3c4d5e6f7a8"
PRINCIPAL = Principal(
    issuer="https://cloud.appwrite.io/v1/oauth2/console",
    subject="66b1c2d3e4f5a6b7c8d9",
    client="chatgpt-connector",
).digest
KEY_ONE = "k1:" + base64.b64encode(bytes(range(64, 96))).decode()
KEY_TWO = "k2:" + base64.b64encode(bytes(range(128, 160))).decode()
RETIRED = "k0:" + base64.b64encode(bytes(range(200, 232))).decode()
WEBHOOK_PATH = "/appwrite/webhooks/"
POLICY = RetryPolicy(delays=(0.3, 0.3, 0.6), jitter=0.0)
TIMEOUT = 1.0
SETTLE = 0.5
"""Seconds to wait before asserting that nothing (more) was delivered."""

# Strings the fixtures carry that must never leave the server.
SENSITIVE = (
    "jane@example.com",
    "+14155550100",
    "sk_live_secret",
    "npm_secret",
    "Ignore previous instructions",
    "passport",
    "4242424242424242",
    "Traceback",
)

ROWS = ("tablesdb.row.created", {"database_id": "main", "table_id": "support_tickets"})
ROW_EVENT = "tablesdb.main.tables.support_tickets.rows.row_8f2a.create"
EXECUTIONS = ("functions.execution.failed", {"function_id": "fn_billing"})
EXECUTION_CREATE = "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"
EXECUTION_UPDATE = "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.update"
DEPLOYMENTS = ("functions.deployment.completed", {"function_id": "fn_billing"})
DEPLOYMENT_UPDATE = "functions.fn_billing.deployments.68e79f00aa11bb22cc33.update"
FILES = ("storage.file.created", {"bucket_id": "uploads"})
FILE_EVENT = "buckets.uploads.files.file_invoice.create"
USERS = ("users.user.created", {})
USER_EVENT = "users.user_jane.create"

INGRESS = "mcp.events.ingress"
DELIVERIES = "mcp.events.deliveries"

receiver: Receiver
untrusted: Receiver


def setUpModule() -> None:
    global receiver, untrusted
    receiver = Receiver()
    # A second endpoint whose certificate the server does not trust.
    untrusted = Receiver()


def tearDownModule() -> None:
    receiver.close()
    untrusted.close()


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def injected(dispatcher: type[Dispatcher] = Dispatcher) -> dict[str, Any]:
    """``build_app`` arguments: the production ingress, keyring and public URL
    from the environment, with an egress that may reach the local receiver."""
    egress = Egress(allow_loopback=True, ssl_context=receiver.trust(), timeout=TIMEOUT)
    return {
        "ingress": Ingress(
            Keyring.from_env(),
            egress,
            dispatcher(egress, POLICY),
            base=public_base_url(),
        )
    }


def events_on(keys: str) -> dict[str, str]:
    return {"MCP_EVENTS": "1", "MCP_EVENTS_SEALING_KEYS": keys}


class IngressFlow(unittest.TestCase):
    """Shared steps: subscribe, deliver from Appwrite, observe the receiver."""

    keys = KEY_ONE
    server: Server
    appwrite: Appwrite

    def subscribe(
        self,
        name: str,
        arguments: dict[str, str],
        *,
        secrets: tuple[str, ...] | None = None,
        replies: tuple[Reply, ...] = (),
        expires: int | None = None,
        keys: str | None = None,
        target: Receiver | None = None,
        also_check: tuple[str, ...] = (),
    ) -> tuple[Webhook, str, tuple[str, ...]]:
        """Create the webhook PR 5's ``events/subscribe`` will create. Returns
        it with the callback URL and the subscription's secrets. The receiver
        checks each delivery against the secrets, then ``also_check``."""
        secrets = secrets or (whsec(),)
        callback = (target or receiver).endpoint((*secrets, *also_check), replies)
        subscription = Subscription.create(
            project=PROJECT,
            name=name,
            arguments={"project_id": PROJECT, **arguments},
            callback=callback,
            secrets=secrets,
            expires=expires or int(time.time() * 1000) + 3_600_000,
            principal=PRINCIPAL,
        )
        keyring = Keyring.parse(keys or self.keys)
        try:
            events = tuple(lookup(name).patterns(subscription.arguments))
        except Exception:
            events = ()
        webhook = Webhook(
            id=subscription.id,
            url=f"{PUBLIC_URL}{WEBHOOK_PATH}{subscription.id}",
            secret=keyring.signing_key(subscription.id),
            events=events,
            project=PROJECT,
            user="mcp",
            password=keyring.seal(subscription),
        )
        return webhook, callback, secrets

    def accepted(self, response) -> str:
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "accepted", body)
        self.assertRegex(body["eventId"], r"^evt_[0-9a-f]{32}$")
        return body["eventId"]

    def dropped(self, response, reason: str) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "dropped", "reason": reason})

    def rejected(self, response, reason: str) -> None:
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json(), {"status": "rejected", "reason": reason})

    def delivered(self, callback: str, event_id: str, *, count: int = 1):
        """The single (or ``count``-th) verified POST for ``event_id``."""
        received = receiver.wait(callback, count)[count - 1]
        self.assertTrue(received.verified and all(received.verified), received)
        self.assertEqual(received.headers["webhook-id"], event_id)
        event = received.json()
        self.assertEqual(
            set(event), {"eventId", "name", "timestamp", "data", "cursor"}, event
        )
        self.assertEqual(event["eventId"], event_id)
        self.assertIsNone(event["cursor"])
        return received, event

    def nothing_more(self, *callbacks: tuple[str, int]) -> None:
        time.sleep(SETTLE)
        for callback, count in callbacks:
            self.assertEqual(len(receiver.received(callback)), count, callback)


class DeliveryFlow(IngressFlow):
    """A matching Appwrite change reaches the subscriber exactly once,
    signed, and projected down to IDs and metadata."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(events_on(cls.keys), injected).__enter__()
        cls.appwrite = Appwrite(cls.server)
        with cls.server.client() as client:
            _, message = client.call("events/list")
        cls.schemas = {
            event["name"]: event["payloadSchema"]
            for event in message["result"]["events"]
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls.appwrite.close()
        cls.server.__exit__(None, None, None)

    def test_every_catalog_event_reaches_the_subscriber(self):
        cases = [
            (EXECUTIONS, "execution_sync_failed", EXECUTION_CREATE),
            (DEPLOYMENTS, "deployment_function_ready", DEPLOYMENT_UPDATE),
            (("sites.deployment.completed", {"site_id": "site_docs"}), "deployment_site_ready", "sites.site_docs.deployments.68e7c000aa11bb22cc55.update"),
            (ROWS, "row_created", ROW_EVENT),
            (FILES, "file_created", FILE_EVENT),
            (USERS, "user_created", USER_EVENT),
        ]  # fmt: skip
        self.assertEqual({name for (name, _), _, _ in cases}, set(self.schemas))
        callbacks = []
        for (name, arguments), body, appwrite_event in cases:
            with self.subTest(event=name):
                webhook, callback, _ = self.subscribe(name, arguments)
                started = time.time()
                event_id = self.accepted(
                    self.appwrite.deliver(webhook, appwrite_event, fixture(body))
                )
                received, event = self.delivered(callback, event_id)
                callbacks.append((callback, 1))

                self.assertEqual(event["name"], name)
                schema = self.schemas[name]
                Draft202012Validator(schema).validate(event["data"])
                self.assertEqual(set(event["data"]), set(schema["properties"]))
                for text in SENSITIVE:
                    self.assertNotIn(text.encode(), received.body)

                headers = received.headers
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["x-mcp-subscription-id"], webhook.id)
                self.assertLess(abs(int(headers["webhook-timestamp"]) - started), 5)
                # The egress dialled 127.0.0.1 but kept the callback's
                # hostname for SNI, certificate validation and Host.
                self.assertEqual(received.server_name, "localhost")
                self.assertEqual(headers["host"], f"localhost:{receiver.port}")

                if name == "tablesdb.row.created":
                    self.assertEqual(
                        event,
                        {
                            "eventId": event_id,
                            "name": name,
                            "timestamp": "2026-10-09T10:00:00.250Z",
                            "data": {
                                "database_id": "main",
                                "table_id": "support_tickets",
                                "row_id": "row_8f2a",
                                "created_at": "2026-10-09T10:00:00.250Z",
                            },
                            "cursor": None,
                        },
                    )
                if name == "users.user.created":
                    self.assertEqual(
                        event["data"],
                        {
                            "user_id": "user_jane",
                            "created_at": "2026-10-09T10:10:00.000Z",
                        },
                    )
                    for text in ("Jane Doe", "vip", "dark"):
                        self.assertNotIn(text.encode(), received.body)
        self.nothing_more(*callbacks)

    def test_appwrite_quirks_are_normalized_or_dropped(self):
        silent = []

        # Sync executions fire only .create, already failed.
        webhook, callback, _ = self.subscribe(*EXECUTIONS)
        event_id = self.accepted(
            self.appwrite.deliver(
                webhook, EXECUTION_CREATE, fixture("execution_sync_failed")
            )
        )
        _, event = self.delivered(callback, event_id)
        self.assertEqual(
            event["data"],
            {
                "function_id": "fn_billing",
                "execution_id": "68e7a1b2c3d4e5f6a7b8",
                "status": "failed",
                "trigger": "http",
                "response_status_code": 500,
                "created_at": "2026-10-09T09:38:26.634Z",
            },
        )
        self.assertEqual(event["timestamp"], "2026-10-09T09:38:27.101Z")

        # Async executions fire .create while waiting, then .update when done;
        # the update has DB-format timestamps and no $createdAt.
        webhook, callback, _ = self.subscribe(*EXECUTIONS)
        self.dropped(
            self.appwrite.deliver(
                webhook, EXECUTION_CREATE, fixture("execution_async_waiting")
            ),
            "status",
        )
        event_id = self.accepted(
            self.appwrite.deliver(
                webhook, EXECUTION_UPDATE, fixture("execution_async_failed")
            )
        )
        _, event = self.delivered(callback, event_id)
        self.assertEqual(event["data"]["trigger"], "schedule")
        self.assertIsNone(event["data"]["created_at"])
        self.assertEqual(event["timestamp"], "2026-10-09T09:38:31.950Z")
        silent.append((callback, 1))

        # An execution of a site is not a function execution.
        webhook, callback, _ = self.subscribe(*EXECUTIONS)
        body = {**fixture("execution_sync_failed"), "resourceType": "sites"}
        self.dropped(self.appwrite.deliver(webhook, EXECUTION_CREATE, body), "shape")
        silent.append((callback, 0))

        # A terminal deployment is delivered, with its DB-format time normalized.
        webhook, callback, _ = self.subscribe(*DEPLOYMENTS)
        event_id = self.accepted(
            self.appwrite.deliver(
                webhook, DEPLOYMENT_UPDATE, fixture("deployment_function_failed")
            )
        )
        _, event = self.delivered(callback, event_id)
        self.assertEqual(
            event["data"],
            {
                "function_id": "fn_billing",
                "deployment_id": "68e79f00aa11bb22cc33",
                "status": "failed",
                "updated_at": "2026-10-09T09:31:12.500Z",
            },
        )
        # Activation fires the same event with a function model; a duplicate
        # is still waiting.
        self.dropped(
            self.appwrite.deliver(
                webhook, DEPLOYMENT_UPDATE, fixture("function_activation")
            ),
            "shape",
        )
        self.dropped(
            self.appwrite.deliver(
                webhook,
                "functions.fn_billing.deployments.68e7b000aa11bb22cc44.update",
                fixture("deployment_function_duplicate"),
            ),
            "status",
        )
        silent.append((callback, 1))

        # The status argument narrows delivery.
        name, arguments = DEPLOYMENTS
        webhook, callback, _ = self.subscribe(name, {**arguments, "status": "failed"})
        self.dropped(
            self.appwrite.deliver(
                webhook, DEPLOYMENT_UPDATE, fixture("deployment_function_ready")
            ),
            "status",
        )
        silent.append((callback, 0))

        # Bad types never reach the payload; zoned times convert to UTC.
        webhook, callback, _ = self.subscribe(*FILES)
        body = {
            **fixture("file_created"),
            "mimeType": 7,
            "sizeOriginal": True,
            "$createdAt": "2026-10-09T14:10:00.5+02:00",
        }
        event_id = self.accepted(self.appwrite.deliver(webhook, FILE_EVENT, body))
        _, event = self.delivered(callback, event_id)
        self.assertEqual(
            event["data"],
            {
                "bucket_id": "uploads",
                "file_id": "file_invoice",
                "mime_type": None,
                "size": None,
                "created_at": "2026-10-09T12:10:00.500Z",
            },
        )
        # A body without an ID is not a file.
        without_id = {key: value for key, value in body.items() if key != "$id"}
        self.dropped(self.appwrite.deliver(webhook, FILE_EVENT, without_id), "shape")
        silent.append((callback, 1))

        # An unparseable time is null and the event is stamped on receipt.
        webhook, callback, _ = self.subscribe(*USERS)
        started = time.time()
        body = {**fixture("user_created"), "$createdAt": "yesterday"}
        event_id = self.accepted(self.appwrite.deliver(webhook, USER_EVENT, body))
        _, event = self.delivered(callback, event_id)
        self.assertIsNone(event["data"]["created_at"])
        self.assertRegex(
            event["timestamp"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"
        )
        stamped = datetime.fromisoformat(event["timestamp"]).timestamp()
        self.assertLess(abs(stamped - started), 5)
        silent.append((callback, 1))

        self.nothing_more(*silent)

    def test_event_ids_are_stable_across_appwrite_retries(self):
        webhook, callback, _ = self.subscribe(*ROWS)
        other, other_callback, _ = self.subscribe(*ROWS)
        first = self.accepted(
            self.appwrite.deliver(
                webhook, ROW_EVENT, fixture("row_created"), delivery="d1"
            )
        )
        again = self.accepted(
            self.appwrite.deliver(
                webhook, ROW_EVENT, fixture("row_created"), delivery="d1"
            )
        )
        later = self.accepted(
            self.appwrite.deliver(
                webhook, ROW_EVENT, fixture("row_created"), delivery="d2"
            )
        )
        elsewhere = self.accepted(
            self.appwrite.deliver(
                other, ROW_EVENT, fixture("row_created"), delivery="d1"
            )
        )
        self.assertEqual(first, again)
        self.assertEqual(len({first, later, elsewhere}), 3)
        received = receiver.wait(callback, 3)
        self.assertEqual(
            sorted(request.headers["webhook-id"] for request in received),
            sorted([first, first, later]),
        )
        self.delivered(other_callback, elsewhere)
        self.nothing_more((callback, 3), (other_callback, 1))

    def test_rotated_client_secrets_both_verify(self):
        current, previous, unrelated = whsec(), whsec(64), whsec()
        webhook, callback, _ = self.subscribe(
            *ROWS, secrets=(current, previous), also_check=(unrelated,)
        )
        event_id = self.accepted(
            self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
        )
        received = receiver.wait(callback, 1)[0]
        self.assertEqual(received.headers["webhook-id"], event_id)
        self.assertEqual(len(received.headers["webhook-signature"].split(" ")), 2)
        self.assertEqual(received.verified, (True, True, False))

    def test_ingress_acknowledges_what_it_must_not_deliver(self):
        # Everything from the subscriber's own webhook gets a 200, so Appwrite
        # never pauses it and never emails the owner.
        before = {
            reason: collector().count(INGRESS, outcome="dropped", reason=reason)
            for reason in (
                "expired",
                "event_mismatch",
                "resource_mismatch",
                "malformed",
                "unknown_event",
                "too_large",
                "retired_key",
            )
        }
        too_large_before = collector().count(DELIVERIES, outcome="too_large")
        silent = []

        webhook, callback, _ = self.subscribe(
            *ROWS, expires=int(time.time() * 1000) - 1
        )
        self.dropped(
            self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created")),
            "expired",
        )
        silent.append((callback, 0))

        webhook, callback, _ = self.subscribe(*ROWS)
        other_table = "tablesdb.main.tables.orders.rows.row_8f2a.create"
        self.dropped(
            self.appwrite.deliver(webhook, other_table, fixture("row_created")),
            "event_mismatch",
        )
        self.dropped(
            self.appwrite.deliver(
                webhook, ROW_EVENT, fixture("row_created"), events=[]
            ),
            "event_mismatch",
        )
        body = {**fixture("row_created"), "$tableId": "orders"}
        self.dropped(
            self.appwrite.deliver(webhook, ROW_EVENT, body), "resource_mismatch"
        )
        for raw in (b"not json", b"[1, 2]"):
            self.dropped(self.appwrite.deliver(webhook, ROW_EVENT, raw), "malformed")
        # Signed bodies past the 2 MiB read limit are hashed in full, then
        # dropped; the same body with a bad signature is still rejected.
        huge = {**fixture("row_created"), "body": "x" * (2 * 1024 * 1024)}
        self.dropped(self.appwrite.deliver(webhook, ROW_EVENT, huge), "too_large")
        self.rejected(
            self.appwrite.deliver(
                webhook,
                ROW_EVENT,
                huge,
                headers={"X-Appwrite-Webhook-Signature": "AAAA"},
            ),
            "signature",
        )
        # A projected event over the 256 KiB delivery limit is accepted, then
        # never sent.
        oversized = {**fixture("row_created"), "$id": "r" * (300 * 1024)}
        self.accepted(self.appwrite.deliver(webhook, ROW_EVENT, oversized))
        silent.append((callback, 0))

        webhook, callback, _ = self.subscribe(*EXECUTIONS)
        body = {**fixture("execution_sync_failed"), "resourceId": "fn_other"}
        self.dropped(
            self.appwrite.deliver(webhook, EXECUTION_CREATE, body), "resource_mismatch"
        )
        silent.append((callback, 0))

        # An event this server's catalog does not have (an older or newer
        # deployment sealed it).
        webhook, callback, _ = self.subscribe("tablesdb.row.deleted", {})
        self.dropped(
            self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created")),
            "unknown_event",
        )
        silent.append((callback, 0))

        # Sealed with a key the server no longer has: an orphaned webhook of
        # an expired subscription, acknowledged rather than rejected.
        webhook, callback, _ = self.subscribe(*ROWS, keys=RETIRED)
        self.dropped(
            self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created")),
            "retired_key",
        )
        silent.append((callback, 0))

        self.nothing_more(*silent)
        expected = {
            "expired": 1,
            "event_mismatch": 2,
            "resource_mismatch": 2,
            "malformed": 2,
            "unknown_event": 1,
            "too_large": 1,
            "retired_key": 1,
        }
        for reason, increase in expected.items():
            with self.subTest(counter=reason):
                self.assertEqual(
                    collector().count(INGRESS, outcome="dropped", reason=reason),
                    before[reason] + increase,
                )
        self.assertEqual(
            collector().wait(DELIVERIES, too_large_before + 1, outcome="too_large"),
            too_large_before + 1,
        )

    def test_forged_deliveries_are_rejected(self):
        # Only a request that fails authentication gets 401, and nothing from
        # it is delivered.
        rejected_before = collector().count(INGRESS, outcome="rejected")
        webhook, callback, _ = self.subscribe(*ROWS)
        other, _, _ = self.subscribe(*EXECUTIONS)
        body = fixture("row_created")

        self.rejected(
            self.appwrite.deliver(
                webhook,
                ROW_EVENT,
                body,
                headers={
                    "X-Appwrite-Webhook-Signature": "SgM0XNLzyHAwzlgQA2iZykDQI8M="
                },
            ),
            "signature",
        )
        self.rejected(
            self.appwrite.deliver(
                webhook,
                ROW_EVENT,
                body,
                headers={"X-Appwrite-Webhook-Signature": ""},
            ),
            "signature",
        )
        # Signed over the URL the request arrived on instead of the
        # registered one.
        inbound = f"{self.server.url}{WEBHOOK_PATH}{webhook.id}"
        self.rejected(
            self.appwrite.deliver(webhook, ROW_EVENT, body, signed_url=inbound),
            "signature",
        )

        envelope = webhook.password
        flipped = "A" if envelope[-10] != "A" else "B"
        tampered = Webhook(
            **{**webhook.__dict__, "password": envelope[:-10] + flipped + envelope[-9:]}
        )
        self.rejected(self.appwrite.deliver(tampered, ROW_EVENT, body), "envelope")
        moved = Webhook(**{**webhook.__dict__, "project": "7740f1a2b3c4d5e6f7a8"})
        self.rejected(self.appwrite.deliver(moved, ROW_EVENT, body), "envelope")
        copied = Webhook(**{**webhook.__dict__, "password": other.password})
        self.rejected(self.appwrite.deliver(copied, ROW_EVENT, body), "envelope")

        for authorization in (
            None,
            "Bearer token",
            "Basic !!!",
            "Basic " + base64.b64encode(b"mcp:").decode(),
            "Basic " + base64.b64encode(b"mcp:v9.k1.AAAA").decode(),
        ):
            with self.subTest(authorization=authorization):
                if authorization is None:
                    anonymous = Webhook(**{**webhook.__dict__, "user": ""})
                    response = self.appwrite.deliver(anonymous, ROW_EVENT, body)
                else:
                    response = self.appwrite.deliver(
                        webhook,
                        ROW_EVENT,
                        body,
                        headers={"Authorization": authorization},
                    )
                self.rejected(response, "credentials")

        stranger = Webhook(
            **{
                **webhook.__dict__,
                "url": f"{PUBLIC_URL}{WEBHOOK_PATH}not-a-subscription",
            }
        )
        self.rejected(self.appwrite.deliver(stranger, ROW_EVENT, body), "credentials")
        response = self.appwrite.deliver(webhook, ROW_EVENT, body, method="GET")
        self.assertEqual(response.status_code, 405)

        # The inbound Host is never part of the signed URL.
        event_id = self.accepted(
            self.appwrite.deliver(
                webhook,
                ROW_EVENT,
                body,
                headers={"Host": "attacker.example", "X-Forwarded-Host": "x.test"},
            )
        )
        self.delivered(callback, event_id)
        self.nothing_more((callback, 1))
        self.assertEqual(
            collector().count(INGRESS, outcome="rejected"), rejected_before + 12
        )

    def test_failed_deliveries_are_retried_then_given_up(self):
        counts = {
            key: collector().count(DELIVERIES, outcome=outcome, reason=reason)
            for key, (outcome, reason) in {
                "gone": ("rejected", "http_4xx"),
                "down": ("abandoned", "http_5xx"),
                "tls": ("abandoned", "tls_error"),
            }.items()
        }
        delivered_before = collector().count(DELIVERIES, outcome="delivered")
        elsewhere = receiver.endpoint(())

        flaky, flaky_callback, _ = self.subscribe(
            *ROWS,
            replies=(
                Reply(500),
                Reply(delay=TIMEOUT + 1),
                Reply(302, location=elsewhere),
                Reply(200),
            ),
        )
        gone, gone_callback, _ = self.subscribe(*ROWS, replies=(Reply(410),))
        too_large, too_large_callback, _ = self.subscribe(*ROWS, replies=(Reply(413),))
        down, down_callback, _ = self.subscribe(*ROWS, replies=(Reply(503),))
        handshakes = untrusted.handshake_failures
        broken, broken_callback, _ = self.subscribe(*ROWS, target=untrusted)

        ids = {
            name: self.accepted(
                self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
            )
            for name, webhook in (
                ("flaky", flaky),
                ("gone", gone),
                ("too_large", too_large),
                ("down", down),
                ("broken", broken),
            )
        }

        # 500, timeout, redirect (not followed), then 200: four attempts, each
        # re-signed with a fresh timestamp under the same webhook-id.
        attempts = receiver.wait(flaky_callback, 4, timeout=15)
        for attempt in attempts:
            self.assertEqual(attempt.headers["webhook-id"], ids["flaky"])
            self.assertEqual(attempt.verified, (True,))
        stamps = [int(a.headers["webhook-timestamp"]) for a in attempts]
        self.assertEqual(stamps, sorted(stamps))
        self.assertGreater(stamps[-1], stamps[0])
        signatures = {a.headers["webhook-signature"] for a in attempts}
        self.assertEqual(len(signatures), len(set(stamps)))
        # 410 and 413 are final; anything else is retried up to the policy.
        receiver.wait(down_callback, POLICY.attempts, timeout=15)
        self.nothing_more(
            (flaky_callback, 4),
            (elsewhere, 0),
            (gone_callback, 1),
            (too_large_callback, 1),
            (down_callback, POLICY.attempts),
        )
        # A callback whose certificate does not verify gets no request at all.
        self.assertEqual(untrusted.received(broken_callback), [])
        self.assertGreater(untrusted.handshake_failures, handshakes)

        self.assertEqual(
            collector().wait(DELIVERIES, delivered_before + 1, outcome="delivered"),
            delivered_before + 1,
        )
        for key, increase, (outcome, reason) in (
            ("gone", 2, ("rejected", "http_4xx")),
            ("down", 1, ("abandoned", "http_5xx")),
            ("tls", 1, ("abandoned", "tls_error")),
        ):
            with self.subTest(counter=key):
                expected = counts[key] + increase
                self.assertEqual(
                    collector().wait(
                        DELIVERIES, expected, outcome=outcome, reason=reason
                    ),
                    expected,
                )

    def test_a_burst_stays_within_the_per_host_limit(self):
        webhook, callback, _ = self.subscribe(*ROWS, replies=(Reply(delay=0.4),))
        ids = {
            self.accepted(
                self.appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
            )
            for _ in range(8)
        }
        received = receiver.wait(callback, 8, timeout=15)
        self.assertEqual({r.headers["webhook-id"] for r in received}, ids)
        self.assertEqual(receiver.peak(callback), 4)


class RotationFlow(IngressFlow):
    """Sealing keys rotate by restarting the server with a new key ring."""

    def test_sealing_keys_rotate_across_restarts(self):
        old, old_callback, _ = self.subscribe(*ROWS, keys=KEY_ONE)
        new, new_callback, _ = self.subscribe(*ROWS, keys=KEY_TWO)

        with Server(events_on(KEY_ONE), injected) as server:
            appwrite = Appwrite(server)
            self.delivered(
                old_callback,
                self.accepted(appwrite.deliver(old, ROW_EVENT, fixture("row_created"))),
            )
            appwrite.close()

        # The new key seals; the old one still opens until every subscription
        # sealed with it has expired.
        with Server(events_on(f"{KEY_TWO},{KEY_ONE}"), injected) as server:
            appwrite = Appwrite(server)
            self.delivered(
                old_callback,
                self.accepted(appwrite.deliver(old, ROW_EVENT, fixture("row_created"))),
                count=2,
            )
            self.delivered(
                new_callback,
                self.accepted(appwrite.deliver(new, ROW_EVENT, fixture("row_created"))),
            )
            appwrite.close()

        with Server(events_on(KEY_TWO), injected) as server:
            appwrite = Appwrite(server)
            self.dropped(
                appwrite.deliver(old, ROW_EVENT, fixture("row_created")),
                "retired_key",
            )
            self.delivered(
                new_callback,
                self.accepted(appwrite.deliver(new, ROW_EVENT, fixture("row_created"))),
                count=2,
            )
            appwrite.close()
        self.nothing_more((old_callback, 2), (new_callback, 2))


class ProductionEgressFlow(IngressFlow):
    """The ingress as production builds it, from the environment alone."""

    def test_default_egress_refuses_loopback_callbacks(self):
        before = collector().count(
            DELIVERIES, outcome="rejected", reason="connection_refused"
        )
        webhook, callback, _ = self.subscribe(*ROWS)
        with Server(events_on(self.keys)) as server:
            appwrite = Appwrite(server)
            self.accepted(appwrite.deliver(webhook, ROW_EVENT, fixture("row_created")))
            self.assertEqual(
                collector().wait(
                    DELIVERIES,
                    before + 1,
                    outcome="rejected",
                    reason="connection_refused",
                ),
                before + 1,
            )
            appwrite.close()
        self.nothing_more((callback, 0))


class CrashOnce(Dispatcher):
    """Raises from the first delivery, like a bug in the dispatcher would."""

    crashed = False

    async def deliver(self, callback, event):
        if not CrashOnce.crashed:
            CrashOnce.crashed = True
            raise RuntimeError("injected dispatcher failure")
        return await super().deliver(callback, event)


class FaultFlow(IngressFlow):
    def test_a_crashing_delivery_does_not_stop_later_ones(self):
        before = collector().count(DELIVERIES, outcome="error")
        webhook, callback, _ = self.subscribe(*ROWS)
        with Server(events_on(self.keys), lambda: injected(CrashOnce)) as server:
            appwrite = Appwrite(server)
            self.accepted(appwrite.deliver(webhook, ROW_EVENT, fixture("row_created")))
            self.assertEqual(
                collector().wait(DELIVERIES, before + 1, outcome="error"), before + 1
            )
            event_id = self.accepted(
                appwrite.deliver(webhook, ROW_EVENT, fixture("row_created"))
            )
            self.delivered(callback, event_id)
            appwrite.close()


class StartupFlow(unittest.TestCase):
    """With events on, the server refuses to start without usable keys."""

    def test_entry_point_exits_without_sealing_keys(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in ("MCP_EVENTS", "MCP_EVENTS_SEALING_KEYS")
        }
        environment["MCP_PUBLIC_URL"] = PUBLIC_URL
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mcp_server_appwrite",
                "--transport",
                "http",
                "--events",
                "1",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MCP_EVENTS_SEALING_KEYS is not set", result.stderr)

    def test_weak_or_malformed_keys_stop_startup(self):
        cases = {
            "short": "k1:" + base64.b64encode(bytes(range(16))).decode(),
            "zeros": "k1:" + base64.b64encode(bytes(32)).decode(),
            "repeated": "k1:" + base64.b64encode(b"ab" * 16).decode(),
            "not base64": "k1:not base64!",
            "no id": base64.b64encode(bytes(range(32))).decode(),
            "duplicate id": f"{KEY_ONE},k1:{KEY_TWO.split(':', 1)[1]}",
        }
        for label, keys in cases.items():
            with self.subTest(keys=label):
                with self.assertRaises(KeyringError):
                    with Server(events_on(keys)):
                        pass


if __name__ == "__main__":
    unittest.main()
