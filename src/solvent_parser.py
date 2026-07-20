"""
Solvent Model Parser for BDF Auto Test Framework

Detects and parses solvent/solModel keywords in BDF input files.
Supports: CPCM, IEFPCM, COSMO, SMD, and other PCM-type solvent models.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SolventModel(Enum):
    """Supported solvent model types in BDF"""
    CPCM = "cpcm"
    IEFPCM = "iefpcm"
    COSMO = "cosmo"
    SMD = "smd"
    UNKNOWN = "unknown"


@dataclass
class SolventInfo:
    """Parsed solvent configuration from a BDF input file"""
    enabled: bool = False
    model: SolventModel = SolventModel.UNKNOWN
    solvent_name: Optional[str] = None
    additional_params: dict = None  # extra keywords after solModel

    def __post_init__(self):
        if self.additional_params is None:
            self.additional_params = {}


# Common organic solvents recognized by BDF
SOLVENT_NAMES = {
    "water", "methanol", "ethanol", "acetonitrile", "acetone",
    "dmso", "dmf", "chloroform", "dichloromethane", "thf",
    "benzene", "toluene", "ether", "ccl4", "cs2",
}


def parse_scf_block(lines: list[str]) -> SolventInfo:
    """
    Parse a $SCF block and extract solvent information.

    Returns:
        SolventInfo with parsed solvent configuration
    """
    info = SolventInfo()
    text = "\n".join(lines).lower()

    # Check if solvent is enabled
    if "solvent" not in text:
        return info

    info.enabled = True

    # Find the solvent model type (solModel keyword)
    for i, line in enumerate(lines):
        line_upper = line.strip().upper()
        if line_upper in ("SOLMODEL", "SOLV_MODEL", "SOL-MODEL"):
            if i + 1 < len(lines):
                model_str = lines[i + 1].strip().lower()
                # Match known model names
                for model in SolventModel:
                    if model.value == model_str:
                        info.model = model
                        break
            break

    # Also check for Solvent keyword (solvent name after it)
    for i, line in enumerate(lines):
        line_upper = line.strip().upper()
        if line_upper == "SOLVENT":
            if i + 1 < len(lines):
                solvent = lines[i + 1].strip().lower()
                if solvent:
                    info.solvent_name = solvent
            break

    # Collect any additional parameters after solModel
    for i, line in enumerate(lines):
        line_upper = line.strip().upper()
        if line_upper in ("SOLMODEL", "SOLV_MODEL", "SOL-MODEL"):
            # Capture subsequent parameter lines until $END or next keyword
            params = []
            for j in range(i + 2, len(lines)):
                next_line = lines[j].strip()
                if not next_line or next_line.startswith("$") or next_line[0].isalpha() and next_line.isupper():
                    break
                params.append(next_line)
            if params:
                info.additional_params["extra_lines"] = params
            break

    return info


def parse_mcscf_block(lines: list[str]) -> SolventInfo:
    """
    Parse a $MCSCF block and check for solvate keyword.
    MCSCF blocks may contain 'solvate' keyword to enable solvent in CASSCF.
    """
    info = SolventInfo()
    text = "\n".join(lines)

    if "solvate" in text.lower():
        info.enabled = True
        # MCSCF solvent inherits from corresponding SCF block settings

    return info


def find_block_lines(all_lines: list[str], block_name: str) -> list[list[str]]:
    """
    Find all occurrences of a block (e.g., '$SCF', '$MCSCF') in the input.
    Returns a list of line lists, one per block instance.
    """
    blocks = []
    current_block: list[str] = []
    in_block = False

    for line in all_lines:
        upper = line.strip().upper()
        if upper == block_name:
            in_block = True
            current_block = []
            continue
        if in_block:
            if upper.startswith("$END") or upper == "END":
                in_block = False
                if current_block:
                    blocks.append(current_block)
            else:
                current_block.append(line)

    return blocks


def extract_solvent_from_input(input_text: str) -> SolventInfo:
    """
    Main entry point: parse a BDF input file text and extract solvent info.

    Supports both UPPER-case keywords (BDF standard) and mixed-case.
    """
    lines = input_text.split("\n")

    # Parse all $SCF blocks
    scf_blocks = find_block_lines(lines, "$SCF")
    for block in scf_blocks:
        info = parse_scf_block(block)
        if info.enabled:
            return info

    # Also check $MCSCF for solvate keyword
    mcscf_blocks = find_block_lines(lines, "$MCSCF")
    for block in mcscf_blocks:
        info = parse_mcscf_block(block)
        if info.enabled:
            return info

    return SolventInfo()


def has_solvent(input_text: str) -> bool:
    """Quick check if input file contains solvent keywords."""
    text_lower = input_text.lower()
    return "solvent" in text_lower or "solmodel" in text_lower or "solvate" in text_lower


def format_solvent_report(info: SolventInfo) -> str:
    """Format solvent info as a human-readable string."""
    if not info.enabled:
        return "No solvent model detected"

    parts = [f"Model: {info.model.value.upper() if info.model != SolventModel.UNKNOWN else 'Unknown'}"]
    if info.solvent_name:
        parts.append(f"Solvent: {info.solvent_name}")

    result = " | ".join(parts)

    if info.additional_params:
        extra = info.additional_params.get("extra_lines", [])
        if extra:
            result += f" | Params: {'; '.join(extra[:3])}"

    return result
