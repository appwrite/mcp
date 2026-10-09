"""``events/subscribe`` and ``events/unsubscribe`` through the real hosted server.

Every party around the server runs on localhost: :class:`support.Cloud` is the
Appwrite REST API the server calls with the caller's token, the subscriber's
HTTPS endpoint (:class:`support.Receiver`) answers the verification handshake
and checks every request with the official ``standardwebhooks`` library, and
Appwrite's webhooks worker (:meth:`support.Appwrite.fire`) delivers from the
webhooks the server stored in :class:`support.Cloud`, with their real
``authPassword``, secret and URL. That closes the loop: subscribe, handshake,
webhook stored, Appwrite event, ingress, signed delivery, unsubscribe, webhook
gone, no delivery.

Like the ingress flows, the server uses the production ingress with an egress
that may reach the local receiver and trusts its certificate, and a 1 s
request timeout.
"""

import base64
import json
import re
import secrets
import socket
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from support import (
    CLIENT_ID,
    ISSUER,
    OTHER_SUBJECT,
    OTHER_TOKEN,
    PUBLIC_URL,
    SCOPES,
    SUBJECT,
    SUBJECTLESS_TOKEN,
    SUBJECTS,
    TOKEN,
    Appwrite,
    Client,
    Cloud,
    Project,
    Receiver,
    Reply,
    Server,
    collector,
    whsec,
)

from mcp_server_appwrite.auth import public_base_url
from mcp_server_appwrite.events.catalog import lookup
from mcp_server_appwrite.events.delivery import Dispatcher, RetryPolicy
from mcp_server_appwrite.events.egress import Egress
from mcp_server_appwrite.events.envelope import (
    Keyring,
    Principal,
    subscription_id,
)
from mcp_server_appwrite.events.ingress import Ingress

FIXTURES = Path(__file__).parent / "fixtures" / "appwrite"
KEYS = "k1:" + base64.b64encode(bytes(range(64, 96))).decode()
TIMEOUT = 1.0
SETTLE = 0.5
"""Seconds to wait before asserting that nothing (more) was received."""

MAIN = "6630f1a2b3c4d5e6f7a8"
FREE = "7740f1a2b3c4d5e6f7a8"
"""A Free-plan project: at most two webhooks."""
TIDY = "8850f1a2b3c4d5e6f7a8"
"""A project with other people's webhooks in it."""
MISSING = "9960f1a2b3c4d5e6f7a8"
"""A project that does not exist."""

READ_ONLY = "e2e-read-only"
"""Granted every scope but ``webhooks.read`` / ``webhooks.write``."""
NO_FUNCTIONS = "e2e-no-functions"
"""Granted every scope but ``functions.read``."""
STRANGER = "e2e-stranger"
"""A user who is not a member of any project."""

PRINCIPAL = Principal(ISSUER, SUBJECT, CLIENT_ID).digest
OTHER_PRINCIPAL = Principal(ISSUER, OTHER_SUBJECT, CLIENT_ID).digest

ROWS = ("tablesdb.row.created", {"database_id": "main", "table_id": "support_tickets"})
ROW_EVENT = "tablesdb.main.tables.support_tickets.rows.row_8f2a.create"

CASES = [
    ("functions.execution.failed", {"function_id": "fn_billing"}, "execution_sync_failed", "functions.fn_billing.executions.68e7a1b2c3d4e5f6a7b8.create", "/v1/functions/fn_billing"),
    ("functions.deployment.completed", {"function_id": "fn_billing", "status": "ready"}, "deployment_function_ready", "functions.fn_billing.deployments.68e79f00aa11bb22cc33.update", "/v1/functions/fn_billing"),
    ("sites.deployment.completed", {"site_id": "site_docs"}, "deployment_site_ready", "sites.site_docs.deployments.68e7c000aa11bb22cc55.update", "/v1/sites/site_docs"),
    (*ROWS, "row_created", ROW_EVENT, "/v1/tablesdb/main/tables/support_tickets"),
    ("storage.file.created", {"bucket_id": "uploads"}, "file_created", "buckets.uploads.files.file_invoice.create", "/v1/storage/buckets/uploads"),
    ("users.user.created", {}, "user_created", "users.user_jane.create", "/v1/users"),
]  # fmt: skip
"""Per catalog event: arguments, the recorded Appwrite body and event that
should reach the subscriber, and the read that authorizes the subscription."""

