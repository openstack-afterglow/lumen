"""Persisted personal scope; an unresolved row belongs only to its origin project."""

from collections.abc import Mapping

from sqlalchemy import and_, false, or_


def history_domain_id(principal: Mapping | None) -> str | None:
    """Only the unrestricted Keystone path supplies verified domain authority."""
    if not principal or principal.get("credential_restricted"):
        return None
    if principal.get("credential_class", principal.get("auth_type")) != "keystone":
        return None
    domain_id = principal.get("domain_id")
    return domain_id if isinstance(domain_id, str) and domain_id else None


def scope_clause(domain_column, project_column, *, domain_id: str | None, project_id: str | None):
    if not project_id:
        return false()
    legacy = and_(domain_column.is_(None), project_column == project_id)
    return or_(domain_column == domain_id, legacy) if domain_id else legacy


def scope_matches(
    row_domain: str | None, row_project: str | None, *, domain_id: str | None, project_id: str | None,
) -> bool:
    return bool(project_id) and (row_domain == domain_id if row_domain is not None else row_project == project_id)


def personal_mcp_scope_key(user_id: str, project_id: str, domain_id: str | None) -> str:
    return f"user:{user_id}:domain:{domain_id}" if domain_id else f"user:{user_id}:{project_id}"
