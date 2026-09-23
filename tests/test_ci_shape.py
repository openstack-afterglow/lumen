"""Contract tests pinning the CI workflow, image build and test-harness shape.

These invariants carry the measured critical-path and duplicate-run savings
described in AGENTS.md (CI 파이프라인 성능 규정). Changing one is a deliberate,
re-measured decision, not a drive-by edit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"
CI_WORKFLOW_REF = "./.github/workflows/ci.yml"
CI_INPUTS = {"lumen_repository", "lumen_ref", "afterglow_crypto_ref"}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML follows YAML 1.1 and parses the bare `on` key as boolean True.
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict)
    return triggers


def _compact(expression: object) -> str:
    return " ".join(str(expression).split())


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _docker_jobs() -> dict:
    return _workflow("docker-build.yml")["jobs"]


# --- trigger dedup (rules 3, 9) -------------------------------------------------


def test_ci_workflow_is_reusable_only() -> None:
    triggers = _triggers(_workflow("ci.yml"))

    assert set(triggers) == {"workflow_call", "workflow_dispatch"}
    for trigger in ("workflow_call", "workflow_dispatch"):
        assert set(triggers[trigger]["inputs"]) == CI_INPUTS


def test_docker_build_is_the_single_push_and_pr_test_entry() -> None:
    workflow = _workflow("docker-build.yml")

    assert {"push", "pull_request"} <= set(_triggers(workflow))
    callers = [name for name, job in workflow["jobs"].items() if job.get("uses") == CI_WORKFLOW_REF]
    assert callers == ["test"]


def test_tests_run_unless_dedup_reports_a_duplicate() -> None:
    test = _docker_jobs()["test"]

    assert _needs(test) == ["dedup"]
    # A skipped (push/tag/dispatch) or failed dedup leaves the output empty,
    # so only an explicit 'true' may skip the tests.
    assert _compact(test["if"]) == "${{ !cancelled() && needs.dedup.outputs.duplicate != 'true' }}"


def test_image_build_is_gated_on_the_whole_test_workflow() -> None:
    build = _docker_jobs()["build-and-push"]

    assert set(_needs(build)) == {"dedup", "test"}
    assert _compact(build["if"]) == "${{ !cancelled() && needs.test.result == 'success' }}"


def test_reusable_ci_jobs_override_the_implicit_success_check() -> None:
    # The caller's `test` job needs `dedup`, which is skipped outside PRs. An
    # explicit status function keeps a skipped caller ancestor from skipping the
    # inner jobs (actions/runner#2205); without inner `needs` it hides nothing.
    jobs = _workflow("ci.yml")["jobs"]

    assert set(jobs) == {"service", "sdk", "kolla", "integration", "system"}
    for name, job in jobs.items():
        assert _compact(job.get("if")) == "${{ !cancelled() }}", name
        assert "needs" not in job, name


def test_no_job_uses_a_self_hosted_runner() -> None:
    for name in ("ci.yml", "docker-build.yml", "release.yml"):
        for job_name, job in _workflow(name)["jobs"].items():
            if "uses" in job:
                continue
            assert "self-hosted" not in str(job["runs-on"]), f"{name}:{job_name}"


# --- identical-tree PR dedup (rule 9) -------------------------------------------

DEDUP_ENV = {
    "GH_TOKEN": "${{ github.token }}",
    "REPO": "${{ github.repository }}",
    "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
    "HEAD_REF": "${{ github.head_ref }}",
    "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
    "ACTOR": "${{ github.actor }}",
    "MERGE_SHA": "${{ github.sha }}",
    "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
}


def _dedup_step() -> dict:
    dedup = _docker_jobs()["dedup"]
    (step,) = [step for step in dedup["steps"] if step.get("id") == "tree"]
    return step


def test_dedup_job_is_pr_only_read_only_and_injection_safe() -> None:
    dedup = _docker_jobs()["dedup"]
    step = _dedup_step()

    assert _compact(dedup["if"]) == "${{ github.event_name == 'pull_request' }}"
    assert dedup["permissions"] == {"contents": "read"}
    assert dedup["runs-on"] == "ubuntu-latest"
    assert dedup["outputs"] == {"duplicate": "${{ steps.tree.outputs.duplicate }}"}
    assert step["shell"] == "bash"
    assert step["env"] == DEDUP_ENV
    # Head ref and head repository are attacker-controlled on fork PRs; they
    # must reach the script only through env, never via expression expansion.
    assert "${{" not in step["run"]


REPO = "openstack-afterglow/lumen"
MERGE_SHA = "a" * 40
HEAD_SHA = "b" * 40
TREE = "c" * 40
SAME_TREES = {MERGE_SHA: TREE, HEAD_SHA: TREE}

GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_GH_LOG"
[ "$1" = api ] || exit 3
path="$2"
case "$path" in
  "repos/${REPO}/git/commits/"*) ;;
  *) exit 3 ;;
esac
var="STUB_TREE_${path##*/}"
if [ -n "${!var:-}" ]; then
  printf '%s\\n' "${!var}"
  exit 0
fi
echo "gh: Not Found (HTTP 404)" >&2
exit 1
"""


