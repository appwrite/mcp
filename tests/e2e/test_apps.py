"""MCP Apps through the real hosted server, as a host that renders them drives it.

The host lists tools, finds ``appwrite_show`` pointing at ``ui://appwrite/view``
and reads that resource. It then calls the tool for the model, and calls it
again (and the app-only ``appwrite_view_write``) for the view, which is how
the view pages, refreshes, follows a build and saves an edit.
:class:`support.Cloud` is the Appwrite API behind it, so every refusal is also
checked to have reached no Appwrite write at all.
"""

import base64
import datetime
import hashlib
import json
import re
import unittest
from typing import Any

from support import (
    APPS_SCOPES,
    PNG,
    SCOPES,
    TOKEN,
    Client,
    Cloud,
    Project,
    Server,
    column,
    deployment,
    execution,
    file,
    row,
    session,
    user,
)

PROJECT = "6630f1a2b3c4d5e6f7a8"
DATABASE = "commerce"
TABLE = "orders"
FUNCTION = "checkout"
EXTENSION = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
VIEW_URI = "ui://appwrite/view"
UI = {"extensions": {EXTENSION: {"mimeTypes": [MIME_TYPE]}}}
ROWS = 30
PAGE = 25
LOG_LINES = 250
EXECUTIONS = 30
BUCKET = "photos"
PHOTOS = 30
USERS = 27
SECRETS = (
    "argon2",
    "c2FsdA",
    "theme",
    "target",
    "provider-access-token",
    "provider-refresh-token",
    "session-secret",
)
"""Values of the user and session fields the view must never carry."""
# How open-runtimes colors a build log line.
ANSI_LINE = (
    "\x1b[90m[13:23:45] \x1b[31m[\x1b[0mopen-runtimes\x1b[31m] "
    "\x1b[32mBuild finished. \x1b[0m"
)

cloud: Cloud


