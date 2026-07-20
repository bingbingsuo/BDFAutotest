"""
Main orchestration entrypoint for the BDF Auto Test Framework
"""

import argparse
from pathlib import Path
from typing import Optional, List
import sys
import time

try:
    from .config_loader import ConfigLoader
    from .logger import setup_logger
    from .git_manager import GitManager
    from .build_manager import BuildManager
    from .compile_manager import CompileManager
    from .compilation_analyzer import CompilationAnalyzer
    from .llm_analyzer import LLMAnalyzer
    from .test_runner import TestRunner
    from .report_generator import ReportGenerator
    from .models import BuildResult
    from .error_event_parser import ErrorEventParser
    from .utils import resolve_source_dir
except ImportError:  # pragma: no cover
    from config_loader import ConfigLoader  # type: ignore
    from logger import setup_logger  # type: ignore
    from git_manager import GitManager  # type: ignore
    from build_manager import BuildManager  # type: ignore
    from compile_manager import CompileManager  # type: ignore
    from compilation_analyzer import CompilationAnalyzer  # type: ignore
    from llm_analyzer import LLMAnalyzer  # type: ignore
    from test_runner import TestRunner  # type: ignore
    from report_generator import ReportGenerator  # type: ignore
    from models import BuildResult  # type: ignore
    from error_event_parser import ErrorEventParser  # type: ignore
    from utils import resolve_source_dir  # type: ignore


