"""Tests for solvent_parser module"""
import sys
from pathlib import Path

# Allow running `pytest` from anywhere by adding src/ to sys.path.
# Prefer the project layout (tests/ sibling to src/) over a hardcoded path.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from solvent_parser import (  # noqa: E402
    parse_scf_block, parse_mcscf_block, extract_solvent_from_input,
    has_solvent, format_solvent_report, SolventModel, SolventInfo
)


def test_no_solvent():
    inp = """
$COMPASS
Title
 test
$END

$SCF
RHF
charge 0
$END
"""
    assert not has_solvent(inp)
    info = extract_solvent_from_input(inp)
    assert not info.enabled


def test_cosmo_solvent():
    inp = """
$SCF
RHF
Solvent
 water
SolModel
 cpcm
$END
"""
    info = extract_solvent_from_input(inp)
    assert info.enabled
    assert info.solvent_name == "water"
    assert info.model == SolventModel.CPCM


def test_iefpcm_solvent():
    inp = """
$SCF
UKS
DFT
 b3lyp
Solvent
 methanol
SolModel
 IEFPCM
$END
"""
    info = extract_solvent_from_input(inp)
    assert info.enabled
    assert info.solvent_name == "methanol"
    assert info.model == SolventModel.IEFPCM


def test_smoke_format():
    info = SolventInfo(enabled=True, model=SolventModel.CPCM, solvent_name="water")
    report = format_solvent_report(info)
    assert "CPCM" in report
    assert "water" in report


if __name__ == "__main__":
    test_no_solvent()
    test_cosmo_solvent()
    test_iefpcm_solvent()
    test_smoke_format()
    print("All solvent_parser tests passed")
