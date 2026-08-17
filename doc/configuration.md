## Configuration Manual (`config/config.yaml`)

This document explains all sections and keys in `config/config.yaml` and how
they affect the BDF Auto Test Framework.

The best starting point is `config/config.yaml.example`, which you can copy and
customize:

```bash
cp config/config.yaml.example config/config.yaml
```

---

### 1. `git` – Source Code Location

```yaml
git:
  remote_url: "ssh://user@host:port/path/to/bdf-pkg"
  branch: "master"
  local_path: "./package_source"
```

- **`remote_url`**: SSH/HTTPS URL of the BDF repository.
- **`branch`**: Branch to track (e.g. `master`, `main`, or feature branch).
- **`local_path`**: Directory where the repository is cloned locally.

The orchestrator uses this to run `git pull` (unless `--skip-git` is set).

---

### 2. `build` – How to Configure and Build BDF

```yaml
build:
  build_dir: "build"
  build_command: "./setup"
  compiler_set: "gnu"
  compilers:
    gnu:
      fortran: "gfortran"
      c: "gcc"
      cpp: "g++"
    intel:
      fortran: "ifx"
      c: "icx"
      cpp: "icpx"
    llvm:
      fortran: "Flang"
      c: "clang"
      cpp: "clang++"
  use_mkl: false
  mkl_option: "TBB"
  math_library:
    mathinclude_flags: "-I/path/to/lapack/include -I/path/to/cblas/include"
    mathlib_flags: "-L/path/to/lapack/lib -llapack -lblas -lcblas -llapacke"
    blasdir: "/path/to/blas"
    lapackdir: "/path/to/lapack"
  build_mode: "release"
  preserve_build: false
  always_use:
    - "--int64"
    - "--omp"
  additional_args: []
```

- **Paths**
  - `build_dir`: Subdirectory under `git.local_path` where CMake/build files live (e.g. `package_source/build`).
  - `build_command`: Command run in `source_dir` to configure the build (often `./setup`).

- **Compilers**
  - `compiler_set`: One of `gnu`, `intel`, `llvm`. Controls which compiler triplet the setup script uses.
  - `compilers`: Optional overrides for Fortran/C/C++ executable names per compiler set.

- **Math Libraries**
  - `use_mkl`: `true` to use Intel MKL, `false` to use a custom BLAS/LAPACK.
  - `mkl_option`: Forwarded as `--mkl=<value>` to the setup script when `use_mkl: true`.
  - `math_library.*`: Paths and flags for custom BLAS/LAPACK when not using MKL.

- **Build Mode and Behavior**
  - `build_mode`: `release` or `debug`.
  - `preserve_build`: If `true`, the existing `build_dir` is kept between runs. Useful for development.
  - `always_use`: Flags always passed to the setup script (e.g. `--int64`, `--omp`).
  - `additional_args`: Extra flags appended to the setup command for experiments.

