"""Layered test environment CLI (contract, integration, system)."""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import socket
import subprocess
import sys


def _run_cmd(cmd: list[str], env: dict[str, str] | None = None) -> int:
    """Run subprocess command and return exit code."""
    res = subprocess.run(cmd, env=env)
    return res.returncode


def _free_loopback_port() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return str(listener.getsockname()[1])


def _compose_env(layer: str) -> dict[str, str]:
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = (
        os.environ.get("LUMEN_TEST_COMPOSE_PROJECT")
        or f"lumen-{layer}-{os.getpid()}-{secrets.token_hex(4)}"
    )
    env["MARIADB_PORT"] = os.environ.get("MARIADB_PORT") or _free_loopback_port()
    env["REDIS_PORT"] = os.environ.get("REDIS_PORT") or _free_loopback_port()
    return env




def run_contract(extra_args: list[str] | None = None) -> int:
    """Run contract layer: root service tests (not integration and not system), root ruff, SDK pytest, SDK ruff."""
    extra = extra_args or []

    # 1. Root non-integration/non-system pytest
    pytest_cmd = ["pytest", "-m", "not integration and not system", "tests"] + extra
    code = _run_cmd(pytest_cmd)
    if code != 0:
        return code

    # 2. Root ruff check
    code = _run_cmd(["ruff", "check", "lumen", "tests"])
    if code != 0:
        return code

    # 3. SDK pytest
    code = _run_cmd(["pytest", "sdk/tests"])
    if code != 0:
        return code

    # 4. SDK ruff check
    code = _run_cmd(["ruff", "check", "sdk"])
    return code


def run_integration(extra_args: list[str] | None = None) -> int:
    """Run integration layer: start MariaDB+Redis from docker-compose.system.yml, run migrations, run pytest -m integration, tear down."""
    extra = extra_args or []
    compose_file = "docker-compose.system.yml"

    env = _compose_env("integration")
    mariadb_port = env["MARIADB_PORT"]
    redis_port = env["REDIS_PORT"]

    env["DATABASE_URL"] = (
        os.environ.get("LUMEN_TEST_DATABASE_URL")
        or f"mysql+aiomysql://lumen:lumen@127.0.0.1:{mariadb_port}/lumen"
    )
    env["REDIS_URL"] = os.environ.get("LUMEN_TEST_REDIS_URL") or f"redis://127.0.0.1:{redis_port}/0"
    env["LUMEN_ENCRYPTION_KEY"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    try:
        # Start DB + Redis
        up_code = _run_cmd(
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                "mariadb",
                "redis",
            ],
            env=env,
        )
        if up_code != 0:
            return up_code

        # Apply migrations
        mig_code = _run_cmd(["lumen-migrate", "--apply"], env=env)
        if mig_code != 0:
            return mig_code

        # Run integration tests
        pytest_cmd = ["pytest", "-m", "integration"] + extra
        return _run_cmd(pytest_cmd, env=env)
    finally:
        _run_cmd(["docker", "compose", "-f", compose_file, "down", "-v", "--remove-orphans"], env=env)


def run_system(extra_args: list[str] | None = None) -> int:
    """Run the process stack, then execute its external-HTTP system tests."""
    compose = ["docker", "compose", "-f", "docker-compose.system.yml"]
    env = _compose_env("system")
    if extra_args:
        env["SYSTEM_PYTEST_ADDOPTS"] = shlex.join(extra_args)

    exit_code = 1
    try:
        exit_code = _run_cmd([*compose, "build", "system-tests"], env=env)
        if exit_code != 0:
            return exit_code

        exit_code = _run_cmd(
            [
                *compose,
                "up",
                "-d",
                "--build",
                "--wait",
                "--wait-timeout",
                "180",
                "lumen-api",
                "lumen-worker",
            ],
            env=env,
        )
        if exit_code != 0:
            return exit_code

        exit_code = _run_cmd([*compose, "run", "--rm", "--no-deps", "system-tests"], env=env)
        return exit_code
    finally:
        if exit_code != 0:
            _run_cmd([*compose, "logs"], env=env)
        _run_cmd([*compose, "down", "-v", "--remove-orphans"], env=env)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumen layered test environment runner.")
    parser.add_argument("layer", choices=["contract", "integration", "system"], help="Test layer to execute")
    parser.add_argument("extra", nargs=argparse.REMAINDER, help="Additional arguments forwarded to pytest")

    args = parser.parse_args()

    try:
        if args.layer == "contract":
            code = run_contract(args.extra)
        elif args.layer == "integration":
            code = run_integration(args.extra)
        elif args.layer == "system":
            code = run_system(args.extra)
        else:
            code = 1
    except KeyboardInterrupt:
        sys.exit(130)

    sys.exit(code)


if __name__ == "__main__":
    main()
