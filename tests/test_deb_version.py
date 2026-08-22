# tests/test_deb_version.py
"""``packaging/deb-version.py`` derives a Debian version from git.

This is a standalone stdlib+git script (it must run in a bare Debian build
container with no third-party packages installed), so these tests drive it
via ``subprocess`` under plain ``python3`` rather than importing it -- that
also proves it truly needs nothing beyond the standard library.

Each test builds its own throwaway git repo under ``tmp_path`` and points the
script at it via the ``DEB_VERSION_REPO`` env var override, so nothing here
depends on this repo's own tag history.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "packaging" / "deb-version.py"

_DEBIAN_VERSION_RE = re.compile(r"^\d+(\.\d+)*(\.post\d+)?$")


def _run_in_repo(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Invoke the script with ``DEB_VERSION_REPO`` pointed at a temp repo.

    ``deb-version.py`` normally derives its own repo location from
    ``__file__``, but honours the ``DEB_VERSION_REPO`` env var override so it
    can be pointed at a throwaway git repo for these tests without touching
    the real project checkout.
    """
    env = os.environ.copy()
    env["DEB_VERSION_REPO"] = str(repo)
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _commit(path, "initial commit")


def _commit(path: Path, message: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test User",
         "-C", str(path), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
    )


def test_script_is_executable_and_has_correct_shebang():
    assert SCRIPT.stat().st_mode & 0o111, "packaging/deb-version.py must be executable"
    first_line = SCRIPT.read_text().splitlines()[0]
    assert first_line == "#!/usr/bin/env python3"


def test_only_stdlib_imports():
    """Must run in a bare Debian container before any deps are installed."""
    stdlib_allow = {
        "os",
        "re",
        "subprocess",
        "sys",
        "pathlib",
        "__future__",
    }
    tree = ast.parse(SCRIPT.read_text())
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    assert seen <= stdlib_allow, f"non-stdlib imports found: {seen - stdlib_allow}"


def test_version_is_a_valid_debian_version_string(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "commit 1")
    printed = _run_in_repo(repo).stdout.strip()
    assert _DEBIAN_VERSION_RE.match(printed), printed


def test_no_tag_falls_back_to_commit_count(tmp_path):
    """With no vX.Y.Z tag at all the version is 0.0.post<commit count>."""
    repo = tmp_path / "tagless-repo"
    _init_repo(repo)
    for i in range(2):
        _commit(repo, f"commit {i}")
    n = int(
        subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    assert _run_in_repo(repo).stdout.strip() == f"0.0.post{n}"


def test_at_tag_gives_bare_version(tmp_path):
    """vX.Y.Z at HEAD -> X.Y.Z, no .postN suffix."""
    repo = tmp_path / "tag-repo"
    _init_repo(repo)
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test User",
         "-C", str(repo), "tag", "-a", "v0.1.0", "-m", "v0.1.0"],
        check=True,
    )

    result = _run_in_repo(repo)
    assert result.stdout.strip() == "0.1.0"


def test_commits_after_tag_get_post_suffix(tmp_path):
    """N commits after vX.Y.Z -> X.Y.Z.postN."""
    repo = tmp_path / "tag-repo-2"
    _init_repo(repo)
    subprocess.run(
        ["git", "-c", "user.email=test@example.invalid", "-c", "user.name=Test User",
         "-C", str(repo), "tag", "-a", "v0.1.0", "-m", "v0.1.0"],
        check=True,
    )

    n = 2
    for i in range(n):
        _commit(repo, f"commit {i} after tag")

    result = _run_in_repo(repo)
    assert result.stdout.strip() == f"0.1.0.post{n}"


def test_shallow_clone_fails_loudly_instead_of_wrong_version(tmp_path):
    """A shallow clone must FAIL, not silently emit a truncated 0.0.post1.

    Regression test for the bug where ``git rev-list --count HEAD`` returns 1
    in a ``--depth 1`` clone, producing a badly wrong version that looks like
    a downgrade from previously published releases.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    for i in range(5):
        _commit(origin, f"commit {i}")

    shallow = tmp_path / "shallow-clone"
    subprocess.run(
        # file:// forces a real shallow clone; git silently ignores --depth
        # for plain local-path clones ("--depth is ignored in local clones").
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
        check=True,
    )

    result = _run_in_repo(shallow, check=False)

    assert result.returncode != 0, (
        f"expected non-zero exit on a shallow clone, got stdout={result.stdout!r}"
    )
    assert "0.0.post1" not in result.stdout
    assert "shallow" in result.stderr.lower()
