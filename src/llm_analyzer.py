"""
LLM integration layer for analyzing build and test failures.

Supports:
- Local LLM via HTTP endpoint (e.g., Ollama)
- Remote LLM (currently OpenAI) via HTTPS
- 'auto' mode: try local first, then remote as fallback
"""

import logging
import os
import re
from typing import Dict, Any, Optional, List

import requests

from .models import BuildResult, TestResult, LLMAnalysis
from .error_event_parser import ErrorEventParser

# Knowledge library: Known false positive patterns (non-errors that contain "error" keyword)
# These patterns should not be treated as errors when found in logs
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"(?i)IsOrthogonalizeDiisErrorMatrix\s*=", re.IGNORECASE),
    # Add more false positive patterns here as needed
]

# Domain knowledge shown when a failure log indicates a missing Python h5py
# package: BDF built with HDF5 runs Python helpers (bdfhdf5.py etc.) during
# checkpoint/restart, and those require the h5py runtime.
H5PY_MISSING_NOTE = (
    "BDF was built with HDF5 support; its Python helper scripts "
    "(bdfhdf5.py, bdfh5_manifest.py) import h5py at runtime. "
    "This failure is caused by the missing 'h5py' Python package, not by BDF "
    "code. Fix: pip install h5py (declared in requirements.txt), or rebuild "
    "BDF without HDF5 (build.hdf5.enabled: false)."
)


