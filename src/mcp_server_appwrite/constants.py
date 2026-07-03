"""Single home for the package's constants, grouped by the module that uses them."""

from __future__ import annotations

from pathlib import Path

from appwrite.models.bucket import Bucket
from appwrite.models.database import Database
from appwrite.models.function import Function
from appwrite.models.message import Message
from appwrite.models.site import Site
from appwrite.models.team import Team
from appwrite.models.user import User

# --- server ---------------------------------------------------------------

SERVER_VERSION = "0.8.3"

DEFAULT_ENDPOINT = "https://cloud.appwrite.io/v1"
DEFAULT_TRANSPORT = "stdio"
TRANSPORTS = {"stdio", "http"}
VALIDATION_SERVICE_ORDER = (
    "tables_db",
    "users",
    "teams",
    "functions",
    "sites",
    "storage",
    "messaging",
    "locale",
    "avatars",
)

# Service modules in the Appwrite SDK to skip (none by default — every service the
# installed SDK ships is exposed). Add a module name here to hide a service.
EXCLUDED_SERVICES: frozenset[str] = frozenset()

MAX_FETCH_BYTES = 25 * 1024 * 1024  # 25 MB cap on server-fetched files
MAX_INLINE_BYTES = 256 * 1024  # 256 KB cap on decoded inline content
FETCH_TIMEOUT_SECONDS = 30.0
FETCH_MAX_REDIRECTS = 5

HOSTED_PATH_GUIDANCE = (
    "The hosted Appwrite MCP server cannot read local file paths. For '{param}', pass a "
    'public URL as {{"url": "https://..."}} (preferred), or a small file inline as '
    '{{"filename": "...", "content": "<base64>", "encoding": "base64"}}.'
)

# --- auth -----------------------------------------------------------------

DEFAULT_PROJECT_ID = "console"

PREFERRED_SCOPES = [
    "openid",
    "profile",
    "email",
    "all",
]

DISCOVERY_TTL_SECONDS = 300.0

# --- http_app -------------------------------------------------------------

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id, Mcp-Protocol-Version",
    "Access-Control-Expose-Headers": "Mcp-Session-Id, WWW-Authenticate",
}

# --- operator -------------------------------------------------------------

SEARCH_LIMIT = 8
PREVIEW_THRESHOLD = 800
RESULT_STORE_SIZE = 50
CATALOG_URI = "appwrite://operator/catalog"
RESULT_URI_TEMPLATE = "appwrite://operator/results/{result_id}"
VERBS = {"list", "get", "create", "update", "delete"}
READ_VERBS = {"list", "get"}
CREATE_HINTS = {"add", "build", "create", "insert", "make", "new", "provision"}
UPDATE_HINTS = {"change", "edit", "modify", "rename", "set", "update"}
DELETE_HINTS = {"delete", "destroy", "drop", "remove"}
READ_HINTS = {"fetch", "find", "get", "list", "read", "search", "show", "view"}

# --- docs_search ----------------------------------------------------------

DOCS_TOOL_NAME = "appwrite_search_docs"
EMBED_MODEL = "text-embedding-3-small"
DOCS_DEFAULT_LIMIT = 5
DOCS_MAX_LIMIT = 10
DOCS_DEFAULT_MIN_SCORE = 0.25
DOCS_MIN_QUERY_LENGTH = 3

DATA_DIR = Path(__file__).parent / "data"
VECTORS_FILE = "docs_index.npz"
META_FILE = "docs_index_meta.json"

# --- context --------------------------------------------------------------

SERVICE_PROBES = {
    "tablesdb": {
        "path": "/tablesdb",
        "items_key": "databases",
        "model": Database,
    },
    "users": {
        "path": "/users",
        "items_key": "users",
        "model": User,
    },
    "storage": {
        "path": "/storage/buckets",
        "items_key": "buckets",
        "model": Bucket,
    },
    "functions": {
        "path": "/functions",
        "items_key": "functions",
        "model": Function,
    },
    "sites": {
        "path": "/sites",
        "items_key": "sites",
        "model": Site,
    },
    "messaging": {
        "path": "/messaging/messages",
        "items_key": "messages",
        "model": Message,
    },
    "teams": {
        "path": "/teams",
        "items_key": "teams",
        "model": Team,
    },
}

REDACTED_KEYS = {"password", "secret", "key", "token", "otp", "cookie", "session"}

# --- telemetry ------------------------------------------------------------

ACTIVE_WINDOW_SECONDS = 300.0  # rolling window for "active users/clients" gauges
