"""
Compile manager: runs the actual compilation command after setup
"""

import logging
import os
import shlex
import subprocess
import time
from multiprocessing import cpu_count
from pathlib import Path
from typing import Dict, Any, List, Optional

from .models import BuildResult
from .utils import find_python_interpreter, fix_python_shebangs, resolve_source_dir


class CompileManager:
    """Execute the user-defined compile command"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        compile_cfg = config.get("compile", {})
        build_cfg = config.get("build", {})
        default_working_dir = resolve_source_dir(config) / build_cfg.get("build_dir", "build")
        self.working_dir = Path(compile_cfg.get("working_dir", default_working_dir)).resolve()
        self.command = compile_cfg.get("command", "make")
        
        python_cfg = build_cfg.get("python_interpreter")
        self.python_interpreter = find_python_interpreter(python_cfg) if python_cfg else find_python_interpreter()

        # Determine number of parallel jobs:
        # - if compile.jobs is omitted / null / "auto": derive from CPU count
        # - otherwise: use validated integer from config
        raw_jobs = compile_cfg.get("jobs", None)
        if raw_jobs is None or raw_jobs == "auto":
            detected = cpu_count() or 1
            self.jobs = max(1, int(detected))
        else:
            self.jobs = int(raw_jobs)
        self.target = compile_cfg.get("target")
        self.extra_args = compile_cfg.get("extra_args", [])
        self.log_file = Path(compile_cfg.get("log_file", "make.log"))
        if not self.log_file.is_absolute():
            self.log_file = self.working_dir / self.log_file
        self.env = os.environ.copy()
        self.env.update(compile_cfg.get("environment", {}))
        self.logger = logger or logging.getLogger("bdf_autotest.compile")

    def _command_list(self) -> List[str]:
        if isinstance(self.command, list):
            cmd = self.command[:]
        else:
            cmd = shlex.split(self.command)

        if self.jobs:
            cmd.append(f"-j{self.jobs}")
        if self.target:
            cmd.append(self.target)
        if self.extra_args:
            cmd.extend(self.extra_args)
        return cmd

    def run(self) -> BuildResult:
        self.logger.info("Starting compilation inside %s", self.working_dir)
        if not self.working_dir.exists():
            raise FileNotFoundError(f"Working directory not found: {self.working_dir}")
        command = self._command_list()
        start_time = time.monotonic()
        process = subprocess.run(
            command,
            cwd=self.working_dir,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        duration = time.monotonic() - start_time

        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_file, "w", encoding="utf-8") as log_f:
            log_f.write(f"Command: {' '.join(command)}\n")
            log_f.write(f"Exit Code: {process.returncode}\n")
            log_f.write("--- STDOUT ---\n")
            log_f.write(process.stdout or "")
            log_f.write("\n--- STDERR ---\n")
            log_f.write(process.stderr or "")

        result = BuildResult(
            success=process.returncode == 0,
            command=command,
            cwd=str(self.working_dir),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            duration=duration,
            build_dir=self.log_file.parent,
            metadata={"log_file": str(self.log_file)},
        )

        if result.success:
            self.logger.info("Compilation completed successfully in %.2fs", duration)
            install_dir = self.working_dir / "bdf-pkg-full"
            if install_dir.exists():
                fixed_count = fix_python_shebangs(install_dir, self.python_interpreter, self.logger)
                if fixed_count > 0:
                    self.logger.info("Fixed %d Python script shebang(s) after compilation", fixed_count)
        else:
            self.logger.error("Compilation failed with exit code %s", process.returncode)
        return result

