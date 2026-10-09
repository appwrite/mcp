"""The Appwrite webhook ingress, driven through the real hosted Starlette app.

Appwrite bodies come from recorded-shape fixtures in ``fixtures/appwrite``
(built from Appwrite's response models, with the sensitive fields a real
project would carry). Outbound deliveries go through the real
:class:`Dispatcher` and :class:`Egress` onto an ``httpx.MockTransport``, so each
test sees exactly the POSTs a subscriber would receive."""

import base64
import hashlib
import hmac
import itertools
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from jsonschema import Draft202012Validator
from starlette.testclient import TestClient

from mcp_server_appwrite import flags, telemetry
from mcp_server_appwrite.events import catalog
from mcp_server_appwrite.events.delivery import Dispatcher, RetryPolicy
from mcp_server_appwrite.events.egress import Egress
from mcp_server_appwrite.events.envelope import (
    KEYS_ENV,
    Keyring,
    KeyringError,
    Subscription,
    appwrite_signature,
)
from mcp_server_appwrite.events.ingress import (
    BODY_LIMIT_BYTES,
    Ingress,
    event_id,
    fired,
    password,
)
from mcp_server_appwrite.events.projection import (
    PROJECTORS,
    Drop,
    Dropped,
    project,
    timestamp,
)
from mcp_server_appwrite.http_app import build_app

FIXTURES = Path(__file__).parent / "fixtures" / "appwrite"
BASE = "https://mcp.example.test"
PROJECT = "6630f1a2b3c4d5e6f7a8"
CALLBACK = "https://callback.example.test/mcp/events"
SECRET = "whsec_" + base64.b64encode(bytes(range(32))).decode()
PRINCIPAL = "OPxPhaa1hwu4ngsYV4zRuw"
KEY = base64.b64encode(bytes(range(64, 96))).decode()
OLD_KEY = base64.b64encode(bytes(range(128, 160))).decode()
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


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def generated(event: str) -> str:
    """``X-Appwrite-Webhook-Events`` for a concrete Appwrite event: every form
    with IDs replaced by ``*``, with and without the action, the way
    ``Event::generateEvents`` lists them."""
    parts = event.split(".")
    action = parts[-1]
    resources = parts[:-1]
    ids = range(1, len(resources), 2)
    forms: list[str] = []
    for mask in itertools.product((False, True), repeat=len(ids)):
        names = list(resources)
        for position, wildcard in zip(ids, mask, strict=True):
            if wildcard:
                names[position] = "*"
        forms.append(".".join([*names, action]))
        forms.append(".".join(names))
    return ",".join(dict.fromkeys(forms))


def subscription(
    name: str, arguments: dict[str, str], *, expires: int | None = None
) -> Subscription:
    return Subscription.create(
        project=PROJECT,
        name=name,
        arguments={"project_id": PROJECT, **arguments},
        callback=CALLBACK,
        secrets=(SECRET,),
        expires=expires or int(time.time() * 1000) + 3_600_000,
        principal=PRINCIPAL,
    )


ROWS = ("tablesdb.row.created", {"database_id": "main", "table_id": "support_tickets"})
ROW_EVENT = "tablesdb.main.tables.support_tickets.rows.row_8f2a.create"
EXECUTIONS = ("functions.execution.failed", {"function_id": "fn_billing"})
DEPLOYMENTS = ("functions.deployment.completed", {"function_id": "fn_billing"})


