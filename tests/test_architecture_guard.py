"""Black-box regression tests for the root architecture freshness guard."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
GUARD = REPOSITORY_ROOT / "scripts" / "check_architecture.py"
HEADINGS = (
    "Overview",
    "Development status",
    "System context",
    "Code map",
    "Runtime flows",
    "Data and contracts",
    "Deployment and operations",
    "Security boundaries",
    "Development and verification",
    "Change guide",
    "Maintenance",
    "Glossary",
)


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def run_guard(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "check_architecture.py"), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def architecture_body() -> str:
    return "# Fixture Architecture\n\n" + "\n\n".join(f"## {heading}\nfixture" for heading in HEADINGS) + "\n"


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy2(GUARD, tmp_path / "scripts" / "check_architecture.py")
    (tmp_path / "scripts" / "check_architecture.py").chmod(0o755)
    (tmp_path / "ARCHITECTURE.md").write_text(architecture_body(), encoding="utf-8")
    (tmp_path / "app.py").write_text("print('fixture')\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("source rule\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("source rule\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "old.md").write_text("docs\n", encoding="utf-8")
    (tmp_path / "openspec").mkdir()
    (tmp_path / "openspec" / "plan.md").write_text("plan\n", encoding="utf-8")
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "fixture@example.test")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    stamped = run_guard(tmp_path, "--stamp", "--summary", "initial source review")
    assert stamped.returncode == 0, stamped.stderr
    git(tmp_path, "add", "ARCHITECTURE.md")
    git(tmp_path, "commit", "-qm", "stamp")
    assert run_guard(tmp_path).returncode == 0
    return tmp_path


def test_fresh_stale_and_restamp_cycle(fixture_repo: Path) -> None:
    source = fixture_repo / "app.py"
    source.write_text("print('changed')\n", encoding="utf-8")
    stale = run_guard(fixture_repo)
    assert stale.returncode == 1
    assert run_guard(fixture_repo, "--stamp", "--summary", "reviewed app change").returncode == 0
    assert run_guard(fixture_repo).returncode == 0
    source.write_text("print('changed twice')\n", encoding="utf-8")
    assert run_guard(fixture_repo).returncode == 1


def test_staged_mode_isolated_from_unstaged_document_and_source(fixture_repo: Path) -> None:
    (fixture_repo / "app.py").write_text("print('staged')\n", encoding="utf-8")
    git(fixture_repo, "add", "app.py")
    assert run_guard(fixture_repo, "--staged").returncode == 1
    assert run_guard(fixture_repo, "--stamp", "--staged", "--summary", "reviewed staged app").returncode == 0
    git(fixture_repo, "add", "ARCHITECTURE.md")
    assert run_guard(fixture_repo, "--staged").returncode == 0
    (fixture_repo / "app.py").write_text("print('unstaged')\n", encoding="utf-8")
    assert run_guard(fixture_repo, "--staged").returncode == 0
    assert run_guard(fixture_repo).returncode == 1


def test_source_changes_include_add_delete_rename_mode_lock_and_ci(fixture_repo: Path) -> None:
    cases = ["add", "delete", "rename", "mode", "lock", "ci"]
    for case in cases:
        if case == "add":
            path = fixture_repo / "new.py"
            path.write_text("new\n", encoding="utf-8")
            git(fixture_repo, "add", "new.py")
        elif case == "delete":
            (fixture_repo / "new.py").unlink()
            git(fixture_repo, "add", "-u")
        elif case == "rename":
            (fixture_repo / "rename-old.py").write_text("rename\n", encoding="utf-8")
            git(fixture_repo, "add", "rename-old.py")
            git(fixture_repo, "commit", "-qm", "before rename")
            (fixture_repo / "rename-old.py").rename(fixture_repo / "rename-new.py")
            git(fixture_repo, "add", "-A")
        elif case == "mode":
            path = fixture_repo / "app.py"
            path.chmod(0o755)
            git(fixture_repo, "add", "app.py")
        elif case == "lock":
            (fixture_repo / "deps.lock").write_text("lock\n", encoding="utf-8")
            git(fixture_repo, "add", "deps.lock")
        else:
            workflow = fixture_repo / ".github" / "workflows"
            workflow.mkdir(parents=True, exist_ok=True)
            (workflow / "ci.yml").write_text("name: fixture\n", encoding="utf-8")
            git(fixture_repo, "add", ".github/workflows/ci.yml")
        assert run_guard(fixture_repo, "--staged").returncode == 1, case
        assert run_guard(fixture_repo, "--stamp", "--staged", "--summary", f"reviewed {case}").returncode == 0
        git(fixture_repo, "add", "ARCHITECTURE.md")
        assert run_guard(fixture_repo, "--staged").returncode == 0


def test_docs_root_markdown_and_ignored_untracked_are_excluded(fixture_repo: Path) -> None:
    (fixture_repo / "README.md").write_text("changed readme\n", encoding="utf-8")
    (fixture_repo / "docs" / "old.md").write_text("changed docs\n", encoding="utf-8")
    (fixture_repo / "openspec" / "plan.md").write_text("changed plan\n", encoding="utf-8")
    (fixture_repo / "ignored.txt").write_text("not source\n", encoding="utf-8")
    assert run_guard(fixture_repo).returncode == 0


def test_source_markdown_is_included(fixture_repo: Path) -> None:
    path = fixture_repo / "src"
    path.mkdir()
    prompt = path / "prompt.md"
    prompt.write_text("source prompt\n", encoding="utf-8")
    assert run_guard(fixture_repo).returncode == 1


def test_malformed_missing_and_duplicate_markers_fail_without_corruption(fixture_repo: Path) -> None:
    document = fixture_repo / "ARCHITECTURE.md"
    original = document.read_bytes()
    document.write_bytes(original.replace(b"architecture-review:start", b"architecture-review:wrong"))
    malformed = run_guard(fixture_repo)
    assert malformed.returncode == 1
    malformed_bytes = document.read_bytes()
    assert run_guard(fixture_repo, "--stamp", "--summary", "must not stamp malformed").returncode == 1
    assert document.read_bytes() == malformed_bytes
    document.write_bytes(original.replace(b'"schema_version": 1', b'"schema_version": 2'))
    assert run_guard(fixture_repo).returncode == 1
    document.write_bytes(original + original[original.index(b"<!-- architecture-review:start -->") :])
    assert run_guard(fixture_repo).returncode == 1
    document.write_text(architecture_body(), encoding="utf-8")
    assert run_guard(fixture_repo).returncode == 1
    assert run_guard(fixture_repo, "--stamp", "--summary", "first review").returncode == 0
    assert run_guard(fixture_repo).returncode == 0
    assert run_guard(fixture_repo, "--stamp", "--summary", " ").returncode == 2


def test_invalid_review_fields_and_missing_document_fail(fixture_repo: Path) -> None:
    document = fixture_repo / "ARCHITECTURE.md"
    original = document.read_bytes()

    invalid_digest = re.sub(
        rb'"source_sha256": "[0-9a-f]{64}"',
        b'"source_sha256": "' + (b"g" * 64) + b'"',
        original,
    )
    document.write_bytes(invalid_digest)
    assert run_guard(fixture_repo).returncode == 1

    invalid_timestamp = re.sub(
        rb'"reviewed_at": "[^"]+"',
        b'"reviewed_at": "2026-13-40T25:61:61Z"',
        original,
    )
    document.write_bytes(invalid_timestamp)
    assert run_guard(fixture_repo).returncode == 1

    document.write_bytes(original.replace(b'"summary": "initial source review"', b'"summary": " "'))
    assert run_guard(fixture_repo).returncode == 1

    document.unlink()
    assert run_guard(fixture_repo).returncode == 1
    assert run_guard(fixture_repo, "--stamp", "--summary", "must not create").returncode == 1
    assert not document.exists()


def test_bad_arguments_non_git_and_unmerged_index_fail_with_exit_two(fixture_repo: Path, tmp_path: Path) -> None:
    bad_args = run_guard(fixture_repo, "--summary", "without stamp")
    assert bad_args.returncode == 2
    non_git = tmp_path / "non-git"
    non_git.mkdir()
    (non_git / "scripts").mkdir()
    shutil.copy2(GUARD, non_git / "scripts" / "check_architecture.py")
    not_git = subprocess.run(
        [sys.executable, str(non_git / "scripts" / "check_architecture.py")],
        cwd=non_git,
        text=True,
        capture_output=True,
        check=False,
    )
    assert not_git.returncode == 2

    branch_name = git(fixture_repo, "branch", "--show-current").stdout.strip()
    branch = git(fixture_repo, "branch", "conflict", check=False)
    assert branch.returncode == 0
    (fixture_repo / "app.py").write_text("ours\n", encoding="utf-8")
    git(fixture_repo, "add", "app.py")
    git(fixture_repo, "commit", "-qm", "ours")
    git(fixture_repo, "checkout", "-q", "conflict")
    (fixture_repo / "app.py").write_text("theirs\n", encoding="utf-8")
    git(fixture_repo, "add", "app.py")
    git(fixture_repo, "commit", "-qm", "theirs")
    git(fixture_repo, "checkout", "-q", "-B", "fixture-main", branch_name)
    conflict = git(fixture_repo, "merge", "conflict", check=False)
    assert conflict.returncode != 0
    assert run_guard(fixture_repo, "--staged").returncode == 2
    assert run_guard(fixture_repo).returncode == 2


def test_gitlink_is_rejected_as_unsupported_source_boundary(fixture_repo: Path) -> None:
    commit = git(fixture_repo, "rev-parse", "HEAD").stdout.strip()
    git(fixture_repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/module")
    assert run_guard(fixture_repo, "--staged").returncode == 2
    assert run_guard(fixture_repo).returncode == 2


def test_newline_names_and_symlink_hash_target_without_following(fixture_repo: Path) -> None:
    odd = fixture_repo / "name with\nnewline.py"
    odd.write_text("odd\n", encoding="utf-8")
    target = fixture_repo.parent / "external-target"
    target.write_text("one\n", encoding="utf-8")
    link = fixture_repo / "link.py"
    link.symlink_to(target)
    git(fixture_repo, "add", "-A")
    assert run_guard(fixture_repo, "--stamp", "--staged", "--summary", "reviewed names").returncode == 0
    git(fixture_repo, "add", "ARCHITECTURE.md")
    assert run_guard(fixture_repo, "--staged").returncode == 0
    target.write_text("two\n", encoding="utf-8")
    assert run_guard(fixture_repo).returncode == 0
    link.unlink()
    link.symlink_to(fixture_repo / "another-target")
    assert run_guard(fixture_repo).returncode == 1


def test_digest_is_independent_of_commit_metadata_and_checkout_location(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "scripts").mkdir(parents=True)
        shutil.copy2(GUARD, root / "scripts" / "check_architecture.py")
        (root / "ARCHITECTURE.md").write_text(architecture_body(), encoding="utf-8")
        (root / "source.py").write_text("same\n", encoding="utf-8")
        git(root, "init", "-q")
        git(root, "config", "user.email", "fixture@example.test")
        git(root, "config", "user.name", "Fixture")
        git(root, "add", ".")
        git(root, "commit", "-qm", "different metadata")
        assert run_guard(root, "--stamp", "--summary", "same review").returncode == 0
        git(root, "add", "ARCHITECTURE.md")
        assert run_guard(root).returncode == 0
    first_digest = run_guard(first).stdout.split("source_sha256=", 1)[1].split()[0]
    second_digest = run_guard(second).stdout.split("source_sha256=", 1)[1].split()[0]
    assert first_digest == second_digest


def test_stamp_preserves_mode_and_json_contract(fixture_repo: Path) -> None:
    document = fixture_repo / "ARCHITECTURE.md"
    document.chmod(0o640)
    before = document.read_bytes()
    assert run_guard(fixture_repo, "--stamp", "--summary", "mode-preserving review").returncode == 0
    assert stat.S_IMODE(document.stat().st_mode) == 0o640
    after = document.read_bytes()
    assert after != before
    marker = after.split(b"<!-- architecture-review:start -->", 1)[1].split(b"<!-- architecture-review:end -->", 1)[0]
    payload = json.loads(marker.split(b"```json", 1)[1].split(b"```", 1)[0])
    assert payload["schema_version"] == 1
    assert len(payload["source_sha256"]) == 64
    assert payload["summary"] == "mode-preserving review"


def test_failure_does_not_echo_source_content(fixture_repo: Path) -> None:
    secret = "DO_NOT_PRINT_SOURCE_9d4d1e"
    (fixture_repo / "app.py").write_text(secret + "\n", encoding="utf-8")
    result = run_guard(fixture_repo)
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr
