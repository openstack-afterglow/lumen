"""Validation primitives for project-scoped remote code workspaces.

Persistence/provisioning is intentionally separate from this module so every API,
worker, and package path shares the same Git-origin contract.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from lumen.services import ssrf


class CodeWorkspaceValidationError(ValueError):
    pass


def normalize_git_host(host: str) -> str:
    if not isinstance(host, str) or not (value := host.strip().rstrip(".")):
        raise CodeWorkspaceValidationError("Git host is required")
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise CodeWorkspaceValidationError("Git host is invalid")
    try:
        return value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise CodeWorkspaceValidationError("Git host is invalid") from exc


def normalize_repository_url(value: str, *, allowed_domains: tuple[str, ...] = ()) -> tuple[str, str]:
    """Accept only credential-free HTTPS Git origins and return ``(url, host)``."""
    if not isinstance(value, str) or not (raw := value.strip()):
        raise CodeWorkspaceValidationError("repository URL is required")
    if any(char.isspace() or ord(char) < 32 for char in raw):
        raise CodeWorkspaceValidationError("repository URL is invalid")
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CodeWorkspaceValidationError("repository URL port is invalid") from exc
    try:
        safe_host, _port, scheme = ssrf._parse_and_validate_url(raw, allowed_domains=allowed_domains)
    except ssrf.SsrfBlocked as exc:
        raise CodeWorkspaceValidationError("repository URL origin is not allowed") from exc
    if scheme != "https" or parsed.query or parsed.fragment or port not in (None, 443) or not parsed.path:
        raise CodeWorkspaceValidationError("repository URL must be credential-free HTTPS")
    host = normalize_git_host(safe_host)
    path = parsed.path.rstrip("/")
    if not path or path in {"/", "/."}:
        raise CodeWorkspaceValidationError("repository URL path is required")
    return urlunsplit(("https", host, path, "", "")), host


def validate_workspace_source(
    *,
    source_kind: str,
    repository_url: str | None,
    source_revision: str | None,
    credential_host: str | None,
    allowed_domains: tuple[str, ...] = (),
) -> tuple[str | None, str | None]:
    """Validate empty/Git source combinations before any runtime or DB side effect."""
    if source_kind == "empty":
        if repository_url is not None or source_revision is not None or credential_host is not None:
            raise CodeWorkspaceValidationError("empty workspace cannot select repository or credential")
        return None, None
    if source_kind != "git":
        raise CodeWorkspaceValidationError("workspace source_kind is invalid")
    normalized_url, host = normalize_repository_url(repository_url or "", allowed_domains=allowed_domains)
    if credential_host is not None and normalize_git_host(credential_host) != host:
        raise CodeWorkspaceValidationError("Git credential host does not match repository origin")
    if source_revision is not None and (not source_revision.strip() or len(source_revision) > 255):
        raise CodeWorkspaceValidationError("source revision is invalid")
    return normalized_url, host
