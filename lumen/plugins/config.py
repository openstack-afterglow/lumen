"""Operator-owned, exact distribution allowlists checked before importing code."""
from __future__ import annotations

from typing import Any

from lumen_plugin_api.contracts import PluginKind
from pydantic import BaseModel, ConfigDict, Field, model_validator


class PluginApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    distribution: str = Field(min_length=1, max_length=190)
    name: str = Field(min_length=1, max_length=190)
    version: str = Field(min_length=1, max_length=64)
    kind: PluginKind


def default_allowlist() -> list[PluginApproval]:
    return [PluginApproval(distribution=distribution, name=name, kind=kind, version="0.1.0") for kind, name, distribution in (
        ("database", "mariadb", "lumen-database-mariadb"),
        ("memory", "default-memory", "lumen-memory-default"),
        ("tools", "default-tools", "lumen-tools-default"),
        ("skills", "default-skills", "lumen-skills-default"),
        ("mcp", "remote-mcp", "lumen-mcp-default"),
        ("mcp", "afterglow-mcp", "lumen-mcp-default"),
    )]


class PluginRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allowlist: list[PluginApproval] = Field(default_factory=default_allowlist)
    database: str = "mariadb"
    memory: str = "default-memory"
    tools: tuple[str, ...] = ("default-tools",)
    skills: tuple[str, ...] = ("default-skills",)
    mcp: tuple[str, ...] = ("remote-mcp", "afterglow-mcp")
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict, repr=False)

    def selections(self) -> dict[str, tuple[str, ...]]:
        return {"database": (self.database,), "memory": (self.memory,), "tools": self.tools, "skills": self.skills, "mcp": self.mcp}

    @model_validator(mode="after")
    def validate_selection(self) -> PluginRuntimeConfig:
        approved = [(item.kind, item.name) for item in self.allowlist]
        if len(approved) != len(set(approved)):
            raise ValueError("duplicate plugin approval")
        names: set[str] = set()
        for kind, selected in self.selections().items():
            for name in selected:
                if not name or name in names:
                    raise ValueError("duplicate or empty selected plugin identity")
                if (kind, name) not in approved:
                    raise ValueError("selected plugin is not approved")
                names.add(name)
        if set(self.settings) - names:
            raise ValueError("configuration provided for unselected plugin")
        return self
