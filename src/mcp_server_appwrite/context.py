from __future__ import annotations

from collections.abc import Callable
from typing import Any

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.models.project import Project
from appwrite.models.team import Team
from appwrite.models.user import User
from appwrite.query import Query

from .constants import REDACTED_KEYS, SERVICE_PROBES

ContextClientFactory = Callable[[str | None, str | None], Client]
DEFAULT_SERVICE_PROJECT_LIMIT = 5
DEFAULT_SERVICE_DETAIL = "totals"
SERVICE_DETAILS = {"totals", "samples"}


def get_appwrite_context(
    base_client: Client,
    *,
    mode: str,
    client_factory: ContextClientFactory | None = None,
    project_id: str | None = None,
    organization_id: str | None = None,
    include_services: bool = True,
    sample_limit: int = 5,
    service_detail: str = DEFAULT_SERVICE_DETAIL,
) -> dict[str, Any]:
    sample_limit = _normalize_sample_limit(sample_limit)
    service_detail = _normalize_service_detail(service_detail)
    client_factory = client_factory or (
        lambda _project_id, _organization_id: base_client
    )

    context: dict[str, Any] = {
        "connection": {
            "mode": mode,
            "endpoint": getattr(base_client, "_endpoint", ""),
        },
        "projects": [],
    }

    if mode == "api_key_project":
        configured_project_id = project_id or base_client.get_config("project")
        context["connection"]["projectId"] = configured_project_id
        project_client = client_factory(configured_project_id, None)
        project = _get_current_project(project_client, configured_project_id)
        if project is None:
            project = {"$id": configured_project_id}
        context["projects"] = [
            _project_context(
                project,
                project_client,
                include_services=include_services,
                sample_limit=sample_limit,
                service_detail=service_detail,
            )
        ]
        context["serviceSummary"] = _service_summary_metadata(
            include_services=include_services,
            effective_include_services=include_services,
            service_detail=service_detail,
            project_count=1,
        )
        return context

    console_client = client_factory(None, None)
    account = _safe_call(console_client, "get", "/account")
    if account.ok and isinstance(account.value, dict):
        context["account"] = _compact_document(_model_dict(account.value, User))
    elif not account.ok:
        context["account"] = {"error": account.error}

    organizations = _list_organizations(console_client, organization_id)
    context["organizations"] = organizations

    project_candidates: list[tuple[dict[str, Any], str | None]] = []
    if organization_id and not organizations:
        organizations = [{"$id": organization_id}]

    for organization in organizations:
        org_id = organization.get("$id")
        if not isinstance(org_id, str) or not org_id:
            continue
        org_client = client_factory(None, org_id)
        org_projects = _list_projects_for_organization(org_client)
        for project in org_projects:
            if not isinstance(project, dict):
                continue
            discovered_project_id = project.get("$id")
            if project_id and discovered_project_id != project_id:
                continue
            project_candidates.append((project, org_id))

    if not project_candidates:
        # Per-organization project listings need organization-tier scopes; a
        # grant narrowed to project scopes only still enumerates its bound
        # projects through the accessible-resources endpoint.
        for accessible in _accessible_resource_ids(console_client, "projects"):
            accessible_id = accessible.get("$id")
            if not isinstance(accessible_id, str) or not accessible_id:
                continue
            if project_id and accessible_id != project_id:
                continue
            project_client = client_factory(accessible_id, organization_id)
            project = _get_current_project(project_client, accessible_id) or {
                "$id": accessible_id
            }
            project_candidates.append((project, organization_id))

    if project_id and not project_candidates:
        project_candidates.append(({"$id": project_id}, organization_id))

    effective_include_services = _should_include_services(
        include_services=include_services,
        project_id=project_id,
        project_count=len(project_candidates),
    )
    projects: list[dict[str, Any]] = []
    for project, project_organization_id in project_candidates:
        discovered_project_id = project.get("$id")
        needs_project_client = effective_include_services or (
            project_id is not None and project == {"$id": project_id}
        )
        project_client = (
            client_factory(
                (
                    discovered_project_id
                    if isinstance(discovered_project_id, str)
                    else None
                ),
                project_organization_id,
            )
            if needs_project_client
            else console_client
        )
        if project_id and project == {"$id": project_id}:
            project = _get_current_project(project_client, project_id) or project
        projects.append(
            _project_context(
                project,
                project_client,
                organization_id=project_organization_id,
                include_services=effective_include_services,
                sample_limit=sample_limit,
                service_detail=service_detail,
            )
        )

    context["projects"] = projects
    context["serviceSummary"] = _service_summary_metadata(
        include_services=include_services,
        effective_include_services=effective_include_services,
        service_detail=service_detail,
        project_count=len(projects),
    )
    return context