- **HDF5 (optional, default off)**

  BDF can optionally use HDF5 for checkpoint/restart. When enabled, the framework appends `--hdf5 --hdf5root=<home>` to the setup command. Before invoking setup it performs a light pre-build check so obvious environment mistakes fail fast instead of mid-way through a long CMake run.

  ```yaml
  build:
    hdf5:
      enabled: false              # set to true to enable HDF5
      hdf5home: null              # HDF5 install root, or null to read $HDF5HOME
      allow_downgrade: true       # on check failure: true=build without HDF5, false=let setup fail
  ```

  - `enabled`: Default `false`. The whole `hdf5:` section may be omitted; that is equivalent to `enabled: false`.
  - `hdf5home`: HDF5 install root (the directory that contains `include/` and `lib/`). If null/empty, the framework reads the `HDF5HOME` environment variable. The literal placeholders `$HDF5HOME` and `${HDF5HOME}` in the config value are expanded against the process environment, so `hdf5home: "${HDF5HOME}"` is equivalent to leaving it unset. Common values: `/usr`, `/opt/homebrew/opt/hdf5`, `/usr/local/hdf5`.
  - `allow_downgrade`: Controls behavior when HDF5 is enabled but the pre-build check fails (missing header or library, or HDF5HOME unset):
    - `true` (default): logs a warning and builds **without** `--hdf5`. The resulting BDF binary will not have HDF5 support.
    - `false`: logs an error and still passes `--hdf5` to setup, which will then fail naturally (use this if you want missing HDF5 to be a hard error).

  **Pre-build check.** When `enabled: true`, the framework looks for:
  1. `<hdf5home>/include/hdf5.h`
  2. `libhdf5.*` under `<hdf5home>/lib` (or `<hdf5home>/lib64` on systems that use it)

  The check uses only filesystem presence — it does not run `h5cc` or `pkg-config`. CMake re-validates the library during configure, so a passing check here does not guarantee a successful build; it only catches the common case of a wrong or unset HDF5 home before the expensive build starts.

  **Python runtime dependency (h5py).** BDF built with HDF5 invokes Python helper scripts (`bdfhdf5.py`, `bdfh5_manifest.py`) during checkpoint/restart, and those `import h5py` at runtime. The `h5py` package is declared in `requirements.txt` — install it in the Python environment used to run tests. When HDF5 is enabled, `TestRunner` verifies at startup that `h5py` is importable and logs a clear error (with install hint) before any test runs. Failure logs containing `ModuleNotFoundError: No module named 'h5py'` are classified as `environment` error events and the LLM analysis includes the same install hint.

  **Example.** Enable HDF5 against a Homebrew install on macOS:

  ```yaml
  build:
    hdf5:
      enabled: true
      hdf5home: "/opt/homebrew/opt/hdf5"
  ```

  Or rely on the environment variable (recommended for shared configs across machines):

  ```yaml
  build:
    hdf5:
      enabled: true
      # hdf5home left null; export HDF5HOME=/opt/homebrew/opt/hdf5 in your shell
  ```

  The resolved status (`ok` / `downgraded` / `config_error` / `disabled`), the home directory, and the detected version are logged at the start of the build step and recorded in `BuildResult.metadata.hdf5`, which flows into the JSON/HTML reports.

---

### 3. `compile` – How to Run `make`

```yaml
compile:
  command: "make"
  jobs: "auto"
  target: "install"
  extra_args: []
  log_file: "make.log"
  environment: {}
```

- `command`: Base compile command (usually `make`).
- `jobs`: Number of parallel jobs for `-j`.  
  - `"auto"` (or `null`) → detect from CPU count.
  - Integer (e.g. `8`) → use `-j8`.
- `target`: Make target to build (e.g. `install`).
- `extra_args`: Additional arguments appended to the make command.
- `log_file`: Name of the file capturing stdout/stderr from the compile step.
- `environment`: Extra environment variables for the compile step (rarely needed).

---

### 4. `llm` – Local/Remote LLM Configuration

```yaml
llm:
  mode: "auto"           # local | remote | auto
  analysis_mode: "simple"  # simple | detailed
  max_tokens: 2000
  temperature: 0.3

  local:
    enabled: true
    endpoint: "http://localhost:11434"
    model: "my-local-llm"
    timeout: 300

  remote:
    enabled: true
    provider: "openai"   # openai | openrouter | deepseek | groq
    model: "gpt-4o"
    api_key_env: "OPENAI_API_KEY"
```

- **Top-level behavior**
  - `mode`:
    - `local`: use only the local LLM.
    - `remote`: use only the remote LLM.
    - `auto`: try local first, fall back to remote if local fails.
  - `analysis_mode`:
    - `simple`: no LLM call; extract and summarize errors quickly.
    - `detailed`: call LLM for full analysis (slower, requires keys).
  - `max_tokens`, `temperature`: standard LLM generation controls.

- **Local LLM**
  - `local.enabled`: enable/disable local LLM use.
  - `endpoint`: HTTP URL for the local LLM (e.g. Ollama).
  - `model`: model name recognized by your local endpoint.
  - `timeout`: per-request timeout in seconds.

- **Remote LLM**
  - `remote.enabled`: enable/disable remote LLM.
  - `provider`: which OpenAI-compatible service to use.
  - `model`: provider-specific model name (e.g. `gpt-4o`).
  - `api_key_env`: environment variable that stores the API key (e.g. `OPENAI_API_KEY`).

---

### 5. `tests` – Regression Test Settings

