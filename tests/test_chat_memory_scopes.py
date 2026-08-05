import pytest

from lumen.services import memory_store as ms


@pytest.mark.parametrize(
    ("scope", "project_id", "workspace_id"),
    [
        ("account", None, None),
        ("project", "project-a", None),
        ("workspace", "project-a", 7),
    ],
)
def test_valid_memory_namespace(scope, project_id, workspace_id):
    ms._validate_namespace(scope=scope, project_id=project_id, workspace_id=workspace_id)


@pytest.mark.parametrize(
    ("scope", "project_id", "workspace_id"),
    [
        ("account", "project-a", None),
        ("account", None, 7),
        ("project", None, None),
        ("project", "project-a", 7),
        ("workspace", "project-a", None),
        ("workspace", None, 7),
        ("unknown", None, None),
    ],
)
def test_invalid_memory_namespace_is_rejected(scope, project_id, workspace_id):
    with pytest.raises(ms.MemoryValidationError):
        ms._validate_namespace(scope=scope, project_id=project_id, workspace_id=workspace_id)
