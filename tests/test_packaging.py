"""Consistency checks between pyproject, nfpm.yaml and the unit/udev files."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nfpm_version_comes_from_git_describe():
    # nfpm substitutes only plain ${VERSION} (no bash-style defaults). Both
    # workflows compute VERSION via packaging/deb-version.py (git-describe
    # derived), not from pyproject.toml's static version -- the deb is a
    # rolling release while pyproject.toml's version stays the wheel/series
    # base (see README.md "Releases (rolling)").
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert nfpm["name"] == "fpgas-online-tt"
    assert nfpm["arch"] == "all"
    assert nfpm["version"] == "${VERSION}"

    version_cmd = "python3 packaging/deb-version.py"
    ci_yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    build_deb_yml = (ROOT / ".github" / "workflows" / "build-deb.yml").read_text()
    assert version_cmd in ci_yml, "ci.yml must derive VERSION from deb-version.py"
    assert version_cmd in build_deb_yml, "build-deb.yml must derive VERSION from deb-version.py"


def test_build_deb_workflow_manages_series_release():
    # The workflow must error if no vX.Y series tag exists (tags are
    # human-pushed over SSH, not created by Actions). It must also upload
    # assets to the release and not attempt tag creation.
    build_deb_yml = (ROOT / ".github" / "workflows" / "build-deb.yml").read_text()
    assert "gh release upload" in build_deb_yml, "workflow must upload assets to the series release"
    assert '::error::no vX.Y series tag reachable from this commit' in build_deb_yml, \
        "workflow must error if no series tag exists"
    assert 'gh release create "$SERIES" --target' not in build_deb_yml, \
        "workflow must not use --target with gh release create (cannot create tags via GITHUB_TOKEN)"


def test_nfpm_depends_on_bookworm_packages_only():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert set(nfpm["depends"]) == {
        "python3 (>= 3.11)",
        "python3-aiohttp",
        "python3-serial",
        "python3-serial-asyncio",
        "udev",
    }


def test_nfpm_ships_package_unit_rule_and_wrapper():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    dsts = {c["dst"] for c in nfpm["contents"]}
    assert "/usr/lib/python3/dist-packages/fpgas_tt" in dsts
    assert "/usr/bin/fpgas-tt" in dsts
    assert "/usr/lib/systemd/system/fpgas-tt.service" in dsts
    assert "/etc/udev/rules.d/60-fpgas-tt.rules" in dsts
    # the demos directory (--demos-dir default) is shipped empty; the demos deb fills it
    assert "/usr/share/fpgas-tt/demos" in dsts
    for c in nfpm["contents"]:
        if c.get("type") == "dir":
            assert "src" not in c, c
            continue
        assert (ROOT / c["src"]).exists(), c["src"]


def test_service_runs_daemon_as_pi_with_restart():
    unit = (ROOT / "debian" / "fpgas-tt.service").read_text()
    assert "ExecStart=/usr/bin/fpgas-tt" in unit
    assert re.search(r"^User=pi$", unit, re.M)
    assert re.search(r"^Restart=always$", unit, re.M)
    assert re.search(r"^RestartSec=1$", unit, re.M)


def test_udev_rule_symlinks_rp2040_and_rp2350_cdc():
    rule = (ROOT / "debian" / "60-fpgas-tt.rules").read_text()
    assert 'SYMLINK+="ttboard"' in rule
    assert 'ATTRS{idVendor}=="2e8a"' in rule
    assert 'ATTRS{idProduct}=="0005"' in rule  # RP2040 MicroPython CDC
    assert 'ATTRS{idProduct}=="000f"' in rule  # RP2350 MicroPython CDC