```yaml
tests:
  test_dir: "tests/input"
  reference_dir: "tests/check"
  tolerance: 1e-6
  timeout: 3600
  enabled_range:
    min: 1
    max: 161
  tolerance_mode: "strict"        # strict | loose
  tolerance_scale:
    strict: 1.0
    loose: 5.0
  profiles:
    smoke:
      min: 1
      max: 5
    core:
      min: 1
      max: 20
    full:
      min: 1
      max: 161
  profile: null
  max_parallel: 2
  env:
    # OMP_NUM_THREADS (optional override)
    OMP_STACKSIZE: "512M"
    BDF_TMPDIR: "/tmp/$RANDOM"
    # BDF_WORKDIR: "/path/to/workdir"

  input_pattern: "test*.inp"
  reference_pattern: "test*.check"
  check_pattern: "test*.check"

  test_command: "{BDFHOME}/sbin/bdfdrv.py"
  test_args_template: "-r {input_file}"

  log_file_pattern: "test*.log"
  result_extraction:
    method: "grep"
    pattern: "CHECKDATA"
```

- **Directories and discovery**
  - `test_dir`: Where `test*.inp` live (relative to BDF source).
  - `reference_dir`: Where `test*.check` reference files live.
  - `input_pattern`: Glob for input files.
  - `reference_pattern`, `check_pattern`: Globs for reference and generated `.check` files.

- **Which tests to run**
  - `enabled_range.min/max`: Numeric range for `testNNN`.
  - `profiles`: Named subsets (e.g. `smoke`, `core`, `full`) that define their own min/max.
  - `profile`:
    - `null`: use `enabled_range`.
    - `"smoke"`, `"core"`, etc.: use the corresponding profile range.

- **Execution**
  - `test_command`: Executable used to run each test. `{BDFHOME}` is substituted with the actual BDF installation path.
  - `test_args_template`: Arguments, formatted with `{input_file}` (file name only).
  - `timeout`: Per-test timeout in seconds.
  - `max_parallel`: How many tests to run concurrently.

- **Environment**
  - `env.OMP_NUM_THREADS`:
    - If omitted, automatically chosen as roughly `num_cores / max_parallel`.
  - `env.OMP_STACKSIZE`: OpenMP stack size (e.g. `512M`).
  - `env.BDF_TMPDIR`: Template for scratch directories (supports `$RANDOM` so each run gets its own directory).
  - `env.BDF_WORKDIR`:
    - Optional work directory for the `run-input` command.
    - If not set, `run-input` uses the input file’s directory by default.

- **Result extraction**
  - `log_file_pattern`: Pattern for test logs.
  - `result_extraction.method`: Currently `grep`-style extraction.
  - `result_extraction.pattern`: Line substring (`CHECKDATA`) used to build `.check` files from logs.

---

### 6. `reporting` – Reports & Error Events

```yaml
reporting:
  output_dir: "./reports"
  format: ["html", "json"]
  include_llm_analysis: true
  timestamp_format: "%Y-%m-%d_%H-%M-%S"
  structured_events_dir: "./reports/error_events"
  save_error_events: true
```

- `output_dir`: Where reports are written.
- `format`: List of formats to generate (`html`, `json`).
- `include_llm_analysis`: If `true`, embed LLM analysis text into reports when available.
- `timestamp_format`: Controls the timestamp in report filenames.
- `structured_events_dir`: Directory where JSON error events are stored.
- `save_error_events`: Enable/disable saving of per-error JSON records.

---

### 7. `logging` – Framework Logs

```yaml
logging:
  level: "INFO"                # DEBUG | INFO | WARNING | ERROR
  log_dir: "./logs"
  log_file: "autotest_{timestamp}.log"
```

- `level`: Minimum log level to emit.
- `log_dir`: Directory for log files.
- `log_file`: Filename pattern, usually including `{timestamp}`.

---

### 8. Tips for Customizing `config.yaml`

- Start from `config.yaml.example` and change **only one section at a time**.
- Verify each stage:
  1. Git + build (`--skip-tests`)
  2. Single `run-input` with a small input file
  3. A few regression tests via a narrow `enabled_range` or `run-test`
- Once everything is stable, widen the test range or use the `full` profile.


