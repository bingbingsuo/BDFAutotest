"""Tests for BDF_H5_CHKFIL_PRIMARY env injection in TestRunner."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import logging

from src.test_runner import TestRunner
from src.models import TestCase

LOGGER = logging.getLogger("t.env")


def _fake_hdf5_home(tmp_path: Path) -> str:
    """Create a minimal self-contained HDF5 layout so resolve_hdf5 passes."""
    home = tmp_path / "hdf5home"
    (home / "include").mkdir(parents=True)
    (home / "include" / "hdf5.h").write_text("// fake\n")
    (home / "lib").mkdir()
    (home / "lib" / "libhdf5.so").write_text("")
    return str(home)


def _make_runner(build_cfg: dict, env_cfg: dict | None = None) -> TestRunner:
    config = {
        "build": build_cfg,
        "tests": {"env": env_cfg or {}},
    }
    return TestRunner(config, logger=LOGGER)


def _make_case(tmp_path: Path) -> TestCase:
    return TestCase(
        name="test001",
        input_file=tmp_path / "test001.inp",
        log_file=tmp_path / "test001.log",
        reference_file=tmp_path / "test001.check",
        command=["bdf"],
    )


def test_h5chkfil_set_when_hdf5_enabled(tmp_path):
    home = _fake_hdf5_home(tmp_path)
    runner = _make_runner({"hdf5": {"enabled": True, "hdf5home": home}})
    assert runner.hdf5.enabled is True
    env = runner._build_test_env(_make_case(tmp_path))
    assert env["BDF_H5_CHKFIL_PRIMARY"] == "1"


def test_h5chkfil_not_set_when_hdf5_disabled(tmp_path):
    runner = _make_runner({"hdf5": {"enabled": False}})
    env = runner._build_test_env(_make_case(tmp_path))
    assert "BDF_H5_CHKFIL_PRIMARY" not in env


def test_h5chkfil_not_set_when_section_absent(tmp_path):
    runner = _make_runner({})
    env = runner._build_test_env(_make_case(tmp_path))
    assert "BDF_H5_CHKFIL_PRIMARY" not in env


def test_h5chkfil_not_set_when_home_invalid(tmp_path):
    # enabled=true but a bogus home -> downgraded -> flag must NOT be set
    runner = _make_runner({"hdf5": {"enabled": True, "hdf5home": str(tmp_path / "missing")}})
    assert runner.hdf5.enabled is False  # downgraded
    env = runner._build_test_env(_make_case(tmp_path))
    assert "BDF_H5_CHKFIL_PRIMARY" not in env


def test_tests_env_overrides_automatic_value(tmp_path):
    home = _fake_hdf5_home(tmp_path)
    # An explicit value in tests.env wins over the automatic "1".
    runner = _make_runner(
        {"hdf5": {"enabled": True, "hdf5home": home}},
        env_cfg={"BDF_H5_CHKFIL_PRIMARY": "0"},
    )
    env = runner._build_test_env(_make_case(tmp_path))
    assert env["BDF_H5_CHKFIL_PRIMARY"] == "0"


def test_basic_env_keys_present(tmp_path):
    runner = _make_runner({})
    env = runner._build_test_env(_make_case(tmp_path))
    assert env["BDFHOME"]
    assert env["BDF_TMPDIR"].endswith("test001")
    assert "OMP_NUM_THREADS" in env
    assert "OMP_STACKSIZE" in env


def test_hdf5_state_cached_on_runner(tmp_path):
    home = _fake_hdf5_home(tmp_path)
    enabled = _make_runner({"hdf5": {"enabled": True, "hdf5home": home}})
    assert enabled.hdf5.enabled is True
    disabled = _make_runner({})
    assert disabled.hdf5.enabled is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
