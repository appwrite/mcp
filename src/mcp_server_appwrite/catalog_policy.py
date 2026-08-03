"""Catalog profiles and target-context metadata for Appwrite SDK services.

The console SDK contains both project APIs and console control-plane APIs. Hosted
OAuth sessions may use the complete catalog, while local API-key sessions must
only advertise endpoints that accept project API keys. Keeping this policy in one
module makes that trust boundary explicit and independently testable.
"""

from __future__ import annotations

from typing import Literal

CatalogProfile = Literal["oauth", "api_key"]
ContextScope = Literal["console", "organization", "project"]

OAUTH_PROFILE: CatalogProfile = "oauth"
API_KEY_PROFILE: CatalogProfile = "api_key"

# Services present in the former server SDK, plus the server-capable document and
# vector APIs introduced by appwrite-console. Any future console SDK service stays
# hidden from API-key mode until it is deliberately reviewed and added here.
API_KEY_SERVICES: frozenset[str] = frozenset(
    {
        "account",
        "activities",
        "advisor",
        "apps",
        "avatars",
        "backups",
        "databases",
        "documents_db",
        "embeddings",
        "functions",
        "graphql",
        "locale",
        "messaging",
        "oauth2",
        "organization",
        "presences",
        "project",
        "proxy",
        "sites",
        "storage",
        "tables_db",
        "teams",
        "tokens",
        "users",
        "vectors_db",
        "webhooks",
    }
)

# Methods added to existing service modules by the console SDK that are not
# available to project API keys. The documents/vectors exclusions are console
# administration operations; their remaining methods are server endpoints.
API_KEY_EXCLUDED_METHODS: dict[str, frozenset[str]] = {
    "account": frozenset(
        {
            "create_billing_address",
            "create_key",
            "create_o_auth2_session",
            "create_payment_method",
            "create_push_target",
            "delete",
            "delete_billing_address",
            "delete_key",
            "delete_payment_method",
            "delete_push_target",
            "get_billing_address",
            "get_coupon",
            "get_key",
            "get_payment_method",
            "list_billing_addresses",
            "list_invoices",
            "list_keys",
            "list_payment_methods",
            "update_billing_address",
            "update_key",
            "update_payment_method",
            "update_payment_method_mandate_options",
            "update_payment_method_provider",
            "update_push_target",
        }
    ),
    "apps": frozenset({"delete_installation"}),
    "documents_db": frozenset(
        {
            "create_documents",
            "create_failover",
            "get_replicas",
            "get_status",
            "list_operations",
            "list_specifications",
        }
    ),
    "functions": frozenset({"get_template", "list_templates"}),
    "oauth2": frozenset({"logout", "logout_post"}),
    "presences": frozenset({"get_usage"}),
    "project": frozenset({"get_usage"}),
    "sites": frozenset({"get_template", "list_templates"}),
    "tables_db": frozenset(
        {
            "create_migration",
            "delete_migration",
            "get_migration",
            "list_migrations",
            "list_operations",
        }
    ),
    "teams": frozenset({"list_logs"}),
    "users": frozenset({"get_usage"}),
    "vectors_db": frozenset(
        {
            "create_documents",
            "create_failover",
            "create_query",
            "get_replicas",
            "get_status",
            "list_operations",
            "list_specifications",
        }
    ),
}

# Target metadata is surfaced in search results and enforced by hosted OAuth
# calls. API-key mode already has a fixed project on its configured client.
PROJECT_CONTEXT_SERVICES: frozenset[str] = frozenset(
    {
        "databases",
        "documents_db",
        "embeddings",
        "functions",
        "messaging",
        "migrations",
        "mongo",
        "mysql",
        "postgresql",
        "sites",
        "storage",
        "tables_db",
        "teams",
        "usage",
        "users",
        "vcs",
        "vectors_db",
        "waf",
    }
)
ORGANIZATION_CONTEXT_SERVICES: frozenset[str] = frozenset({"domains"})


def method_allowed(
    profile: CatalogProfile, service_name: str, method_name: str
) -> bool:
    """Return whether a method belongs in the selected authentication profile."""
    if profile == OAUTH_PROFILE:
        return True
    if service_name not in API_KEY_SERVICES:
        return False
    return method_name not in API_KEY_EXCLUDED_METHODS.get(service_name, ())


def context_scope(service_name: str) -> ContextScope:
    """Return the target context a hosted OAuth call must provide."""
    if service_name in PROJECT_CONTEXT_SERVICES:
        return "project"
    if service_name in ORGANIZATION_CONTEXT_SERVICES:
        return "organization"
    return "console"
