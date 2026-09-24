"""Load administrator-approved installed wheels once; fail closed on mismatches."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields
from importlib import metadata
from typing import Any

from jsonschema import Draft202012Validator
from lumen_plugin_api.contracts import Plugin, PluginError, PluginHost, PluginManifest

from .config import PluginRuntimeConfig


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str).encode()
    ).hexdigest()


def _distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def validate_configuration(schema: dict, value: dict) -> None:
    # Config validation never follows network references, even in installed schemas.
    def check_refs(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                    raise ValueError("external schema references are not permitted")
                check_refs(child)
        elif isinstance(node, list):
            for child in node:
                check_refs(child)
    check_refs(schema)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(value)


class PluginRegistry:
    def __init__(self, config: PluginRuntimeConfig) -> None:
        self.config = config
        self._plugins: dict[str, Plugin] = {}
        self._started: list[Plugin] = []
        self._hosts: dict[str, PluginHost] = {}
        self._loaded = False
        self.ready = False

    def load(self) -> None:
        if self._loaded:
            return
        selected = self.config.selections()
        approvals = {(item.kind, item.name): item for item in self.config.allowlist}
        entries = metadata.entry_points()
        plugins: dict[str, Plugin] = {}
        for kind, names in selected.items():
            for name in names:
                approval = approvals[kind, name]
                candidates = list(entries.select(group=f"lumen.{kind}", name=name))
                if len(candidates) != 1:
                    raise PluginError("plugin_unavailable", f"Required plugin {name} is missing or ambiguous")
                entry = candidates[0]
                dist = entry.dist
                if dist is None or _distribution_name(dist.metadata["Name"]) != _distribution_name(approval.distribution) or dist.version != approval.version:
                    raise PluginError("plugin_incompatible", f"Plugin {name} distribution is not approved")
                try:
                    instance = entry.load()()
                    manifest = PluginManifest.model_validate(instance.manifest)
                    validate_configuration(manifest.configuration_schema, self.config.settings.get(name, {}))
                except Exception as exc:
                    raise PluginError("plugin_incompatible", f"Plugin {name} contract is invalid") from exc
                if manifest.id != name or manifest.kind != kind or manifest.version != approval.version:
                    raise PluginError("plugin_incompatible", f"Plugin {name} metadata does not match approval")
                if name in plugins:
                    raise PluginError("plugin_incompatible", "Duplicate plugin identity")
                for capability in manifest.required_capabilities:
                    if capability not in {field.name for field in fields(PluginHost)} - {"configuration"}:
                        raise PluginError("plugin_incompatible", f"Plugin {name} requires an unknown capability")
                plugins[name] = instance
        self._plugins = plugins
        self._loaded = True

    def get(self, kind: str, plugin_id: str | None = None) -> Plugin:
        self.load()
        names = self.config.selections().get(kind, ())
        if plugin_id is None:
            if len(names) != 1:
                raise PluginError("plugin_unavailable", "Plugin identity is required for this kind")
            plugin_id = names[0]
        plugin = self._plugins.get(plugin_id)
        if plugin_id not in names or plugin is None:
            raise PluginError("plugin_unavailable")
        return plugin

    async def start(self, host: PluginHost) -> None:
        if self.ready:
            return
        self.load()
        try:
            for name, plugin in self._plugins.items():
                capabilities = {}
                for capability in plugin.manifest.required_capabilities:
                    value = getattr(host, capability, None)
                    if value is None:
                        raise PluginError("plugin_unavailable", f"Plugin {name} requires unavailable host capability")
                    capabilities[capability] = value
                scoped = PluginHost(configuration=self.config.settings.get(name, {}), **capabilities)
                # A start failure can still allocate resources; always close that instance too.
                self._hosts[name] = scoped
                self._started.append(plugin)
                await plugin.start(scoped)
        except BaseException:
            await self.close()
            raise
        self.ready = True

    async def close(self) -> None:
        self.ready = False
        self._hosts.clear()
        errors: list[Exception] = []
        while self._started:
            try:
                await self._started.pop().close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Plugin shutdown failed", errors)

    def status(self) -> list[dict[str, Any]]:
        self.load()
        return [{"manifest": item.manifest.model_dump(mode="json"), "ready": self.ready} for item in self._plugins.values()]

    def host(self, plugin_id: str) -> PluginHost:
        if not self.ready or plugin_id not in self._hosts:
            raise PluginError("plugin_unavailable", "Plugin lifecycle has not started")
        return self._hosts[plugin_id]

    @property
    def digest(self) -> str:
        self.load()
        return fingerprint([{ "manifest": plugin.manifest.model_dump(mode="json"), "config": self.config.settings.get(name, {})} for name, plugin in sorted(self._plugins.items())])


_registry: PluginRegistry | None = None


def get_registry() -> PluginRegistry:
    global _registry
    if _registry is None:
        from lumen.config import get_settings
        _registry = PluginRegistry(get_settings().plugin_config)
    return _registry


def get_plugin(kind: str, plugin_id: str | None = None) -> Plugin:
    return get_registry().get(kind, plugin_id)
