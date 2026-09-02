"""Unit tests for test_layers CLI orchestration logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from lumen.scripts.test_layers import run_contract, run_integration, run_system


@patch("subprocess.run")
def test_run_contract_success(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    code = run_contract()
    assert code == 0
    assert mock_run.call_count == 4

    calls = mock_run.call_args_list
    assert calls[0][0][0] == ["pytest", "-m", "not integration and not system", "tests"]
    assert calls[1][0][0] == ["ruff", "check", "lumen", "tests"]
    assert calls[2][0][0] == ["pytest", "sdk/tests"]
    assert calls[3][0][0] == ["ruff", "check", "sdk"]


@patch("subprocess.run")
def test_run_contract_early_failure(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=1)

    code = run_contract()
    assert code == 1
    assert mock_run.call_count == 1
    assert mock_run.call_args_list[0][0][0] == ["pytest", "-m", "not integration and not system", "tests"]


@patch("subprocess.run")
def test_run_integration_success(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    code = run_integration()
    assert code == 0

    calls = mock_run.call_args_list
    assert len(calls) == 4
    assert calls[0][0][0] == [
        "docker", "compose", "-f", "docker-compose.system.yml",
        "up", "-d", "--wait", "--wait-timeout", "120", "mariadb", "redis",
    ]
    assert calls[1][0][0] == ["lumen-migrate", "--apply"]
    assert calls[2][0][0] == ["pytest", "-m", "integration"]
    assert calls[3][0][0] == ["docker", "compose", "-f", "docker-compose.system.yml", "down", "-v", "--remove-orphans"]
    projects = {call.kwargs["env"]["COMPOSE_PROJECT_NAME"] for call in calls}
    assert len(projects) == 1
    assert next(iter(projects)).startswith("lumen-integration-")


@patch("subprocess.run")
def test_run_integration_cleanup_on_failure(mock_run: MagicMock) -> None:
    # Fail on pytest (3rd call)
    mock_run.side_effect = [
        MagicMock(returncode=0),  # up
        MagicMock(returncode=0),  # migrate
        MagicMock(returncode=1),  # pytest
        MagicMock(returncode=0),  # down
    ]

    code = run_integration()
    assert code == 1

    calls = mock_run.call_args_list
    assert len(calls) == 4
    assert calls[3][0][0] == ["docker", "compose", "-f", "docker-compose.system.yml", "down", "-v", "--remove-orphans"]


@patch("subprocess.run")
def test_run_system_success(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0)

    code = run_system(["-k", "process_stack"])
    assert code == 0

    calls = mock_run.call_args_list
    assert len(calls) == 4
    compose = ["docker", "compose", "-f", "docker-compose.system.yml"]
    assert calls[0][0][0] == [*compose, "build", "system-tests"]
    assert calls[1][0][0] == [
        *compose, "up", "-d", "--build", "--wait", "--wait-timeout", "180", "lumen-api", "lumen-worker",
    ]
    assert calls[2][0][0] == [*compose, "run", "--rm", "--no-deps", "system-tests"]
    assert calls[3][0][0] == [*compose, "down", "-v", "--remove-orphans"]
    projects = {call.kwargs["env"]["COMPOSE_PROJECT_NAME"] for call in calls}
    assert len(projects) == 1
    assert next(iter(projects)).startswith("lumen-system-")
    assert calls[2].kwargs["env"]["SYSTEM_PYTEST_ADDOPTS"] == "-k process_stack"


@patch("subprocess.run")
def test_run_system_logs_and_cleanup_on_failure(mock_run: MagicMock) -> None:
    mock_run.side_effect = [
        MagicMock(returncode=1),  # up fails
        MagicMock(returncode=0),  # logs
        MagicMock(returncode=0),  # down
    ]

    code = run_system()
    assert code == 1

    calls = mock_run.call_args_list
    assert len(calls) == 3
    assert calls[1][0][0] == ["docker", "compose", "-f", "docker-compose.system.yml", "logs"]
    assert calls[2][0][0] == ["docker", "compose", "-f", "docker-compose.system.yml", "down", "-v", "--remove-orphans"]
