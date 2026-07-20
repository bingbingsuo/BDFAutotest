"""
Generate JSON/HTML reports summarizing build and test outcomes
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from jinja2 import Environment, BaseLoader

from .models import BuildResult, TestResult, LLMAnalysis


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BDF Auto Test Report {{ timestamp }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 2rem; }
    .success { color: #2e7d32; }
    .failure { color: #c62828; }
    .expected-fail { color: #f9a825; }
    pre { background: #f5f5f5; padding: 1rem; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
    th, td { border: 1px solid #ddd; padding: 0.5rem; text-align: left; }
    .summary { margin-bottom: 1rem; }
  </style>
</head>
<body>
  <h1>BDF Auto Test Report</h1>
  <p>Generated: {{ timestamp }}</p>

  <div class="summary">
    <h2>Summary</h2>
    <ul>
      <li>Total tests: {{ summary.total_tests }}</li>
      <li><span class="success">Passed:</span> {{ summary.passed }}</li>
      <li><span class="failure">Failed:</span> {{ summary.failed }}</li>
      {% if summary.skipped > 0 %}
      <li>Skipped: {{ summary.skipped }}</li>
      {% endif %}
    </ul>
  </div>

  {% if git %}
  <div class="summary">
    <h2>Git Information</h2>
    <ul>
      {% if git.remote_url %}<li>Remote: {{ git.remote_url }}</li>{% endif %}
      {% if git.branch %}<li>Branch: {{ git.branch }}</li>{% endif %}
      {% if git.old_commit and git.new_commit %}
      <li>Updated: {{ git.old_commit[:7] if git.old_commit }} → {{ git.new_commit[:7] if git.new_commit }}</li>
      {% elif git.new_commit %}
      <li>HEAD: {{ git.new_commit[:7] }}</li>
      {% endif %}
    </ul>
  </div>
  {% endif %}

  {% if build_config %}
  <div class="summary">
    <h2>Build Configuration</h2>
    <ul>
      <li>Source dir: {{ build_config.source_dir if build_config.source_dir is defined else build_config.get('source_dir') }}</li>
      <li>Build dir: {{ build_config.build_dir if build_config.build_dir is defined else build_config.get('build_dir') }}</li>
      <li>Build command: {{ build_config.build_command if build_config.build_command is defined else build_config.get('build_command') }}</li>
      <li>Build mode: {{ build_config.build_mode if build_config.build_mode is defined else build_config.get('build_mode') }}</li>
      <li>Compiler set: {{ build_config.compiler_set if build_config.compiler_set is defined else build_config.get('compiler_set') }}</li>
      {% set cs = (build_config.compiler_set if build_config.compiler_set is defined else build_config.get('compiler_set')) %}
      {% set compilers = build_config.compilers if build_config.compilers is defined else build_config.get('compilers') %}
      {% if compilers and cs and cs in compilers %}
      <li>Fortran compiler: {{ compilers[cs].fortran if compilers[cs].fortran is defined else compilers[cs].get('fortran') }}</li>
      <li>C compiler: {{ compilers[cs].c if compilers[cs].c is defined else compilers[cs].get('c') }}</li>
      <li>C++ compiler: {{ compilers[cs].cpp if compilers[cs].cpp is defined else compilers[cs].get('cpp') }}</li>
      {% endif %}
      {% set use_mkl = build_config.use_mkl if build_config.use_mkl is defined else build_config.get('use_mkl') %}
      {% if use_mkl %}
      <li>Math library: MKL ({{ build_config.mkl_option if build_config.mkl_option is defined else build_config.get('mkl_option') }})</li>
      {% else %}
      {% set math = build_config.math_library if build_config.math_library is defined else build_config.get('math_library') %}
      {% if math %}
      <li>Math library (custom):</li>
      <ul>
        {% if math.mathinclude_flags is defined or math.get('mathinclude_flags') %}
        <li>Includes: {{ math.mathinclude_flags if math.mathinclude_flags is defined else math.get('mathinclude_flags') }}</li>
        {% endif %}
        {% if math.mathlib_flags is defined or math.get('mathlib_flags') %}
        <li>Lib flags: {{ math.mathlib_flags if math.mathlib_flags is defined else math.get('mathlib_flags') }}</li>
        {% endif %}
        {% if math.blasdir is defined or math.get('blasdir') %}
        <li>BLAS dir: {{ math.blasdir if math.blasdir is defined else math.get('blasdir') }}</li>
        {% endif %}
        {% if math.lapackdir is defined or math.get('lapackdir') %}
        <li>LAPACK dir: {{ math.lapackdir if math.lapackdir is defined else math.get('lapackdir') }}</li>
        {% endif %}
      </ul>
      {% endif %}
      {% endif %}
      {% if build_version %}
      <li>Build VERSION: {{ build_version }}</li>
      {% endif %}
    </ul>
  </div>
  {% endif %}

  <h2>Build Status: <span class="{{ 'success' if build.success else 'failure' }}">
    {{ 'Success' if build.success else 'Failure' }}
  </span></h2>

  {% if not build.success %}
  <h3>Build Errors</h3>
  <pre>{{ build.stderr }}</pre>
  {% if llm %}
  <h3>LLM Analysis</h3>
  <pre>{{ llm.summary }}</pre>
  {% endif %}
  {% endif %}

  <h2>Test Results</h2>
  {% if tests|length == 0 %}
    <p class="summary"><span class="success">All tests passed.</span> No failed tests to display.</p>
  {% else %}
    <table>
      <thead>
        <tr>
          <th>Test</th>
          <th>Status</th>
          <th>Solvent</th>
          <th>Exit Code</th>
          <th>Matched</th>
          <th>Files</th>
          <th>Differences</th>
        </tr>
      </thead>
      <tbody>
        {% for test in tests %}
        <tr>
          <td>{{ test.test_case.name }}</td>
          <td class="{{ 'success' if test.success else 'failure' }}">
            {{ 'Pass' if test.success else 'Fail' }}
          </td>
          <td>
            {% if test.test_case.solvent_info and test.test_case.solvent_info.enabled %}
              {{ test.test_case.solvent_info.model.value | upper if test.test_case.solvent_info.model.value is defined else test.test_case.solvent_info.model | upper }}
              ({{ test.test_case.solvent_info.solvent_name }})
            {% else %}
              —
            {% endif %}
          </td>
          <td>{{ test.exit_code }}</td>
          <td>
            {% if test.comparison is not none and test.comparison.matched is not none %}
              {{ 'Yes' if test.comparison.matched else 'No' }}
            {% else %}
              &mdash;
            {% endif %}
          </td>
          <td>
            <div>Log: {{ test.test_case.log_file }}</div>
            <div>Check: {{ test.test_case.log_file.with_suffix('.check') }}</div>
            <div>Ref: {{ test.test_case.reference_file }}</div>
          </td>
          <td>
            {% if not test.success and test.comparison %}
            <pre>{{ test.comparison.differences }}</pre>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}

  {% if llm and tests|length > 0 %}
  <h2>LLM Analysis</h2>
  <div class="summary">
    <p>The following analysis was generated by the LLM for the test failure{% if tests|length > 1 %}s{% endif %}{% if tests|length > 0 %} (analyzed: {{ tests[0].test_case.name }}){% endif %}:</p>
    <pre>{{ llm.summary }}</pre>
    {% if llm.suggestions %}
    <h3>Suggestions</h3>
    <ul>
      {% for suggestion in llm.suggestions %}
      <li>{{ suggestion }}</li>
      {% endfor %}
    </ul>
    {% endif %}
  </div>
  {% endif %}
</body>
</html>
"""


