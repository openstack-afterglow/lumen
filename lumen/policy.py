"""Operator-owned oslo.policy rules shared by HTTP, key and worker admission."""
from __future__ import annotations

from functools import lru_cache
from threading import RLock, get_ident

import yaml
from fastapi import HTTPException
from oslo_config import cfg
from oslo_policy import _parser, policy

_LOCK = RLock()


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
    from lumen.service_authority import READER_CAPABILITIES, SCOPE_CAPABILITIES

    rules = []
    for action, leaves in SCOPE_CAPABILITIES.items():
        grades = ["lumen_admin"]
        if "lumen-resources_admin" not in leaves:
            grades.append("lumen_editor")
        if any(leaf.endswith("_user") for leaf in leaves):
            grades.append("lumen_user")
        if set(leaves).intersection(READER_CAPABILITIES):
            grades.extend(["lumen_user", "lumen_reader"])
        check = " or ".join(["is_system_admin:True", "personal_project_owner:True", "role:admin"] + [f"role:{role}" for role in grades] + [f"role:{leaf}" for leaf in leaves])
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
    credentials = {
        "roles": list(principal.get("roles", ())),
        "user_id": principal["user_id"], "project_id": principal["project_id"],
        "is_system_admin": bool(principal.get("is_system_admin") or principal.get("service_system_admin")),
        "personal_project_owner": principal.get("personal_project_owner") is True and not principal.get("credential_restricted"),
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
            enforcer.check_rules(raise_on_violation=True)
            return bool(enforcer.authorize(action, target, credentials, do_raise=False))
        except Exception as exc:
            enforcer.clear()
            enforcer.use_conf = True
            enforcer.register_defaults(list_rules())
            raise HTTPException(status_code=503, detail="Current Lumen policy unavailable") from exc
