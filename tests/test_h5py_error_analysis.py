"""Tests for h5py/HDF5-aware error analysis (error_event_parser + llm_analyzer)."""
import sys
import logging
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.error_event_parser import ErrorEventParser
from src.error_event_schema import ErrorCategory, ErrorType
from src.llm_analyzer import LLMAnalyzer, H5PY_MISSING_NOTE
from src.models import TestCase, TestResult, ComparisonResult

LOG_WITH_H5PY_MISSING = """\
 ...
 This step took 0.12 s
 Start running module scf
 ... normal output ...
 %$BDFHOME/sbin/bdfhdf5.py chkfil
 Traceback (most recent call last):
   File "/opt/bdf/sbin/bdfhdf5.py", line 6, in <module>
     import h5py
 ModuleNotFoundError: No module named 'h5py'
"""

LOG_WITH_HDF5_CMAKE_FAIL = """\
CMake Error at CMakeLists.txt:120 (find_package):
  Could NOT find HDF5 (missing: HDF5_LIBRARIES HDF5_INCLUDE_DIRS)
"""

LOG_ORDINARY = """\
 some normal output
 Start running module scf
 SCF not converged
"""

CONFIG_MIN = {"build": {}, "tests": {}}


def _make_test_result(tmp_path: Path, log_text: str) -> TestResult:
    log_file = tmp_path / "test001.log"
    log_file.write_text(log_text)
    case = TestCase(
        name="test001",
        input_file=tmp_path / "test001.inp",
        log_file=log_file,
        reference_file=tmp_path / "test001.check",
        command=["bdf"],
    )
    return TestResult(
        success=False,
        command=["bdf"],
        cwd=str(tmp_path),
        exit_code=1,
        stdout="",
        stderr="",
        duration=1.0,
        test_case=case,
        comparison=ComparisonResult(matched=False, differences="Line count differs"),
    )


# --- pattern recognition ---------------------------------------------------

def test_h5py_missing_pattern_matches_common_forms():
    p = ErrorEventParser.H5PY_MISSING_PATTERN
    assert p.search("ModuleNotFoundError: No module named 'h5py'")
    assert p.search('ModuleNotFoundError: No module named "h5py"')
    assert p.search("ImportError: h5py version mismatch")
    assert p.search("h5py raise ImportError here")
    assert not p.search(LOG_ORDINARY)
    assert not p.search("error while loading shared libraries: libhdf5.so")


def test_hdf5_cmake_missing_pattern():
    p = ErrorEventParser.HDF5_CMAKE_MISSING_PATTERN
    assert p.search("Could NOT find HDF5 (missing: HDF5_LIBRARIES)")
    assert p.search("HDF5 include dir not found")
    assert not p.search("HDF5 support enabled")


# --- categorization --------------------------------------------------------

def test_h5py_missing_categorized_as_environment():
    parser = ErrorEventParser()
    cat = parser._categorize_error(LOG_WITH_H5PY_MISSING, ErrorType.TEST_EXECUTION)
    assert cat == ErrorCategory.ENVIRONMENT


def test_hdf5_cmake_fail_categorized_as_environment():
    parser = ErrorEventParser()
    cat = parser._categorize_error(LOG_WITH_HDF5_CMAKE_FAIL, ErrorType.BUILD_SETUP)
    assert cat == ErrorCategory.ENVIRONMENT


def test_ordinary_failure_not_environment():
    parser = ErrorEventParser()
    cat = parser._categorize_error(LOG_ORDINARY, ErrorType.TEST_EXECUTION)
    assert cat != ErrorCategory.ENVIRONMENT


# --- full event parsing ----------------------------------------------------

def test_parse_test_result_sets_environment_category(tmp_path):
    parser = ErrorEventParser()
    result = _make_test_result(tmp_path, LOG_WITH_H5PY_MISSING)
    events = parser.parse_test_result(result, CONFIG_MIN)
    assert events, "expected at least one error event"
    # The execution event reads the log and must classify the missing h5py
    # as an environment issue. The companion comparison event keeps its own
    # (numerical) category because it only sees the CHECKDATA diff.
    exec_event = next(e for e in events if e.error_type == ErrorType.TEST_EXECUTION)
    assert exec_event.category == ErrorCategory.ENVIRONMENT


def test_primary_message_mentions_h5py(tmp_path):
    parser = ErrorEventParser()
    result = _make_test_result(tmp_path, LOG_WITH_H5PY_MISSING)
    events = parser.parse_test_result(result, CONFIG_MIN)
    exec_event = next(e for e in events if e.error_type == ErrorType.TEST_EXECUTION)
    assert "h5py" in exec_event.message.lower()


# --- LLM simple analysis ---------------------------------------------------

def test_simple_analysis_includes_h5py_note(tmp_path):
    analyzer = LLMAnalyzer({"llm": {"analysis_mode": "simple"}})
    result = _make_test_result(tmp_path, LOG_WITH_H5PY_MISSING)
    analysis = analyzer.analyze_test_failure(result)
    assert analysis is not None
    assert "h5py" in analysis.summary
    assert "pip install h5py" in analysis.summary


def test_simple_analysis_no_h5py_note_for_ordinary_failure(tmp_path):
    analyzer = LLMAnalyzer({"llm": {"analysis_mode": "simple"}})
    result = _make_test_result(tmp_path, LOG_ORDINARY)
    analysis = analyzer.analyze_test_failure(result)
    assert analysis is not None
    assert "pip install h5py" not in analysis.summary


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