class _CallResult:
    def __init__(self, ok: bool, value: Any = None, error: str | None = None) -> None:
        self.ok = ok
        self.value = value
        self.error = error


def _safe_call(
    client: Client, method: str, path: str, params: dict[str, Any] | None = None
) -> _CallResult:
    try:
        headers = {"accept": "application/json"}
        project_id = client.get_config("project")
        if project_id:
            headers["X-Appwrite-Project"] = project_id
        return _CallResult(
            True, client.call(method, path, headers=headers, params=params or {})
        )
    except AppwriteException as exc:
        details = []
        if getattr(exc, "code", None):
            details.append(f"code={exc.code}")
        if getattr(exc, "type", None):
            details.append(f"type={exc.type}")
        suffix = f" ({', '.join(details)})" if details else ""
        return _CallResult(False, error=f"{exc}{suffix}")
    except Exception as exc:
        return _CallResult(False, error=str(exc))


def _get_current_project(client: Client, project_id: str) -> dict[str, Any] | None:
    result = _safe_call(
        client,
        "get",
        "/project",
        params={},
    )
    if result.ok and isinstance(result.value, dict):
        return result.value
    return {"$id": project_id, "error": result.error} if not result.ok else None


def _accessible_resource_ids(client: Client, resource: str) -> list[dict[str, Any]]:
    """Fallback discovery via the OAuth2 accessible-resources endpoints
    (``/oauth2/<project>/organizations`` / ``/oauth2/<project>/projects``).
    Their scopes are granted on every token, so a consent-narrowed grant (no
    console-wide ``all``) can still enumerate exactly the resources it is
    bound to. Returns id-only documents."""
    project_id = client.get_config("project") or ""
    if not project_id:
        return []
    result = _safe_call(client, "get", f"/oauth2/{project_id}/{resource}")
    if not result.ok or not isinstance(result.value, dict):
        return []
    items = result.value.get(resource)
    if not isinstance(items, list):
        return []
    return [
        {"$id": item["$id"]}
        for item in items
        if isinstance(item, dict) and isinstance(item.get("$id"), str)
    ]


def _list_organizations(
    client: Client, organization_id: str | None
) -> list[dict[str, Any]]:
    result = _safe_call(client, "get", "/organizations")
    if not result.ok or not isinstance(result.value, dict):
        # The console-wide organizations listing needs scopes only the `all`
        # grant carries; a narrowed grant falls back to the accessible-
        # resources enumeration (ids only, names resolved by later calls).
        accessible = _accessible_resource_ids(client, "organizations")
        if accessible:
            if organization_id:
                return [
                    organization
                    for organization in accessible
                    if organization.get("$id") == organization_id
                ]
            return accessible
        return (
            [{"$id": organization_id, "error": result.error}] if organization_id else []
        )

    teams = result.value.get("teams")
    if not isinstance(teams, list):
        return []
    organizations = [
        _compact_document(_model_dict(team, Team))
        for team in teams
        if isinstance(team, dict)
    ]
    if organization_id:
        organizations = [
            organization
            for organization in organizations
            if organization.get("$id") == organization_id
        ]
    return organizations


def _list_projects_for_organization(client: Client) -> list[dict[str, Any]]:
    result = _safe_call(
        client,
        "get",
        "/organization/projects",
        {"queries": [Query.limit(100)]},
    )
    if not result.ok or not isinstance(result.value, dict):
        return []
    projects = result.value.get("projects")
    return projects if isinstance(projects, list) else []


