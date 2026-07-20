"""
Unified JSON schema for error events in BDF Auto Test Framework.

This schema is designed for integration with BDFDev AI Agent and provides
structured error information that can be used for prompt engineering and
few-shot learning.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any


class ErrorType(str, Enum):
    """Type of error event"""
    BUILD_SETUP = "build_setup"  # Setup/configure phase failure
    COMPILATION = "compilation"  # Compilation error
    LINKER = "linker"  # Linker error
    TEST_EXECUTION = "test_execution"  # Test execution failure
    TEST_COMPARISON = "test_comparison"  # Test output mismatch
    RUNTIME = "runtime"  # Runtime error (segfault, abort, etc.)
    TIMEOUT = "timeout"  # Process timeout
    UNKNOWN = "unknown"  # Unclassified error


class ErrorSeverity(str, Enum):
    """Severity level of the error"""
    CRITICAL = "critical"  # Build/test completely fails
    HIGH = "high"  # Major functionality broken
    MEDIUM = "medium"  # Partial failure, some functionality works
    LOW = "low"  # Minor issue, mostly works
    INFO = "info"  # Informational, not necessarily an error


class ErrorCategory(str, Enum):
    """Category of error for classification"""
    SYNTAX = "syntax"  # Syntax errors
    TYPE_MISMATCH = "type_mismatch"  # Type errors
    UNDEFINED_SYMBOL = "undefined_symbol"  # Missing symbols
    LINKER_ERROR = "linker_error"  # Linker issues
    NUMERICAL = "numerical"  # Numerical precision/accuracy issues
    CONVERGENCE = "convergence"  # Convergence failures
    MEMORY = "memory"  # Memory issues
    MODULE_FAILURE = "module_failure"  # BDF module-specific failure
    CONFIGURATION = "configuration"  # Configuration errors
    ENVIRONMENT = "environment"  # Environment setup issues
    OTHER = "other"  # Other categories


@dataclass
class ErrorLocation:
    """Location information for an error"""
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    function: Optional[str] = None
    module: Optional[str] = None  # BDF module name if applicable


@dataclass
class ErrorContext:
    """Contextual information about when/where the error occurred"""
    command: List[str] = field(default_factory=list)
    working_directory: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)
    compiler: Optional[str] = None
    compiler_flags: List[str] = field(default_factory=list)
    build_config: Dict[str, Any] = field(default_factory=dict)
    test_name: Optional[str] = None
    git_commit: Optional[str] = None
    git_branch: Optional[str] = None
    solvent_model: Optional[str] = None  # e.g. "iefpcm", "cpcm", "smd"


@dataclass
class ErrorEvent:
    """
    Unified error event structure for BDF Auto Test Framework.
    
    This schema provides structured error information that can be:
    - Used for prompt engineering with LLMs
    - Stored for few-shot learning examples
    - Analyzed programmatically
    - Integrated with AI agents
    """
    # Core identification
    event_id: str  # Unique identifier for this event
    timestamp: str  # ISO 8601 timestamp
    error_type: ErrorType
    severity: ErrorSeverity
    category: ErrorCategory
    
    # Error details
    message: str  # Primary error message
    details: List[str] = field(default_factory=list)  # Additional error details
    location: Optional[ErrorLocation] = None
    stack_trace: Optional[str] = None
    
    # Context
    context: ErrorContext = field(default_factory=ErrorContext)
    
    # Related information
    failed_modules: List[str] = field(default_factory=list)  # BDF modules that failed
    related_events: List[str] = field(default_factory=list)  # IDs of related events
    
    # Analysis results
    llm_analysis: Optional[Dict[str, Any]] = None  # LLM analysis if available
    suggestions: List[str] = field(default_factory=list)  # Suggested fixes
    
    # Metadata
    source: str = "bdf_autotest"  # Source system
    version: str = "1.0"  # Schema version
    metadata: Dict[str, Any] = field(default_factory=dict)  # Additional metadata
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        result = asdict(self)
        # Convert enums to strings
        result['error_type'] = self.error_type.value
        result['severity'] = self.severity.value
        result['category'] = self.category.value
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ErrorEvent':
        """Create ErrorEvent from dictionary"""
        # Convert string enums back to enum types
        if isinstance(data.get('error_type'), str):
            data['error_type'] = ErrorType(data['error_type'])
        if isinstance(data.get('severity'), str):
            data['severity'] = ErrorSeverity(data['severity'])
        if isinstance(data.get('category'), str):
            data['category'] = ErrorCategory(data['category'])
        
        # Handle nested ErrorLocation
        if data.get('location') and isinstance(data['location'], dict):
            data['location'] = ErrorLocation(**data['location'])
        
        # Handle nested ErrorContext
        if data.get('context') and isinstance(data['context'], dict):
            data['context'] = ErrorContext(**data['context'])
        
        return cls(**data)


def create_event_id(prefix: str = "evt") -> str:
    """Generate a unique event ID"""
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def get_timestamp() -> str:
    """Get current timestamp in ISO 8601 format"""
    return datetime.utcnow().isoformat() + "Z"

