"""
Dataclasses and shared models for the BDF Auto Test Framework
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any, Union


@dataclass
class CommandResult:
    """Generic result for a shell command execution"""

    success: bool
    command: List[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildResult(CommandResult):
    """Extends CommandResult with build specific metadata"""

    build_dir: Path = Path(".")


@dataclass
class TestCase:
    """Represents a single test input and reference"""

    name: str
    input_file: Path
    log_file: Path
    reference_file: Path
    command: List[str]
    solvent_info: Optional[Union[Any, Dict[str, Any]]] = None  # lazy-populated by test_runner


@dataclass
class ComparisonResult:
    """Result of comparing test output to reference data"""

    matched: bool
    differences: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult(CommandResult):
    """Aggregated result for a single test run"""

    test_case: TestCase = None  # type: ignore
    comparison: Optional[ComparisonResult] = None


@dataclass
class LLMAnalysis:
    """Holds LLM-provided insights"""

    summary: str
    suggestions: List[str] = field(default_factory=list)
    raw_response: Optional[str] = None

