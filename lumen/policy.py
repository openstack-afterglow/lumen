"""Operator-owned oslo.policy rules shared by HTTP, key and worker admission."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from threading import RLock, get_ident

import yaml
from fastapi import HTTPException
from oslo_config import cfg
from oslo_policy import _checks, _parser, policy

_LOCK = RLock()


def _validate_native_rule_graph(enforcer: policy.Enforcer) -> None:
    """Validate parsed references, including oslo.policy's singular NotCheck child."""
    complete: set[str] = set()

    def visit_rule(name: str, active: frozenset[str]) -> None:
        if name not in enforcer.rules or name in active:
            raise ValueError("Undefined or cyclic native policy reference")
        if name in complete:
            return
        visit_check(enforcer.rules[name], active | {name})
        complete.add(name)

    def visit_check(check, active: frozenset[str]) -> None:
        if isinstance(check, _checks.RuleCheck):
            visit_rule(check.match, active)
        elif isinstance(check, _checks.NotCheck):
            visit_check(check.rule, active)
        else:
            for child in getattr(check, "rules", ()):
                visit_check(child, active)

    for name in enforcer.rules:
        visit_rule(name, frozenset())


class _ParserLog:
    """Observe native parse failures even when logging levels suppress output."""

    def __init__(self, logger):
        self.logger = logger
        self.thread = get_ident()
        self.failed = False

    def error(self, *args, **kwargs):
        if get_ident() == self.thread:
            self.failed = True
        return self.logger.error(*args, **kwargs)

    def exception(self, *args, **kwargs):
        if get_ident() == self.thread:
            self.failed = True
        return self.logger.exception(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.logger, name)


class _Enforcer(policy.Enforcer):
    _reload_failed = False

    def load_rules(self, force_reload: bool = False) -> None:
        # Enforcer.enforce reloads again internally. Every reload must validate
        # the exact parsed graph before evaluation, not only its caller's preflight.
        # Native parsing can report an error and return FalseCheck; `not` would
        # otherwise invert that failed parse into allow. Observe diagnostics,
        # not grammar. Missing configured policy directories fail closed;
        # an absent main file selects registered defaults per Lumen contract.
        with _LOCK:
            original = _parser.LOG
            monitor = _ParserLog(original)
            _parser.LOG = monitor
            try:
                for directory in self.conf.oslo_policy.policy_dirs:
                    if not Path(directory).is_dir():
                        raise FileNotFoundError("Configured policy directory is unavailable")
                super().load_rules(force_reload=force_reload or self._reload_failed)
                if monitor.failed:
                    raise ValueError("Native oslo.policy parser rejected an expression")
                self.check_rules(raise_on_violation=True)
                _validate_native_rule_graph(self)
                self._reload_failed = False
            except Exception:
                self._reload_failed = True
                self.clear()
                self.use_conf = True
                self.register_defaults(list_rules())
                raise
            finally:
                _parser.LOG = original

    def _load_policy_file(self, path: str, force_reload: bool, overwrite: bool = True) -> bool:
        # The real parser can substitute FalseCheck for an invalid clause;
        # `not` would turn that into allow. Observe its reports, not its grammar.
        with _LOCK:
            original = _parser.LOG
            monitor = _ParserLog(original)
            _parser.LOG = monitor
            try:
                changed = super()._load_policy_file(path, force_reload, overwrite=overwrite)
                if monitor.failed:
                    raise ValueError("Native oslo.policy parser rejected an expression")
                return changed
            finally:
                _parser.LOG = original

    def _record_file_rules(self, data: str, overwrite: bool = False) -> None:
        # oslo accepts falsey scalar YAML as an empty policy. Never turn an
        # explicitly malformed override into the more permissive defaults.
        rules = yaml.safe_load(data)
        if rules is None and any(line.strip() and not line.lstrip().startswith("#") for line in data.splitlines()):
            raise ValueError("Policy must be a YAML mapping, not null")
        if rules is not None and (
            not isinstance(rules, dict)
            or any(not isinstance(name, str) or not name or not isinstance(check, str) for name, check in rules.items())
        ):
            raise ValueError("Policy must map rule names to check strings")
        super()._record_file_rules(data, overwrite=overwrite)


def list_rules() -> list[policy.RuleDefault]:
    from lumen.service_authority import PROJECT_WRITE_ROLES, SCOPE_CAPABILITIES

    rules = []
    for action in SCOPE_CAPABILITIES:
        roles = sorted(PROJECT_WRITE_ROLES)
        if action.endswith(":read"):
            roles.append("reader")
        check = " or ".join(["is_system_admin:True", *(f"role:{role}" for role in roles)])
        rules.append(policy.RuleDefault(action, check, description=f"Lumen {action}; project/user ownership and credential ceilings are enforced separately."))
    rules.append(policy.RuleDefault("system:admin", "is_system_admin:True", description="Verified human system administrator only; independent hard boundary also enforced."))
    return rules


@lru_cache(maxsize=8)
def _enforcer(policy_file: str, policy_dirs: tuple[str, ...]) -> policy.Enforcer:
    conf = cfg.ConfigOpts()
    conf([], project="lumen", default_config_files=[])
    enforcer = _Enforcer(conf)
    conf.set_override("policy_file", policy_file, group="oslo_policy")
    conf.set_override("policy_dirs", list(policy_dirs), group="oslo_policy")
    enforcer.policy_file = policy_file
    enforcer.register_defaults(list_rules())
    return enforcer


def get_enforcer() -> policy.Enforcer:
    from lumen.config import get_settings

    settings = get_settings()
    return _enforcer(settings.oslo_policy_policy_file, tuple(settings.oslo_policy_policy_dirs))


def authorize(action: str, principal: dict) -> bool:
    """Unknown actions and malformed policies fail closed, never use stale allow."""
    if not principal.get("project_id") or not principal.get("user_id"):
        return False
    if action == "system:admin" and (
        not principal.get("is_system_admin") or principal.get("auth_type") == "api_key"
        or principal.get("credential_restricted")
    ):
        return False
    if principal.get("auth_type") == "api_key" and action not in principal.get("scopes", ()):
        return False
    if action != "system:admin":
        from lumen.service_authority import PROJECT_WRITE_ROLES, SCOPE_CAPABILITIES

        if action not in SCOPE_CAPABILITIES:
            return False
        roles = set(principal.get("roles", ()))
        if not (
            principal.get("is_system_admin") or principal.get("service_system_admin")
            or roles.intersection(PROJECT_WRITE_ROLES)
            or (action.endswith(":read") and "reader" in roles)
        ):
            return False
    credentials = {
        "roles": list(principal.get("roles", ())),
        "user_id": principal["user_id"], "project_id": principal["project_id"],
        "is_system_admin": bool(principal.get("is_system_admin") or principal.get("service_system_admin")),
    }
    target = {"user_id": principal["user_id"], "project_id": principal["project_id"]}
    with _LOCK:
        enforcer = get_enforcer()
        if action not in enforcer.registered_rules:
            return False
        # oslo.policy checks file/directory mtimes on every authorize. Parsing and
        # undefined/cyclic references must not fall back to registered allows.
        try:
            enforcer.load_rules()
            return bool(enforcer.authorize(action, target, credentials, do_raise=False))
        except Exception as exc:
            enforcer.clear()
            enforcer.use_conf = True
            enforcer.register_defaults(list_rules())
            raise HTTPException(status_code=503, detail="Current Lumen policy unavailable") from exc
