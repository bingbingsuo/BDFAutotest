"""
Error event parser: abstracts log parsing into structured error events.

This module extracts error information from build results, test results,
and log files, converting them into the unified ErrorEvent schema.
"""

import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from .error_event_schema import (
    ErrorEvent, ErrorType, ErrorSeverity, ErrorCategory,
    ErrorLocation, ErrorContext,
    create_event_id, get_timestamp
)
from .models import BuildResult, TestResult
from .utils import resolve_source_dir


class ErrorEventParser:
    """Parse logs and build/test results into structured error events"""
    
    # Common error patterns
    COMPILATION_ERROR_PATTERNS = {
        'fortran': re.compile(r'(?i)(error|fatal).*fortran', re.MULTILINE),
        'c': re.compile(r'(?i)(error|fatal).*(gcc|clang|icc)', re.MULTILINE),
        'linker': re.compile(r'(?i)(ld:|linker|undefined reference)', re.MULTILINE),
        'syntax': re.compile(r'(?i)(syntax error|parse error|expected)', re.MULTILINE),
        'undefined': re.compile(r'(?i)(undefined reference|undefined symbol)', re.MULTILINE),
    }
    
    RUNTIME_ERROR_PATTERNS = {
        'segfault': re.compile(r'(?i)(segmentation fault|segfault|signal 11)', re.MULTILINE),
        'abort': re.compile(r'(?i)(abort|aborted|terminated)', re.MULTILINE),
        'timeout': re.compile(r'(?i)(timeout|timed out)', re.MULTILINE),
        'memory': re.compile(r'(?i)(out of memory|memory error|allocation failed)', re.MULTILINE),
    }
    
    MODULE_PATTERN = re.compile(r'\s*Start\s+running\s+module\s+(\w+)', re.IGNORECASE)
    MODULE_END_PATTERN = re.compile(r'\s*End\s+running\s+module\s+(\w+)', re.IGNORECASE)
    
    FILE_LINE_PATTERN = re.compile(r'([^:]+):(\d+)(?::(\d+))?')
    
    # Knowledge library: Known false positive patterns (non-errors that contain "error" keyword)
    # These patterns should not be treated as errors when found in logs
    FALSE_POSITIVE_PATTERNS = [
        re.compile(r'(?i)IsOrthogonalizeDiisErrorMatrix\s*=', re.IGNORECASE),
        # Add more false positive patterns here as needed
    ]
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("bdf_autotest.error_parser")
    
    def parse_build_result(self, result: BuildResult, config: Dict[str, Any]) -> Optional[ErrorEvent]:
        """Parse a BuildResult into an ErrorEvent"""
        if result.success:
            return None
        
        # Determine error type
        error_type = self._classify_build_error(result)
        severity = ErrorSeverity.CRITICAL
        category = self._categorize_error(result.stderr or result.stdout, error_type)
        
        # Extract error details
        error_text = result.stderr or result.stdout or ""
        message = self._extract_primary_message(error_text, error_type)
        details = self._extract_error_details(error_text)
        location = self._extract_location(error_text)
        
        # Build context
        build_cfg = config.get("build", {})
        context = ErrorContext(
            command=result.command,
            working_directory=result.cwd,
            compiler=build_cfg.get("compiler_set"),
            build_config=build_cfg,
        )
        
        # Create event
        event = ErrorEvent(
            event_id=create_event_id("build"),
            timestamp=get_timestamp(),
            error_type=error_type,
            severity=severity,
            category=category,
            message=message,
            details=details,
            location=location,
            context=context,
            metadata={
                "exit_code": result.exit_code,
                "duration": result.duration,
                "build_dir": str(result.build_dir),
            }
        )
        
        return event
    
    def parse_test_result(self, result: TestResult, config: Dict[str, Any]) -> List[ErrorEvent]:
        """Parse a TestResult into one or more ErrorEvents"""
        events = []
        
        # Test execution failure
        if not result.success or result.exit_code != 0:
            event = self._parse_test_execution_error(result, config)
            if event:
                events.append(event)
        
        # Test comparison failure
        if result.comparison and not result.comparison.matched:
            event = self._parse_test_comparison_error(result, config)
            if event:
                events.append(event)
        
        return events
    
    def _parse_test_execution_error(self, result: TestResult, config: Dict[str, Any]) -> Optional[ErrorEvent]:
        """Parse test execution failure"""
        error_text = self._get_test_error_text(result)
        
        # Classify error
        error_type = self._classify_test_error(error_text, result.exit_code)
        severity = ErrorSeverity.HIGH if error_type == ErrorType.RUNTIME else ErrorSeverity.MEDIUM
        category = self._categorize_error(error_text, error_type)
        
        # Extract details
        message = self._extract_primary_message(error_text, error_type)
        details = self._extract_error_details(error_text)
        location = self._extract_location(error_text)
        
        # Detect failed modules
        failed_modules = self._detect_failed_modules(error_text)
        
        # Build context
        tests_cfg = config.get("tests", {})
        build_cfg = config.get("build", {})
        source_dir = str(resolve_source_dir(config))

        # Extract solvent model from test_case if available
        solvent_model = None
        si = result.test_case.solvent_info if result.test_case else None
        if si is not None:
            if hasattr(si, "model") and hasattr(si.model, "value"):
                solvent_model = si.model.value
            elif hasattr(si, "model"):
                solvent_model = str(si.model)

        context = ErrorContext(
            command=result.command,
            working_directory=result.cwd,
            test_name=result.test_case.name if result.test_case else None,
            environment={
                "BDFHOME": str(source_dir),
            },
            solvent_model=solvent_model,
        )
        
        event = ErrorEvent(
            event_id=create_event_id("test"),
            timestamp=get_timestamp(),
            error_type=error_type,
            severity=severity,
            category=category,
            message=message,
            details=details,
            location=location,
            context=context,
            failed_modules=list(failed_modules),
            metadata={
                "exit_code": result.exit_code,
                "duration": result.duration,
                "test_case": result.test_case.name if result.test_case else None,
            }
        )
        
        return event
    
    def _parse_test_comparison_error(self, result: TestResult, config: Dict[str, Any]) -> Optional[ErrorEvent]:
        """Parse test comparison failure (output mismatch)"""
        if not result.comparison or not result.comparison.differences:
            return None
        
        differences = result.comparison.differences
        
        # Analyze differences to determine category
        category = ErrorCategory.NUMERICAL
        if "CHECKDATA" in differences:
            # Check if it's a numerical precision issue
            if re.search(r'[-+]?\d*\.\d+', differences):
                category = ErrorCategory.NUMERICAL
            else:
                category = ErrorCategory.OTHER
        
        # Extract key differences
        diff_lines = differences.split('\n')[:20]  # First 20 lines
        message = f"Test output mismatch: {len(diff_lines)} differences found"
        details = diff_lines
        
        context = ErrorContext(
            command=result.command,
            test_name=result.test_case.name if result.test_case else None,
        )

        # Extract solvent model from test_case if available
        si = result.test_case.solvent_info if result.test_case else None
        if si is not None:
            if hasattr(si, "model") and hasattr(si.model, "value"):
                context.solvent_model = si.model.value
            elif hasattr(si, "model"):
                context.solvent_model = str(si.model)

        event = ErrorEvent(
            event_id=create_event_id("compare"),
            timestamp=get_timestamp(),
            error_type=ErrorType.TEST_COMPARISON,
            severity=ErrorSeverity.MEDIUM,
            category=category,
            message=message,
            details=details,
            context=context,
            metadata={
                "test_case": result.test_case.name if result.test_case else None,
                "differences_count": len(diff_lines),
            }
        )
        
        return event
    
    def _classify_build_error(self, result: BuildResult) -> ErrorType:
        """Classify the type of build error"""
        error_text = (result.stderr or result.stdout or "").lower()
        
        if any(pattern.search(error_text) for pattern in [
            self.COMPILATION_ERROR_PATTERNS['linker'],
            self.COMPILATION_ERROR_PATTERNS['undefined']
        ]):
            return ErrorType.LINKER
        
        if any(pattern.search(error_text) for pattern in [
            self.COMPILATION_ERROR_PATTERNS['fortran'],
            self.COMPILATION_ERROR_PATTERNS['c'],
            self.COMPILATION_ERROR_PATTERNS['syntax']
        ]):
            return ErrorType.COMPILATION
        
        return ErrorType.BUILD_SETUP
    
    def _classify_test_error(self, error_text: str, exit_code: int) -> ErrorType:
        """Classify the type of test error"""
        text_lower = error_text.lower()
        
        if self.RUNTIME_ERROR_PATTERNS['timeout'].search(text_lower):
            return ErrorType.TIMEOUT
        
        if self.RUNTIME_ERROR_PATTERNS['segfault'].search(text_lower):
            return ErrorType.RUNTIME
        
        if self.RUNTIME_ERROR_PATTERNS['abort'].search(text_lower):
            return ErrorType.RUNTIME
        
        if exit_code == 124 or exit_code == -9:  # Common timeout exit codes
            return ErrorType.TIMEOUT
        
        return ErrorType.TEST_EXECUTION
    
    def _categorize_error(self, error_text: str, error_type: ErrorType) -> ErrorCategory:
        """Categorize error into specific category"""
        text_lower = error_text.lower()
        
        if error_type == ErrorType.LINKER:
            if "undefined" in text_lower:
                return ErrorCategory.UNDEFINED_SYMBOL
            return ErrorCategory.LINKER_ERROR
        
        if error_type == ErrorType.COMPILATION:
            if "syntax" in text_lower or "parse" in text_lower:
                return ErrorCategory.SYNTAX
            if "undefined" in text_lower:
                return ErrorCategory.UNDEFINED_SYMBOL
            if "type" in text_lower or "mismatch" in text_lower:
                return ErrorCategory.TYPE_MISMATCH
        
        if "convergence" in text_lower or "scf" in text_lower:
            return ErrorCategory.CONVERGENCE
        
        if "memory" in text_lower or "allocation" in text_lower:
            return ErrorCategory.MEMORY
        
        if self.MODULE_PATTERN.search(error_text):
            return ErrorCategory.MODULE_FAILURE
        
        return ErrorCategory.OTHER
    
    def _is_false_positive(self, line: str) -> bool:
        """Check if a line matches a known false positive pattern (non-error)"""
        for pattern in self.FALSE_POSITIVE_PATTERNS:
            if pattern.search(line):
                return True
        return False
    
    def _extract_primary_message(self, error_text: str, error_type: ErrorType) -> str:
        """Extract the primary error message"""
        lines = error_text.split('\n')
        
        # Look for lines with "error" keyword, excluding false positives
        error_lines = [
            line.strip() for line in lines 
            if 'error' in line.lower() and not self._is_false_positive(line)
        ]
        if error_lines:
            # Return first substantial error line
            for line in error_lines:
                if len(line) > 20:  # Substantial error message
                    return line[:200]  # Limit length
        
        # Fallback: first non-empty line
        for line in lines:
            if line.strip():
                return line.strip()[:200]
        
        return f"{error_type.value} error occurred"
    
    def _extract_error_details(self, error_text: str, limit: int = 20) -> List[str]:
        """Extract additional error details"""
        lines = error_text.split('\n')
        details = []
        
        keywords = ['error', 'fatal', 'warning', 'failed', 'undefined', 'cannot']
        for line in lines:
            line_lower = line.lower()
            if any(kw in line_lower for kw in keywords):
                # Skip false positives (known non-errors)
                if self._is_false_positive(line):
                    continue
                stripped = line.strip()
                if stripped and len(stripped) > 10:
                    details.append(stripped)
                    if len(details) >= limit:
                        break
        
        return details
    
    def _extract_location(self, error_text: str) -> Optional[ErrorLocation]:
        """Extract file/line location from error text"""
        # Try to find file:line patterns
        match = self.FILE_LINE_PATTERN.search(error_text)
        if match:
            file_path = match.group(1)
            line_num = int(match.group(2)) if match.group(2) else None
            column = int(match.group(3)) if match.group(3) else None
            
            # Try to extract module name
            module_match = self.MODULE_PATTERN.search(error_text)
            module = module_match.group(1) if module_match else None
            
            return ErrorLocation(
                file=file_path,
                line=line_num,
                column=column,
                module=module
            )
        
        # Try to extract just module name
        module_match = self.MODULE_PATTERN.search(error_text)
        if module_match:
            return ErrorLocation(module=module_match.group(1))
        
        return None
    
    def _detect_failed_modules(self, error_text: str) -> set:
        """Detect which BDF modules failed"""
        started_modules = {}
        for match in self.MODULE_PATTERN.finditer(error_text):
            module_name = match.group(1).lower()
            started_modules[module_name] = match.start()
        
        ended_modules = set()
        for match in self.MODULE_END_PATTERN.finditer(error_text):
            module_name = match.group(1).lower()
            ended_modules.add(module_name)
        
        # Modules that started but didn't end
        failed = set()
        for module, start_pos in started_modules.items():
            if module not in ended_modules:
                failed.add(module)
        
        # If no clear pattern, return last started module
        if not failed and started_modules:
            sorted_modules = sorted(started_modules.items(), key=lambda x: x[1], reverse=True)
            last_module = sorted_modules[0][0]
            if last_module not in ended_modules:
                failed.add(last_module)
        
        return failed
    
    def _get_test_error_text(self, result: TestResult) -> str:
        """Get error text from test result (prefer log file)"""
        if result.test_case and result.test_case.log_file:
            log_path = Path(result.test_case.log_file)
            if log_path.exists():
                try:
                    return log_path.read_text(encoding='utf-8', errors='ignore')
                except Exception:
                    pass
        
        # Fallback to stdout/stderr
        return (result.stderr or "") + "\n" + (result.stdout or "")

