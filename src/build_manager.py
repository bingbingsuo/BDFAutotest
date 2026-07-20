"""
Build manager: orchestrates running the setup command with proper options
"""

import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional
import shutil

from .models import BuildResult
from .utils import find_python_interpreter, resolve_source_dir


# --- HDF5 support --------------------------------------------------------

@dataclass
class Hdf5Config:
    """Resolution result for the optional HDF5 build feature."""

    enabled: bool                 # whether to pass --hdf5 to setup
    hdf5home: Optional[str]       # resolved HDF5 root (non-empty when enabled=True)
    status: str                   # "ok" | "downgraded" | "disabled" | "config_error"
    reason: str = ""              # human-readable detail (why downgraded / errored)
    version: Optional[str] = None  # best-effort HDF5 version string (may be None)


# Matches both $HDF5HOME and ${HDF5HOME}; resolved against the env dict
# handed to resolve_hdf5().
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand_env(value: str, env: Dict[str, str]) -> str:
    """Expand $VAR / ${VAR} references against the given environment mapping."""
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, "")
    return _ENV_VAR_PATTERN.sub(repl, value)


def _detect_hdf5_version(home: Path) -> Optional[str]:
    """
    Best-effort HDF5 version extraction from H5public.h.

    HDF5 exposes H5_VERS_MAJOR / H5_VERS_MINOR / H5_VERS_RELEASE as integer
    macros; we combine them into a "M.N.R" string. Library filenames
    (e.g. libhdf5.320.1.1.dylib) carry ABI soname numbers, not the HDF5
    release version, so they are intentionally not consulted.
    """
    h5public = home / "include" / "H5public.h"
    if not h5public.exists():
        return None
    try:
        text = h5public.read_text(errors="ignore")
        major = re.search(r"^#define\s+H5_VERS_MAJOR\s+(\d+)", text, re.MULTILINE)
        minor = re.search(r"^#define\s+H5_VERS_MINOR\s+(\d+)", text, re.MULTILINE)
        release = re.search(r"^#define\s+H5_VERS_RELEASE\s+(\d+)", text, re.MULTILINE)
        if major and minor and release:
            return f"{major.group(1)}.{minor.group(1)}.{release.group(1)}"
    except Exception:
        pass
    return None


def _find_libhdf5(lib_dir: Path) -> bool:
    """Return True if lib_dir contains a libhdf5 library (any suffix/kind)."""
    if not lib_dir.exists():
        return False
    for entry in lib_dir.iterdir():
        name = entry.name
        if name == "libhdf5.a" or name == "libhdf5.dll":
            return True
        if name.startswith("libhdf5.so") or name.startswith("libhdf5.dylib"):
            return True
    return False


def resolve_hdf5(config: Dict[str, Any], env: Optional[Dict[str, str]] = None) -> Hdf5Config:
    """
    Resolve the HDF5 build option from config and environment.

    Precedence for the HDF5 home directory (only consulted when enabled):
        config.build.hdf5.hdf5home  >  env['HDF5HOME']
    Environment references (``$HDF5HOME`` / ``${HDF5HOME}``) in config values
    are expanded against ``env`` (defaults to ``os.environ``).

    Does *not* auto-probe common install locations like /opt/homebrew/opt/hdf5;
    sources are strictly config and the HDF5HOME environment variable.
    """
    env = env if env is not None else dict(os.environ)
    build_cfg = config.get("build", {}) or {}
    hdf5_cfg = build_cfg.get("hdf5") or {}

    if not isinstance(hdf5_cfg, dict):
        return Hdf5Config(
            enabled=False, hdf5home=None, status="config_error",
            reason="build.hdf5 must be a mapping",
        )

    if not bool(hdf5_cfg.get("enabled", False)):
        # Default-off: do not touch the environment or filesystem.
        return Hdf5Config(enabled=False, hdf5home=None, status="disabled")

    allow_downgrade = bool(hdf5_cfg.get("allow_downgrade", True))

    # Resolve hdf5home: config wins, env is fallback.
    raw_home = hdf5_cfg.get("hdf5home")
    if isinstance(raw_home, str) and raw_home.strip():
        hdf5home = _expand_env(raw_home.strip(), env)
    else:
        hdf5home = env.get("HDF5HOME", "").strip()

    if not hdf5home:
        reason = (
            "HDF5HOME not set (neither build.hdf5.hdf5home nor the HDF5HOME "
            "environment variable)"
        )
        if allow_downgrade:
            return Hdf5Config(enabled=False, hdf5home=None, status="downgraded", reason=reason)
        return Hdf5Config(enabled=True, hdf5home=None, status="config_error", reason=reason)

    home = Path(hdf5home)
    header = home / "include" / "hdf5.h"
    if not header.exists():
        reason = f"header not found: {header}"
        if allow_downgrade:
            return Hdf5Config(enabled=False, hdf5home=hdf5home, status="downgraded", reason=reason)
        return Hdf5Config(enabled=True, hdf5home=hdf5home, status="config_error", reason=reason)

    # lib or lib64 must contain libhdf5.*
    if _find_libhdf5(home / "lib") or _find_libhdf5(home / "lib64"):
        version = _detect_hdf5_version(home)
        return Hdf5Config(
            enabled=True, hdf5home=hdf5home, status="ok", version=version,
        )

    reason = f"libhdf5.* not found under {home}/lib or {home}/lib64"
    if allow_downgrade:
        return Hdf5Config(enabled=False, hdf5home=hdf5home, status="downgraded", reason=reason)
    return Hdf5Config(enabled=True, hdf5home=hdf5home, status="config_error", reason=reason)