class LLMAnalyzer:
    """Provide failure analysis using configured LLM provider"""

    def __init__(self, config: Dict[str, Any], logger: Optional[logging.Logger] = None):
        llm_cfg = config.get("llm", {})
        self.mode = llm_cfg.get("mode", "auto")  # 'local', 'remote', or 'auto'
        self.analysis_mode = llm_cfg.get("analysis_mode", "simple")  # 'simple' or 'detailed'
        self.max_tokens = llm_cfg.get("max_tokens", 2000)
        self.temperature = llm_cfg.get("temperature", 0.3)

        # Sub-configs
        self.local_cfg = llm_cfg.get("local", {}) or {}
        self.remote_cfg = llm_cfg.get("remote", {}) or {}

        # Derived local settings
        self.local_enabled = bool(self.local_cfg.get("enabled", True))
        self.local_endpoint = self.local_cfg.get("endpoint", "http://localhost:11434")
        self.local_model = self.local_cfg.get("model", "gpt-oss:120b")
        self.local_timeout = int(self.local_cfg.get("timeout", 60))  # Timeout in seconds

        # Derived remote settings (OpenAI, OpenRouter, DeepSeek, Groq, etc.)
        self.remote_enabled = bool(self.remote_cfg.get("enabled", True))
        self.remote_provider = self.remote_cfg.get("provider", "openai")
        self.remote_model = self.remote_cfg.get("model", "gpt-4o")
        self.remote_api_key_env = self.remote_cfg.get("api_key_env", "OPENAI_API_KEY")

        self.logger = logger or logging.getLogger("bdf_autotest.llm")

    def analyze_build_failure(self, build_result: BuildResult) -> Optional[LLMAnalysis]:
        """Generate an analysis for a failed build"""
        if self.analysis_mode == "simple":
            return self._simple_build_analysis(build_result)
        else:
            prompt = self._build_failure_prompt(build_result)
            return self._request_llm(prompt, topic="build failure")

    def analyze_test_failure(self, test_result: TestResult) -> Optional[LLMAnalysis]:
        """Generate an analysis for a failed test"""
        if self.analysis_mode == "simple":
            return self._simple_test_analysis(test_result)
        else:
            prompt = self._test_failure_prompt(test_result)
            return self._request_llm(prompt, topic=f"test {test_result.test_case.name}")

    def _build_failure_prompt(self, build_result: BuildResult) -> str:
        return (
            "You are an expert build engineer. Analyze the following build failure and "
            "provide root cause insights and recommended fixes.\n\n"
            f"Command: {' '.join(build_result.command)}\n"
            f"Exit Code: {build_result.exit_code}\n"
            f"Stdout:\n{build_result.stdout}\n\n"
            f"Stderr:\n{build_result.stderr}\n"
        )

    def _test_failure_prompt(self, test_result: TestResult) -> str:
        comparison = test_result.comparison
        differences = comparison.differences if comparison else "N/A"

        # Get full output text for module detection
        from pathlib import Path
        error_text = ""
        if test_result.test_case and test_result.test_case.log_file:
            log_path = Path(test_result.test_case.log_file)
            if log_path.exists():
                try:
                    error_text = log_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    error_text = (test_result.stderr or "") + "\n" + (test_result.stdout or "")
        else:
            error_text = (test_result.stderr or "") + "\n" + (test_result.stdout or "")

        # Detect failed modules and build module‑specific guidance
        failed_modules = self._detect_failed_modules(error_text)
        module_info = self._build_module_context(failed_modules, error_text)

        # Build a short log excerpt (last ~80 non-empty lines) to keep prompt focused
        excerpt = ""
        if error_text:
            lines = [ln for ln in error_text.splitlines() if ln.strip()]
            tail_lines = lines[-80:]
            excerpt = "\n".join(tail_lines)

        # Add important domain knowledge about MCSCF and grad module relationship
        domain_knowledge = ""
        if failed_modules and "mcscf" in failed_modules:
            domain_knowledge = (
                "\nIMPORTANT DOMAIN KNOWLEDGE:\n"
                "- The grad module calculates the gradient of MCSCF energy.\n"
                "- If MCSCF calculation stops or fails, the grad module will still be executed by bdfdrv.py.\n"
                "- However, when MCSCF fails, the grad results will be incomplete or incorrect since they depend on MCSCF energy.\n"
                "- Missing CHECKDATA:GRAD lines or incomplete gradient data when MCSCF fails is expected behavior.\n"
                "- The root cause should focus on why MCSCF failed, not on missing grad data.\n\n"
            )
        
        # Check if TDDFT tolerance issues are present
        if test_result.comparison and test_result.comparison.differences:
            diff_text = test_result.comparison.differences.lower()
            if "tddft" in diff_text and "tolerance" in diff_text:
                if domain_knowledge:
                    domain_knowledge += "\n"
                domain_knowledge += (
                    "IMPORTANT DOMAIN KNOWLEDGE (TDDFT):\n"
                    "- TDDFT energy differences may be caused by different default settings in TDDFT from the reference value.\n"
                    "- This commonly happens during development when default parameters are changed.\n"
                    "- To investigate: Check out an old version from the git repository to compare default settings and identify what changed.\n"
                    "- Compare TDDFT input parameters and default values between the reference version and current version.\n\n"
                )
        
        # Check if NMR-related failures are present
        error_text_lower = error_text.lower()
        if "nmr" in error_text_lower or (test_result.comparison and "nmr" in test_result.comparison.differences.lower()):
            # Check if it's a failure (not just tolerance)
            is_nmr_failure = (
                "nmr" in failed_modules or
                "segmentation" in error_text_lower or
                (test_result.comparison and "line count differs" in test_result.comparison.differences.lower())
            )
            if is_nmr_failure:
                if domain_knowledge:
                    domain_knowledge += "\n"
                domain_knowledge += (
                    "IMPORTANT DOMAIN KNOWLEDGE (NMR):\n"
                    "- NMR (Nuclear Magnetic Response) calculation failures may indicate bugs in the NMR module.\n"
                    "- If NMR calculation fails with segmentation fault or produces incomplete output, "
                    "this is a known issue that needs to be checked and fixed in the NMR module code.\n"
                    "- The NMR module may have bugs that cause calculation failures.\n\n"
                )
        
        # Check if NRCC-related failures are present
        if "nrcc" in error_text_lower or (test_result.comparison and "nrcc" in test_result.comparison.differences.lower()):
            # Check if it's a failure (missing output)
            is_nrcc_failure = (
                "nrcc" in failed_modules or
                (test_result.comparison and "line count differs" in test_result.comparison.differences.lower())
            )
            if is_nrcc_failure:
                if domain_knowledge:
                    domain_knowledge += "\n"
                domain_knowledge += (
                    "IMPORTANT DOMAIN KNOWLEDGE (NRCC):\n"
                    "- NRCC (Coupled Cluster) calculation failures may indicate program bugs in the NRCC module.\n"
                    "- If NRCC calculation fails or produces incomplete output (missing CHECKDATA:NRCC lines), "
                    "this is a known issue that needs to be checked and fixed in the NRCC module code.\n"
                    "- NRCC module may have bugs that cause calculation failures.\n\n"
                )

        # Environment issue: missing Python h5py package for HDF5-enabled builds
        if ErrorEventParser.H5PY_MISSING_PATTERN.search(error_text):
            if domain_knowledge:
                domain_knowledge += "\n"
            domain_knowledge += (
                "IMPORTANT DOMAIN KNOWLEDGE (h5py runtime):\n"
                f"- {H5PY_MISSING_NOTE}\n\n"
            )

        return (
            "You are an expert in quantum‑chemistry program debugging, especially for the BDF package.\n"
            "A regression test failed when comparing output to reference CHECKDATA.\n"
            "Identify the most likely root cause(s) and propose concrete debugging steps.\n\n"
            f"Test Name: {test_result.test_case.name}\n"
            f"Command: {' '.join(test_result.command)}\n"
            f"Exit Code: {test_result.exit_code}\n"
            f"{module_info}"
            f"{domain_knowledge}"
            "Please structure your answer into:\n"
            "1) Short TL;DR\n"
            "2) Likely root causes (with specific BDF modules / stages)\n"
            "3) Concrete debugging steps (commands, input edits, code areas to inspect)\n"
            "4) Any notes about numerical tolerances vs real bugs.\n\n"
            f"Differences (reference vs current CHECKDATA, may be truncated):\n{differences}\n\n"
            f"Relevant log excerpt (tail of module output):\n{excerpt}\n"
        )

    def _build_module_context(self, failed_modules: set, error_text: str) -> str:
        """
        Build a module-specific context string to guide the LLM.

        Uses known BDF modules (mcscf, scf, compass, xuanyuan, etc.) and
        explains their role and typical failure modes.
        """
        if not failed_modules:
            if error_text:
                return (
                    "Failed Module(s): unknown\n"
                    "Note: Module detection attempted but no clear 'Start/End running module' "
                    "patterns were found in the log. Treat this as a generic test failure.\n\n"
                )
            return ""

        modules_sorted = sorted(failed_modules)

        # Short descriptions for common modules
        module_descriptions = {
            "mcscf": (
                "mcscf: Multiconfigurational SCF (CASSCF) module. Sensitive to active‑space "
                "definition, symmetry, CI dimensions and integral/scratch handling. "
                "IMPORTANT: If MCSCF calculation stops or fails, the grad module will still be "
                "executed by bdfdrv.py, but the gradient results will be incomplete or incorrect "
                "since they depend on MCSCF energy."
            ),
            "grad": (
                "grad: Gradient calculation module. Calculates the gradient of MCSCF energy. "
                "IMPORTANT: This module will still execute even if MCSCF fails, but the results "
                "will be incomplete since it depends on successful MCSCF completion. When MCSCF "
                "fails, missing CHECKDATA:GRAD lines in the output are expected."
            ),
            "scf": (
                "scf: Hartree–Fock / DFT SCF driver. Typical issues: SCF convergence, wrong "
                "occupations, bad initial guess, or numerical instabilities."
            ),
            "compass": (
                "compass: Preprocessing / symmetry detection and molecule setup. Typical "
                "issues: bad geometry, basis‑set problems, symmetry detection."
            ),
            "xuanyuan": (
                "xuanyuan: One electron and two electron integrals, Spin–orbit and related relativistic/effective Hamiltonian integrals. "
                "Typical issues: missing SO integrals, inconsistent basis/symmetry data."
            ),
            "tddft": (
                "tddft: Time‑dependent DFT excitation calculations. Typical issues: root "
                "selection, convergence, response solver failures. "
                "IMPORTANT: TDDFT energy differences may be caused by different default settings "
                "in TDDFT from the reference value. This commonly happens during development. "
                "To investigate, check out an old version from the git repository to compare "
                "default settings and identify what changed."
            ),
            "mp2": (
                "mp2: Second‑order Møller–Plesset correlation. Typical issues: bad orbital "
                "energies, insufficient virtual space, or integral problems."
            ),
            "mrci": (
                "mrci: Multireference CI on top of MCSCF. Typical issues: CI space too large, "
                "memory exhaustion, or inconsistent reference space."
            ),
            "nmr": (
                "nmr: Nuclear Magnetic Response (NMR) calculations. Calculates NMR shielding constants "
                "and chemical shifts. IMPORTANT: NMR module may have bugs that cause calculation failures. "
                "If NMR calculation fails (segmentation fault or incomplete output), this is a known issue "
                "that needs to be checked and fixed in the NMR module code."
            ),
            "nrcc": (
                "nrcc: Coupled Cluster (CC) calculations including CCD, CCSD, and EOM-CC methods. "
                "IMPORTANT: NRCC calculation failures (missing output or incomplete calculations) may indicate "
                "program bugs in the NRCC module. If NRCC calculation fails or produces incomplete output, "
                "this is a known issue that needs to be checked and fixed in the NRCC module code."
            ),
        }

        lines: list[str] = []
        lines.append(f"Failed Module(s): {', '.join(modules_sorted)}")

        # Add per-module hints for known modules
        for mod in modules_sorted:
            key = mod.lower()
            desc = module_descriptions.get(key)
            if desc:
                lines.append(f"- {desc}")

        lines.append("")  # blank line at end
        return "\n".join(lines) + "\n"

    def _request_llm(self, prompt: str, topic: str) -> Optional[LLMAnalysis]:
        """
        Dispatch to local, remote, or both (auto) according to configuration.
        """
        try:
            if self.mode == "local":
                if not self.local_enabled:
                    self.logger.warning("Local LLM disabled; skipping LLM call.")
                    return None
                response_text = self._call_local_llm(prompt)
            elif self.mode == "remote":
                if not self.remote_enabled:
                    self.logger.warning("Remote LLM disabled; skipping LLM call.")
                    return None
                response_text = self._call_remote_llm(prompt)
            else:  # auto
                response_text = None
                if self.local_enabled:
                    try:
                        response_text = self._call_local_llm(prompt)
                    except Exception as local_exc:  # noqa: broad-except
                        self.logger.warning(
                            "Local LLM request failed for %s: %s; attempting remote fallback.",
                            topic,
                            local_exc,
                        )
                if response_text is None and self.remote_enabled:
                    response_text = self._call_remote_llm(prompt)
                if response_text is None:
                    return None
        except Exception as exc:  # noqa: broad-except
            self.logger.error("LLM request failed for %s: %s", topic, exc)
            return None

        suggestions = self._extract_suggestions(response_text)
        return LLMAnalysis(summary=response_text, suggestions=suggestions, raw_response=response_text)

    def _call_local_llm(self, prompt: str) -> str:
        """Call a local LLM endpoint (e.g., Ollama)"""
        payload = {
            "model": self.local_model,
            "prompt": prompt,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        url = f"{self.local_endpoint.rstrip('/')}/api/generate"
        self.logger.debug("Calling local LLM at %s with model %s (timeout=%ds)", url, self.local_model, self.local_timeout)
        response = requests.post(url, json=payload, timeout=self.local_timeout)
        response.raise_for_status()
        data = response.json()
        return data.get("response") or data.get("text") or ""

    def _call_remote_llm(self, prompt: str) -> str:
        """
        Call remote LLM via the configured provider.
        """
        provider = (self.remote_provider or "openai").lower()
        if provider == "openai":
            return self._call_openai_chat(prompt)
        elif provider == "openrouter":
            return self._call_openrouter_chat(prompt)
        elif provider == "deepseek":
            return self._call_deepseek_chat(prompt)
        elif provider == "groq":
            return self._call_groq_chat(prompt)
        elif provider == "minimax":
            return self._call_minimax_chat(prompt)
        else:
            raise RuntimeError(f"Remote provider {self.remote_provider!r} is not supported yet.")

    def _call_openai_compatible_chat(self, *, url: str, prompt: str, headers_extra: Optional[Dict[str, str]] = None, log_provider: str = "OpenAI") -> str:
        """
        Helper for OpenAI‑compatible chat completion APIs.

        Many providers (OpenAI, DeepSeek, Groq, some proxies) use the same schema:
        POST <url> with {model, messages, max_tokens, temperature}.
        """
        api_key = os.getenv(self.remote_api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Remote LLM API key not found in environment variable {self.remote_api_key_env}. "
                "Set it before running the orchestrator."
            )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if headers_extra:
            headers.update(headers_extra)

        payload = {
            "model": self.remote_model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant for build and numerical test failures."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        self.logger.debug("Calling remote LLM via %s model=%s", log_provider, self.remote_model)
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return message.get("content", "") or ""

    def _call_openai_chat(self, prompt: str) -> str:
        """Call OpenAI-compatible chat completion API (api.openai.com)."""
        url = "https://api.openai.com/v1/chat/completions"
        return self._call_openai_compatible_chat(url=url, prompt=prompt, log_provider="OpenAI")

    def _call_openrouter_chat(self, prompt: str) -> str:
        """Call OpenRouter chat completion API (https://openrouter.ai)."""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers_extra = {
            # Optional but recommended for OpenRouter; users can override via env/proxy if needed.
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://bdf-autotest.local"),  # noqa: E501
            "X-Title": os.getenv("OPENROUTER_X_TITLE", "BDF AutoTest"),  # noqa: E501
        }
        return self._call_openai_compatible_chat(
            url=url,
            prompt=prompt,
            headers_extra=headers_extra,
            log_provider="OpenRouter",
        )

    def _call_deepseek_chat(self, prompt: str) -> str:
        """Call DeepSeek chat completion API (https://api.deepseek.com)."""
        url = "https://api.deepseek.com/chat/completions"
        return self._call_openai_compatible_chat(
            url=url,
            prompt=prompt,
            log_provider="DeepSeek",
        )

    def _call_groq_chat(self, prompt: str) -> str:
        """Call Groq chat completion API (OpenAI-compatible endpoint)."""
        url = "https://api.groq.com/openai/v1/chat/completions"
        return self._call_openai_compatible_chat(
            url=url,
            prompt=prompt,
            log_provider="Groq",
        )

    def _call_minimax_chat(self, prompt: str) -> str:
        """
        Call MiniMax chat completion API (Anthropic-compatible endpoint).

        MiniMax uses an Anthropic-compatible /v1/messages endpoint.
        API key is read from the configured env var (default: MINIMAX_CN_API_KEY).
        """
        api_key = os.getenv(self.remote_api_key_env)
        if not api_key:
            raise RuntimeError(
                f"MiniMax LLM API key not found in environment variable {self.remote_api_key_env}. "
                "Set it before running the orchestrator."
            )

        # MiniMax Anthropic-compatible endpoint
        base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/anthropic")
        url = f"{base_url.rstrip('/')}/v1/messages"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self.remote_model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        self.logger.debug("Calling MiniMax LLM at %s with model=%s", url, self.remote_model)
        response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()

        # Anthropic returns {"content": [...], "id": "...", ...}
        content = data.get("content") or []
        if isinstance(content, list) and content:
            return content[0].get("text", "") or ""
        return ""

    def _simple_build_analysis(self, build_result: BuildResult) -> LLMAnalysis:
        """Extract basic information from build failure without LLM"""
        lines = []
        lines.append("## Build Failure Summary")
        lines.append("")
        lines.append(f"**Command:** {' '.join(build_result.command)}")
        lines.append(f"**Exit Code:** {build_result.exit_code}")
        lines.append("")
        
        # Extract error messages
        error_text = build_result.stderr or build_result.stdout or ""
        if error_text:
            lines.append("**Error Messages:**")
            # Extract key error lines (non-empty, likely errors)
            # Filter out false positives (known non-errors)
            error_lines = [
                line.strip() for line in error_text.splitlines() 
                if line.strip() and any(keyword in line.lower() 
                for keyword in ['error', 'failed', 'fatal', 'undefined', 'cannot'])
                and not any(pattern.search(line) for pattern in FALSE_POSITIVE_PATTERNS)
            ]
            if error_lines:
                for line in error_lines[:10]:  # Limit to first 10 error lines
                    lines.append(f"- {line}")
            else:
                # If no obvious error keywords, show last few lines
                last_lines = [line.strip() for line in error_text.splitlines()[-5:] if line.strip()]
                for line in last_lines:
                    lines.append(f"- {line}")
            lines.append("")
        
        summary = "\n".join(lines)
        return LLMAnalysis(summary=summary, suggestions=[], raw_response=None)

    def _detect_failed_modules(self, output_text: str) -> set:
        """
        Detect which BDF modules failed by looking for "Start running module X" 
        without corresponding "End running module X".
        
        Returns a set of module names that started but didn't end successfully.
        """
        import re
        
        # Pattern to match " Start running module <name>" or "Start running module <name>"
        # Handles optional leading/trailing whitespace
        start_pattern = r'\s*Start\s+running\s+module\s+(\w+)'
        end_pattern = r'\s*End\s+running\s+module\s+(\w+)'
        
        # Find all modules that started
        started_modules = {}
        for match in re.finditer(start_pattern, output_text, re.IGNORECASE):
            module_name = match.group(1).lower()
            # Store the position where module started
            started_modules[module_name] = match.start()
        
        # Find all modules that ended
        ended_modules = set()
        for match in re.finditer(end_pattern, output_text, re.IGNORECASE):
            module_name = match.group(1).lower()
            ended_modules.add(module_name)
        
        # Modules that started but didn't end are the failed ones
        failed_modules = set()
        for module, start_pos in started_modules.items():
            if module not in ended_modules:
                failed_modules.add(module)
        
        # If we found failed modules, return them
        if failed_modules:
            return failed_modules
        
        # Fallback: if no clear pattern found, try to find the last module that started
        # This helps when the output is truncated
        if started_modules:
            # Sort by position (last one to start is likely the one that failed)
            sorted_modules = sorted(started_modules.items(), key=lambda x: x[1], reverse=True)
            # Return the last module that started (most likely to have failed)
            last_module = sorted_modules[0][0]
            if last_module not in ended_modules:
                return {last_module}
        
        return set()

    def _simple_test_analysis(self, test_result: TestResult) -> LLMAnalysis:
        """Extract basic information from test failure without LLM"""
        import re
        from pathlib import Path
        
        lines = []
        lines.append("## Test Failure Summary")
        lines.append("")
        lines.append(f"**Test Name:** {test_result.test_case.name}")
        lines.append(f"**Exit Code:** {test_result.exit_code}")
        lines.append("")
        
        # Get full output text - try to read log file first, then fall back to stdout/stderr
        error_text = ""
        if test_result.test_case and test_result.test_case.log_file:
            log_path = Path(test_result.test_case.log_file)
            if log_path.exists():
                try:
                    error_text = log_path.read_text(encoding='utf-8', errors='ignore')
                except Exception:
                    error_text = (test_result.stderr or "") + "\n" + (test_result.stdout or "")
        else:
            error_text = (test_result.stderr or "") + "\n" + (test_result.stdout or "")
        
        # Detect failed modules using "Start running module X" and "End running module X" patterns
        failed_modules = self._detect_failed_modules(error_text)
        
        if failed_modules:
            lines.append("**Failed Module(s):**")
            for module in sorted(failed_modules):
                lines.append(f"- {module}")
            lines.append("")
            
            # Add domain knowledge note if MCSCF failed
            if "mcscf" in failed_modules:
                lines.append("**Note:** The grad module calculates gradient of MCSCF energy. ")
                lines.append("If MCSCF fails, grad will still execute but produce incomplete results. ")
                lines.append("Missing CHECKDATA:GRAD lines when MCSCF fails is expected behavior.")
                lines.append("")
        
        # Extract key error messages
        lines.append("**Error Messages:**")
        error_lines = []
        for line in error_text.splitlines():
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in ['error', 'failed', 'fatal', 'segmentation', 'abort', 'crash', 'return code']):
                # Skip false positives (known non-errors)
                if any(pattern.search(line) for pattern in FALSE_POSITIVE_PATTERNS):
                    continue
                error_lines.append(line.strip())
        
        if error_lines:
            for line in error_lines[:15]:  # Limit to first 15 error lines
                lines.append(f"- {line}")
        else:
            # Show last few lines if no obvious errors
            last_lines = [line.strip() for line in error_text.splitlines()[-10:] if line.strip()]
            for line in last_lines:
                lines.append(f"- {line}")
        lines.append("")

        # Environment note: missing Python h5py package (HDF5-enabled builds)
        if ErrorEventParser.H5PY_MISSING_PATTERN.search(error_text):
            lines.append("**Note:** " + H5PY_MISSING_NOTE)
            lines.append("")

        # Add comparison differences if available
        if test_result.comparison and test_result.comparison.differences:
            lines.append("**Comparison Differences:**")
            diff_preview = test_result.comparison.differences[:500]  # Limit preview
            lines.append(f"```\n{diff_preview}")
            if len(test_result.comparison.differences) > 500:
                lines.append("... (truncated)")
            lines.append("```")
            lines.append("")
            
            # Add TDDFT-specific note if applicable
            diff_text = test_result.comparison.differences.lower()
            if "tddft" in diff_text and "tolerance" in diff_text:
                lines.append("**Note:** TDDFT energy differences may be caused by different default ")
                lines.append("settings in TDDFT from the reference value. This commonly happens during ")
                lines.append("development. To investigate, check out an old version from the git ")
                lines.append("repository to compare default settings and identify what changed.")
                lines.append("")
            
            # Add NMR-specific note if applicable
            if "nmr" in diff_text:
                error_text_lower = error_text.lower()
                is_nmr_failure = (
                    "segmentation" in error_text_lower or
                    "line count differs" in diff_text
                )
                if is_nmr_failure:
                    lines.append("**Note:** NMR (Nuclear Magnetic Response) calculation failures may ")
                    lines.append("indicate bugs in the NMR module. If NMR calculation fails with ")
                    lines.append("segmentation fault or produces incomplete output, this is a known ")
                    lines.append("issue that needs to be checked and fixed in the NMR module code.")
                    lines.append("")
            
            # Add NRCC-specific note if applicable
            if "nrcc" in diff_text:
                is_nrcc_failure = "line count differs" in diff_text
                if is_nrcc_failure:
                    lines.append("**Note:** NRCC (Coupled Cluster) calculation failures may indicate ")
                    lines.append("program bugs in the NRCC module. If NRCC calculation fails or produces ")
                    lines.append("incomplete output (missing CHECKDATA:NRCC lines), this is a known issue ")
                    lines.append("that needs to be checked and fixed in the NRCC module code.")
                    lines.append("")
        
        summary = "\n".join(lines)
        return LLMAnalysis(summary=summary, suggestions=[], raw_response=None)

    def _extract_suggestions(self, text: str) -> List[str]:
        """Simple heuristic to extract bullet-like suggestions from LLM output"""
        suggestions = []
        for line in text.splitlines():
            stripped = line.strip(" -•\t")
            if stripped and (line.strip().startswith(("-", "*", "•")) or stripped.lower().startswith("suggestion")):
                suggestions.append(stripped)
        return suggestions

