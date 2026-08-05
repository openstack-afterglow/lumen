import pytest

from lumen.services.extension_packages import ExtensionPackageValidationError, manifest_hash, validate_manifest


def _manifest(**overrides):
    value = {
        "schema_version": 1,
        "name": "review-kit",
        "version": "1.2.3",
        "description": "Project-safe review workflow",
        "components": {
            "skills": [{"ref": "review-skill", "instructions": "Review the selected code."}],
            "commands": [{"ref": "review", "template": "Review: {{args}}"}],
        },
    }
    value.update(overrides)
    return value


def test_package_manifest_is_strict_and_hashes_canonical_content():
    manifest = validate_manifest(_manifest())

    assert manifest["components"]["agents"] == []
    assert manifest_hash(_manifest()) == manifest_hash(manifest)


@pytest.mark.parametrize(
    "manifest",
    [
        _manifest(extra=True),
        _manifest(components={"skills": [{"ref": "skill", "hook": "curl evil"}]}),
        _manifest(components={"mcp_servers": [{"ref": "local", "url": "stdio://worker"}]}),
        _manifest(components={"skills": [{"ref": "skill", "token": "secret"}]}),
    ],
)
def test_package_manifest_rejects_unknown_executable_and_secret_content(manifest):
    with pytest.raises(ExtensionPackageValidationError):
        validate_manifest(manifest)
