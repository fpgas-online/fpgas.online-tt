#!/usr/bin/env python3
"""Derive the Debian package version from ``git describe``.

Series tags are two-component, ``vX.Y`` (e.g. ``v0.1``; ``v0.0`` is the
convention for the root commit) -- this matches the repo's tag ruleset,
which only admits vX.Y-shaped tags. At tag ``vX.Y`` the version is ``X.Y``;
N commits later ``X.Y.postN``. With no matching tag (the current upstream
state) it falls back to ``0.0.post<commit count>``. It increments on every
commit, so each push publishes a new, upgradeable package with no manual
bump and no tag. All forms are valid Debian versions verbatim, and match
the ``hatch-vcs`` post-release scheme used elsewhere in the fpgas.online
rolling-release projects.

Usage:
    python3 packaging/deb-version.py   # print the version
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# REPO normally points at this script's own checkout, but can be overridden
# (e.g. by tests) to point ``git`` at an arbitrary repo instead.
_DEFAULT_REPO = Path(__file__).resolve().parent.parent
_REPO_OVERRIDE = os.environ.get("DEB_VERSION_REPO")
REPO = Path(_REPO_OVERRIDE) if _REPO_OVERRIDE else _DEFAULT_REPO


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fail(message: str) -> None:
    """Print a clear error to stderr and exit non-zero (no traceback noise)."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _is_shallow_repo() -> bool:
    """Best-effort shallow-clone detection.

    Returns False (i.e. "assume non-shallow") if the git binary is missing or
    too old to support ``--is-shallow-repository`` -- those situations are
    handled explicitly by the callers that actually need git to work.
    """
    try:
        return _git("rev-parse", "--is-shallow-repository") == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def version() -> str:
    """git-describe derived version: vX.Y.Z -> X.Y.Z, N commits later -> X.Y.Z.postN."""
    if _is_shallow_repo():
        _fail(
            "this is a shallow git clone (e.g. `git clone --depth 1`, or a CI "
            "checkout without full history). packaging/deb-version.py derives "
            "the package version from the full commit history (git describe / "
            "`git rev-list --count`), and in a shallow clone that count is "
            "truncated -- it would silently produce a wrong version (e.g. "
            "0.0.post1) that looks like a downgrade from previously published "
            "releases, instead of failing. Fix: use a full clone, e.g. `git "
            "fetch --unshallow`, or set `fetch-depth: 0` on the checkout step "
            "in the build workflow."
        )
    try:
        describe = _git("describe", "--tags", "--long", "--match", "v[0-9]*")
        m = re.match(r"^v(.+)-(\d+)-g[0-9a-f]+$", describe)
        if m:
            base, n = m.group(1), int(m.group(2))
            return base if n == 0 else f"{base}.post{n}"
    except subprocess.CalledProcessError:
        pass
    except FileNotFoundError:
        _fail(
            "'git' executable not found on PATH; packaging/deb-version.py "
            "cannot derive a package version without it. Install git (and "
            "ensure it is on PATH) before running this script or the build."
        )
    # No matching tag yet: fall back to a monotonic count-based version.
    try:
        return "0.0.post" + _git("rev-list", "--count", "HEAD")
    except Exception:
        return "0.0"


def main() -> None:
    print(version())


if __name__ == "__main__":
    main()