class ReportGenerator:
    """Create HTML and JSON reports"""

    def __init__(self, config: Dict[str, Any]):
        reporting_cfg = config.get("reporting", {})
        self.output_dir = Path(reporting_cfg.get("output_dir", "./reports"))
        self.formats = reporting_cfg.get("format", ["html"])
        self.include_llm = reporting_cfg.get("include_llm_analysis", True)
        self.timestamp_format = reporting_cfg.get("timestamp_format", "%Y-%m-%d_%H-%M-%S")

    def generate(
        self,
        build_result: BuildResult,
        test_results: List[TestResult],
        llm_analysis: Optional[LLMAnalysis] = None,
        git_info: Optional[Dict[str, Any]] = None,
        build_config: Optional[Dict[str, Any]] = None,
        version_info: Optional[str] = None,
    ) -> Dict[str, Path]:
        timestamp = datetime.now().strftime(self.timestamp_format)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        artifacts: Dict[str, Path] = {}
        # Basic summary for quick inspection and machine use
        total_tests = len(test_results)
        passed = sum(1 for r in test_results if r.success)
        failed = total_tests - passed
        failed_results = [r for r in test_results if not r.success]
        summary = {
            "total_tests": total_tests,
            "passed": passed,
            "failed": failed,
            "skipped": 0,  # reserved for future use
        }
        payload = {
            "timestamp": timestamp,
            "build": self._build_payload(build_result),
            # Only include failed tests in the detailed list
            "tests": [self._test_payload(result) for result in failed_results],
            "llm": llm_analysis.summary if (llm_analysis and self.include_llm) else None,
            "summary": summary,
            "git": git_info,
            "build_config": build_config,
            "build_version": version_info,
        }

        if "json" in self.formats:
            path = self.output_dir / f"report_{timestamp}.json"
            path.write_text(json.dumps(payload, indent=2))
            artifacts["json"] = path

        if "html" in self.formats:
            # autoescape=True so that build.stderr / comparison differences /
            # LLM output are rendered as text rather than executed as HTML.
            env = Environment(loader=BaseLoader(), autoescape=True)
            template = env.from_string(HTML_TEMPLATE)
            html = template.render(
                timestamp=timestamp,
                build=build_result,
                # Only include failed tests in the detailed table
                tests=failed_results,
                llm=llm_analysis,
                summary=summary,
                git=git_info,
                build_config=build_config,
                build_version=version_info,
            )
            path = self.output_dir / f"report_{timestamp}.html"
            path.write_text(html)
            artifacts["html"] = path

        return artifacts

    def _build_payload(self, result: BuildResult) -> Dict[str, Any]:
        return {
            "success": result.success,
            "command": result.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration": result.duration,
        }

    def _test_payload(self, result: TestResult) -> Dict[str, Any]:
        comparison = result.comparison
        payload = {
            "name": result.test_case.name,
            "success": result.success,
            "command": result.command,
            "exit_code": result.exit_code,
            "comparison": {
                "matched": comparison.matched if comparison else None,
                "differences": comparison.differences if comparison else None,
            },
        }
        # Include solvent info if present
        si = result.test_case.solvent_info
        if si is not None:
            # Support both SolventInfo object and dict forms
            if hasattr(si, "enabled"):
                payload["solvent"] = {
                    "enabled": si.enabled,
                    "model": si.model.value if hasattr(si.model, "value") else str(si.model),
                    "solvent_name": si.solvent_name,
                }
            elif isinstance(si, dict):
                payload["solvent"] = si
        return payload