@pytest.mark.parametrize(
    ("overrides", "trees", "duplicate", "calls_gh"),
    [
        pytest.param({}, SAME_TREES, "true", True, id="same-repo-dev-identical-tree"),
        pytest.param({"HEAD_REF": "main"}, SAME_TREES, "true", True, id="same-repo-main-identical-tree"),
        pytest.param({"HEAD_REPO": "someone/lumen"}, SAME_TREES, "false", False, id="fork-same-branch-name"),
        pytest.param({"HEAD_REPO": ""}, SAME_TREES, "false", False, id="deleted-fork"),
        pytest.param({"PR_AUTHOR": "dependabot[bot]"}, SAME_TREES, "false", False, id="dependabot-author"),
        pytest.param({"ACTOR": "dependabot[bot]"}, SAME_TREES, "false", False, id="dependabot-actor"),
        pytest.param({"HEAD_REF": "feature/x"}, SAME_TREES, "false", False, id="feature-branch-without-push-run"),
        pytest.param({}, {MERGE_SHA: TREE, HEAD_SHA: "d" * 40}, "false", True, id="trees-differ"),
        pytest.param({}, {}, "false", True, id="gh-api-fails"),
        pytest.param({}, {HEAD_SHA: TREE}, "false", True, id="merge-lookup-fails"),
        pytest.param({}, {MERGE_SHA: "null", HEAD_SHA: "null"}, "false", True, id="non-sha-tree"),
    ],
)
def test_dedup_script_marks_only_identical_same_repo_branch_trees(
    tmp_path: Path, overrides: dict, trees: dict, duplicate: str, calls_gh: bool
) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute the workflow step")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    script = tmp_path / "dedup.sh"
    script.write_text(_dedup_step()["run"], encoding="utf-8")
    output = tmp_path / "github_output"
    gh_log = tmp_path / "gh.log"

    env = {
        # The stub must shadow a real gh so the test never touches the network.
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_OUTPUT": str(output),
        "STUB_GH_LOG": str(gh_log),
        "REPO": REPO,
        "HEAD_REPO": REPO,
        "HEAD_REF": "dev",
        "PR_AUTHOR": "maintainer",
        "ACTOR": "maintainer",
        "MERGE_SHA": MERGE_SHA,
        "HEAD_SHA": HEAD_SHA,
        **overrides,
        **{f"STUB_TREE_{sha}": tree for sha, tree in trees.items()},
    }
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines == [f"duplicate={duplicate}"]
    assert gh_log.exists() is calls_gh


# --- build cache (rule 4) -------------------------------------------------------


def test_only_dev_and_main_image_builds_export_the_gha_cache() -> None:
    # Cache entries are restorable only from the ref that wrote them and from
    # the default branch, so PR, tag and feature-branch dispatch runs read the
    # cache but never export it.
    workflow = _workflow("docker-build.yml")
    steps = workflow["jobs"]["build-and-push"]["steps"]
    (build,) = [step for step in steps if str(step.get("uses", "")).startswith("docker/build-push-action")]

    assert set(_triggers(workflow)["push"]["branches"]) == {"main", "dev"}
    assert build["with"]["cache-from"] == "type=gha,scope=${{ matrix.target }}"
    assert _compact(build["with"]["cache-to"]) == (
        "${{ (github.ref == 'refs/heads/main' || github.ref == 'refs/heads/dev')"
        " && format('type=gha,mode=max,scope={0}', matrix.target) || '' }}"
    )


