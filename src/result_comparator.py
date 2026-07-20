"""
Compare test outputs against reference data
"""

import difflib
from pathlib import Path
from typing import Optional, Dict, Tuple, List

from .models import ComparisonResult


class ResultComparator:
    """Provide comparison utilities for test outputs"""

    def __init__(self, tolerance: float = 0.0, mode: str = "strict", scale_map: Optional[Dict[str, float]] = None):
        """
        Args:
            tolerance: default numeric tolerance (not used for CHECKDATA rules).
            mode: 'strict' or 'loose' – controls scaling of CHECKDATA tolerances.
            scale_map: optional mapping from mode -> scale factor. If None,
                       defaults to {'strict': 1.0, 'loose': 5.0}.
        """
        # Default tolerance (not used in per-key CHECKDATA rules)
        self.tolerance = tolerance

        # Per-key CHECKDATA absolute tolerances for numerical comparison.
        # The keys are prefixes at the start of a CHECKDATA line.
        # You can add more rules here manually.
        self.checkdata_tolerances: Dict[str, float] = {
            "CHECKDATA:HF:ENERGY": 2.0e-8,  # Increased from 1.0e-8 to 2.0e-8
            "CHECKDATA:MCSCF:MCENERGY": 1.0e-6,
            "CHECKDATA:GRAD:ERI_GRAD": 2.0e-5,
            "CHECKDATA:GRAD:TOT_GRAD": 2.0e-5,
            "CHECKDATA:GRAD:GS+EX": 3.0e-6,  # Tolerance for ground+excited state gradients
            "CHECKDATA:GRAD:NAC": 2.0e-4,  # Tolerance for non-adiabatic coupling gradients (all values compared)
            "CHECKDATA:GRAD:GS": 3.0e-6,  # Tolerance for ground state gradients (all values compared)
            "CHECKDATA:TDDFT:EXCITENE": 2.0e-4,
            "CHECKDATA:MRCI:ECI": 5.0e-8,
            "CHECKDATA:MRCI:ECI_DAV": 5.0e-8,
            "CHECKDATA:MP2:Eab": 1.0e-7,
            "CHECKDATA:MP2:Emp2": 1.0e-7,
            "CHECKDATA:MP2:Ecorr": 1.0e-7,
            "CHECKDATA:BDFOPT:OPTGEOM": 1.1e-4,  # Tolerance for optimized geometry coordinates (all values compared)
            "CHECKDATA:BDFOPT:HESSIAN": 1.1e-4,  # Tolerance for Hessian matrix elements (all values compared)
            "CHECKDATA:BDFOPT:FREQ": 1.0,  # Tolerance for vibrational frequencies (all values compared)
            # EOM-related quantities
            "CHECKDATA:EOMEESO:ECCSD": 1.0e-7,
            "CHECKDATA:EOMIPSO:EXCITEDSTATE": 1.0e-7,
            "CHECKDATA:EOMEASO:EXCITEDSTATE": 1.0e-7,
        }
        # Special relative tolerance for all ELECOUP quantities:
        # |gen - ref| / |ref| <= elecoup_relative_tol  -> treated as matched.
        # This is applied for any line starting with 'CHECKDATA:ELECOUP:'.
        self.elecoup_relative_tol = 0.05

        # Apply tolerance scaling based on mode
        scale_map = scale_map or {"strict": 1.0, "loose": 5.0}
        scale = scale_map.get(mode, 1.0)
        if scale != 1.0:
            for key in list(self.checkdata_tolerances.keys()):
                self.checkdata_tolerances[key] *= scale
            # Optionally scale ELECOUP relative tolerance as well
            self.elecoup_relative_tol *= scale

    def compare_check_files(self, generated: Path, reference: Path) -> ComparisonResult:
        """
        Compare two CHECKDATA files line-by-line.

        Rules:
        - Ignore whitespace differences (collapse runs of spaces).
        - For known CHECKDATA keys, compare numeric values with
          key-specific tolerances (see self.checkdata_tolerances).
        - For other lines, require exact match after whitespace normalization.
        """
        if not generated.exists():
            return ComparisonResult(matched=False, differences=f"File not found: {generated}")
        if not reference.exists():
            return ComparisonResult(matched=False, differences=f"File not found: {reference}")

        gen_lines = generated.read_text().splitlines()
        ref_lines = reference.read_text().splitlines()

        if len(gen_lines) != len(ref_lines):
            diff = "\n".join(
                difflib.unified_diff(
                    ref_lines,
                    gen_lines,
                    fromfile=str(reference),
                    tofile=str(generated),
                    lineterm="",
                )
            )
            return ComparisonResult(
                matched=False,
                differences="Line count differs between generated and reference\n" + diff,
                details={"generated_lines": len(gen_lines), "reference_lines": len(ref_lines)},
            )

        mismatches: List[str] = []

        for idx, (g_line, r_line) in enumerate(zip(gen_lines, ref_lines), start=1):
            g_norm = " ".join(g_line.split())
            r_norm = " ".join(r_line.split())

            # Skip completely empty lines after normalization
            if not g_norm and not r_norm:
                continue

            # Ignore all differences for SO2EINT lines as requested
            if g_norm.startswith("CHECKDATA:XUANYUAN:SO2EINT") and r_norm.startswith(
                "CHECKDATA:XUANYUAN:SO2EINT"
            ):
                continue

            # Try per-key numeric comparison for known CHECKDATA prefixes
            handled_numeric = False
            for key, tol in self.checkdata_tolerances.items():
                if g_norm.startswith(key) and r_norm.startswith(key):
                    # Special handling for keys with multiple float values - compare all floats
                    multi_value_keys = [
                        "CHECKDATA:GRAD:GS+EX",
                        "CHECKDATA:GRAD:NAC",
                        "CHECKDATA:GRAD:GS",
                        "CHECKDATA:BDFOPT:OPTGEOM",
                        "CHECKDATA:BDFOPT:HESSIAN",
                        "CHECKDATA:BDFOPT:FREQ",
                    ]
                    if key in multi_value_keys:
                        g_floats, r_floats, ok = self._extract_all_floats(g_norm, r_norm)
                        if not ok:
                            # Fallback: require exact normalized equality if parsing failed
                            if g_norm != r_norm:
                                mismatches.append(f"Line {idx}: text mismatch\n  gen: {g_norm}\n  ref: {r_norm}")
                        else:
                            if len(g_floats) != len(r_floats):
                                mismatches.append(
                                    f"Line {idx}: {key} has different number of values\n"
                                    f"  gen: {len(g_floats)} values\n"
                                    f"  ref: {len(r_floats)} values"
                                )
                            else:
                                # Compare each float value
                                for i, (g_val, r_val) in enumerate(zip(g_floats, r_floats)):
                                    if abs(g_val - r_val) > tol:
                                        mismatches.append(
                                            f"Line {idx}: {key} value {i+1} differs beyond tolerance {tol}\n"
                                            f"  gen: {g_val}\n"
                                            f"  ref: {r_val}"
                                        )
                    else:
                        # For other keys, compare only the last float (existing behavior)
                        g_val, r_val, ok = self._extract_last_float(g_norm, r_norm)
                        if not ok:
                            # Fallback: require exact normalized equality if parsing failed
                            if g_norm != r_norm:
                                mismatches.append(f"Line {idx}: text mismatch\n  gen: {g_norm}\n  ref: {r_norm}")
                        else:
                            if abs(g_val - r_val) > tol:
                                mismatches.append(
                                    f"Line {idx}: {key} differs beyond tolerance {tol}\n"
                                    f"  gen: {g_val}\n"
                                    f"  ref: {r_val}"
                                )
                    handled_numeric = True
                    break

            # Relative-tolerance rule for all ELECOUP quantities
            if not handled_numeric and g_norm.startswith("CHECKDATA:ELECOUP:") and r_norm.startswith(
                "CHECKDATA:ELECOUP:"
            ):
                g_val, r_val, ok = self._extract_last_float(g_norm, r_norm)
                if not ok:
                    if g_norm != r_norm:
                        mismatches.append(f"Line {idx}: text mismatch\n  gen: {g_norm}\n  ref: {r_norm}")
                else:
                    if r_val == 0.0:
                        rel_err = float("inf") if g_val != 0.0 else 0.0
                    else:
                        rel_err = abs(g_val - r_val) / abs(r_val)
                    if rel_err > self.elecoup_relative_tol:
                        mismatches.append(
                            "Line {idx}: CHECKDATA:ELECOUP relative difference beyond 5%\n"
                            f"  gen: {g_val}\n"
                            f"  ref: {r_val}\n"
                            f"  rel_err: {rel_err:.6f}"
                        )
                handled_numeric = True

            if handled_numeric:
                continue

            # For non-special lines, require exact match ignoring redundant whitespace
            if g_norm != r_norm:
                mismatches.append(f"Line {idx}: text mismatch\n  gen: {g_norm}\n  ref: {r_norm}")

        if not mismatches:
            return ComparisonResult(matched=True)

        return ComparisonResult(
            matched=False,
            differences="CHECKDATA comparison failed:\n" + "\n".join(mismatches),
            details={"mismatch_count": len(mismatches)},
        )

    @staticmethod
    def _extract_last_float(gen: str, ref: str) -> Tuple[float, float, bool]:
        """
        Extract the last float from each normalized line.
        Returns (gen_value, ref_value, success_flag).
        """
        def last_float(s: str) -> Optional[float]:
            for token in reversed(s.split()):
                try:
                    return float(token)
                except ValueError:
                    continue
            return None

        g_val = last_float(gen)
        r_val = last_float(ref)
        if g_val is None or r_val is None:
            return 0.0, 0.0, False
        return g_val, r_val, True

    @staticmethod
    def _extract_all_floats(gen: str, ref: str) -> Tuple[List[float], List[float], bool]:
        """
        Extract all float values from each normalized line.
        Returns (gen_floats, ref_floats, success_flag).
        """
        def extract_floats(s: str) -> List[float]:
            floats = []
            for token in s.split():
                try:
                    floats.append(float(token))
                except ValueError:
                    continue
            return floats

        g_floats = extract_floats(gen)
        r_floats = extract_floats(ref)
        if not g_floats or not r_floats:
            return [], [], False
        return g_floats, r_floats, True