def _project_context(
    project: dict[str, Any],
    client: Client,
    *,
    organization_id: str | None = None,
    include_services: bool,
    sample_limit: int,
    service_detail: str,
) -> dict[str, Any]:
    project_summary = _compact_document(_model_dict(project, Project))
    if organization_id:
        project_summary["organizationId"] = organization_id
    if "error" in project:
        project_summary["error"] = project["error"]
    if include_services:
        project_summary["services"] = _summarize_services(
            client, sample_limit, service_detail
        )
    return project_summary


def _summarize_services(
    client: Client, sample_limit: int, service_detail: str
) -> dict[str, Any]:
    services: dict[str, Any] = {}
    query_limit = 1 if service_detail == "totals" else sample_limit
    params = {"queries": [Query.limit(query_limit)]}
    for service_name, probe in SERVICE_PROBES.items():
        result = _safe_call(client, "get", str(probe["path"]), params)
        if not result.ok:
            services[service_name] = {"error": result.error}
            continue
        payload = result.value if isinstance(result.value, dict) else {}
        items = payload.get(str(probe["items_key"]), [])
        if not isinstance(items, list):
            items = []
        summary: dict[str, Any] = {"total": payload.get("total", len(items))}
        if service_detail == "samples":
            summary["items"] = [
                _compact_document(_model_dict(item, probe["model"]))
                for item in items
                if isinstance(item, dict)
            ]
        services[service_name] = summary
    return services


def _should_include_services(
    *, include_services: bool, project_id: str | None, project_count: int
) -> bool:
    if not include_services:
        return False
    return project_id is not None or project_count <= DEFAULT_SERVICE_PROJECT_LIMIT


def _service_summary_metadata(
    *,
    include_services: bool,
    effective_include_services: bool,
    service_detail: str,
    project_count: int,
) -> dict[str, Any]:
    if effective_include_services:
        return {
            "included": True,
            "detail": service_detail,
            "projectCount": project_count,
        }
    if not include_services:
        return {
            "included": False,
            "reason": "disabled",
            "projectCount": project_count,
        }
    return {
        "included": False,
        "reason": "project_count_exceeds_limit",
        "projectCount": project_count,
        "maxProjects": DEFAULT_SERVICE_PROJECT_LIMIT,
        "detail": service_detail,
        "hint": "Pass project_id to inspect services for a specific project.",
    }


def _model_dict(source: dict[str, Any], model_type: type) -> dict[str, Any]:
    try:
        if model_type is User:
            model = User.with_data(source)
        elif model_type is Team:
            model = Team.with_data(source)
        else:
            model = model_type.from_dict(source)
        if model is not None and hasattr(model, "to_dict"):
            return model.to_dict()
    except Exception:
        pass
    return source


def _compact_document(source: dict[str, Any]) -> dict[str, Any]:
    candidates: dict[str, Any] = {}
    for key, value in source.items():
        if _is_sensitive_key(key):
            continue
        if value is None or value == "":
            continue
        if not isinstance(value, bool) and value == 0:
            continue
        if isinstance(value, (str, int, float, bool)):
            candidates[key] = value
        elif key in {"prefs"} and isinstance(value, dict):
            candidates[key] = value

    compact: dict[str, Any] = {}
    for key in sorted(candidates, key=_summary_key_rank):
        compact[key] = candidates[key]
        if len(compact) >= 12:
            break
    return compact


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(marker in normalized for marker in REDACTED_KEYS)


def _summary_key_rank(key: str) -> tuple[int, str]:
    normalized = key.lower().replace("$", "")
    if normalized in {"id", "name", "email", "status", "region", "total"}:
        return (0, key)
    if normalized.endswith("id"):
        return (1, key)
    if normalized in {"createdat", "updatedat", "accessedat"}:
        return (2, key)
    if normalized in {"enabled", "runtime", "framework", "type"}:
        return (3, key)
    return (4, key)


def _normalize_sample_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(limit, 25))


def _normalize_service_detail(value: Any) -> str:
    if isinstance(value, str) and value in SERVICE_DETAILS:
        return value
    return DEFAULT_SERVICE_DETAIL
