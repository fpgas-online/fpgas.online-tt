"""Consistency checks between pyproject, nfpm.yaml and the unit/udev files."""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_nfpm_version_tracks_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert nfpm["name"] == "fpgas-online-tt"
    assert nfpm["arch"] == "all"
    # nfpm takes VERSION from the environment in CI; the fallback must match pyproject.
    assert nfpm["version"] == "${VERSION:-%s}" % pyproject["project"]["version"]


def test_nfpm_depends_on_bookworm_packages_only():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    assert set(nfpm["depends"]) == {
        "python3 (>= 3.11)",
        "python3-aiohttp",
        "python3-serial-asyncio",
        "python3-yaml",
        "udev",
    }


def test_nfpm_ships_package_unit_rule_and_wrapper():
    nfpm = yaml.safe_load((ROOT / "nfpm.yaml").read_text())
    dsts = {c["dst"] for c in nfpm["contents"]}
    assert "/usr/lib/python3/dist-packages/fpgas_tt" in dsts
    assert "/usr/bin/fpgas-tt" in dsts
    assert "/usr/lib/systemd/system/fpgas-tt.service" in dsts
    assert "/etc/udev/rules.d/60-fpgas-tt.rules" in dsts
    for c in nfpm["contents"]:
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