class BuildManager:
    """Run the package setup/build command using configuration options"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.config = config
        self.build_cfg = config.get("build", {})
        self.source_dir = resolve_source_dir(config)
        self.build_dir = self.source_dir / self.build_cfg.get("build_dir", "build")
        self.build_command = self.build_cfg.get("build_command", "./setup")
        # Resolve HDF5 once at construction; logging is deferred to run() so
        # instantiating BuildManager for inspection does not spam the log.
        self.hdf5 = resolve_hdf5(config)
        self.logger = logger or logging.getLogger("bdf_autotest.build")

    def _compiler_args(self) -> List[str]:
        compiler_set_key = self.build_cfg.get("compiler_set", "gnu")
        compilers = self.build_cfg.get("compilers", {})
        selected = compilers.get(compiler_set_key, {})

        args = []
        fc = selected.get("fortran")
        if fc:
            args.append(f"--fc={fc}")
        cc = selected.get("c")
        if cc:
            args.append(f"--cc={cc}")
        cxx = selected.get("cpp")
        if cxx:
            args.append(f"--cxx={cxx}")
        return args

    def _math_args(self) -> List[str]:
        args = []
        use_mkl = self.build_cfg.get("use_mkl", False)
        if use_mkl:
            mkl_option = self.build_cfg.get("mkl_option", "TBB")
            args.extend(["--mkl", mkl_option])
        else:
            math_cfg = self.build_cfg.get("math_library", {})
            for key, option in [
                ("mathinclude_flags", "--mathinclude-flags"),
                ("mathlib_flags", "--mathlib-flags"),
                ("blasdir", "--blasdir"),
                ("lapackdir", "--lapackdir"),
            ]:
                value = math_cfg.get(key)
                if value:
                    args.append(f"{option}={value}")
        return args

    def _mode_args(self) -> List[str]:
        """
        Build mode mapping:
        - release: no extra option (default)
        - debug: add --debug flag
        """
        build_mode = (self.build_cfg.get("build_mode") or "release").lower()
        if build_mode == "debug":
            return ["--debug"]
        return []

    def _always_use_args(self) -> List[str]:
        always = self.build_cfg.get("always_use", [])
        return always if isinstance(always, list) else list(always)

    def _python_args(self) -> List[str]:
        """Get Python interpreter argument if configured"""
        python_cfg = self.build_cfg.get("python_interpreter")
        if python_cfg:
            python_path = find_python_interpreter(python_cfg)
            return [f"--python={python_path}"]
        return []

    def _hdf5_args(self) -> List[str]:
        """Return --hdf5 / --hdf5root flags when HDF5 is enabled."""
        if not self.hdf5.enabled:
            return []
        # setup wraps hdf5root in quotes when forwarding to CMake; we pass the
        # raw value as a single argv element.
        return ["--hdf5", f"--hdf5root={self.hdf5.hdf5home}"]

    def _additional_args(self) -> List[str]:
        return self.build_cfg.get("additional_args", [])

    def _assemble_command(self) -> List[str]:
        args = []
        args.extend(self._compiler_args())
        args.extend(self._math_args())
        args.extend(self._hdf5_args())
        args.extend(self._mode_args())
        args.extend(self._always_use_args())
        args.extend(self._python_args())
        args.extend(self._additional_args())

        command_parts = shlex.split(self.build_command)
        command_parts.extend(args)
        return command_parts

    def run(self) -> BuildResult:
        """Execute the build command"""
        self.logger.info("Starting build inside %s", self.source_dir)

        # Surface HDF5 resolution result before invoking setup. The "disabled"
        # case is silent to keep the default-off path noise-free.
        self._log_hdf5_status()

        preserve_build = self.build_cfg.get("preserve_build", False)
        if self.build_dir.exists():
            if preserve_build:
                self.logger.info("Preserving existing build directory at %s (preserve_build=true)", self.build_dir)
            else:
                self.logger.info("Removing existing build directory at %s", self.build_dir)
                shutil.rmtree(self.build_dir)
        self.build_dir.mkdir(parents=True, exist_ok=True)
        setup_log = self.build_dir / "setup.log"

        command = self._assemble_command()
        start_time = time.monotonic()
        env = os.environ.copy()
        env.update(self.build_cfg.get("environment", {}))
        process = subprocess.run(
            command,
            cwd=self.source_dir,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.monotonic() - start_time

        # Persist command output for later debugging
        with open(setup_log, "w", encoding="utf-8") as log_file:
            log_file.write(f"Command: {' '.join(command)}\n")
            log_file.write(f"Exit Code: {process.returncode}\n")
            log_file.write("--- STDOUT ---\n")
            log_file.write(process.stdout or "")
            log_file.write("\n--- STDERR ---\n")
            log_file.write(process.stderr or "")

        result = BuildResult(
            success=process.returncode == 0,
            command=command,
            cwd=str(self.source_dir),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            duration=duration,
            build_dir=self.build_dir,
            metadata={
                "build_mode": self.build_cfg.get("build_mode", "release"),
                "log_file": str(setup_log),
                "hdf5": asdict(self.hdf5),
            },
        )

        if result.success:
            self.logger.info("Build completed successfully in %.2fs", duration)
        else:
            self.logger.error("Build failed with exit code %s", process.returncode)
        return result

    def _log_hdf5_status(self) -> None:
        """Emit the appropriate log line for the resolved HDF5 status."""
        h = self.hdf5
        if h.status == "ok":
            self.logger.info(
                "HDF5 support enabled (root=%s, version=%s)",
                h.hdf5home,
                h.version or "unknown",
            )
        elif h.status == "downgraded":
            self.logger.warning(
                "HDF5 requested but unavailable; building without --hdf5: %s",
                h.reason,
            )
        elif h.status == "config_error":
            self.logger.error(
                "HDF5 requested but pre-build check failed: %s "
                "(setup will likely fail)", h.reason,
            )
        # "disabled" → silent

