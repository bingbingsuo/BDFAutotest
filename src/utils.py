"""
Utility helpers shared across modules
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Tuple, Optional, Dict, Any


def raise_stack_limit(logger: Optional[logging.Logger] = None) -> bool:
    """
    Raise RLIMIT_STACK to its hard limit so child processes run with an
    unlimited (or maximal) stack, equivalent to `ulimit -s unlimited`.

    BDF regression tests need a large stack on Linux; subprocesses inherit
    the parent's limits, so raising once before spawning tests covers them
    all. Best effort: on platforms that refuse post-start raises (macOS
    rejects any stack raise) the call is skipped with a debug log and the
    run continues with the inherited limit.

    Returns True if the limit is now at its maximum (or already was).
    """
    logger = logger or logging.getLogger("bdf_autotest.utils")
    try:
        import resource
    except ImportError:  # non-POSIX platform
        logger.debug("resource module unavailable; skipping stack limit raise")
        return False

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_STACK)
    except (ValueError, OSError) as exc:
        logger.debug("Could not read RLIMIT_STACK: %s", exc)
        return False

    if soft == hard or soft == resource.RLIM_INFINITY:
        logger.debug("Stack limit already at maximum (soft=%s)", soft)
        return True

    try:
        resource.setrlimit(resource.RLIMIT_STACK, (hard, hard))
        new_soft, _ = resource.getrlimit(resource.RLIMIT_STACK)
        logger.info(
            "Raised stack limit from %s to %s (hard cap) for test subprocesses",
            soft, new_soft,
        )
        return True
    except (ValueError, OSError) as exc:
        # E.g. macOS: "Operation not permitted" — cannot raise after start.
        logger.warning(
            "Could not raise stack limit (soft=%s, hard=%s): %s. "
            "Tests will inherit the current limit; on Linux you can also "
            "run 'ulimit -s unlimited' before starting the framework.",
            soft, hard, exc,
        )
        return False


def resolve_source_dir(config: Dict[str, Any]) -> Path:
    """
    Return the resolved package source directory from config.

    ConfigLoader already normalizes ``build.source_dir`` to ``git.local_path``
    (falling back to ``./package_source``); this helper exists so that modules
    constructed outside ConfigLoader (or with a hand-built config) all apply
    the same precedence:
        build.source_dir  >  git.local_path  >  ./package_source
    """
    build_cfg = config.get("build", {}) or {}
    git_cfg = config.get("git", {}) or {}
    default = git_cfg.get("local_path", "./package_source")
    return Path(build_cfg.get("source_dir", default)).resolve()


def resolve_source_path(source_dir: Path, relative_path: str) -> Path:
    """Return an absolute path inside the source directory"""
    return (source_dir / relative_path).resolve()


def wildcard_to_name(pattern: str, base_name: str) -> str:
    """
    Replace '*' wildcard in a pattern with the provided base name.
    If no wildcard is present, append the base name before the suffix.

    Special handling to avoid duplicated prefixes like 'testtest001.log'
    when pattern is 'test*.log' and base_name is 'test001':
    - If base_name already starts with the prefix before '*', we drop
      that prefix and only use base_name + suffix.
    """
    if "*" in pattern:
        pre, post = pattern.split("*", 1)
        if pre and base_name.startswith(pre):
            # base already includes the prefix, don't duplicate it
            return f"{base_name}{post}"
        return f"{pre}{base_name}{post}"
    path = Path(pattern)
    suffix = "".join(path.suffixes) or ""
    return f"{base_name}{suffix}"


def derive_test_paths(
    source_dir: Path,
    test_dir: str,
    reference_dir: str,
    log_pattern: str,
    reference_pattern: str,
    input_file: Path,
) -> Tuple[Path, Path]:
    """
    Given an input file, derive the associated log file and reference file paths.
    """
    base_name = input_file.stem
    log_name = wildcard_to_name(log_pattern, base_name)
    ref_name = wildcard_to_name(reference_pattern, base_name)
    log_file = resolve_source_path(source_dir, f"{test_dir}/{log_name}")
    reference_file = resolve_source_path(source_dir, f"{reference_dir}/{ref_name}")
    return log_file, reference_file


def find_python_interpreter(preferred: Optional[str] = None) -> str:
    """
    Find a suitable Python interpreter.
    
    Returns:
        Path to Python interpreter
    """
    if preferred:
        preferred_path = Path(preferred)
        if preferred_path.exists() and preferred_path.is_file():
            return str(preferred_path)
        found = shutil.which(preferred)
        if found:
            return found
    
    python3 = shutil.which("python3")
    if python3:
        return python3
    
    python = shutil.which("python")
    if python:
        return python
    
    for path in ["/usr/bin/python3", "/usr/local/bin/python3"]:
        if Path(path).exists():
            return path
    
    return "python3"


def fix_python_shebangs(
    install_dir: Path,
    python_interpreter: str,
    logger: Optional[logging.Logger] = None,
) -> int:
    """
    Fix hardcoded Python shebang lines in installed scripts.
    
    Args:
        install_dir: Root directory of installed package
        python_interpreter: Python interpreter to use
        logger: Optional logger for messages
    
    Returns:
        Number of files fixed
    """
    if logger is None:
        logger = logging.getLogger("bdf_autotest.utils")
    
    if not install_dir.exists():
        logger.warning("Install directory does not exist: %s", install_dir)
        return 0
    
    if Path(python_interpreter).is_absolute():
        new_shebang = f"#!{python_interpreter}"
    else:
        cmd_name = Path(python_interpreter).name
        new_shebang = f"#!/usr/bin/env {cmd_name}"
    
    # Patterns to match old shebangs
    old_patterns = [
        re.compile(rb'^#!/usr/bin/python\s'),
        re.compile(rb'^#!/usr/bin/python\s+-u\s'),
        re.compile(rb'^#!/usr/bin/env\s+python\s'),
        re.compile(rb'^#!/usr/bin/env\s+python\s+-u\s'),
    ]
    
    fixed_count = 0
    
    # Find all Python scripts
    for py_file in install_dir.rglob("*.py"):
        try:
            with open(py_file, "rb") as f:
                first_line = f.readline()
            
            needs_fix = any(pattern.match(first_line) for pattern in old_patterns)
            
            if needs_fix:
                with open(py_file, "rb") as f:
                    content = f.read()
                
                lines = content.split(b"\n", 1)
                if len(lines) > 1:
                    new_content = new_shebang.encode("utf-8") + b"\n" + lines[1]
                else:
                    new_content = new_shebang.encode("utf-8") + b"\n"
                
                with open(py_file, "wb") as f:
                    f.write(new_content)
                
                fixed_count += 1
                logger.debug("Fixed shebang in: %s", py_file.relative_to(install_dir))
        
        except Exception as e:
            logger.warning("Failed to fix shebang in %s: %s", py_file, e)
    
    if fixed_count > 0:
        logger.info("Fixed Python shebang in %d script(s) using: %s", fixed_count, new_shebang)
    
    return fixed_count