def run_workflow(
    config_path: str,
    skip_git: bool = False,
    skip_build: bool = False,
    skip_tests: bool = False,
    profile: Optional[str] = None,
) -> int:
    """
    Execute the full workflow: git pull -> build -> tests -> report
    Returns process exit code (0 success, non-zero failure)
    """
    loader = ConfigLoader(config_path)
    config = loader.load()

    # Optional test profile override from CLI (e.g. --profile smoke, --smoke)
    if profile:
        tests_cfg = config.setdefault("tests", {})
        tests_cfg["profile"] = profile
    logger = setup_logger(config=config)

    # Determine workflow mode from config (can be overridden by CLI flags)
    workflow_cfg = config.get("workflow", {})
    workflow_mode = workflow_cfg.get("mode", "full")
    
    # Map workflow mode to skip flags (CLI flags take precedence)
    if workflow_mode == "test-only":
        # Skip both git and build - only run tests
        skip_git = True
        skip_build = True
    elif workflow_mode == "build-test":
        # Skip git only - run build and tests
        skip_git = True
    # For "full" mode, use CLI-provided skip_git/skip_build values as-is
    
    # Capture build configuration (compiler and math library settings) for reporting
    build_config = config.get("build", {})
    version_info = None

    git_manager = GitManager(config, logger=logger)
    git_info = None
    if not skip_git:
        logger.info("Step 1: Syncing repository")
        try:
            old_commit, new_commit = git_manager.sync()
            git_info = {
                "remote_url": git_manager.remote_url,
                "branch": git_manager.branch,
                "old_commit": old_commit,
                "new_commit": new_commit,
            }
        except Exception as exc:  # noqa: broad-except
            logger.error("Git sync failed: %s", exc)
            return 1
    else:
        logger.info("Skipping git synchronization step")

    build_manager = BuildManager(config, logger=logger)
    setup_result: Optional[BuildResult] = None
    if not skip_build:
        logger.info("Step 2: Running setup")
        setup_result = build_manager.run()
    else:
        logger.info("Skipping setup step (assumed successful)")
        setup_result = _fake_successful_build(build_manager)

    llm_analyzer = LLMAnalyzer(config, logger=logger)
    report_generator = ReportGenerator(config)
    
    # Initialize error event parser and output directory
    error_parser = ErrorEventParser(logger=logger)
    all_error_events = []
    reporting_cfg = config.get("reporting", {})
    save_events = reporting_cfg.get("save_error_events", True)
    events_dir = Path(reporting_cfg.get("structured_events_dir", "./reports/error_events"))
    if save_events:
        events_dir.mkdir(parents=True, exist_ok=True)

    if not setup_result.success:
        logger.error("Setup failed; analyzing error")
        compilation_analyzer = CompilationAnalyzer(logger=logger)
        build_analysis = compilation_analyzer.analyze(setup_result)
        logger.debug("Compilation analysis: %s", build_analysis)
        llm_analysis = llm_analyzer.analyze_build_failure(setup_result)
        
        # Parse error event
        if save_events:
            error_event = error_parser.parse_build_result(setup_result, config)
            if error_event:
                all_error_events.append(error_event)
                _save_error_event(error_event, events_dir, logger)
        
        report_generator.generate(
            setup_result,
            test_results=[],
            llm_analysis=llm_analysis,
            git_info=git_info,
            build_config=build_config,
        )
        return 2

    compile_manager = CompileManager(config, logger=logger)
    compile_result = compile_manager.run()
    # Try to read VERSION file from build directory (if present)
    try:
        version_path = compile_manager.working_dir / "VERSION"
        if version_path.exists():
            version_info = version_path.read_text(encoding="utf-8").strip()
    except Exception:
        version_info = None
    if not compile_result.success:
        logger.error("Compilation failed; analyzing error")
        compilation_analyzer = CompilationAnalyzer(logger=logger)
        compile_analysis = compilation_analyzer.analyze(compile_result)
        logger.debug("Compilation analysis: %s", compile_analysis)
        llm_analysis = llm_analyzer.analyze_build_failure(compile_result)
        
        # Parse error event
        if save_events:
            error_event = error_parser.parse_build_result(compile_result, config)
            if error_event:
                all_error_events.append(error_event)
                _save_error_event(error_event, events_dir, logger)
        
        report_generator.generate(
            compile_result,
            test_results=[],
            llm_analysis=llm_analysis,
            git_info=git_info,
            build_config=build_config,
            version_info=version_info,
        )
        return 3

    test_results = []
    if not skip_tests:
        logger.info("Step 3: Running tests")
        test_runner = TestRunner(config, logger=logger)
        test_results = test_runner.run_all()
    else:
        logger.info("Skipping tests")

    llm_analysis = None
    failed_tests = [result for result in test_results if not result.success]
    if failed_tests:
        logger.warning("%s test(s) failed", len(failed_tests))
        
        # Parse error events for all failed tests
        if save_events:
            for test_result in failed_tests:
                test_events = error_parser.parse_test_result(test_result, config)
                for event in test_events:
                    all_error_events.append(event)
                    _save_error_event(event, events_dir, logger)
        
        # In simple mode, analyze all failed tests; in detailed mode, analyze first one (LLM is expensive)
        if llm_analyzer.analysis_mode == "simple" and len(failed_tests) > 1:
            # Combine analysis for all failed tests
            analyses = []
            for test_result in failed_tests:
                analysis = llm_analyzer.analyze_test_failure(test_result)
                if analysis:
                    analyses.append(f"### {test_result.test_case.name}\n\n{analysis.summary}")
            if analyses:
                from .models import LLMAnalysis
                combined_summary = "\n\n---\n\n".join(analyses)
                llm_analysis = LLMAnalysis(summary=combined_summary, suggestions=[], raw_response=None)
        else:
            # Detailed mode or single failure: analyze first failed test
            llm_analysis = llm_analyzer.analyze_test_failure(failed_tests[0])
    else:
        logger.info("All tests passed")

    # Save summary of all error events
    if save_events and all_error_events:
        _save_events_summary(all_error_events, events_dir, logger)

    report_generator.generate(
        compile_result,
        test_results,
        llm_analysis,
        git_info=git_info,
        build_config=build_config,
        version_info=version_info,
    )
    return 0 if not failed_tests else 4


def _save_error_event(event, events_dir: Path, logger):
    """Save a single error event to JSON file"""
    import json
    try:
        filename = f"error_event_{event.event_id}.json"
        filepath = events_dir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(event.to_dict(), f, indent=2, ensure_ascii=False)
        logger.debug("Saved error event %s to %s", event.event_id, filepath)
    except Exception as e:
        logger.warning("Failed to save error event %s: %s", event.event_id, e)