LABEL = re.compile(
    r"MCP events \| (?P<event>[a-z.]+) \| until "
    r"(?P<until>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) \| owner (?P<owner>[A-Za-z0-9_-]{8})"
)

SUBSCRIPTIONS = "mcp.events.subscriptions"

INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
NOT_FOUND = -32011
FORBIDDEN = -32012
RESOURCE_EXHAUSTED = -32013
UNSUPPORTED = -32014
CALLBACK_ENDPOINT = -32015

receiver: Receiver
untrusted: Receiver
cloud: Cloud


def setUpModule() -> None:
    global receiver, untrusted, cloud
    receiver = Receiver()
    untrusted = Receiver()
    cloud = Cloud()
    SUBJECTS.update(
        {
            READ_ONLY: "88d3e4f5a6b7c8d9e0f1",
            NO_FUNCTIONS: "99e4f5a6b7c8d9e0f1a2",
            STRANGER: "aaf5a6b7c8d9e0f1a2b3",
        }
    )
    cloud.add(
        Project(
            MAIN,
            functions={"fn_billing"},
            sites={"site_docs"},
            tables={("main", "support_tickets"), ("main", "orders")},
            buckets={"uploads"},
        )
    )
    cloud.add(
        Project(
            FREE,
            tables={("main", "tickets"), ("main", "orders")},
            webhook_limit=2,
        )
    )
    cloud.add(Project(TIDY, tables={("main", "support_tickets")}))
    cloud.grant(TOKEN, {MAIN, FREE, TIDY})
    cloud.grant(OTHER_TOKEN, {MAIN, TIDY})
    cloud.grant(READ_ONLY, {MAIN}, SCOPES - {"webhooks.read", "webhooks.write"})
    cloud.grant(NO_FUNCTIONS, {MAIN}, SCOPES - {"functions.read"})
    cloud.grant(STRANGER, set())


def tearDownModule() -> None:
    receiver.close()
    untrusted.close()
    cloud.close()
    for token in (READ_ONLY, NO_FUNCTIONS, STRANGER):
        SUBJECTS.pop(token, None)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def injected() -> dict[str, Any]:
    """``build_app`` arguments: the production ingress, keyring and public URL
    from the environment, with an egress that may reach the local receiver."""
    egress = Egress(allow_loopback=True, ssl_context=receiver.trust(), timeout=TIMEOUT)
    policy = RetryPolicy(delays=(0.3,), jitter=0.0)
    return {
        "ingress": Ingress(
            Keyring.from_env(),
            egress,
            Dispatcher(egress, policy),
            base=public_base_url(),
        )
    }


def environment() -> dict[str, str]:
    return {
        "MCP_EVENTS": "1",
        "MCP_EVENTS_SEALING_KEYS": KEYS,
        "APPWRITE_ENDPOINT": cloud.endpoint,
    }


def request(
    name: str,
    arguments: dict[str, str],
    url: str,
    secret: str | None,
    *,
    project: str = MAIN,
    **extra: Any,
) -> dict[str, Any]:
    """``events/subscribe`` params as ChatGPT sends them."""
    delivery: dict[str, Any] = {"mode": "webhook", "url": url}
    if secret is not None:
        delivery["secret"] = secret
    return {
        "name": name,
        "arguments": {"project_id": project, **arguments},
        "delivery": delivery,
        "cursor": None,
        **extra,
    }


def target(
    name: str, arguments: dict[str, str], url: str, *, project: str = MAIN
) -> dict[str, Any]:
    """``events/unsubscribe`` params."""
    return {
        "name": name,
        "arguments": {"project_id": project, **arguments},
        "delivery": {"url": url},
    }


