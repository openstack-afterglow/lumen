"""Conformance for default-skills against a concrete scoped extension host."""

from copy import deepcopy

import pytest
from lumen_plugin_api.contracts import Namespace, PluginError, PluginHost
from lumen_plugin_api.skills import SkillAccess, SkillRef, SkillSnapshot
from lumen_plugin_api.testing import check_lifecycle, check_manifest, check_skill_provider
from lumen_skills_default import create_plugin

_BINDING_ID = "2a2b2c2d-2222-4222-8222-22222222abcd"
_NAMESPACE = Namespace(user_id="u1", project_id="p1")


class FakeSkillExtensions:
    """Only caller-visible, active skill records are returned to the provider."""

    def __init__(self):
        self.items = {
            7: {"id": 7, "scope": "user", "name": "Database skill", "description": None,
                "instructions": "Review carefully", "is_active": True},
            _BINDING_ID: {"id": _BINDING_ID, "kind": "skill", "plugin_id": "default-skills",
                          "export_key": "default", "name": "Installed skill",
                          "config": {"name": "Config label", "instructions": "Summarize briefly"},
                          "config_version": 1, "is_active": True},
        }
        self.calls = []

    async def resolve(self, kind, identifier, namespace):
        self.calls.append((kind, identifier, namespace))
        assert kind == "skill"
        assert namespace == _NAMESPACE
        item = self.items.get(identifier)
        if item is None or not item["is_active"]:
            raise PluginError("plugin_authority_revoked", "skill is unavailable")
        return deepcopy(item)


def _access(host):
    return SkillAccess(namespace=_NAMESPACE, extensions=host)


async def test_public_skill_provider_conformance():
    host = FakeSkillExtensions()
    plugin = create_plugin()
    assert check_manifest(plugin, expected_kind="skills").id == "default-skills"
    await check_lifecycle(create_plugin, PluginHost(configuration={}))
    refs = (SkillRef(database_id=7), SkillRef(binding_id=_BINDING_ID))
    snapshots = await check_skill_provider(plugin, refs, _access(host))
    assert [snapshot.instruction for snapshot in snapshots] == ["Review carefully", "Summarize briefly"]


def test_skill_refs_require_one_valid_identity():
    with pytest.raises(ValueError):
        SkillRef()
    with pytest.raises(ValueError):
        SkillRef(database_id=7, binding_id=_BINDING_ID)
    with pytest.raises(ValueError):
        SkillRef(database_id=0)
    with pytest.raises(ValueError):
        SkillRef(binding_id="not-a-uuid")
    with pytest.raises(ValueError):
        SkillRef(binding_id=_BINDING_ID.upper())
    assert SkillRef(binding_id=_BINDING_ID).binding_id == _BINDING_ID


async def test_resolves_both_ref_shapes_through_scoped_extensions():
    host = FakeSkillExtensions()
    plugin = create_plugin()
    refs = (SkillRef(database_id=7), SkillRef(binding_id=_BINDING_ID))

    snapshots = await plugin.resolve(refs, _access(host))

    assert host.calls == [("skill", 7, _NAMESPACE), ("skill", _BINDING_ID, _NAMESPACE)]
    assert tuple(snapshot.reference for snapshot in snapshots) == refs
    assert [snapshot.instruction for snapshot in snapshots] == ["Review carefully", "Summarize briefly"]
    assert [snapshot.name for snapshot in snapshots] == ["Database skill", "Installed skill"]
    assert all(snapshot.identity.plugin_id == "default-skills" for snapshot in snapshots)
    assert all(snapshot.resources == () for snapshot in snapshots)
    assert [SkillSnapshot.model_validate(snapshot.model_dump(mode="json")).name for snapshot in snapshots] == [
        "Database skill", "Installed skill"
    ]
    # An older frozen payload without the optional display label remains readable.
    old_payload = snapshots[0].model_dump(mode="json")
    del old_payload["name"]
    assert SkillSnapshot.model_validate(old_payload).name == ""


@pytest.mark.parametrize("identifier", [7, _BINDING_ID])
async def test_revalidate_unchanged_and_rejects_changed_names(identifier):
    host = FakeSkillExtensions()
    plugin = create_plugin()
    ref = SkillRef(database_id=identifier) if isinstance(identifier, int) else SkillRef(binding_id=identifier)
    (snapshot,) = await plugin.resolve((ref,), _access(host))

    await plugin.revalidate(snapshot, _access(host))
    host.items[identifier]["name"] = "Renamed skill"
    with pytest.raises(PluginError) as exc:
        await plugin.revalidate(snapshot, _access(host))
    assert exc.value.code == "plugin_configuration_changed"
    assert host.calls == [("skill", identifier, _NAMESPACE)] * 3


@pytest.mark.parametrize("identifier", [7, _BINDING_ID])
async def test_revalidate_rejects_instruction_changes_and_revocation(identifier):
    host = FakeSkillExtensions()
    plugin = create_plugin()
    ref = SkillRef(database_id=identifier) if isinstance(identifier, int) else SkillRef(binding_id=identifier)
    (snapshot,) = await plugin.resolve((ref,), _access(host))

    if isinstance(identifier, int):
        host.items[identifier]["instructions"] = "New instructions"
    else:
        host.items[identifier]["config"]["instructions"] = "New instructions"
        host.items[identifier]["config_version"] = 2
    with pytest.raises(PluginError) as exc:
        await plugin.revalidate(snapshot, _access(host))
    assert exc.value.code == "plugin_configuration_changed"

    host.items[identifier]["is_active"] = False
    with pytest.raises(PluginError) as exc:
        await plugin.revalidate(snapshot, _access(host))
    assert exc.value.code == "plugin_authority_revoked"


async def test_missing_and_unusable_skill_cannot_be_frozen():
    host = FakeSkillExtensions()
    plugin = create_plugin()
    with pytest.raises(PluginError) as exc:
        await plugin.resolve((SkillRef(database_id=999),), _access(host))
    assert exc.value.code == "plugin_authority_revoked"

    host.items[7]["instructions"] = "  "
    with pytest.raises(PluginError) as exc:
        await plugin.resolve((SkillRef(database_id=7),), _access(host))
    assert exc.value.code == "plugin_unavailable"