def _save_events_summary(events, events_dir: Path, logger):
    """Save summary of all error events"""
    import json
    from datetime import datetime
    try:
        summary = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "total_events": len(events),
            "events": [event.to_dict() for event in events],
        }
        filepath = events_dir / "events_summary.json"
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        logger.info("Saved error events summary (%d events) to %s", len(events), filepath)
    except Exception as e:
        logger.warning("Failed to save events summary: %s", e)


def _fake_successful_build(build_manager: BuildManager) -> BuildResult:
    """Create a dummy successful build result when skipping build step"""
    return BuildResult(
        success=True,
        command=[build_manager.build_command],
        cwd=str(build_manager.source_dir),
        exit_code=0,
        stdout="Build skipped",
        stderr="",
        duration=0.0,
        build_dir=build_manager.build_dir,
    )


def run_input_command(
    input_file: str,
    config_path: str = "config/config.yaml",
) -> int:
    """
    Run a calculation with an input file directly, printing stdout/stderr to console.
    
    Args:
        input_file: Path to input file (can be absolute or relative)
        config_path: Path to configuration file
    """
    import subprocess
    import os
    import random
    import shutil
    import shlex
    
    loader = ConfigLoader(config_path)
    config = loader.load()
    
    build_cfg = config.get("build", {})
    tests_cfg = config.get("tests", {})
    source_dir = resolve_source_dir(config)
    build_dir = source_dir / build_cfg.get("build_dir", "build")
    bdf_home = build_dir / "bdf-pkg-full"
    
    # Check if BDFHOME exists
    if not bdf_home.exists():
        print(f"Error: BDF installation not found at {bdf_home}")
        print("Please run the full workflow first (without --skip-build) to build the package.")
        return 1
    
    # Resolve input file path - try multiple approaches
    input_path = None
    tried_paths = []
    
    # First try as absolute path
    if os.path.isabs(input_file):
        input_path = Path(input_file)
        tried_paths.append(str(input_path))
        if not input_path.exists():
            # Try with /Users instead of /User if that's the issue (common typo)
            if input_file.startswith("/User/"):
                alt_path = Path(input_file.replace("/User/", "/Users/", 1))
                tried_paths.append(str(alt_path))
                if alt_path.exists():
                    input_path = alt_path
                    print(f"Note: Corrected path from /User/ to /Users/: {input_path}")
    
    # If not absolute or not found, try as relative path
    if input_path is None or not input_path.exists():
        input_path = Path(input_file)
        if not input_path.is_absolute():
            # Try relative to current working directory
            cwd_path = Path.cwd() / input_file
            tried_paths.append(str(cwd_path))
            if cwd_path.exists():
                input_path = cwd_path
            else:
                # Try relative to source_dir
                source_path = source_dir / input_file
                tried_paths.append(str(source_path))
                if source_path.exists():
                    input_path = source_path
                else:
                    # Try as-is (might be relative to something else)
                    tried_paths.append(str(input_path))
    
    # Final check - input file must exist, otherwise do not execute
    if not input_path or not input_path.exists():
        print(f"Error: Input file not found: {input_file}")
        print(f"\nTried the following paths:")
        for path in tried_paths:
            exists = "✓" if Path(path).exists() else "✗"
            print(f"  {exists} {path}")
        print(f"\nPlease provide the correct path to the input file.")
        print(f"Current working directory: {Path.cwd()}")
        print(f"\nCalculation will not be executed without a valid input file.")
        return 1
    
    # Validate input file extension - must be .inp
    if input_path.suffix.lower() != ".inp":
        print(f"Error: Input file must have .inp extension")
        print(f"  Provided file: {input_path}")
        print(f"  File extension: {input_path.suffix}")
        print(f"\nPlease provide a file with .inp extension (e.g., test.inp)")
        return 1
    
    print(f"✓ Using input file: {input_path}")
    
    # Get working directory and temporary directory from config
    # Allow user to specify BDF_WORKDIR and BDF_TMPDIR in config
    env_cfg = tests_cfg.get("env", {})
    
    # Determine working directory for a single BDF run:
    # - Prefer tests.env.BDF_WORKDIR if it exists
    # - Otherwise, use the directory where the input file is located
    workdir_cfg = env_cfg.get("BDF_WORKDIR")
    if workdir_cfg:
        work_dir = Path(workdir_cfg).resolve()
        if not work_dir.exists():
            print(f"Warning: BDF_WORKDIR from config does not exist: {workdir_cfg}")
            print(f"Using input file directory instead: {input_path.parent}")
            work_dir = input_path.parent
    else:
        # Default: use the input file's directory as working directory
        work_dir = input_path.parent
    
    # Determine temporary directory: use config BDF_TMPDIR or system temp
    tmpdir_cfg = env_cfg.get("BDF_TMPDIR")
    if tmpdir_cfg:
        # Support $RANDOM placeholder
        if "$RANDOM" in tmpdir_cfg:
            rnd = random.randint(0, 999999)
            tmp_dir = Path(tmpdir_cfg.replace("$RANDOM", str(rnd)))
        else:
            tmp_dir = Path(tmpdir_cfg)
        tmp_dir.mkdir(parents=True, exist_ok=True)
    else:
        # Use system temporary directory
        import tempfile
        tmp_dir = Path(tempfile.gettempdir())
    
    # Build command using test configuration
    test_command_template = tests_cfg.get("test_command", "{BDFHOME}/sbin/bdfdrv.py")
    test_args_template = tests_cfg.get("test_args_template", "-r {input_file}")
    
    command_str = test_command_template.replace("{BDFHOME}", str(bdf_home))
    command = shlex.split(command_str)
    test_args_template_list = shlex.split(test_args_template)
    # Use only the input file name inside the working directory. If the working
    # directory is different from the input file directory, copy the input file
    # *and all related support files* (same stem, different extensions), so BDF
    # can find everything it needs (e.g. test075.inp, test075.extcharge, etc.)
    input_name = input_path.name
    stem = input_path.stem
    if input_path.parent != work_dir:
        try:
            # Copy all files matching stem.* into the work directory
            for src in input_path.parent.glob(f"{stem}.*"):
                dst = work_dir / src.name
                try:
                    shutil.copy2(src, dst)
                except Exception as exc:
                    print(f"Warning: Failed to copy {src} to work directory: {exc}")
        except Exception as exc:
            print(f"Warning: Failed to copy support files to work directory: {exc}")
    args = [arg.format(input_file=input_name) for arg in test_args_template_list]
    command.extend(args)
    
    # Prepare environment
    env = os.environ.copy()
    env["BDFHOME"] = str(bdf_home)
    env["BDF_WORKDIR"] = str(work_dir)
    env["BDF_TMPDIR"] = str(tmp_dir)
    
    # OpenMP settings
    env["OMP_NUM_THREADS"] = str(env_cfg.get("OMP_NUM_THREADS", os.cpu_count() or 1))
    env["OMP_STACKSIZE"] = str(env_cfg.get("OMP_STACKSIZE", "512M"))
    
    # Add any other environment variables from config
    for key, value in env_cfg.items():
        if key not in {"BDF_TMPDIR", "BDF_WORKDIR", "OMP_NUM_THREADS", "OMP_STACKSIZE"}:
            env[str(key)] = str(value)
    
    # Prepare log file paths (in working directory)
    log_file = work_dir / f"{input_path.stem}.log"
    err_file = work_dir / f"{input_path.stem}.err"
    
    try:
        # Run the command and capture output
        print("\n" + "=" * 80)
        print("BDF Calculation")
        print("=" * 80)
        print(f"Command: {' '.join(command)}")
        print(f"Input file: {input_path}")
        print(f"Working directory: {work_dir}")
        print(f"BDFHOME: {bdf_home}")
        print(f"BDF_WORKDIR: {work_dir}")
        print(f"BDF_TMPDIR: {tmp_dir}")
        print(f"Stdout log: {log_file}")
        print(f"Stderr log: {err_file}")
        print("=" * 80)
        print()
        
        # Run command and capture both stdout and stderr
        with open(log_file, "w", encoding="utf-8") as log_f, \
             open(err_file, "w", encoding="utf-8") as err_f:
            process = subprocess.run(
                command,
                cwd=work_dir,
                env=env,
                stdout=log_f,
                stderr=err_f,
                check=False,
            )
        
        # Read and display the output
        if log_file.exists():
            log_content = log_file.read_text(encoding="utf-8", errors="replace")
            print(log_content)
        
        if err_file.exists() and err_file.stat().st_size > 0:
            err_content = err_file.read_text(encoding="utf-8", errors="replace")
            if err_content.strip():
                print("\n" + "=" * 80)
                print("Standard Error Output:")
                print("=" * 80)
                print(err_content)
        
        print()
        print("=" * 80)
        print(f"Calculation completed with exit code: {process.returncode}")
        if process.returncode == 0:
            print("✓ Calculation succeeded")
        else:
            print("✗ Calculation failed")
        print("=" * 80)
        print(f"\nOutput files:")
        print(f"  - Stdout: {log_file}")
        print(f"  - Stderr: {err_file}")
        
        # Check for BDFOPT output file (test.out.tmp) - contains detailed module outputs
        out_tmp_file = work_dir / f"{input_path.stem}.out.tmp"
        if out_tmp_file.exists():
            print(f"  - BDFOPT detailed output: {out_tmp_file}")
            if process.returncode != 0:
                print(f"    ⚠️  IMPORTANT: This file contains detailed BDF module outputs")
                print(f"       and is essential for error analysis when calculation fails.")
        
        # Show other output files if they exist in working directory
        output_files = list(work_dir.glob(f"{input_path.stem}.*"))
        excluded_names = {input_path.name, log_file.name, err_file.name}
        if out_tmp_file.exists():
            excluded_names.add(out_tmp_file.name)
        other_files = [f for f in output_files if f.name not in excluded_names]
        if other_files:
            print(f"\nOther output files generated in {work_dir}:")
            for f in sorted(other_files):
                print(f"  - {f.name}")
        
        return process.returncode
    finally:
        # Note: We don't clean up tmp_dir if it's from config (user may want to keep it)
        # Only clean up if we created a random temp directory
        if tmpdir_cfg and "$RANDOM" in tmpdir_cfg:
            try:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)
            except Exception:
                pass