class Callbacks:
    """Fake subscriber endpoint behind the egress."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []
        self.received = threading.Event()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.received.set()
        return httpx.Response(self.status, json={"ok": True})

    def wait(self) -> httpx.Request:
        if not self.received.wait(5):
            raise AssertionError("no delivery reached the callback")
        return self.requests[-1]


def verify_standard_webhook(request: httpx.Request) -> bool:
    """Receiver-side Standard Webhooks check, written from the spec."""
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    content = (
        f"{request.headers['webhook-id']}.{request.headers['webhook-timestamp']}."
    ).encode() + request.content
    expected = base64.b64encode(hmac.new(key, content, hashlib.sha256).digest())
    return any(
        hmac.compare_digest(entry.removeprefix("v1,").encode(), expected)
        for entry in request.headers["webhook-signature"].split(" ")
    )


class IngressTestCase(unittest.TestCase):
    keys = f"k1:{KEY}"

    def setUp(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in (flags.EVENTS.env, KEYS_ENV, "MCP_PUBLIC_URL")
        }
        environment[flags.EVENTS.env] = "1"
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keyring = Keyring.parse(self.keys)
        self.callbacks = Callbacks()
        egress = Egress(transport=httpx.MockTransport(self.callbacks))
        self.ingress = Ingress(
            self.keyring,
            egress,
            Dispatcher(egress, RetryPolicy(delays=())),
            base=BASE,
        )
        self.client = TestClient(build_app(self.ingress))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(
        self,
        target: Subscription,
        body: dict[str, Any] | bytes,
        events: str,
        *,
        envelope: str | None = None,
        signed_url: str | None = None,
        signature: str | None = None,
        project: str = PROJECT,
        delivery: str = "d41d8cd98f00b204e9800998ecf8427e",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        envelope = envelope or self.keyring.seal(target)
        url = signed_url or f"{BASE}/appwrite/webhooks/{target.id}"
        credentials = base64.b64encode(f"mcp:{envelope}".encode()).decode()
        return self.client.post(
            f"/appwrite/webhooks/{target.id}",
            content=content,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {credentials}",
                "X-Appwrite-Webhook-Id": target.id,
                "X-Appwrite-Webhook-Delivery-Id": delivery,
                "X-Appwrite-Webhook-Events": events,
                "X-Appwrite-Webhook-Project-Id": project,
                "X-Appwrite-Webhook-Signature": signature
                or appwrite_signature(
                    url, content, self.keyring.signing_key(target.id)
                ),
                **(headers or {}),
            },
        )

    def delivered(self, response: httpx.Response) -> dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "accepted")
        request = self.callbacks.wait()
        self.assertEqual(len(self.callbacks.requests), 1)
        return json.loads(request.content)

    def assertDropped(self, response: httpx.Response, reason: Drop) -> None:
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"status": "dropped", "reason": reason})
        self.assertEqual(self.callbacks.requests, [])

    def assertRejected(self, response: httpx.Response, reason: str) -> None:
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json(), {"status": "rejected", "reason": reason})
        self.assertEqual(self.callbacks.requests, [])


class DeliveryTests(IngressTestCase):
    def test_signed_delivery_is_forwarded_once_with_standard_webhook_headers(self):
        target = subscription(*ROWS)
        response = self.post(target, fixture("row_created"), generated(ROW_EVENT))
        event = self.delivered(response)
        request = self.callbacks.requests[0]

        self.assertEqual(str(request.url), CALLBACK)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["webhook-id"], event["eventId"])
        self.assertEqual(request.headers["x-mcp-subscription-id"], target.id)
        self.assertLess(
            abs(int(request.headers["webhook-timestamp"]) - time.time()), 60
        )
        self.assertTrue(verify_standard_webhook(request))
        self.assertEqual(response.json()["eventId"], event["eventId"])
        self.assertEqual(
            event,
            {
                "eventId": event_id("d41d8cd98f00b204e9800998ecf8427e", target.id),
                "name": "tablesdb.row.created",
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
        for text in SENSITIVE:
            self.assertNotIn(text.encode(), request.content)

    def test_event_id_is_stable_across_appwrite_retries(self):
        target = subscription(*ROWS)
        first = self.post(target, fixture("row_created"), generated(ROW_EVENT))
        again = self.post(target, fixture("row_created"), generated(ROW_EVENT))
        other = self.post(
            target, fixture("row_created"), generated(ROW_EVENT), delivery="other"
        )
        self.assertEqual(first.json()["eventId"], again.json()["eventId"])
        self.assertNotEqual(first.json()["eventId"], other.json()["eventId"])
        self.assertNotEqual(
            event_id("d1", target.id), event_id("d1", subscription(*EXECUTIONS).id)
        )

    def test_payload_matches_the_published_schema_for_every_fixture(self):
        cases = [
            (EXECUTIONS, "execution_sync_failed", "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"),
            (DEPLOYMENTS, "deployment_function_ready", "functions.fn_billing.deployments.68e79f00aa11bb22cc33.update"),
            (("sites.deployment.completed", {"site_id": "site_docs"}), "deployment_site_ready", "sites.site_docs.deployments.68e7c000aa11bb22cc55.update"),
            (ROWS, "row_created", ROW_EVENT),
            (("storage.file.created", {"bucket_id": "uploads"}), "file_created", "buckets.uploads.files.file_invoice.create"),
            (("users.user.created", {}), "user_created", "users.user_jane.create"),
        ]  # fmt: skip
        self.assertEqual({case[0][0] for case in cases}, set(catalog.CATALOG))
        for (name, arguments), body, appwrite_event in cases:
            with self.subTest(event=name):
                self.callbacks.requests.clear()
                self.callbacks.received.clear()
                target = subscription(name, arguments)
                event = self.delivered(
                    self.post(target, fixture(body), generated(appwrite_event))
                )
                schema = catalog.CATALOG[name].payload_schema
                Draft202012Validator(schema).validate(event["data"])
                self.assertEqual(set(event["data"]), set(schema["properties"]))
                for text in SENSITIVE:
                    self.assertNotIn(text, json.dumps(event))

    def test_users_payload_has_no_personal_data(self):
        target = subscription("users.user.created", {})
        event = self.delivered(
            self.post(
                target, fixture("user_created"), generated("users.user_jane.create")
            )
        )
        self.assertEqual(
            event["data"],
            {"user_id": "user_jane", "created_at": "2026-10-09T10:10:00.000Z"},
        )
        raw = self.callbacks.requests[0].content
        for text in ("jane@example.com", "+14155550100", "Jane Doe", "vip", "dark"):
            self.assertNotIn(text.encode(), raw)

    def test_canonical_url_is_signed_and_inbound_host_is_ignored(self):
        target = subscription(*ROWS)
        inbound = f"http://testserver/appwrite/webhooks/{target.id}"
        self.assertRejected(
            self.post(
                target,
                fixture("row_created"),
                generated(ROW_EVENT),
                signed_url=inbound,
            ),
            "signature",
        )
        self.delivered(
            self.post(
                target,
                fixture("row_created"),
                generated(ROW_EVENT),
                headers={"Host": "attacker.example", "X-Forwarded-Host": "x.test"},
            )
        )

    def test_a_failing_delivery_does_not_stop_later_ones(self):
        target = subscription(*ROWS)
        failed = threading.Event()

        async def explode(callback, event):
            failed.set()
            raise RuntimeError("boom")

        with (
            mock.patch.object(Dispatcher, "deliver", side_effect=explode),
            mock.patch.object(telemetry, "record_event_delivery") as recorded,
        ):
            response = self.post(target, fixture("row_created"), generated(ROW_EVENT))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(failed.wait(5))
            deadline = time.monotonic() + 5
            while not recorded.called and time.monotonic() < deadline:
                time.sleep(0.01)
        recorded.assert_called_once_with("tablesdb.row.created", "error", None)
        self.delivered(self.post(target, fixture("row_created"), generated(ROW_EVENT)))


class ExecutionTests(IngressTestCase):
    def test_sync_execution_fires_create_already_failed(self):
        event = self.delivered(
            self.post(
                subscription(*EXECUTIONS),
                fixture("execution_sync_failed"),
                generated(
                    "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"
                ),
            )
        )
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

    def test_async_create_while_waiting_is_dropped(self):
        self.assertDropped(
            self.post(
                subscription(*EXECUTIONS),
                fixture("execution_async_waiting"),
                generated(
                    "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"
                ),
            ),
            Drop.STATUS,
        )

    def test_async_update_with_failed_is_delivered_and_db_timestamps_normalized(self):
        event = self.delivered(
            self.post(
                subscription(*EXECUTIONS),
                fixture("execution_async_failed"),
                generated(
                    "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.update"
                ),
            )
        )
        self.assertEqual(event["data"]["trigger"], "schedule")
        self.assertIsNone(event["data"]["created_at"])
        self.assertEqual(event["timestamp"], "2026-10-09T09:38:31.950Z")

    def test_execution_of_another_resource_type_is_dropped(self):
        body = {**fixture("execution_sync_failed"), "resourceType": "sites"}
        self.assertDropped(
            self.post(
                subscription(*EXECUTIONS),
                body,
                generated(
                    "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"
                ),
            ),
            Drop.SHAPE,
        )


class DeploymentTests(IngressTestCase):
    UPDATE = "functions.fn_billing.deployments.68e79f00aa11bb22cc33.update"

    def test_terminal_deployment_is_delivered(self):
        event = self.delivered(
            self.post(
                subscription(*DEPLOYMENTS),
                fixture("deployment_function_failed"),
                generated(self.UPDATE),
            )
        )
        self.assertEqual(
            event["data"],
            {
                "function_id": "fn_billing",
                "deployment_id": "68e79f00aa11bb22cc33",
                "status": "failed",
                "updated_at": "2026-10-09T09:31:12.500Z",
            },
        )

    def test_activation_carries_a_function_model_and_is_dropped(self):
        self.assertDropped(
            self.post(
                subscription(*DEPLOYMENTS),
                fixture("function_activation"),
                generated(self.UPDATE),
            ),
            Drop.SHAPE,
        )

    def test_duplicate_is_still_waiting_and_is_dropped(self):
        self.assertDropped(
            self.post(
                subscription(*DEPLOYMENTS),
                fixture("deployment_function_duplicate"),
                generated(
                    "functions.fn_billing.deployments.68e7b000aa11bb22cc44.update"
                ),
            ),
            Drop.STATUS,
        )

    def test_status_argument_narrows_delivery(self):
        name, arguments = DEPLOYMENTS
        target = subscription(name, {**arguments, "status": "failed"})
        self.assertDropped(
            self.post(
                target, fixture("deployment_function_ready"), generated(self.UPDATE)
            ),
            Drop.STATUS,
        )


class DropTests(IngressTestCase):
    def test_expired_subscription_is_acknowledged_and_dropped(self):
        target = subscription(*ROWS, expires=int(time.time() * 1000) - 1)
        self.assertDropped(
            self.post(target, fixture("row_created"), generated(ROW_EVENT)),
            Drop.EXPIRED,
        )

    def test_event_outside_the_subscription_patterns_is_dropped(self):
        self.assertDropped(
            self.post(
                subscription(*ROWS),
                fixture("row_created"),
                generated("tablesdb.main.tables.orders.rows.row_8f2a.create"),
            ),
            Drop.EVENT_MISMATCH,
        )
        self.assertDropped(
            self.post(subscription(*ROWS), fixture("row_created"), ""),
            Drop.EVENT_MISMATCH,
        )

    def test_body_naming_another_resource_is_dropped(self):
        body = {**fixture("row_created"), "$tableId": "orders"}
        self.assertDropped(
            self.post(subscription(*ROWS), body, generated(ROW_EVENT)),
            Drop.RESOURCE_MISMATCH,
        )
        execution = {**fixture("execution_sync_failed"), "resourceId": "fn_other"}
        self.assertDropped(
            self.post(
                subscription(*EXECUTIONS),
                execution,
                generated(
                    "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create"
                ),
            ),
            Drop.RESOURCE_MISMATCH,
        )

    def test_body_that_is_not_a_json_object_is_dropped(self):
        for body in (b"not json", b"[1, 2]"):
            with self.subTest(body=body):
                self.assertDropped(
                    self.post(subscription(*ROWS), body, generated(ROW_EVENT)),
                    Drop.MALFORMED,
                )

    def test_unknown_event_name_is_dropped(self):
        target = Subscription.create(
            project=PROJECT,
            name="tablesdb.row.deleted",
            arguments={"project_id": PROJECT},
            callback=CALLBACK,
            secrets=(SECRET,),
            expires=int(time.time() * 1000) + 60_000,
            principal=PRINCIPAL,
        )
        self.assertDropped(
            self.post(target, fixture("row_created"), generated(ROW_EVENT)),
            Drop.UNKNOWN_EVENT,
        )

    def test_oversized_signed_body_is_hashed_in_full_then_dropped(self):
        body = dict(fixture("row_created"), body="x" * BODY_LIMIT_BYTES)
        self.assertDropped(
            self.post(subscription(*ROWS), body, generated(ROW_EVENT)),
            Drop.TOO_LARGE,
        )
        self.assertRejected(
            self.post(
                subscription(*ROWS), body, generated(ROW_EVENT), signature="AAAA"
            ),
            "signature",
        )


class RotationTests(IngressTestCase):
    keys = f"k2:{KEY},k1:{OLD_KEY}"

    def test_envelope_sealed_before_rotation_still_delivers(self):
        target = subscription(*ROWS)
        old = Keyring.parse(f"k1:{OLD_KEY}")
        envelope = old.seal(target)
        content = json.dumps(fixture("row_created")).encode()
        signature = appwrite_signature(
            f"{BASE}/appwrite/webhooks/{target.id}",
            content,
            old.signing_key(target.id),
        )
        self.delivered(
            self.post(
                target,
                content,
                generated(ROW_EVENT),
                envelope=envelope,
                signature=signature,
            )
        )

    def test_envelope_from_a_retired_key_is_acknowledged_and_dropped(self):
        target = subscription(*ROWS)
        retired = Keyring.parse(
            "k0:" + base64.b64encode(bytes(range(200, 232))).decode()
        )
        self.assertDropped(
            self.post(
                target,
                fixture("row_created"),
                generated(ROW_EVENT),
                envelope=retired.seal(target),
            ),
            Drop.RETIRED_KEY,
        )


class RejectionTests(IngressTestCase):
    def test_bad_signature_is_rejected(self):
        self.assertRejected(
            self.post(
                subscription(*ROWS),
                fixture("row_created"),
                generated(ROW_EVENT),
                signature="SgM0XNLzyHAwzlgQA2iZykDQI8M=",
            ),
            "signature",
        )

    def test_missing_signature_is_rejected(self):
        target = subscription(*ROWS)
        response = self.post(
            target,
            fixture("row_created"),
            generated(ROW_EVENT),
            headers={"X-Appwrite-Webhook-Signature": ""},
        )
        self.assertRejected(response, "signature")

    def test_tampered_envelope_is_rejected(self):
        target = subscription(*ROWS)
        envelope = self.keyring.seal(target)
        flipped = "A" if envelope[-10] != "A" else "B"
        tampered = envelope[:-10] + flipped + envelope[-9:]
        self.assertRejected(
            self.post(
                target, fixture("row_created"), generated(ROW_EVENT), envelope=tampered
            ),
            "envelope",
        )

    def test_envelope_for_another_project_is_rejected(self):
        self.assertRejected(
            self.post(
                subscription(*ROWS),
                fixture("row_created"),
                generated(ROW_EVENT),
                project="another-project",
            ),
            "envelope",
        )

    def test_envelope_copied_to_another_webhook_is_rejected(self):
        target = subscription(*ROWS)
        other = subscription(*EXECUTIONS)
        content = json.dumps(fixture("row_created")).encode()
        signature = appwrite_signature(
            f"{BASE}/appwrite/webhooks/{target.id}",
            content,
            self.keyring.signing_key(target.id),
        )
        self.assertRejected(
            self.post(
                target,
                content,
                generated(ROW_EVENT),
                envelope=self.keyring.seal(other),
                signature=signature,
            ),
            "envelope",
        )

    def test_missing_or_malformed_credentials_are_rejected(self):
        target = subscription(*ROWS)
        for authorization in (
            "",
            "Bearer token",
            "Basic !!!",
            "Basic " + base64.b64encode(b"mcp:").decode(),
            "Basic " + base64.b64encode(b"mcp:v9.k1.AAAA").decode(),
        ):
            with self.subTest(authorization=authorization):
                self.assertRejected(
                    self.post(
                        target,
                        fixture("row_created"),
                        generated(ROW_EVENT),
                        headers={"Authorization": authorization},
                    ),
                    "credentials",
                )

    def test_unknown_subscription_id_format_is_rejected(self):
        response = self.client.post("/appwrite/webhooks/not-a-subscription")
        self.assertEqual(response.status_code, 401)

    def test_route_only_accepts_post(self):
        target = subscription(*ROWS)
        response = self.client.get(f"/appwrite/webhooks/{target.id}")
        self.assertEqual(response.status_code, 405)


class TelemetryTests(IngressTestCase):
    def test_ingress_and_delivery_outcomes_are_recorded(self):
        target = subscription(*ROWS)
        with (
            mock.patch.object(telemetry, "record_ingress") as ingress,
            mock.patch.object(telemetry, "record_event_delivery") as delivery,
        ):
            self.post(target, fixture("row_created"), "")
            self.post(
                target, fixture("row_created"), generated(ROW_EVENT), signature="A"
            )
            self.delivered(
                self.post(target, fixture("row_created"), generated(ROW_EVENT))
            )
            deadline = time.monotonic() + 5
            while not delivery.called and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(
            [call.args for call in ingress.call_args_list],
            [
                ("dropped", Drop.EVENT_MISMATCH, "tablesdb.row.created"),
                ("rejected", "signature", None),
                ("accepted", None, "tablesdb.row.created"),
            ],
        )
        delivery.assert_called_once_with("tablesdb.row.created", "delivered", None)


class MountTests(unittest.TestCase):
    def _environment(self, **values: str) -> mock._patch_dict:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in (flags.EVENTS.env, KEYS_ENV)
        }
        environment.update(values)
        return mock.patch.dict(os.environ, environment, clear=True)

    def test_route_is_absent_when_the_flag_is_off(self):
        for value in (None, "0", "false"):
            values = {} if value is None else {flags.EVENTS.env: value}
            with self.subTest(flag=value), self._environment(**values):
                with TestClient(build_app()) as client:
                    response = client.post(
                        "/appwrite/webhooks/sub_0123456789abcdef0123456789abcdef"
                    )
                self.assertEqual(response.status_code, 404)

    def test_flag_on_without_sealing_keys_fails_at_startup(self):
        with self._environment(**{flags.EVENTS.env: "1"}):
            with self.assertRaises(KeyringError):
                build_app()

    def test_flag_on_builds_the_ingress_from_the_environment(self):
        keys = {KEYS_ENV: f"k1:{KEY}", flags.EVENTS.env: "1"}
        with self._environment(MCP_PUBLIC_URL="https://mcp.example.test/", **keys):
            ingress = Ingress.from_env()
            with TestClient(build_app()) as client:
                response = client.post(
                    "/appwrite/webhooks/sub_0123456789abcdef0123456789abcdef"
                )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            ingress.url("sub_1"), "https://mcp.example.test/appwrite/webhooks/sub_1"
        )


class ProjectionTests(unittest.TestCase):
    def test_every_catalog_event_has_a_projector(self):
        self.assertEqual(set(PROJECTORS), set(catalog.CATALOG))

    def test_timestamps_are_normalized_to_utc(self):
        cases = {
            "2026-10-09 09:38:26.634": "2026-10-09T09:38:26.634Z",
            "2026-10-09T09:38:26.634+00:00": "2026-10-09T09:38:26.634Z",
            "2026-10-09T11:38:26.634+02:00": "2026-10-09T09:38:26.634Z",
            "2026-10-09T09:38:26Z": "2026-10-09T09:38:26.000Z",
            "2026-10-09 09:38:26": "2026-10-09T09:38:26.000Z",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(timestamp(raw), expected)
        for raw in (None, "", "  ", "yesterday", 1760000000, "2026-13-40"):
            with self.subTest(raw=raw):
                self.assertIsNone(timestamp(raw))

    def test_project_requires_a_matching_appwrite_event(self):
        event = catalog.CATALOG["tablesdb.row.created"]
        arguments = {"project_id": PROJECT, **ROWS[1]}
        projection = project(
            event,
            arguments,
            fixture("row_created"),
            fired(generated(ROW_EVENT)),
        )
        self.assertEqual(projection.data["row_id"], "row_8f2a")
        with self.assertRaises(Dropped) as raised:
            project(event, arguments, fixture("row_created"), ["users.*.create"])
        self.assertIs(raised.exception.reason, Drop.EVENT_MISMATCH)

    def test_missing_ids_and_bad_types_never_reach_the_payload(self):
        event = catalog.CATALOG["storage.file.created"]
        arguments = {"project_id": PROJECT, "bucket_id": "uploads"}
        body = {
            **fixture("file_created"),
            "mimeType": 7,
            "sizeOriginal": True,
            "$createdAt": None,
        }
        events = fired(generated("buckets.uploads.files.file_invoice.create"))
        projection = project(event, arguments, body, events)
        self.assertEqual(
            projection.data,
            {
                "bucket_id": "uploads",
                "file_id": "file_invoice",
                "mime_type": None,
                "size": None,
                "created_at": None,
            },
        )
        self.assertIsNone(projection.occurred)
        without_id = {key: value for key, value in body.items() if key != "$id"}
        with self.assertRaises(Dropped) as raised:
            project(event, arguments, without_id, events)
        self.assertIs(raised.exception.reason, Drop.SHAPE)


class HelperTests(unittest.TestCase):
    def test_password_reads_basic_auth(self):
        encoded = base64.b64encode(b"mcp:v1.k1.abc").decode()
        self.assertEqual(password(f"Basic {encoded}"), "v1.k1.abc")
        self.assertEqual(password(f"basic {encoded}"), "v1.k1.abc")
        self.assertIsNone(password(None))
        self.assertIsNone(password(f"Bearer {encoded}"))
        self.assertIsNone(password("Basic " + base64.b64encode(b"nocolon").decode()))
        self.assertIsNone(password("Basic " + base64.b64encode(b"\xff:x").decode()))

    def test_fired_splits_the_events_header(self):
        self.assertEqual(fired("a.b, c.d ,,"), ["a.b", "c.d"])
        self.assertEqual(fired(None), [])

    def test_event_id_shape(self):
        value = event_id("delivery", "sub_x")
        self.assertRegex(value, r"^evt_[0-9a-f]{32}$")
        self.assertEqual(value, event_id("delivery", "sub_x"))


if __name__ == "__main__":
    unittest.main()