# --- per-job fixed cost (rule 4) ------------------------------------------------


@pytest.mark.parametrize("service", ["mariadb", "redis"])
def test_integration_service_health_checks_poll_fast_with_a_wide_window(service: str) -> None:
    options = _workflow("ci.yml")["jobs"]["integration"]["services"][service]["options"]

    interval = int(re.search(r"--health-interval (\d+)s", options).group(1))
    retries = int(re.search(r"--health-retries (\d+)", options).group(1))
    start_period = int(re.search(r"--health-start-period (\d+)s", options).group(1))
    assert interval <= 2
    # Keep at least the old 10s x 10 window (~100s). 'Initialize containers'
    # took up to 47s in the measured baseline runs, and a wide window costs
    # nothing when the service becomes healthy early.
    assert start_period + interval * retries >= 100


def test_system_job_runs_the_stdlib_harness_without_a_host_venv() -> None:
    steps = _workflow("ci.yml")["jobs"]["system"]["steps"]

    assert not any("setup-uv" in str(step.get("uses", "")) for step in steps)
    (run_step,) = [step for step in steps if "run" in step]
    assert run_step["run"].strip() == "python3 -m lumen.scripts.test_layers system"
    assert run_step["env"] == {"AFTERGLOW_CRYPTO_REF": "${{ inputs.afterglow_crypto_ref }}"}


def test_test_layers_imports_only_the_standard_library() -> None:
    # The CI system job runs test_layers with the runner's python3 and no venv.
    code = (
        "import sys\n"
        "import lumen.scripts.test_layers\n"
        "extra = sorted(m for m in sys.modules\n"
        "    if m != '__main__' and m.split('.')[0] not in sys.stdlib_module_names and m.split('.')[0] != 'lumen')\n"
        "print(extra)\n"
        "raise SystemExit(1 if extra else 0)\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


# --- image layers (rule 4) ------------------------------------------------------


def _dockerfile_stages() -> dict[str, list[str]]:
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    stages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in joined.splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("#"):
            continue
        match = re.match(r"FROM \S+ AS (\S+)$", line, re.IGNORECASE)
        if match:
            current = stages.setdefault(match.group(1), [])
            continue
        if current is not None:
            current.append(line)
    return stages


def _index(instructions: list[str], predicate) -> int:
    return next(i for i, line in enumerate(instructions) if predicate(line))


def test_dockerfile_has_no_recursive_chown_or_floating_uv() -> None:
    instructions = [line for stage in _dockerfile_stages().values() for line in stage]

    assert instructions
    assert not any(re.search(r"chown\s+(-\w*R|--recursive)", line) for line in instructions)
    assert not any(re.search(r"astral-sh/uv(:latest)?\s", line) for line in instructions)


def test_uv_is_pinned_and_copied_after_the_apt_layer() -> None:
    builder = _dockerfile_stages()["lumen-builder"]

    uv_copy = _index(builder, lambda line: "ghcr.io/astral-sh/uv:" in line)
    assert re.fullmatch(
        r"COPY --from=ghcr\.io/astral-sh/uv:\d+\.\d+\.\d+@sha256:[0-9a-f]{64} /uv /uvx /bin/", builder[uv_copy]
    )
    apt = _index(builder, lambda line: line.startswith("RUN apt-get") and "build-essential" in line)
    assert apt < uv_copy


@pytest.mark.parametrize(
    ("stage", "compiled"),
    [("lumen-runtime", "lumen"), ("lumen-test", "lumen tests")],
)
def test_runtime_stages_own_files_through_copy_chown(stage: str, compiled: str) -> None:
    instructions = _dockerfile_stages()[stage]

    setup = _index(instructions, lambda line: line.startswith("RUN apt-get") and "adduser" in line)
    assert "chown appuser:appuser /app " in instructions[setup]
    copies = [i for i, line in enumerate(instructions) if line.startswith("COPY ")]
    assert copies and setup < copies[0]
    for i in copies:
        assert instructions[i].startswith("COPY --from=") or instructions[i].startswith("COPY --chown=appuser:appuser ")
        assert "--chown=appuser:appuser" in instructions[i]
    user = instructions.index("USER appuser")
    compile_step = instructions.index(f"RUN python -m compileall -q {compiled}")
    assert copies[-1] < user < compile_step