def run_single_test_command(
    test_name: str,
    config_path: str = "config/config.yaml",
) -> int:
    """
    Run a single regression test by name (e.g. 'test149') without editing config.yaml.

    This:
    - Loads the existing config
    - Assumes the package has already been built/compiled
    - Uses TestRunner to discover all tests
    - Executes only the requested test
    - Prints a short summary to the console
    """
    from .config_loader import ConfigLoader
    from .logger import setup_logger
    from .test_runner import TestRunner

    loader = ConfigLoader(config_path)
    config = loader.load()

    # For a single test run, ignore global enabled_range/profile filters so
    # that we can access any test by name without editing config.yaml.
    tests_cfg = config.setdefault("tests", {})
    # Disable numeric range filter
    tests_cfg["enabled_range"] = {}
    # Disable active profile override
    tests_cfg["profile"] = None
    logger = setup_logger(config=config)

    runner = TestRunner(config, logger=logger)
    cases = runner.discover_tests()

    # Normalize test name: accept either "test149" or "149"
    if not test_name.startswith("test"):
        normalized = f"test{test_name}"
    else:
        normalized = test_name

    target = None
    for case in cases:
        if case.name == normalized:
            target = case
            break

    if target is None:
        logger.error("Requested test %s not found (normalized: %s)", test_name, normalized)
        logger.error("Available tests include: %s ...", ", ".join(c.name for c in cases[:10]))
        return 1

    logger.info("Running single test: %s", target.name)
    result = runner._run_test_case(target)  # use existing execution logic

    status = "PASSED" if result.success else "FAILED"
    logger.info("Single test %s %s (exit code %s)", target.name, status, result.exit_code)

    if result.comparison and not result.comparison.matched:
        logger.info("Comparison differences:\n%s", result.comparison.differences)

    return 0 if result.success else 1


