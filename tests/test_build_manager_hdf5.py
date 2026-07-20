"""Tests for the optional HDF5 build support in build_manager."""
import sys
from pathlib import Path

# Allow running `pytest` from anywhere by adding the project root to sys.path
# so that `from src.build_manager import ...` resolves the package's relative
# imports correctly.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.build_manager import resolve_hdf5, Hdf5Config  # noqa: E402


def _make_fake_hdf5_home(root: Path, *, lib_dirname: str = "lib", lib_name: str = "libhdf5.so") -> Path:
    """Create a fake HDF5 install layout under root and return root."""
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "include" / "hdf5.h").write_text("// fake header\n")
    # Write a H5public.h with the version macros so version detection fires.
    (root / "include" / "H5public.h").write_text(
        "#define H5_VERS_MAJOR 1\n"
        "#define H5_VERS_MINOR 12\n"
        "#define H5_VERS_RELEASE 2\n"
    )
    # lib layout: lib_dirname defaults to "lib"; use lib64 in some tests.
    target_lib = root / lib_dirname
    target_lib.mkdir(parents=True, exist_ok=True)
    (target_lib / lib_name).write_text("")
    return root


# --- disabled (default) ---------------------------------------------------

def test_disabled_when_section_absent():
    r = resolve_hdf5({"build": {}})
    assert r.enabled is False
    assert r.status == "disabled"
    assert r.hdf5home is None


def test_disabled_when_enabled_false():
    r = resolve_hdf5({"build": {"hdf5": {"enabled": False}}})
    assert r.enabled is False
    assert r.status == "disabled"
    # Must NOT consult env when disabled (default-off purity).
    r2 = resolve_hdf5({"build": {"hdf5": {"enabled": False}}}, env={"HDF5HOME": "/usr"})
    assert r2.enabled is False
    assert r2.status == "disabled"


# --- ok -------------------------------------------------------------------

def test_ok_with_config_home(tmp_path):
    home = _make_fake_hdf5_home(tmp_path / "hdf5")
    r = resolve_hdf5({"build": {"hdf5": {"enabled": True, "hdf5home": str(home)}}})
    assert r.status == "ok"
    assert r.enabled is True
    assert r.hdf5home == str(home)
    assert r.version == "1.12.2"


def test_ok_accepts_libhdf5_variants(tmp_path):
    # .dylib under lib/
    home = _make_fake_hdf5_home(tmp_path / "h", lib_name="libhdf5.dylib")
    r = resolve_hdf5({"build": {"hdf5": {"enabled": True, "hdf5home": str(home)}}})
    assert r.status == "ok"
    # .a under lib64/
    home2_dir = tmp_path / "h2"
    home2_dir.mkdir()
    (home2_dir / "include").mkdir()
    (home2_dir / "include" / "hdf5.h").write_text("")
    (home2_dir / "lib64").mkdir()
    (home2_dir / "lib64" / "libhdf5.a").write_text("")
    r2 = resolve_hdf5({"build": {"hdf5": {"enabled": True, "hdf5home": str(home2_dir)}}})
    assert r2.status == "ok"


# --- env fallback and precedence -----------------------------------------

def test_env_fallback_when_config_home_unset(tmp_path):
    home = _make_fake_hdf5_home(tmp_path / "h")
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True}}},
        env={"HDF5HOME": str(home)},
    )
    assert r.status == "ok"
    assert r.hdf5home == str(home)


def test_config_home_takes_precedence_over_env(tmp_path):
    home_cfg = _make_fake_hdf5_home(tmp_path / "cfg")
    home_env = _make_fake_hdf5_home(tmp_path / "env")
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "hdf5home": str(home_cfg)}}},
        env={"HDF5HOME": str(home_env)},
    )
    assert r.status == "ok"
    assert r.hdf5home == str(home_cfg)


def test_env_placeholder_is_expanded(tmp_path):
    home = _make_fake_hdf5_home(tmp_path / "h")
    # ${HDF5HOME} placeholder should be expanded against env.
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "hdf5home": "${HDF5HOME}"}}},
        env={"HDF5HOME": str(home)},
    )
    assert r.status == "ok"
    assert r.hdf5home == str(home)


# --- downgrade path -------------------------------------------------------

def test_downgrade_when_hdf5home_missing_and_allow_downgrade(tmp_path):
    home = _make_fake_hdf5_home(tmp_path / "h")
    # remove header -> check fails
    (home / "include" / "hdf5.h").unlink()
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "hdf5home": str(home), "allow_downgrade": True}}},
    )
    assert r.status == "downgraded"
    assert r.enabled is False
    assert "header not found" in r.reason


def test_downgrade_when_lib_missing(tmp_path):
    home = tmp_path / "h"
    (home / "include").mkdir(parents=True)
    (home / "include" / "hdf5.h").write_text("")
    # no lib/ or lib64/ at all
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "hdf5home": str(home)}}},
    )
    assert r.status == "downgraded"
    assert r.enabled is False
    assert "libhdf5" in r.reason


def test_downgrade_when_home_unset(tmp_path):
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True}}},
        env={"HDF5HOME": ""},  # no home from any source
    )
    assert r.status == "downgraded"
    assert r.enabled is False
    assert "HDF5HOME not set" in r.reason


# --- config_error (allow_downgrade=False) ---------------------------------

def test_config_error_when_check_fails_and_no_downgrade(tmp_path):
    home = _make_fake_hdf5_home(tmp_path / "h")
    (home / "include" / "hdf5.h").unlink()
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "hdf5home": str(home), "allow_downgrade": False}}},
    )
    assert r.status == "config_error"
    # Still enabled=True so setup is called and fails naturally.
    assert r.enabled is True
    assert "header not found" in r.reason


def test_config_error_when_home_unset_and_no_downgrade(tmp_path):
    r = resolve_hdf5(
        {"build": {"hdf5": {"enabled": True, "allow_downgrade": False}}},
        env={"HDF5HOME": ""},
    )
    assert r.status == "config_error"
    assert r.enabled is True
    assert "HDF5HOME not set" in r.reason


# --- shape errors ---------------------------------------------------------

def test_config_error_when_hdf5_not_a_mapping():
    r = resolve_hdf5({"build": {"hdf5": "not a mapping"}})
    assert r.status == "config_error"
    assert r.enabled is False
    assert "mapping" in r.reason


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
