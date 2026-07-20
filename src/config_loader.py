"""
Configuration loader module
Loads and validates configuration from YAML files
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional, List, Sequence


class ConfigLoader:
    """Load and validate configuration files"""
    
    def __init__(self, config_path: str):
        """
        Initialize configuration loader
        
        Args:
            config_path: Path to YAML configuration file
        """
        self.config_path = Path(config_path)
        self.config: Optional[Dict[str, Any]] = None
    
    def load(self) -> Dict[str, Any]:
        """
        Load configuration from file
        
        Returns:
            Configuration dictionary
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If config file is invalid YAML
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f)

        # Normalize path-related settings so users mainly configure git.local_path and build.build_dir.
        # - build.source_dir is forced to git.local_path when present.
        # - compile.working_dir is forced to build.source_dir/build.build_dir.
        self._normalize_paths()
        self._validate()
        return self.config
    
    def _validate(self):
        """Validate configuration structure"""
        if not self.config:
            raise ValueError("Configuration is empty")

        validators = [
            self._validate_git,
            self._validate_build,
            self._validate_compile,
            self._validate_llm,
            self._validate_tests,
            self._validate_reporting,
            self._validate_logging,
        ]

        errors: List[str] = []
        for validator in validators:
            errors.extend(validator())

        if errors:
            formatted = "\n - ".join(errors)
            raise ValueError(f"Configuration validation failed:\n - {formatted}")

    def _normalize_paths(self) -> None:
        """
        Normalize and derive path-related settings.

        Rules:
        - If git.local_path is set, force build.source_dir to the same value.
        - Derive compile.working_dir as build.source_dir/build.build_dir, overriding any user value.
        """
        if not isinstance(self.config, dict):
            return

        git_cfg = self.config.get("git") or {}
        build_cfg = self.config.get("build") or {}
        compile_cfg = self.config.get("compile") or {}

        # 1) build.source_dir <- git.local_path (when provided)
        local_path = git_cfg.get("local_path")
        if isinstance(local_path, str) and local_path.strip():
            build_cfg["source_dir"] = local_path
        else:
            # Fallback to an existing value or the historical default
            existing = build_cfg.get("source_dir")
            if not isinstance(existing, str) or not existing.strip():
                build_cfg["source_dir"] = "./package_source"

        source_dir = build_cfg.get("source_dir", "./package_source")

        # 2) compile.working_dir <- source_dir/build_dir
        build_dir = build_cfg.get("build_dir", "build")
        try:
            working_dir = str(Path(source_dir) / build_dir)
        except TypeError:
            # In case of non-string values, fall back to defaults
            working_dir = str(Path("./package_source") / "build")

        # Always override to avoid confusing users with multiple path knobs
        compile_cfg["working_dir"] = working_dir

        # Write back normalized sections
        self.config["build"] = build_cfg
        self.config["compile"] = compile_cfg
        
    # --- Section validators -------------------------------------------------

    def _validate_git(self) -> List[str]:
        errors: List[str] = []
        git_cfg = self.config.get("git")
        if not isinstance(git_cfg, dict):
            return ["Section 'git' must be a mapping."]

        self._require_strings(git_cfg, ["remote_url", "branch", "local_path"], errors, section="git")
        return errors

    def _validate_build(self) -> List[str]:
        errors: List[str] = []
        build_cfg = self.config.get("build")
        if not isinstance(build_cfg, dict):
            return ["Section 'build' must be a mapping."]

        # Users only need to provide build_dir, build_command and compiler_set.
        # build.source_dir is derived from git.local_path in _normalize_paths.
        self._require_strings(build_cfg, ["build_dir", "build_command", "compiler_set"], errors, section="build")

        compiler_set = build_cfg.get("compiler_set")
        compilers = build_cfg.get("compilers")
        if not isinstance(compilers, dict):
            errors.append("Section 'build.compilers' must be a mapping of compiler sets.")
        elif compiler_set not in compilers:
            errors.append(f"Compiler set '{compiler_set}' not found in build.compilers.")
        else:
            required_compiler_keys = ["fortran", "c", "cpp"]
            compiler_cfg = compilers[compiler_set]
            if not isinstance(compiler_cfg, dict):
                errors.append(f"Compiler definition for set '{compiler_set}' must be a mapping.")
            else:
                self._require_strings(compiler_cfg, required_compiler_keys, errors, section=f"build.compilers.{compiler_set}")

        use_mkl = build_cfg.get("use_mkl")
        if use_mkl:
            self._require_strings(build_cfg, ["mkl_option"], errors, section="build")
        else:
            math_cfg = build_cfg.get("math_library")
            if not isinstance(math_cfg, dict):
                errors.append("When build.use_mkl is false, 'build.math_library' must be provided.")
            else:
                self._require_strings(math_cfg, ["mathinclude_flags", "mathlib_flags", "blasdir", "lapackdir"], errors, section="build.math_library")

        always_use = build_cfg.get("always_use", [])
        if not isinstance(always_use, list) or not all(isinstance(item, str) for item in always_use):
            errors.append("'build.always_use' must be a list of strings.")

        additional_args = build_cfg.get("additional_args", [])
        if not isinstance(additional_args, list):
            errors.append("'build.additional_args' must be a list.")

        errors.extend(self._validate_hdf5(build_cfg))

        return errors

    def _validate_hdf5(self, build_cfg: Dict[str, Any]) -> List[str]:
        """
        Validate the optional build.hdf5 section.

        Shape:
            build:
              hdf5:
                enabled: false          # bool, default false
                hdf5home: null          # optional non-empty string
                allow_downgrade: true   # bool, default true

        Existence of the HDF5 home / library is checked at build time by
        resolve_hdf5(); the loader only checks types and shape here.
        """
        errors: List[str] = []
        hdf5_cfg = build_cfg.get("hdf5")
        if hdf5_cfg is None:
            return errors  # section optional
        if not isinstance(hdf5_cfg, dict):
            errors.append("'build.hdf5' must be a mapping.")
            return errors

        enabled = hdf5_cfg.get("enabled", False)
        # Note: in Python bool is a subclass of int, so isinstance(True, int)
        # is True; the check order below keeps them distinct.
        if not isinstance(enabled, bool):
            errors.append("'build.hdf5.enabled' must be a boolean.")

        hdf5home = hdf5_cfg.get("hdf5home")
        if hdf5home is not None:
            if not isinstance(hdf5home, str) or not hdf5home.strip():
                errors.append("'build.hdf5.hdf5home' must be a non-empty string when set.")
            # Allow $HDF5HOME / ${HDF5HOME} placeholders; resolved at build time.

        allow_downgrade = hdf5_cfg.get("allow_downgrade", True)
        if not isinstance(allow_downgrade, bool):
            errors.append("'build.hdf5.allow_downgrade' must be a boolean.")

        return errors

    def _validate_compile(self) -> List[str]:
        errors: List[str] = []
        compile_cfg = self.config.get("compile")
        if not isinstance(compile_cfg, dict):
            return ["Section 'compile' must be a mapping."]

        # compile.working_dir is derived from build.source_dir and build.build_dir in _normalize_paths.
        self._require_strings(compile_cfg, ["command", "log_file"], errors, section="compile")

        # jobs can be omitted or set to null/"auto" to enable automatic detection from CPU count.
        raw_jobs = compile_cfg.get("jobs", None)
        if raw_jobs is not None and raw_jobs != "auto":
            jobs_value = self._coerce_number(raw_jobs, integer=True, positive=True)
            if jobs_value is None:
                errors.append("'compile.jobs' must be a positive integer when specified.")
            else:
                compile_cfg["jobs"] = jobs_value

        extra_args = compile_cfg.get("extra_args", [])
        if not isinstance(extra_args, list):
            errors.append("'compile.extra_args' must be a list.")

        env_cfg = compile_cfg.get("environment", {})
        if not isinstance(env_cfg, dict):
            errors.append("'compile.environment' must be a mapping of environment variables.")

        return errors

    def _validate_llm(self) -> List[str]:
        errors: List[str] = []
        llm_cfg = self.config.get("llm")
        if not isinstance(llm_cfg, dict):
            return ["Section 'llm' must be a mapping."]

        mode = llm_cfg.get("mode", "auto")
        allowed_modes = {"local", "remote", "auto"}
        if mode not in allowed_modes:
            errors.append(f"'llm.mode' must be one of {sorted(allowed_modes)}.")

        analysis_mode = llm_cfg.get("analysis_mode", "simple")
        allowed_analysis = {"simple", "detailed"}
        if analysis_mode not in allowed_analysis:
            errors.append(f"'llm.analysis_mode' must be one of {sorted(allowed_analysis)}.")

        for field in ("max_tokens",):
            coerced = self._coerce_number(llm_cfg.get(field), integer=True, positive=True)
            if coerced is None:
                errors.append(f"'llm.{field}' must be a positive integer.")
            else:
                llm_cfg[field] = coerced

        temperature = self._coerce_number(llm_cfg.get("temperature"))
        if temperature is None:
            errors.append("'llm.temperature' must be a number.")
        else:
            llm_cfg["temperature"] = temperature

        local_cfg = llm_cfg.get("local", {})
        if not isinstance(local_cfg, dict):
            errors.append("'llm.local' must be a mapping.")
        elif local_cfg.get("enabled", True):
            self._require_strings(local_cfg, ["endpoint", "model"], errors, section="llm.local")
            timeout = self._coerce_number(local_cfg.get("timeout", 60), integer=True, positive=True)
            if timeout is None:
                errors.append("'llm.local.timeout' must be a positive integer.")
            else:
                local_cfg["timeout"] = timeout

        remote_cfg = llm_cfg.get("remote", {})
        if not isinstance(remote_cfg, dict):
            errors.append("'llm.remote' must be a mapping.")
        elif remote_cfg.get("enabled", True):
            self._require_strings(remote_cfg, ["provider", "model", "api_key_env"], errors, section="llm.remote")

        return errors

    def _validate_tests(self) -> List[str]:
        errors: List[str] = []
        tests_cfg = self.config.get("tests")
        if not isinstance(tests_cfg, dict):
            return ["Section 'tests' must be a mapping."]

        self._require_strings(
            tests_cfg,
            ["test_dir", "reference_dir", "input_pattern", "reference_pattern", "check_pattern", "test_command", "test_args_template"],
            errors,
            section="tests",
        )

        tolerance = self._coerce_number(tests_cfg.get("tolerance"))
        if tolerance is None:
            errors.append("'tests.tolerance' must be a number.")
        else:
            tests_cfg["tolerance"] = tolerance

        timeout = self._coerce_number(tests_cfg.get("timeout"), integer=True, positive=True)
        if timeout is None:
            errors.append("'tests.timeout' must be a positive integer.")
        else:
            tests_cfg["timeout"] = timeout

        enabled_range = tests_cfg.get("enabled_range", {})
        if not isinstance(enabled_range, dict):
            errors.append("'tests.enabled_range' must be a mapping with 'min' and 'max'.")
        else:
            min_id = self._coerce_number(enabled_range.get("min"), integer=True, positive=True)
            max_id = self._coerce_number(enabled_range.get("max"), integer=True, positive=True)
            if min_id is None or max_id is None:
                errors.append("'tests.enabled_range.min' and '.max' must be positive integers.")
            elif min_id > max_id:
                errors.append("'tests.enabled_range.min' cannot be greater than 'max'.")
            else:
                enabled_range["min"] = min_id
                enabled_range["max"] = max_id

        # Optional named test profiles, e.g. smoke/core/full
        profiles = tests_cfg.get("profiles")
        profile_name = tests_cfg.get("profile")
        if profiles is not None:
            if not isinstance(profiles, dict):
                errors.append("'tests.profiles' must be a mapping of profile_name -> {min, max}.")
            else:
                if profile_name is not None and profile_name not in profiles:
                    errors.append(f"'tests.profile' is set to '{profile_name}' but no such profile exists in tests.profiles.")
                for name, prof in profiles.items():
                    if not isinstance(prof, dict):
                        errors.append(f"'tests.profiles.{name}' must be a mapping with 'min' and 'max'.")
                        continue
                    p_min = self._coerce_number(prof.get("min"), integer=True, positive=True)
                    p_max = self._coerce_number(prof.get("max"), integer=True, positive=True)
                    if p_min is None or p_max is None:
                        errors.append(f"'tests.profiles.{name}.min' and '.max' must be positive integers.")
                    elif p_min > p_max:
                        errors.append(f"'tests.profiles.{name}.min' cannot be greater than 'max'.")
                    else:
                        prof["min"] = p_min
                        prof["max"] = p_max

        tolerance_mode = tests_cfg.get("tolerance_mode", "strict")
        if tolerance_mode not in {"strict", "loose"}:
            errors.append("'tests.tolerance_mode' must be either 'strict' or 'loose'.")

        scale_cfg = tests_cfg.get("tolerance_scale", {})
        if not isinstance(scale_cfg, dict):
            errors.append("'tests.tolerance_scale' must be a mapping.")
        else:
            for key in ("strict", "loose"):
                value = self._coerce_number(scale_cfg.get(key), positive=True)
                if value is None:
                    errors.append(f"'tests.tolerance_scale.{key}' must be a positive number.")
                else:
                    scale_cfg[key] = value

        env_cfg = tests_cfg.get("env", {})
        if not isinstance(env_cfg, dict):
            errors.append("'tests.env' must be a mapping of environment variables.")

        result_cfg = tests_cfg.get("result_extraction", {})
        if not isinstance(result_cfg, dict):
            errors.append("'tests.result_extraction' must be a mapping.")
        else:
            self._require_strings(result_cfg, ["method", "pattern"], errors, section="tests.result_extraction")

        # Optional: max_parallel for parallel test execution
        max_parallel = tests_cfg.get("max_parallel", 1)
        if max_parallel is not None:
            coerced = self._coerce_number(max_parallel, integer=True, positive=True)
            if coerced is None:
                errors.append("'tests.max_parallel' must be a positive integer.")
            else:
                tests_cfg["max_parallel"] = coerced

        return errors

    def _validate_reporting(self) -> List[str]:
        errors: List[str] = []
        reporting_cfg = self.config.get("reporting")
        if not isinstance(reporting_cfg, dict):
            return ["Section 'reporting' must be a mapping."]

        self._require_strings(reporting_cfg, ["output_dir", "timestamp_format"], errors, section="reporting")

        formats = reporting_cfg.get("format", [])
        allowed_formats = {"html", "json"}
        if not isinstance(formats, list) or not formats:
            errors.append("'reporting.format' must be a non-empty list.")
        elif any(fmt not in allowed_formats for fmt in formats):
            errors.append(f"'reporting.format' entries must be one of {sorted(allowed_formats)}.")

        include_llm = reporting_cfg.get("include_llm_analysis")
        if not isinstance(include_llm, bool):
            errors.append("'reporting.include_llm_analysis' must be a boolean.")

        return errors

    def _validate_logging(self) -> List[str]:
        errors: List[str] = []
        logging_cfg = self.config.get("logging")
        if not isinstance(logging_cfg, dict):
            return ["Section 'logging' must be a mapping."]

        self._require_strings(logging_cfg, ["log_dir", "log_file"], errors, section="logging")

        allowed_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        level = logging_cfg.get("level", "INFO")
        if level not in allowed_levels:
            errors.append(f"'logging.level' must be one of {sorted(allowed_levels)}.")

        return errors

    # --- Helper methods -----------------------------------------------------

    @staticmethod
    def _require_strings(
        cfg: Dict[str, Any],
        keys: Sequence[str],
        errors: List[str],
        *,
        section: str,
    ) -> None:
        """Ensure required keys exist and are strings"""
        for key in keys:
            value = cfg.get(key)
            if value is None:
                errors.append(f"Missing required key '{section}.{key}'.")
            elif not isinstance(value, str) or not value.strip():
                errors.append(f"'{section}.{key}' must be a non-empty string.")

    @staticmethod
    def _coerce_number(value: Any, *, integer: bool = False, positive: bool = False) -> Optional[float]:
        """
        Attempt to coerce a value to a number.
        Returns the coerced value (float or int) or None if conversion fails.
        """
        if isinstance(value, bool):
            return None

        coerced: Optional[float]
        if isinstance(value, (int, float)):
            coerced = float(value)
        elif isinstance(value, str):
            try:
                coerced = float(value.strip())
            except ValueError:
                return None
        else:
            return None

        if integer:
            if not coerced.is_integer():
                return None
            coerced = int(coerced)

        if positive and coerced <= 0:
            return None

        return coerced
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value by key (supports dot notation)
        
        Args:
            key: Configuration key (e.g., 'build.compilers.fortran.command')
            default: Default value if key not found
            
        Returns:
            Configuration value or default
        """
        if not self.config:
            raise RuntimeError("Configuration not loaded. Call load() first.")
        
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value