def compare_reports_command(
    reports_dir: str = "./reports",
    before: Optional[str] = None,
    after: Optional[str] = None,
    n: int = 2,
) -> int:
    """
    Compare test reports
    
    Args:
        reports_dir: Directory containing reports
        before: Path to earlier report (optional, uses Nth latest if not specified)
        after: Path to later report (optional, uses latest if not specified)
        n: Number of reports to compare (if before/after not specified)
    """
    from pathlib import Path
    from .report_comparator import ReportComparator
    
    comparator = ReportComparator(Path(reports_dir))
    
    if before and after:
        comparison = comparator.compare_reports(Path(before), Path(after))
    else:
        comparison = comparator.compare_latest_reports(n)
    
    if not comparison:
        print("Error: Not enough reports to compare. Need at least 2 reports.")
        return 1
    
    # Generate comparison report
    artifacts = comparator.generate_comparison_report(comparison)
    
    print("=== Report Comparison ===")
    print(f"Before: {comparison.before_timestamp}")
    print(f"After: {comparison.after_timestamp}")
    print()
    print("Summary:")
    print(f"  New Failures: {comparison.summary.get('new_failures', 0)}")
    print(f"  Fixed Tests: {comparison.summary.get('fixed', 0)}")
    print(f"  Still Failing: {comparison.summary.get('still_failing', 0)}")
    print(f"  Still Passing: {comparison.summary.get('still_passing', 0)}")
    print(f"  New Tests: {comparison.summary.get('new_tests', 0)}")
    print(f"  Removed Tests: {comparison.summary.get('removed', 0)}")
    print()
    print(f"Comparison report generated:")
    print(f"  HTML: {artifacts.get('html')}")
    print(f"  JSON: {artifacts.get('json')}")
    
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BDF Auto Test Framework")
    parser.add_argument("--config", default="config/config.yaml", help="Path to configuration file")
    parser.add_argument("--skip-git", action="store_true", help="Skip git pull step")
    parser.add_argument("--skip-build", action="store_true", help="Skip build step")
    parser.add_argument("--skip-tests", action="store_true", help="Skip test step")
    parser.add_argument(
        "--profile",
        help="Override tests.profile from config (e.g. smoke, core, full)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Shortcut for --profile smoke (run small smoke test subset)",
    )
    
    # Report comparison subcommand
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    compare_parser = subparsers.add_parser("compare", help="Compare test reports")
    compare_parser.add_argument("--reports-dir", default="./reports", help="Directory containing reports")
    compare_parser.add_argument("--before", help="Path to earlier report")
    compare_parser.add_argument("--after", help="Path to later report")
    compare_parser.add_argument("-n", type=int, default=2, help="Compare N most recent reports (default: 2)")
    
    # Run input file directly
    run_parser = subparsers.add_parser("run-input", help="Run a calculation with an input file directly")
    run_parser.add_argument("input_file", help="Path to input file")
    run_parser.add_argument("--config", default="config/config.yaml", help="Path to configuration file")

    # Run a single regression test (by test name / id)
    single_parser = subparsers.add_parser(
        "run-test",
        help="Run a single regression test (e.g. test149 or 149) without editing config.yaml",
    )
    single_parser.add_argument("test_name", help="Test name or numeric id (e.g. test149 or 149)")
    single_parser.add_argument("--config", default="config/config.yaml", help="Path to configuration file")
    
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "compare":
            return compare_reports_command(
                reports_dir=args.reports_dir,
                before=args.before,
                after=args.after,
                n=args.n,
            )
        elif args.command == "run-input":
            return run_input_command(
                input_file=args.input_file,
                config_path=args.config,
            )
        elif args.command == "run-test":
            return run_single_test_command(
                test_name=args.test_name,
                config_path=args.config,
            )
        else:
            # Determine requested profile override (CLI has precedence over config)
            cli_profile: Optional[str]
            if args.smoke:
                cli_profile = "smoke"
            else:
                cli_profile = args.profile

            return run_workflow(
                config_path=args.config,
                skip_git=args.skip_git,
                skip_build=args.skip_build,
                skip_tests=args.skip_tests,
                profile=cli_profile,
            )
    except KeyboardInterrupt:
        print("Workflow interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

