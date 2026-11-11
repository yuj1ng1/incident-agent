"""Actual temporary Git states must fail before package collection or output."""
from pathlib import Path
import subprocess

import pytest

from scripts.w2_demo_build import build


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, stderr=subprocess.STDOUT).decode().strip()

    git("init")
    for name in ("inventory_app.py", "http_observability.py", "README.md"):
        (repo / name).write_text("# temporary source\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=W3 Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture source")
    return repo, git("rev-parse", "HEAD"), git


def test_unexpected_head_is_never_silently_used(source_repo, tmp_path):
    repo, commit, git = source_repo
    (repo / "README.md").write_text("new revision", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=W3 Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "changed head")
    output = tmp_path / "artifacts"
    with pytest.raises(ValueError, match="demo_source_commit_mismatch"):
        build(repo, output, commit)
    assert not output.exists()


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_source_fails_without_touching_repo(source_repo, tmp_path, staged):
    repo, commit, git = source_repo
    source = repo / "inventory_app.py"
    source.write_text("# local change\n", encoding="utf-8")
    if staged:
        git("add", "inventory_app.py")
    before = git("status", "--porcelain")
    output = tmp_path / "artifacts"
    with pytest.raises(ValueError, match="demo_source_worktree_changed"):
        build(repo, output, commit)
    assert git("status", "--porcelain") == before and not output.exists()


def test_abbreviated_commit_is_not_accepted(source_repo, tmp_path):
    repo, commit, _ = source_repo
    with pytest.raises(ValueError, match="full_expected_source_commit_required"):
        build(repo, tmp_path / "artifacts", commit[:7])