def moment(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def label(event: str, until: float, owner: str) -> str:
    """A managed webhook name, as an earlier subscribe would have written it."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
    return f"MCP events | {event} | until {stamp} | owner {owner}"


class SubscribeFlow(unittest.TestCase):
    """A client subscribes, receives events, refreshes and unsubscribes."""

    server: Server
    appwrite: Appwrite
    client: Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(environment(), injected).__enter__()
        cls.appwrite = Appwrite(cls.server)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.appwrite.close()
        cls.server.__exit__(None, None, None)

    def call(self, method: str, params: dict[str, Any], token: str) -> dict[str, Any]:
        """The result, without the server info the SDK puts in ``_meta``."""
        with Client(self.server.url, token) as client:
            status, message = client.call(method, params)
        self.assertEqual(status, 200, message)
        self.assertNotIn("error", message)
        return {k: v for k, v in message["result"].items() if k != "_meta"}

    def subscribe(
        self, params: dict[str, Any], *, token: str = TOKEN
    ) -> dict[str, Any]:
        return self.call("events/subscribe", params, token)

    def refused(
        self, method: str, params: dict[str, Any], code: int, *, token: str = TOKEN
    ) -> dict[str, Any]:
        with Client(self.server.url, token) as client:
            _, message = client.call(method, params)
        self.assertIn("error", message, message)
        self.assertEqual(message["error"]["code"], code, message)
        return message["error"]

    def unsubscribe(self, params: dict[str, Any], *, token: str = TOKEN) -> None:
        result = self.call("events/unsubscribe", params, token)
        self.assertEqual(result, {"resultType": "complete"})

    def granted(self, result: dict[str, Any], ttl: float, started: float) -> None:
        """``refreshBefore`` is 9/10 of the ``ttl`` (seconds) after ``started``."""
        refresh = moment(result["refreshBefore"])
        self.assertRegex(
            result["refreshBefore"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"
        )
        self.assertAlmostEqual(refresh, started + ttl * 0.9, delta=5)

    def ours(self, project: str = MAIN) -> dict[str, dict[str, Any]]:
        """The stored webhooks this server manages in ``project``."""
        return {
            id: document
            for id, document in cloud.documents(project).items()
            if LABEL.fullmatch(document["name"])
        }

    def accepted(self, response) -> str:
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "accepted", body)
        return body["eventId"]

    def nothing_more(self, callback: str, count: int) -> None:
        time.sleep(SETTLE)
        self.assertEqual(len(receiver.received(callback)), count, callback)

    def test_subscribe_receive_refresh_rotate_unsubscribe(self):
        counts = {
            outcome: collector().count(SUBSCRIPTIONS, outcome=outcome)
            for outcome in ("created", "refreshed", "removed", "absent")
        }
        old, new = whsec(), whsec(64)
        callback = receiver.endpoint((old, new))
        name, arguments = ROWS

        # Subscribe: the callback is verified, then the webhook is stored.
        started = time.time()
        result = self.subscribe(request(name, arguments, callback, old))
        self.assertEqual(
            set(result), {"id", "refreshBefore", "cursor", "truncated", "resultType"}
        )
        id = result["id"]
        self.assertRegex(id, r"^sub_[0-9a-f]{32}$")
        self.assertEqual(
            id,
            subscription_id(
                PRINCIPAL, callback, name, {"project_id": MAIN, **arguments}
            ),
        )
        self.assertIsNone(result["cursor"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["resultType"], "complete")
        self.granted(result, 3600, started)

        handshake = receiver.wait(callback, 1)[0]
        challenge = handshake.json()
        self.assertEqual(set(challenge), {"type", "challenge"})
        self.assertEqual(challenge["type"], "verification")
        self.assertRegex(handshake.headers["webhook-id"], r"^msg_verification_")
        self.assertEqual(handshake.headers["x-mcp-subscription-id"], id)
        self.assertEqual(handshake.verified, (True, False))

        webhook = cloud.documents(MAIN)[id]
        self.assertEqual(webhook["url"], f"{PUBLIC_URL}/appwrite/webhooks/{id}")
        self.assertEqual(
            webhook["events"], ["tablesdb.main.tables.support_tickets.rows.*.create"]
        )
        self.assertTrue(webhook["tls"])
        self.assertTrue(webhook["enabled"])
        self.assertEqual(webhook["authUsername"], "mcp-events")
        self.assertTrue(webhook["httpPass"].startswith("v1.k1."))
        self.assertEqual(webhook["signatureKey"], Keyring.parse(KEYS).signing_key(id))
        match = LABEL.fullmatch(webhook["name"])
        assert match is not None, webhook["name"]
        self.assertEqual(match["event"], name)
        self.assertEqual(match["owner"], PRINCIPAL[:8])
        self.assertAlmostEqual(moment(match["until"]), started + 3600, delta=5)
        # The client's secret and callback are only inside the sealed envelope.
        for text in (old, callback):
            self.assertNotIn(text, json.dumps(webhook))
        # Writes were made with the caller's own token.
        self.assertTrue(
            all(token == TOKEN for method, path, token in cloud.requests[-4:])
        )

        # Appwrite fires the stored webhook; the event reaches the callback.
        responses = self.appwrite.fire(cloud, MAIN, ROW_EVENT, fixture("row_created"))
        self.assertEqual(len(responses), 1)
        event_id = self.accepted(responses[0])
        delivery = receiver.wait(callback, 2)[1]
        self.assertEqual(delivery.headers["webhook-id"], event_id)
        self.assertEqual(delivery.headers["x-mcp-subscription-id"], id)
        self.assertEqual(delivery.verified, (True, False))
        self.assertEqual(delivery.json()["data"]["row_id"], "row_8f2a")
        # Not an event for this table: Appwrite does not fire the webhook.
        other = "tablesdb.main.tables.orders.rows.row_1.create"
        self.assertEqual(
            self.appwrite.fire(cloud, MAIN, other, fixture("row_created")), []
        )

        # Refresh: same id, one webhook, a new grant each time. Suggestions
        # are clamped to 5 minutes .. 24 hours; null gets the default hour.
        # The order of the arguments does not change the subscription.
        managed = set(self.ours())
        reordered = {"project_id": MAIN, **dict(reversed(arguments.items()))}
        reordered = dict(reversed(reordered.items()))
        for suggestion, ttl in ((60_000, 300), (10 * 86_400_000, 86_400), (None, 3600)):
            with self.subTest(ttlMs=suggestion):
                started = time.time()
                params = request(name, arguments, callback, old, ttlMs=suggestion)
                if suggestion is None:
                    params["arguments"] = reordered
                result = self.subscribe(params)
                self.assertEqual(result["id"], id)
                self.granted(result, ttl, started)
                self.assertEqual(set(self.ours()), managed)
                until = LABEL.fullmatch(cloud.documents(MAIN)[id]["name"])
                assert until is not None
                self.assertAlmostEqual(moment(until["until"]), started + ttl, delta=5)
        # Every refresh verifies the callback again (no verification cache),
        # each time with a fresh challenge.
        handshakes = [
            r.json()["challenge"]
            for r in receiver.received(callback)
            if r.json().get("type") == "verification"
        ]
        self.assertEqual(len(handshakes), 4)
        self.assertEqual(len(set(handshakes)), 4)

        # Rotate: the new secret replaces the old one, for the handshake and
        # for every later delivery.
        result = self.subscribe(request(name, arguments, callback, new))
        self.assertEqual(result["id"], id)
        self.assertEqual(receiver.wait(callback, 6)[5].verified, (False, True))
        responses = self.appwrite.fire(cloud, MAIN, ROW_EVENT, fixture("row_created"))
        self.accepted(responses[0])
        self.assertEqual(receiver.wait(callback, 7)[6].verified, (False, True))

        # Another user's unsubscribe for the same event and URL is a different
        # subscription id: nothing of ours is touched.
        self.unsubscribe(target(name, arguments, callback), token=OTHER_TOKEN)
        self.assertIn(id, cloud.documents(MAIN))

        # Unsubscribe: the webhook is gone, so Appwrite has nothing to fire.
        self.unsubscribe(target(name, arguments, callback))
        self.assertNotIn(id, cloud.documents(MAIN))
        self.assertEqual(
            self.appwrite.fire(cloud, MAIN, ROW_EVENT, fixture("row_created")), []
        )
        # Idempotent: unsubscribing again is still {}.
        self.unsubscribe(target(name, arguments, callback))
        self.nothing_more(callback, 7)

        for outcome, increase in (
            ("created", 1),
            ("refreshed", 4),
            ("removed", 1),
            ("absent", 2),
        ):
            with self.subTest(counter=outcome):
                self.assertEqual(
                    collector().count(SUBSCRIPTIONS, outcome=outcome),
                    counts[outcome] + increase,
                )

    def test_every_event_subscribes_and_is_authorized(self):
        managed = set(self.ours())
        ids = set()
        # Secrets span the 24..64 bytes Standard Webhooks allows.
        sizes = (24, 32, 64, 24, 32, 64)
        for (name, arguments, body, event, read), size in zip(
            CASES, sizes, strict=True
        ):
            with self.subTest(event=name):
                secret = whsec(size)
                callback = receiver.endpoint((secret,))
                reads = cloud.calls("GET", read)
                result = self.subscribe(request(name, arguments, callback, secret))
                ids.add(result["id"])
                # The resource was read with the caller's token first.
                self.assertEqual(cloud.calls("GET", read), reads + 1)
                webhook = cloud.documents(MAIN)[result["id"]]
                self.assertEqual(
                    webhook["events"],
                    lookup(name).patterns({"project_id": MAIN, **arguments}),
                )
                responses = self.appwrite.fire(cloud, MAIN, event, fixture(body))
                self.assertEqual(len(responses), 1)
                event_id = self.accepted(responses[0])
                received = receiver.wait(callback, 2)[1]
                self.assertEqual(received.headers["webhook-id"], event_id)
                self.assertEqual(received.json()["name"], name)
                self.unsubscribe(target(name, arguments, callback))
        self.assertEqual(len(ids), len(CASES))

        # The resource must exist: NotFound, kind "resource".
        secret = whsec()
        callback = receiver.endpoint((secret,))
        for name, arguments in (
            ("functions.execution.failed", {"function_id": "fn_gone"}),
            ("sites.deployment.completed", {"site_id": "site_gone"}),
            ("tablesdb.row.created", {"database_id": "main", "table_id": "gone"}),
            ("tablesdb.row.created", {"database_id": "gone", "table_id": "orders"}),
            ("storage.file.created", {"bucket_id": "gone"}),
        ):
            with self.subTest(missing=arguments):
                error = self.refused(
                    "events/subscribe",
                    request(name, arguments, callback, secret),
                    NOT_FOUND,
                )
                self.assertEqual(error["data"], {"kind": "resource"})

        # The caller must be able to read it: Forbidden, whether they lack
        # the project, the scope, or the project does not exist at all.
        execution = ("functions.execution.failed", {"function_id": "fn_billing"})
        error = self.refused(
            "events/subscribe",
            request(*execution, callback, secret),
            FORBIDDEN,
            token=STRANGER,
        )
        self.assertIn(MAIN, error["message"])
        error = self.refused(
            "events/subscribe",
            request(*execution, callback, secret),
            FORBIDDEN,
            token=NO_FUNCTIONS,
        )
        self.assertIn("project:functions.read", error["message"])
        self.refused(
            "events/subscribe",
            request(*execution, callback, secret, project=MISSING),
            FORBIDDEN,
        )
        # A token with no user behind it has no principal to subscribe for.
        self.refused(
            "events/subscribe",
            request(*execution, callback, secret),
            FORBIDDEN,
            token=SUBJECTLESS_TOKEN,
        )
        self.refused(
            "events/unsubscribe",
            target(*execution, callback),
            FORBIDDEN,
            token=SUBJECTLESS_TOKEN,
        )
        # No token at all never reaches JSON-RPC.
        response = httpx.post(
            f"{self.server.url}/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "events/subscribe",
                "params": request(*ROWS, callback, secret),
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        self.assertEqual(response.status_code, 401)
        # None of the refusals contacted the callback or wrote a webhook.
        self.nothing_more(callback, 0)
        self.assertEqual(set(self.ours()), managed)

    def test_invalid_requests_are_refused_before_anything_is_sent(self):
        secret = whsec()
        callback = receiver.endpoint((secret,))
        name, arguments = ROWS
        base = request(name, arguments, callback, secret)
        requests = len(cloud.requests)

        def changed(**delivery: Any) -> dict[str, Any]:
            return {**base, "delivery": {**base["delivery"], **delivery}}

        key = base64.b64encode(b"k" * 32).decode()
        invalid = {
            "unknown argument": request(name, {**arguments, "extra": "x"}, callback, secret),
            "no project": {**base, "arguments": dict(arguments)},
            "arguments not an object": {**base, "arguments": ["main"]},
            "no delivery": {**base, "delivery": None},
            "no url": changed(url=None),
            "http url": changed(url=callback.replace("https://", "http://")),
            "ftp url": changed(url="ftp://hooks.example.com/"),
            "file url": changed(url="file:///etc/passwd"),
            "url without host": changed(url="https:///path"),
            "not a url": changed(url="not a url"),
            "private url": changed(url="https://10.0.0.7/hooks"),
            "metadata url": changed(url="https://169.254.169.254/latest"),
            "credentials in url": changed(url=callback.replace("https://", "https://u:p@")),
            "no secret": changed(secret=None),
            "secret not a string": changed(secret=42),
            "empty secret": changed(secret=""),
            "bare prefix": changed(secret="whsec_"),
            "secret without prefix": changed(secret=key),
            "uppercase prefix": changed(secret="WHSEC_" + key),
            "space after prefix": changed(secret="whsec_ " + key),
            "secret not base64": changed(secret="whsec_not*base64!"),
            "secret bad padding": changed(secret="whsec_" + key[:-1]),
            "secret of 23 bytes": changed(secret="whsec_" + base64.b64encode(b"k" * 23).decode()),
            "secret of 65 bytes": changed(secret="whsec_" + base64.b64encode(b"k" * 65).decode()),
            "envelope too large": changed(url=callback + "?pad=" + "a" * 2000),
            "ttl not a number": {**base, "ttlMs": "1h"},
            "ttl negative": {**base, "ttlMs": -1},
            "maxAge negative": {**base, "maxAgeMs": -5},
        }  # fmt: skip
        # IDs that would widen an Appwrite event pattern, or are not IDs, in
        # every ID argument of every event.
        for event_name, event_arguments, *_ in CASES:
            full = {"project_id": MAIN, **event_arguments}
            for argument in full:
                if argument == "status":
                    continue
                for bad in (
                    "*",
                    "a*",
                    "a.b",
                    "fn1.executions",
                    "",
                    "-lead",
                    "_lead",
                    "a b",
                    "a" * 37,
                ):
                    params = request(event_name, {}, callback, secret)
                    params["arguments"] = {**full, argument: bad}
                    invalid[f"{event_name} {argument}={bad!r}"] = params
        deployments = request("functions.deployment.completed", {"function_id": "fn_billing", "status": "building"}, callback, secret)  # fmt: skip
        invalid["deployment status outside its choices"] = deployments
        for label_, params in invalid.items():
            with self.subTest(invalid=label_):
                self.refused("events/subscribe", params, INVALID_PARAMS)

        error = self.refused(
            "events/subscribe", {**base, "name": "tablesdb.row.deleted"}, NOT_FOUND
        )
        self.assertEqual(error["data"], {"kind": "event"})
        self.refused(
            "events/unsubscribe",
            {**target(*ROWS, callback), "name": "nope"},
            NOT_FOUND,
        )
        self.refused(
            "events/unsubscribe",
            target(name, {**arguments, "table_id": "*"}, callback),
            INVALID_PARAMS,
        )
        for mode in ("poll", "push"):
            with self.subTest(mode=mode):
                self.refused("events/subscribe", changed(mode=mode), UNSUPPORTED)

        # Nothing reached Appwrite or the callback.
        self.assertEqual(len(cloud.requests), requests)
        self.nothing_more(callback, 0)

    def test_handshake_failures_store_nothing(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        secret = whsec()
        elsewhere = receiver.endpoint((secret,))
        cases = [
            # An endpoint that cannot verify the request does not echo.
            ("challenge_failed", receiver.endpoint((whsec(),))),
            # A redirect is never followed, even to an endpoint that would echo.
            ("challenge_failed", receiver.endpoint((secret,), (Reply(307, location=elsewhere),))),
            ("http_4xx", receiver.endpoint((secret,), (Reply(404),))),
            ("http_5xx", receiver.endpoint((secret,), (Reply(503),))),
            ("timeout", receiver.endpoint((secret,), (Reply(delay=TIMEOUT + 1),))),
            ("tls_error", untrusted.endpoint((secret,))),
            ("connection_refused", f"https://localhost:{closed}/hooks/gone"),
        ]  # fmt: skip
        managed = set(self.ours())
        for reason, callback in cases:
            with self.subTest(reason=reason, callback=callback):
                error = self.refused(
                    "events/subscribe",
                    request(*ROWS, callback, secret),
                    CALLBACK_ENDPOINT,
                )
                self.assertEqual(error["data"], {"reason": reason})
                # Raw endpoint responses never surface.
                self.assertNotIn('"ok"', error["message"])
        self.assertEqual(set(self.ours()), managed)
        self.nothing_more(elsewhere, 0)

    def test_appwrite_refusals_leave_no_partial_state(self):
        secret = whsec()
        callback = receiver.endpoint((secret,))
        managed = set(self.ours())
        internal = collector().count("mcp.jsonrpc.errors", error_code="-32603")
        forbidden = collector().count(
            SUBSCRIPTIONS, operation="subscribe", outcome="error", reason="-32012"
        )

        # Without the webhook scopes the request stops before the handshake.
        error = self.refused(
            "events/subscribe",
            request(*ROWS, callback, secret),
            FORBIDDEN,
            token=READ_ONLY,
        )
        self.assertIn("project:webhooks.read", error["message"])
        self.assertIn("project:webhooks.write", error["message"])
        self.refused(
            "events/unsubscribe", target(*ROWS, callback), FORBIDDEN, token=READ_ONLY
        )
        self.nothing_more(callback, 0)

        # Appwrite failing on its side is an internal error, and leaves nothing.
        for method, path in (
            ("GET", r"/v1/tablesdb/main/tables/support_tickets"),
            ("GET", r"/v1/webhooks"),
            ("POST", r"/v1/webhooks"),
        ):
            with self.subTest(failing=f"{method} {path}"):
                cloud.fail(method, path, 503)
                self.refused(
                    "events/subscribe",
                    request(*ROWS, callback, secret),
                    INTERNAL_ERROR,
                )
                self.assertEqual(set(self.ours()), managed)

        # A refresh whose signing-key update fails removes the webhook rather
        # than leave an envelope and key that may disagree.
        result = self.subscribe(request(*ROWS, callback, secret))
        cloud.fail("PATCH", r"/v1/webhooks/[^/]+/secret", 500)
        self.refused(
            "events/subscribe", request(*ROWS, callback, secret), INTERNAL_ERROR
        )
        self.assertNotIn(result["id"], cloud.documents(MAIN))

        # Counted like tools/call: four internal errors, one refusal.
        self.assertEqual(
            collector().count("mcp.jsonrpc.errors", error_code="-32603"), internal + 4
        )
        self.assertEqual(
            collector().count(
                SUBSCRIPTIONS, operation="subscribe", outcome="error", reason="-32012"
            ),
            forbidden + 1,
        )

    def test_free_plan_limit_and_expired_cleanup(self):
        secret = whsec()
        callback = receiver.endpoint((secret,))
        tickets = (
            "tablesdb.row.created",
            {"database_id": "main", "table_id": "tickets"},
        )
        orders = ("tablesdb.row.created", {"database_id": "main", "table_id": "orders"})

        # The Free project holds the user's own webhook and one of ours that
        # expired: the slot it holds is freed before creating.
        cloud.store(
            FREE,
            {
                "$id": "slack",
                "name": "Slack alerts",
                "url": "https://hooks.example.com/a",
            },
        )
        expired = "sub_" + secrets.token_hex(16)
        cloud.store(
            FREE,
            {
                "$id": expired,
                "name": label("tablesdb.row.created", time.time() - 60, PRINCIPAL[:8]),
                "url": f"{PUBLIC_URL}/appwrite/webhooks/{expired}",
            },
        )
        cleaned = collector().count(SUBSCRIPTIONS, operation="cleanup")
        result = self.subscribe(request(*tickets, callback, secret, project=FREE))
        self.assertEqual(set(cloud.documents(FREE)), {"slack", result["id"]})
        self.assertEqual(
            collector().count(SUBSCRIPTIONS, operation="cleanup"), cleaned + 1
        )

        # A second subscription does not fit: ResourceExhausted, nothing sent
        # beyond the handshake, and the user's webhook is untouched.
        error = self.refused(
            "events/subscribe",
            request(*orders, callback, secret, project=FREE),
            RESOURCE_EXHAUSTED,
        )
        self.assertEqual(error["data"], {"limit": "webhooks", "max": 2})
        self.assertIn("Pro", error["message"])
        self.assertIn("unsubscribe", error["message"])
        self.assertEqual(set(cloud.documents(FREE)), {"slack", result["id"]})

        # Unsubscribing frees the slot.
        self.unsubscribe(target(*tickets, callback, project=FREE))
        self.subscribe(request(*orders, callback, secret, project=FREE))
        self.unsubscribe(target(*orders, callback, project=FREE))
        self.assertEqual(set(cloud.documents(FREE)), {"slack"})

    def test_webhooks_this_server_did_not_create_are_never_touched(self):
        secret = whsec()
        callback = receiver.endpoint((secret,))
        name, arguments = ROWS
        past, future = time.time() - 60, time.time() + 3600
        ours_expired = "sub_" + secrets.token_hex(16)
        ours_live = "sub_" + secrets.token_hex(16)
        theirs_expired = "sub_" + secrets.token_hex(16)
        renamed = "sub_" + secrets.token_hex(16)
        seeded = {
            # The user's webhooks, including one that copies our label.
            "slack": "Slack alerts",
            "lookalike": label(name, past, PRINCIPAL[:8]),
            renamed: "Renamed by a person",
            # Another user's expired subscription, and one of ours still live.
            theirs_expired: label(name, past, OTHER_PRINCIPAL[:8]),
            ours_live: label(name, future, PRINCIPAL[:8]),
            # Ours and expired: the only one cleanup removes.
            ours_expired: label(name, past, PRINCIPAL[:8]),
        }
        for id, title in seeded.items():
            cloud.store(TIDY, {"$id": id, "name": title, "url": "https://x.example"})
        before = cloud.documents(TIDY)

        result = self.subscribe(
            request(name, arguments, callback, secret, project=TIDY)
        )
        after = cloud.documents(TIDY)
        self.assertEqual(set(after), set(seeded) - {ours_expired} | {result["id"]})
        for id in set(seeded) - {ours_expired}:
            self.assertEqual(after[id], before[id], id)
        self.unsubscribe(target(name, arguments, callback, project=TIDY))

        # A webhook that holds the subscription's id but not our name (renamed
        # in the Console) is neither rewritten nor deleted.
        id = subscription_id(
            PRINCIPAL, callback, name, {"project_id": TIDY, **arguments}
        )
        cloud.store(TIDY, {"$id": id, "name": "Renamed", "url": "https://x.example"})
        error = self.refused(
            "events/subscribe",
            request(name, arguments, callback, secret, project=TIDY),
            FORBIDDEN,
        )
        self.assertIn(id, error["message"])
        self.unsubscribe(target(name, arguments, callback, project=TIDY))
        self.assertEqual(cloud.documents(TIDY)[id]["name"], "Renamed")


class ProductionEgressFlow(unittest.TestCase):
    """The server as production builds it refuses loopback callbacks."""

    def test_loopback_callback_is_invalid(self):
        secret = whsec()
        callback = receiver.endpoint((secret,))
        requests = len(cloud.requests)
        with Server(environment()) as server, server.client() as client:
            _, message = client.call(
                "events/subscribe", request(*ROWS, callback, secret)
            )
        self.assertEqual(message["error"]["code"], INVALID_PARAMS, message)
        self.assertEqual(len(cloud.requests), requests)
        time.sleep(SETTLE)
        self.assertEqual(receiver.received(callback), [])


if __name__ == "__main__":
    unittest.main()