def setUpModule() -> None:
    global cloud
    cloud = Cloud()
    project = cloud.add(
        Project(
            PROJECT,
            tables={(DATABASE, TABLE)},
            functions={FUNCTION},
            buckets={BUCKET},
        )
    )
    project.columns[(DATABASE, TABLE)] = [
        column("customer", "string"),
        column("total", "double"),
        column("status", "string"),
        column("paid", "boolean"),
    ]
    project.rows[(DATABASE, TABLE)] = [
        row(
            f"ord_{index:03d}",
            DATABASE,
            TABLE,
            index,
            customer=f"Customer {index}",
            total=index * 1.5,
            status="paid",
            paid=True,
        )
        for index in range(1, ROWS + 1)
    ]
    project.deployments[FUNCTION] = [
        deployment("dep_old", FUNCTION, "ready", "done"),
        deployment(
            "dep_new",
            FUNCTION,
            "building",
            "\n".join(f"line {index}" for index in range(1, LOG_LINES))
            + f"\n{ANSI_LINE}",
            providerBranch="main",
            providerCommitHash="a91c3e0f00",
        ),
    ]
    project.executions[FUNCTION] = [
        execution(
            f"exe_{index:03d}",
            FUNCTION,
            "failed" if index % 10 == 0 else "completed",
            requestPath=f"/orders/{index}",
            responseStatusCode=500 if index % 10 == 0 else 200,
        )
        for index in range(1, EXECUTIONS)
    ] + [
        execution(
            "exe_running",
            FUNCTION,
            "processing",
            requestMethod="POST",
            requestPath="/charge",
            requestHeaders=[{"name": "content-type", "value": "application/json"}],
            logs="\n".join(f"step {index}" for index in range(1, LOG_LINES + 1)),
            errors="card_declined\n    at charge (index.js:14:11)",
        )
    ]
    project.files[BUCKET] = [
        file(
            f"img_{index:03d}", BUCKET, f"photo-{index}.jpg", "image/jpeg", 2048 * index
        )
        for index in range(1, PHOTOS)
    ] + [file("invoice", BUCKET, "invoice.pdf", "application/pdf", 40_000)]
    project.users = [
        user(
            f"usr_{index:03d}",
            f"Member {index}",
            f"member{index}@example.com",
            labels=["beta"] if index % 2 else [],
        )
        for index in range(1, USERS - 1)
    ] + [
        user("grace", "Grace Hopper", "grace@navy.example", emailVerification=True),
        user("ada", "Ada Lovelace", "ada@engine.example", mfa=True, labels=["vip"]),
    ]
    project.sessions["ada"] = [
        session(f"ses_{index}", "ada", ip=f"203.0.113.{index}")
        for index in range(1, 13)
    ]
    today = datetime.datetime.now(datetime.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    def day(back: int) -> str:
        return (today - datetime.timedelta(days=back)).isoformat()

    project.usage = {
        # The day ten days back is outside a 7d range.
        "network.requests": {day(0): 120, day(2): 30, day(10): 999},
        "network.inbound": {day(0): 2_000, day(2): 500},
        "network.outbound": {day(0): 1_500_000},
        "executions": {day(1): 4},
        "executions.compute": {day(1): 9_500},
        "users": {day(3): 40, day(0): 42},
        "files.storage": {day(0): 3_200_000},
        "functions": {day(0): 3},
    }
    cloud.grant(TOKEN, {PROJECT}, SCOPES | APPS_SCOPES)


def tearDownModule() -> None:
    cloud.close()


def tools(client: Client) -> dict[str, Any]:
    _, response = client.call("tools/list")
    return {tool["name"]: tool for tool in response["result"]["tools"]}


class AppsDisabledFlow(unittest.TestCase):
    """Without the flag nothing changes, even for a host that renders apps."""

    def test_nothing_is_offered(self):
        with Server({"APPWRITE_ENDPOINT": cloud.endpoint}) as server:
            with server.client(capabilities=UI) as client:
                listed = tools(client)
                self.assertNotIn("appwrite_show", listed)
                self.assertNotIn("appwrite_view_write", listed)

                _, response = client.call("server/discover")
                extensions = response["result"]["capabilities"].get("extensions", {})
                self.assertNotIn(EXTENSION, extensions)

                status, response = client.call("resources/read", {"uri": VIEW_URI})
                self.assertIn("error", response, status)

                result = client.call(
                    "tools/call", {"name": "appwrite_show", "arguments": {}}
                )[1]["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(
                    result["content"][0]["text"], "Tool appwrite_show not found"
                )


class AppsFlow(unittest.TestCase):
    server: Server
    host: Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(
            {"APPWRITE_ENDPOINT": cloud.endpoint, "MCP_APPS": "1"}
        ).__enter__()
        cls.host = cls.server.client(capabilities=UI)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.host.__exit__(None, None, None)
        cls.server.__exit__(None, None, None)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        status, response = self.host.call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        self.assertEqual(status, 200, response)
        return response["result"]

    def show_table(self, **arguments: Any) -> dict[str, Any]:
        result = self.call(
            "appwrite_show",
            {
                "view": "table",
                "project_id": PROJECT,
                "database_id": DATABASE,
                "table_id": TABLE,
                **arguments,
            },
        )
        self.assertFalse(result.get("isError"), result)
        return result

    def test_host_discovers_the_view(self):
        _, response = self.host.call("server/discover")
        extensions = response["result"]["capabilities"]["extensions"]
        self.assertEqual(extensions[EXTENSION], {"mimeTypes": [MIME_TYPE]})

        listed = tools(self.host)
        show = listed["appwrite_show"]
        # Hosts cache UI resources by URI, so the URI carries the HTML's hash:
        # a release that changes the view makes hosts fetch it again.
        uri = show["_meta"]["ui"]["resourceUri"]
        self.assertRegex(uri, rf"^{re.escape(VIEW_URI)}/[0-9a-f]{{12}}$")
        self.assertEqual(
            show["_meta"]["ui"], {"resourceUri": uri, "visibility": ["model", "app"]}
        )
        self.assertEqual(show["_meta"]["ui/resourceUri"], uri)
        self.assertTrue(show["annotations"]["readOnlyHint"])
        write = listed["appwrite_view_write"]
        self.assertEqual(
            write["_meta"]["ui"], {"resourceUri": uri, "visibility": ["app"]}
        )
        self.assertTrue(write["annotations"]["destructiveHint"])
        # The operator tools are unchanged.
        self.assertLessEqual(
            {"appwrite_call_tool", "appwrite_call_write_tool"}, set(listed)
        )

        _, response = self.host.call("resources/list")
        view = next(
            each for each in response["result"]["resources"] if each["uri"] == uri
        )
        self.assertEqual(view["mimeType"], MIME_TYPE)

        _, response = self.host.call("resources/read", {"uri": uri})
        (contents,) = response["result"]["contents"]
        self.assertEqual(contents["uri"], uri)
        self.assertEqual(contents["mimeType"], MIME_TYPE)
        # No CSP domains: the host's restrictive default, no network access.
        self.assertEqual(contents["_meta"]["ui"], {"csp": {}, "prefersBorder": True})
        html = contents["text"]
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('"ui/initialize"', html)
        # Appwrite data reaches the DOM as text only.
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(uri, f"{VIEW_URI}/{digest}")

        # Tool lists cached before the URI was versioned still resolve.
        _, response = self.host.call("resources/read", {"uri": VIEW_URI})
        self.assertEqual(response["result"]["contents"][0]["text"], html)

    def test_hosts_without_apps_are_not_offered_the_view(self):
        with self.server.client() as plain:
            listed = tools(plain)
            self.assertNotIn("appwrite_show", listed)
            self.assertNotIn("appwrite_view_write", listed)
            _, response = plain.call("resources/list")
            uris = {each["uri"] for each in response["result"]["resources"]}
            self.assertFalse(any(each.startswith(VIEW_URI) for each in uris))

        # A 2025-era client on the stateless transport lists tools without a
        # session, so its capabilities are unknown and the view is offered.
        with self.server.client() as legacy:
            status, response = legacy.legacy("tools/list")
            self.assertEqual(status, 200, response)
            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertIn("appwrite_show", names)

    def test_table_view_pages_through_rows(self):
        listed = cloud.calls("GET", rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows")
        result = self.show_table()
        data = result["structuredContent"]
        self.assertEqual(data["view"], "table")
        self.assertEqual(data["name"], TABLE)
        self.assertEqual(data["total"], ROWS)
        self.assertEqual(
            [each["key"] for each in data["columns"]],
            ["customer", "total", "status", "paid"],
        )
        # Newest first, one page.
        ids = [each["$id"] for each in data["rows"]]
        self.assertEqual(ids[0], f"ord_{ROWS:03d}")
        self.assertEqual(len(ids), 25)
        self.assertEqual(data["next"], ids[-1])

        text = result["content"][0]["text"]
        self.assertIn(f"Table {TABLE} ({DATABASE}/{TABLE}): {ROWS} rows", text)
        self.assertIn(f"ord_{ROWS:03d} | Customer {ROWS}", text)
        self.assertIn(f"cursor={data['next']}", text)

        second = self.show_table(cursor=data["next"])["structuredContent"]
        self.assertEqual(
            [each["$id"] for each in second["rows"]],
            [f"ord_{index:03d}" for index in range(5, 0, -1)],
        )
        self.assertIsNone(second["next"])

        # A last page that is exactly full has no next page either.
        full = self.show_table(cursor=f"ord_{PAGE + 1:03d}")["structuredContent"]
        self.assertEqual(len(full["rows"]), PAGE)
        self.assertEqual(full["rows"][-1]["$id"], "ord_001")
        self.assertIsNone(full["next"])
        self.assertEqual(
            cloud.calls("GET", rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows"),
            listed + 3,
        )

    def test_view_writes_only_its_allowlist_and_only_when_confirmed(self):
        row_path = rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows/ord_001"
        writes = cloud.calls("PATCH", row_path)
        update = {
            "tool_name": "tables_db_update_row",
            "project_id": PROJECT,
            "arguments": {
                "database_id": DATABASE,
                "table_id": TABLE,
                "row_id": "ord_001",
                "data": {"status": "refunded"},
            },
        }

        # Only the literal true confirms; a truthy string must not.
        for refusal in (False, "false", "true", 1):
            with self.subTest(confirm_write=refusal):
                result = self.call(
                    "appwrite_view_write", {**update, "confirm_write": refusal}
                )
                self.assertTrue(result["isError"])
                self.assertIn("confirm_write=true", result["content"][0]["text"])

        result = self.call(
            "appwrite_view_write",
            {
                **update,
                "tool_name": "tables_db_delete_row",
                "confirm_write": True,
            },
        )
        self.assertTrue(result["isError"])
        self.assertIn(
            "appwrite_view_write runs only tables_db_update_row",
            result["content"][0]["text"],
        )
        self.assertEqual(cloud.calls("DELETE", row_path), 0)
        self.assertEqual(cloud.calls("PATCH", row_path), writes)

        result = self.call("appwrite_view_write", {**update, "confirm_write": True})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["status"], "refunded")
        self.assertEqual(cloud.calls("PATCH", row_path), writes + 1)
        last = self.show_table(cursor="ord_002")["structuredContent"]["rows"]
        self.assertEqual(last[0]["$id"], "ord_001")
        self.assertEqual(last[0]["status"], "refunded")

    def test_deployment_view_follows_a_build_and_cancels_it(self):
        show = {"view": "deployment", "project_id": PROJECT, "function_id": FUNCTION}
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "deployment")
        self.assertEqual(data["kind"], "function")
        self.assertEqual(data["resourceId"], FUNCTION)
        build = data["deployment"]
        self.assertEqual(build["id"], "dep_new")
        self.assertEqual(build["status"], "building")
        self.assertEqual(build["branch"], "main")
        self.assertFalse(data["done"])
        # Only the end of a long log is carried.
        self.assertEqual(len(build["logs"]), 200)
        # The view gets the colors; the model's summary gets plain text.
        self.assertEqual(build["logs"][-1], ANSI_LINE)
        self.assertTrue(build["truncated"])
        text = result["content"][0]["text"]
        self.assertIn(f"Deployment dep_new of function {FUNCTION} is building.", text)
        self.assertIn("Source: main @ a91c3e0.", text)
        self.assertTrue(
            text.endswith("[13:23:45] [open-runtimes] Build finished. "), text
        )
        self.assertNotIn("\x1b", text)

        status_path = rf"/v1/functions/{FUNCTION}/deployments/dep_new/status"
        result = self.call(
            "appwrite_view_write",
            {
                "tool_name": "functions_update_deployment_status",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": {"function_id": FUNCTION, "deployment_id": "dep_new"},
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(cloud.calls("PATCH", status_path), 1)

        result = self.call("appwrite_show", {**show, "deployment_id": "dep_new"})
        data = result["structuredContent"]
        self.assertEqual(data["deployment"]["status"], "canceled")
        self.assertTrue(data["done"])

    def test_executions_view_lists_filters_and_opens_one(self):
        show = {"view": "executions", "project_id": PROJECT, "function_id": FUNCTION}
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "executions")
        self.assertEqual(data["name"], "Checkout")
        self.assertEqual(data["runtime"], "node-22")
        self.assertEqual(data["total"], EXECUTIONS)
        ids = [each["id"] for each in data["executions"]]
        self.assertEqual(len(ids), PAGE)
        self.assertEqual(ids[0], "exe_running")
        self.assertEqual(data["next"], ids[-1])
        self.assertIsNone(data["execution"])
        # A running execution on the page keeps the view polling.
        self.assertTrue(data["running"])
        first = data["executions"][0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["path"], "/charge")
        self.assertNotIn("logs", first)
        text = result["content"][0]["text"]
        self.assertIn(
            f"Function Checkout ({FUNCTION}): {EXECUTIONS} executions, "
            "showing 25 newest.",
            text,
        )
        self.assertIn("exe_running | processing | http | POST /charge", text)
        self.assertIn(f"cursor={data['next']}", text)

        second = self.call("appwrite_show", {**show, "cursor": data["next"]})
        rest = second["structuredContent"]
        self.assertEqual(len(rest["executions"]), EXECUTIONS - PAGE)
        self.assertIsNone(rest["next"])
        self.assertFalse(rest["running"])

        failed = self.call("appwrite_show", {**show, "status": "failed"})
        data = failed["structuredContent"]
        self.assertEqual(
            [each["id"] for each in data["executions"]],
            ["exe_020", "exe_010"],
        )
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["executions"][0]["statusCode"], 500)
        self.assertIn("2 failed executions", failed["content"][0]["text"])

        opened = self.call("appwrite_show", {**show, "execution_id": "exe_running"})
        detail = opened["structuredContent"]["execution"]
        self.assertEqual(detail["status"], "processing")
        self.assertEqual(
            detail["requestHeaders"],
            [{"name": "content-type", "value": "application/json"}],
        )
        # Only the end of long logs is carried.
        self.assertEqual(len(detail["logs"]), 200)
        self.assertEqual(detail["logs"][-1], f"step {LOG_LINES}")
        self.assertTrue(detail["logsTruncated"])
        self.assertEqual(
            detail["errors"], ["card_declined", "    at charge (index.js:14:11)"]
        )
        text = opened["content"][0]["text"]
        self.assertIn("Execution exe_running: processing, POST /charge", text)
        self.assertIn("Errors (last lines):\ncard_declined", text)

    def test_files_view_pages_a_bucket_with_previews_for_the_view_only(self):
        show = {"view": "files", "project_id": PROJECT, "bucket_id": BUCKET}
        previews = rf"/v1/storage/buckets/{BUCKET}/files/[^/]+/preview"
        before = cloud.calls("GET", previews)
        # The model's call carries no previews.
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "files")
        self.assertEqual(data["bucket"]["name"], "Photos")
        self.assertTrue(data["bucket"]["encryption"])
        self.assertEqual(data["total"], PHOTOS)
        self.assertEqual(len(data["files"]), 24)
        self.assertEqual(data["files"][0]["id"], "invoice")
        self.assertEqual(data["next"], data["files"][-1]["id"])
        self.assertFalse(data["thumbnails"])
        self.assertTrue(all(each["thumbnail"] is None for each in data["files"]))
        self.assertEqual(cloud.calls("GET", previews), before)
        text = result["content"][0]["text"]
        self.assertIn(
            f"Bucket Photos ({BUCKET}): {PHOTOS} files, showing 24 newest.", text
        )
        self.assertIn("invoice | invoice.pdf | application/pdf | 39.1 KB", text)
        self.assertNotIn("base64", text)

        # The view asks for them: one per image on the page, none for the PDF.
        result = self.call("appwrite_show", {**show, "thumbnails": True})
        data = result["structuredContent"]
        self.assertEqual(cloud.calls("GET", previews), before + 23)
        thumbnails = {each["id"]: each["thumbnail"] for each in data["files"]}
        self.assertIsNone(thumbnails.pop("invoice"))
        self.assertEqual(
            set(thumbnails.values()),
            {"data:image/png;base64," + base64.b64encode(PNG).decode()},
        )

        rest = self.call("appwrite_show", {**show, "cursor": data["next"]})
        self.assertEqual(len(rest["structuredContent"]["files"]), PHOTOS - 24)
        self.assertIsNone(rest["structuredContent"]["next"])

        found = self.call("appwrite_show", {**show, "search": "photo-2"})
        names = [each["name"] for each in found["structuredContent"]["files"]]
        self.assertEqual(names[0], "photo-29.jpg")
        self.assertTrue(all(name.startswith("photo-2") for name in names))

        opened = self.call(
            "appwrite_show", {**show, "file_id": "img_003", "thumbnails": True}
        )
        detail = opened["structuredContent"]["file"]
        self.assertEqual(detail["name"], "photo-3.jpg")
        self.assertEqual(detail["size"], 6144)
        self.assertEqual(detail["permissions"], ['read("any")'])
        self.assertTrue(detail["preview"].startswith("data:image/png;base64,"))
        self.assertIn(
            "File img_003 (photo-3.jpg): image/jpeg, 6.0 KB",
            opened["content"][0]["text"],
        )
        # A file Appwrite cannot render opens without a preview.
        pdf = self.call(
            "appwrite_show", {**show, "file_id": "invoice", "thumbnails": True}
        )
        self.assertFalse(pdf.get("isError"), pdf)
        self.assertIsNone(pdf["structuredContent"]["file"]["preview"])

    def test_users_view_searches_opens_and_changes_a_user(self):
        show = {"view": "users", "project_id": PROJECT}
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "users")
        self.assertEqual(data["total"], USERS)
        self.assertEqual(len(data["users"]), PAGE)
        self.assertEqual(data["users"][0]["id"], "ada")
        self.assertEqual(data["next"], data["users"][-1]["id"])
        self.assertIsNone(data["user"])
        ada = data["users"][0]
        self.assertEqual(ada["labels"], ["vip"])
        self.assertTrue(ada["mfa"])
        text = result["content"][0]["text"]
        self.assertIn(f"{USERS} users, showing 25 newest.", text)
        self.assertIn(
            "ada | Ada Lovelace | ada@engine.example | active, unverified", text
        )
        rest = self.call("appwrite_show", {**show, "cursor": data["next"]})
        self.assertEqual(len(rest["structuredContent"]["users"]), USERS - PAGE)
        self.assertIsNone(rest["structuredContent"]["next"])

        found = self.call("appwrite_show", {**show, "search": "navy"})
        data = found["structuredContent"]
        self.assertEqual([each["id"] for each in data["users"]], ["grace"])
        self.assertTrue(data["users"][0]["emailVerification"])
        self.assertIn("1 users matching 'navy'", found["content"][0]["text"])

        opened = self.call("appwrite_show", {**show, "user_id": "ada"})
        detail = opened["structuredContent"]["user"]
        self.assertEqual(detail["sessionsTotal"], 12)
        # The newest ten sessions, newest first.
        self.assertEqual(
            [each["ip"] for each in detail["sessions"]],
            [f"203.0.113.{index}" for index in range(12, 2, -1)],
        )
        self.assertEqual(detail["sessions"][0]["client"], "Chrome 131.0")
        self.assertEqual(detail["sessions"][0]["os"], "Mac 15.1")
        self.assertIn(
            "User ada (Ada Lovelace): active, unverified, mfa; 12 sessions",
            opened["content"][0]["text"],
        )
        # Password hashes, prefs, targets and session tokens never leave the server.
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(opened), secret)

        block = {
            "tool_name": "users_update_status",
            "project_id": PROJECT,
            "arguments": {"user_id": "grace", "status": False},
        }
        status_path = r"/v1/users/grace/status"
        result = self.call("appwrite_view_write", {**block, "confirm_write": "true"})
        self.assertTrue(result["isError"])
        self.assertEqual(cloud.calls("PATCH", status_path), 0)
        for tool_name, arguments in (
            ("users_delete", {"user_id": "grace"}),
            ("users_update_password", {"user_id": "grace", "password": "x" * 12}),
        ):
            with self.subTest(tool_name=tool_name):
                result = self.call(
                    "appwrite_view_write",
                    {
                        **block,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "confirm_write": True,
                    },
                )
                self.assertTrue(result["isError"])
                self.assertIn(
                    "appwrite_view_write runs only", result["content"][0]["text"]
                )
        self.assertEqual(cloud.calls("DELETE", r"/v1/users/grace"), 0)

        result = self.call("appwrite_view_write", {**block, "confirm_write": True})
        self.assertFalse(result.get("isError"), result)
        self.assertFalse(result["structuredContent"]["status"])
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(result), secret)
        self.assertEqual(cloud.calls("PATCH", status_path), 1)

        result = self.call(
            "appwrite_view_write",
            {
                "tool_name": "users_update_labels",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": {"user_id": "grace", "labels": ["admin", "navy"]},
            },
        )
        self.assertEqual(result["structuredContent"]["labels"], ["admin", "navy"])
        result = self.call(
            "appwrite_view_write",
            {
                "tool_name": "users_update_email_verification",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": {"user_id": "grace", "email_verification": False},
            },
        )
        self.assertFalse(result["structuredContent"]["emailVerification"])

        grace = self.call("appwrite_show", {**show, "search": "navy"})
        row = grace["structuredContent"]["users"][0]
        self.assertFalse(row["status"])
        self.assertEqual(row["labels"], ["admin", "navy"])
        self.assertIn(
            "grace | Grace Hopper | grace@navy.example | blocked, unverified | admin,navy",
            grace["content"][0]["text"],
        )

    def test_usage_view_charts_a_range(self):
        show = {"view": "usage", "project_id": PROJECT}
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "usage")
        self.assertEqual(data["range"], "7d")
        self.assertEqual(data["interval"], "1d")
        self.assertEqual(len(data["times"]), 7)
        # Days without traffic are zeros, so the chart keeps its shape.
        self.assertEqual(data["series"]["requests"], [0, 0, 0, 0, 30, 0, 120])
        self.assertEqual(data["series"]["executions"], [0, 0, 0, 0, 0, 4, 0])
        self.assertEqual(
            data["totals"],
            {
                "requests": 150,
                "inbound": 2_500,
                "outbound": 1_500_000,
                "executions": 4,
                "compute": 9_500,
            },
        )
        # Gauges are the newest snapshot; a metric with none is null.
        self.assertEqual(data["resources"]["users"], 42)
        self.assertEqual(data["resources"]["filesStorage"], 3_200_000)
        self.assertIsNone(data["resources"]["sites"])
        text = result["content"][0]["text"]
        self.assertIn(f"Usage of project {PROJECT} over the last 7d", text)
        self.assertIn("Requests: 150", text)
        self.assertIn("Bandwidth: 1.5 MB (2.5 KB in, 1.5 MB out)", text)
        self.assertIn("Executions: 4 (9.5 s of compute)", text)
        self.assertIn("Now: users 42, functions 3.", text)
        self.assertIn(f"Busiest interval: {data['times'][-1]} with 120 requests.", text)

        day = self.call("appwrite_show", {**show, "range": "24h"})["structuredContent"]
        self.assertEqual(day["interval"], "1h")
        self.assertEqual(len(day["times"]), 24)
        month = self.call("appwrite_show", {**show, "range": "30d"})[
            "structuredContent"
        ]
        self.assertEqual(month["totals"]["requests"], 1_149)

        # A range past the plan's history is Appwrite's own refusal.
        cloud.fail("GET", r"/v1/usage/events", 402, "limit_history_exceeded")
        refused = self.call("appwrite_show", {**show, "range": "30d"})
        self.assertTrue(refused["isError"])
        self.assertIn("limit_history_exceeded", refused["content"][0]["text"])

    def test_bad_requests_are_tool_errors(self):
        cases = [
            (
                {"view": "chart"},
                "view must be one of: table, deployment, executions, files, users, usage",
            ),
            (
                {"view": "table", "project_id": PROJECT, "table_id": TABLE},
                "database_id is required for view=table.",
            ),
            (
                {"view": "deployment", "project_id": PROJECT},
                "Pass exactly one of function_id or site_id.",
            ),
            (
                {"view": "executions", "project_id": PROJECT},
                "function_id is required for view=executions.",
            ),
            (
                {
                    "view": "executions",
                    "project_id": PROJECT,
                    "function_id": FUNCTION,
                    "status": "done",
                },
                "status must be one of: waiting, processing, completed",
            ),
            (
                {"view": "files", "project_id": PROJECT},
                "bucket_id is required for view=files.",
            ),
            (
                {"view": "usage", "project_id": PROJECT, "range": "1y"},
                "range must be one of: 24h, 7d, 30d.",
            ),
        ]
        for arguments, error in cases:
            with self.subTest(arguments=json.dumps(arguments)):
                result = self.call("appwrite_show", arguments)
                self.assertTrue(result["isError"])
                self.assertIn(error, result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
