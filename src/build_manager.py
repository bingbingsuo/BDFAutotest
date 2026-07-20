"""
Build manager: orchestrates running the setup command with proper options
"""

import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import shutil

from .models import BuildResult
from .utils import find_python_interpreter


class BuildManager:
    """Run the package setup/build command using configuration options"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        self.config = config
        self.build_cfg = config.get("build", {})
        # Use git.local_path as default if source_dir is not explicitly set
        git_cfg = config.get("git", {})
        default_source_dir = git_cfg.get("local_path", "./package_source")
        self.source_dir = Path(self.build_cfg.get("source_dir", default_source_dir)).resolve()
        self.build_dir = self.source_dir / self.build_cfg.get("build_dir", "build")
        self.build_command = self.build_cfg.get("build_command", "./setup")
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

    def _additional_args(self) -> List[str]:
        return self.build_cfg.get("additional_args", [])

    def _assemble_command(self) -> List[str]:
        args = []
        args.extend(self._compiler_args())
        args.extend(self._math_args())
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
            },
        )

        if result.success:
            self.logger.info("Build completed successfully in %.2fs", duration)
        else:
            self.logger.error("Build failed with exit code %s", process.returncode)
        return result

