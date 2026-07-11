# Multi-CLI CTF Workers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the direct OpenAI SDK call with a configurable Python orchestrator that runs Codex CLI, Claude Code, Kimi CLI, and future CTF agent CLIs concurrently in separate working directories.

**Architecture:** The command-line entry point loads validated JSON provider definitions, then an asyncio runner creates one run directory and dispatches one subprocess executor per enabled provider. Prompts travel through stdin with `shell=False`; results, logs, statuses, durations, and artifacts are persisted and returned in priority order.

**Tech Stack:** Python 3.10+, standard-library `argparse`, `asyncio`, `dataclasses`, `json`, `pathlib`, `subprocess`, and `unittest`.

---

## File Map

- Create `worker/__init__.py`: package marker and public exports.
- Create `worker/models.py`: immutable provider, result, and run-summary data structures.
- Create `worker/config.py`: JSON parsing, validation, duplicate detection, and placeholder validation.
- Create `worker/executor.py`: prompt construction, safe subprocess execution, timeout handling, and log persistence.
- Create `worker/runner.py`: run directory creation, concurrent dispatch, priority sorting, and `results.json` persistence.
- Create `worker/providers.json`: default Codex, Claude Code, and Kimi CLI definitions.
- Modify `main.py`: replace OpenAI API usage with the worker orchestrator CLI.
- Create `tests/__init__.py`: test package marker.
- Create `tests/test_config.py`: provider configuration tests.
- Create `tests/test_executor.py`: subprocess safety, cwd, failure, and timeout tests.
- Create `tests/test_runner.py`: concurrency, ordering, persistence, and aggregate status tests.
- Create `tests/test_main.py`: command-line entry point behavior tests.
- Modify `README.md`: installation, provider configuration, usage, output, and isolation documentation.
- Modify `requirements.txt`: remove the OpenAI SDK because runtime dependencies are standard-library only.
- Delete `.env.example`: API keys are no longer read by this application; each CLI owns its authentication.
- Modify `.gitignore`: exclude generated `worker/runs/` data.

Git commit steps below require a valid repository. The current `.git` directory is empty and must be restored or initialized by the user before those steps can succeed.

### Task 1: Provider Models and Configuration Validation

**Files:**
- Create: `worker/__init__.py`
- Create: `worker/models.py`
- Create: `worker/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing configuration tests**

Create `tests/__init__.py` as an empty file and create `tests/test_config.py` with:

```python
import json
import tempfile
import unittest
from pathlib import Path

from worker.config import ConfigError, load_provider_configs


class ProviderConfigTests(unittest.TestCase):
    def write_config(self, directory: str, payload: object) -> Path:
        path = Path(directory) / "providers.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_valid_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex", "-C", "{work_dir}", "-"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )

            providers = load_provider_configs(path)

            self.assertEqual(len(providers), 1)
            self.assertEqual(providers[0].name, "codex")
            self.assertEqual(providers[0].command[-1], "-")

    def test_rejects_duplicate_names(self) -> None:
        provider = {
            "name": "codex",
            "enabled": True,
            "priority": 100,
            "command": ["codex"],
            "timeout_seconds": 30,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(directory, {"providers": [provider, provider]})
            with self.assertRaisesRegex(ConfigError, "duplicate provider name"):
                load_provider_configs(path)

    def test_rejects_non_positive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex"],
                            "timeout_seconds": 0,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "timeout_seconds"):
                load_provider_configs(path)

    def test_rejects_unknown_command_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                {
                    "providers": [
                        {
                            "name": "codex",
                            "enabled": True,
                            "priority": 100,
                            "command": ["codex", "{prompt}"],
                            "timeout_seconds": 30,
                        }
                    ]
                },
            )
            with self.assertRaisesRegex(ConfigError, "unsupported placeholder"):
                load_provider_configs(path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the configuration tests and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_config -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'worker'`.

- [ ] **Step 3: Add provider and result models**

Create `worker/__init__.py` with:

```python
from worker.models import ProviderConfig, RunSummary, WorkerResult

__all__ = ["ProviderConfig", "RunSummary", "WorkerResult"]
```

Create `worker/models.py` with:

```python
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    enabled: bool
    priority: int
    command: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class WorkerResult:
    provider: str
    priority: int
    status: str
    exit_code: int | None
    duration_seconds: float
    output: str
    stdout_path: str
    stderr_path: str
    work_dir: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    run_dir: Path
    results: tuple[WorkerResult, ...]

    @property
    def exit_code(self) -> int:
        return 0 if any(result.status == "success" for result in self.results) else 1
```

- [ ] **Step 4: Implement strict JSON configuration loading**

Create `worker/config.py` with:

```python
import json
from pathlib import Path
from string import Formatter

from worker.models import ProviderConfig


ALLOWED_PLACEHOLDERS = {"challenge_dir", "work_dir"}


class ConfigError(ValueError):
    pass


def _validate_placeholders(provider_name: str, command: list[str]) -> None:
    formatter = Formatter()
    for argument in command:
        try:
            parsed = formatter.parse(argument)
            for _, field_name, _, _ in parsed:
                if field_name and field_name not in ALLOWED_PLACEHOLDERS:
                    raise ConfigError(
                        f"provider {provider_name!r} uses unsupported placeholder "
                        f"{field_name!r}"
                    )
        except ValueError as exc:
            raise ConfigError(
                f"provider {provider_name!r} has an invalid command placeholder"
            ) from exc


def _parse_provider(raw: object, index: int) -> ProviderConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"providers[{index}] must be an object")

    name = raw.get("name")
    enabled = raw.get("enabled")
    priority = raw.get("priority")
    command = raw.get("command")
    timeout = raw.get("timeout_seconds")

    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"providers[{index}].name must be a non-empty string")
    if not isinstance(enabled, bool):
        raise ConfigError(f"provider {name!r}.enabled must be a boolean")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ConfigError(f"provider {name!r}.priority must be an integer")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(argument, str) or not argument for argument in command)
    ):
        raise ConfigError(
            f"provider {name!r}.command must be a non-empty list of strings"
        )
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout <= 0
    ):
        raise ConfigError(f"provider {name!r}.timeout_seconds must be positive")

    _validate_placeholders(name, command)
    return ProviderConfig(
        name=name.strip(),
        enabled=enabled,
        priority=priority,
        command=tuple(command),
        timeout_seconds=float(timeout),
    )


def load_provider_configs(path: Path) -> tuple[ProviderConfig, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read provider config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in provider config: {path}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ConfigError("provider config must contain a providers list")

    providers = tuple(
        _parse_provider(raw, index)
        for index, raw in enumerate(payload["providers"])
    )
    names = [provider.name for provider in providers]
    if len(names) != len(set(names)):
        raise ConfigError("duplicate provider name in provider config")
    return providers
```

- [ ] **Step 5: Run the configuration tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_config -v
```

Expected: four tests report `ok` and the command ends with `OK`.

- [ ] **Step 6: Commit the configuration layer when Git metadata is valid**

```powershell
git add worker/__init__.py worker/models.py worker/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: add CLI provider configuration"
```

Expected: one commit containing only the configuration layer and its tests.

### Task 2: Safe Single-Provider Subprocess Executor

**Files:**
- Create: `worker/executor.py`
- Create: `tests/test_executor.py`

- [ ] **Step 1: Write failing executor tests**

Create `tests/test_executor.py` with:

```python
import os
import sys
import tempfile
import unittest
from pathlib import Path

from worker.executor import execute_provider
from worker.models import ProviderConfig


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_passes_prompt_through_stdin_and_uses_provider_cwd(self) -> None:
        code = (
            "import os,sys;"
            "data=sys.stdin.read();"
            "print(os.getcwd());"
            "print(data)"
        )
        provider = ProviderConfig(
            name="fake",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", code),
            timeout_seconds=5,
        )
        prompt = "solve | quoted ' value & keep $text"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "fake"
            challenge_dir = root / "challenge"
            challenge_dir.mkdir()

            result = await execute_provider(
                provider, prompt, challenge_dir, work_dir
            )

            self.assertEqual(result.status, "success")
            self.assertIn(str(work_dir.resolve()), result.output)
            self.assertIn(prompt, result.output)
            self.assertIn(str(challenge_dir.resolve()), result.output)
            self.assertTrue(Path(result.stdout_path).is_file())
            self.assertTrue(Path(result.stderr_path).is_file())

    async def test_records_non_zero_exit(self) -> None:
        provider = ProviderConfig(
            name="failed",
            enabled=True,
            priority=10,
            command=(
                sys.executable,
                "-c",
                "import sys;print('bad', file=sys.stderr);sys.exit(7)",
            ),
            timeout_seconds=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory) / "failed"
            result = await execute_provider(provider, "task", None, work_dir)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.exit_code, 7)
            self.assertIn("bad", Path(result.stderr_path).read_text(encoding="utf-8"))

    async def test_records_missing_executable(self) -> None:
        provider = ProviderConfig(
            name="missing",
            enabled=True,
            priority=10,
            command=("definitely-not-a-real-ctf-cli",),
            timeout_seconds=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider, "task", None, Path(directory) / "missing"
            )
            self.assertEqual(result.status, "unavailable")
            self.assertIsNone(result.exit_code)

    async def test_kills_timed_out_provider(self) -> None:
        provider = ProviderConfig(
            name="slow",
            enabled=True,
            priority=10,
            command=(sys.executable, "-c", "import time;time.sleep(5)"),
            timeout_seconds=0.05,
        )
        with tempfile.TemporaryDirectory() as directory:
            result = await execute_provider(
                provider, "task", None, Path(directory) / "slow"
            )
            self.assertEqual(result.status, "timeout")
            self.assertIsNone(result.exit_code)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the executor tests and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_executor -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'worker.executor'`.

- [ ] **Step 3: Implement safe subprocess execution and log persistence**

Create `worker/executor.py` with:

```python
import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

from worker.models import ProviderConfig, WorkerResult


def _expand_command(
    provider: ProviderConfig,
    challenge_dir: Path | None,
    work_dir: Path,
) -> tuple[str, ...]:
    replacements = {
        "{challenge_dir}": str(challenge_dir.resolve()) if challenge_dir else "",
        "{work_dir}": str(work_dir.resolve()),
    }
    expanded: list[str] = []
    for argument in provider.command:
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        expanded.append(argument)
    return tuple(expanded)


def _build_prompt(
    prompt: str,
    challenge_dir: Path | None,
    work_dir: Path,
) -> str:
    challenge = str(challenge_dir.resolve()) if challenge_dir else "not provided"
    return (
        f"CTF challenge directory (read-only input): {challenge}\n"
        f"Writable worker directory: {work_dir.resolve()}\n"
        "Do not modify the challenge directory. Store generated artifacts in the "
        "writable worker directory.\n\n"
        f"Task:\n{prompt}\n"
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.communicate()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


def _write_logs(work_dir: Path, stdout_text: str, stderr_text: str) -> tuple[Path, Path]:
    stdout_path = work_dir / "stdout.txt"
    stderr_path = work_dir / "stderr.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")
    return stdout_path, stderr_path


async def execute_provider(
    provider: ProviderConfig,
    prompt: str,
    challenge_dir: Path | None,
    work_dir: Path,
) -> WorkerResult:
    work_dir.mkdir(parents=True, exist_ok=True)
    command = _expand_command(provider, challenge_dir, work_dir)
    payload = _build_prompt(prompt, challenge_dir, work_dir).encode("utf-8")
    started = time.monotonic()
    stdout_text = ""
    stderr_text = ""
    status = "unavailable"
    exit_code: int | None = None

    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=work_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload),
                timeout=provider.timeout_seconds,
            )
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            exit_code = process.returncode
            status = "success" if exit_code == 0 else "failed"
        except TimeoutError:
            await _terminate_process_tree(process)
            status = "timeout"
            stderr_text = (
                f"provider exceeded timeout of {provider.timeout_seconds:g} seconds"
            )
    except OSError as exc:
        status = "unavailable"
        stderr_text = f"cannot start provider command {command[0]!r}: {exc}"

    stdout_path, stderr_path = _write_logs(work_dir, stdout_text, stderr_text)
    duration = round(time.monotonic() - started, 3)
    return WorkerResult(
        provider=provider.name,
        priority=provider.priority,
        status=status,
        exit_code=exit_code,
        duration_seconds=duration,
        output=stdout_text.strip(),
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        work_dir=str(work_dir.resolve()),
    )
```

- [ ] **Step 4: Run the executor tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_executor -v
```

Expected: four tests report `ok` and the command ends with `OK`.

- [ ] **Step 5: Commit the subprocess executor when Git metadata is valid**

```powershell
git add worker/executor.py tests/test_executor.py
git commit -m "feat: execute isolated CLI workers"
```

Expected: one commit containing the executor and deterministic fake-CLI tests.

### Task 3: Concurrent Runner and Result Persistence

**Files:**
- Create: `worker/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write failing concurrency and persistence tests**

Create `tests/test_runner.py` with:

```python
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from worker.models import ProviderConfig
from worker.runner import run_workers


def fake_provider(name: str, priority: int, delay: float) -> ProviderConfig:
    code = f"import time;time.sleep({delay});print('{name}')"
    return ProviderConfig(
        name=name,
        enabled=True,
        priority=priority,
        command=(sys.executable, "-c", code),
        timeout_seconds=5,
    )


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_enabled_providers_concurrently_and_sorts_by_priority(self) -> None:
        providers = (
            fake_provider("low", 10, 0.4),
            fake_provider("high", 100, 0.4),
        )
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            summary = await run_workers(
                providers,
                "task",
                None,
                Path(directory),
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.7)
            self.assertEqual(
                [result.provider for result in summary.results],
                ["high", "low"],
            )
            self.assertNotEqual(
                summary.results[0].work_dir,
                summary.results[1].work_dir,
            )
            self.assertEqual(summary.exit_code, 0)

    async def test_writes_results_json_and_skips_disabled_providers(self) -> None:
        providers = (
            fake_provider("enabled", 20, 0),
            ProviderConfig(
                name="disabled",
                enabled=False,
                priority=100,
                command=("missing-disabled-command",),
                timeout_seconds=5,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = await run_workers(
                providers,
                "task",
                None,
                Path(directory),
            )
            payload = json.loads(
                (summary.run_dir / "results.json").read_text(encoding="utf-8")
            )

            self.assertEqual([item["provider"] for item in payload["results"]], ["enabled"])
            self.assertEqual(payload["run_id"], summary.run_id)

    async def test_no_enabled_providers_returns_failure_summary(self) -> None:
        provider = ProviderConfig(
            name="disabled",
            enabled=False,
            priority=1,
            command=("unused",),
            timeout_seconds=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = await run_workers(
                (provider,),
                "task",
                None,
                Path(directory),
            )
            self.assertEqual(summary.results, ())
            self.assertEqual(summary.exit_code, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run runner tests and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_runner -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'worker.runner'`.

- [ ] **Step 3: Implement concurrent dispatch and `results.json` writing**

Create `worker/runner.py` with:

```python
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from worker.executor import execute_provider
from worker.models import ProviderConfig, RunSummary


DEFAULT_RUNS_ROOT = Path(__file__).resolve().parent / "runs"


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


async def run_workers(
    providers: tuple[ProviderConfig, ...],
    prompt: str,
    challenge_dir: Path | None,
    runs_root: Path = DEFAULT_RUNS_ROOT,
) -> RunSummary:
    run_id = _new_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    enabled = tuple(provider for provider in providers if provider.enabled)

    pending = tuple(
        execute_provider(
            provider,
            prompt,
            challenge_dir,
            run_dir / provider.name,
        )
        for provider in enabled
    )
    gathered = await asyncio.gather(*pending) if pending else ()
    results = tuple(
        sorted(gathered, key=lambda result: (-result.priority, result.provider))
    )
    payload = {
        "run_id": run_id,
        "challenge_dir": str(challenge_dir.resolve()) if challenge_dir else None,
        "results": [result.to_dict() for result in results],
    }
    (run_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return RunSummary(run_id=run_id, run_dir=run_dir, results=results)
```

- [ ] **Step 4: Run runner tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_runner -v
```

Expected: three tests report `ok`, elapsed-time assertion passes, and the command ends with `OK`.

- [ ] **Step 5: Commit the concurrent runner when Git metadata is valid**

```powershell
git add worker/runner.py tests/test_runner.py
git commit -m "feat: run CLI providers concurrently"
```

Expected: one commit containing the runner and result persistence tests.

### Task 4: CLI Entry Point, Default Providers, and User Documentation

**Files:**
- Modify: `main.py`
- Create: `worker/providers.json`
- Create: `tests/test_main.py`
- Modify: `README.md`
- Modify: `requirements.txt`
- Delete: `.env.example`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing entry-point tests**

Create `tests/test_main.py` with:

```python
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
from worker.models import RunSummary, WorkerResult


def result(status: str) -> WorkerResult:
    return WorkerResult(
        provider="codex",
        priority=100,
        status=status,
        exit_code=0 if status == "success" else 1,
        duration_seconds=1.0,
        output="answer" if status == "success" else "",
        stdout_path="stdout.txt",
        stderr_path="stderr.txt",
        work_dir="codex-work",
    )


class MainTests(unittest.TestCase):
    @patch("main.load_provider_configs", return_value=())
    @patch("main.run_workers", new_callable=AsyncMock)
    def test_prints_all_results_and_returns_summary_exit_code(
        self,
        run_workers: AsyncMock,
        load_provider_configs: unittest.mock.Mock,
    ) -> None:
        run_workers.return_value = RunSummary(
            run_id="run-1",
            run_dir=Path("worker/runs/run-1"),
            results=(result("success"),),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main.run_cli(["--prompt", "solve this"])

        self.assertEqual(exit_code, 0)
        self.assertIn("[codex] success", output.getvalue())
        self.assertIn("answer", output.getvalue())
        run_workers.assert_awaited_once()

    def test_rejects_missing_challenge_directory(self) -> None:
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with redirect_stderr(errors):
                exit_code = main.run_cli(
                    ["--prompt", "solve", "--challenge", str(missing)]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("challenge directory does not exist", errors.getvalue())

    def test_rejects_empty_prompt(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = main.run_cli(["--prompt", "   "])
        self.assertEqual(exit_code, 1)
        self.assertIn("prompt must not be empty", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run entry-point tests and verify the expected failure**

Run:

```powershell
python -m unittest tests.test_main -v
```

Expected: tests fail because the existing `main.py` does not define `run_cli`, `load_provider_configs`, or `run_workers`.

- [ ] **Step 3: Replace the OpenAI entry point with the worker CLI**

Replace `main.py` with:

```python
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Callable, Sequence

from worker.config import ConfigError, load_provider_configs
from worker.runner import run_workers


DEFAULT_CONFIG = Path(__file__).resolve().parent / "worker" / "providers.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run multiple isolated CLI agents against one CTF task."
    )
    parser.add_argument("--challenge", type=Path, help="read-only CTF challenge directory")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--prompt", help="task prompt; requested interactively when omitted")
    return parser


def run_cli(
    argv: Sequence[str] | None = None,
    input_func: Callable[[str], str] = input,
) -> int:
    args = _build_parser().parse_args(argv)
    challenge_dir = args.challenge.resolve() if args.challenge else None
    if challenge_dir and not challenge_dir.is_dir():
        print(
            f"Error: challenge directory does not exist: {challenge_dir}",
            file=sys.stderr,
        )
        return 1

    prompt = args.prompt if args.prompt is not None else input_func("Input: ")
    prompt = prompt.strip()
    if not prompt:
        print("Error: prompt must not be empty", file=sys.stderr)
        return 1

    try:
        providers = load_provider_configs(args.config.resolve())
        summary = asyncio.run(run_workers(providers, prompt, challenge_dir))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: cannot create worker run: {exc}", file=sys.stderr)
        return 1

    for worker_result in summary.results:
        print(
            f"\n[{worker_result.provider}] {worker_result.status} "
            f"priority={worker_result.priority} "
            f"duration={worker_result.duration_seconds:.3f}s"
        )
        if worker_result.output:
            print(worker_result.output)
        elif worker_result.status != "success":
            print(f"See stderr: {worker_result.stderr_path}")
        print(f"Artifacts: {worker_result.work_dir}")

    if not summary.results:
        print("No enabled providers were configured.", file=sys.stderr)
    print(f"\nRun results: {summary.run_dir / 'results.json'}")
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(run_cli())
```

- [ ] **Step 4: Add default provider configuration**

Create `worker/providers.json` with:

```json
{
  "providers": [
    {
      "name": "codex",
      "enabled": true,
      "priority": 100,
      "command": [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--ephemeral",
        "-c",
        "approval_policy=\"never\"",
        "-C",
        "{work_dir}",
        "-"
      ],
      "timeout_seconds": 900
    },
    {
      "name": "claude",
      "enabled": true,
      "priority": 90,
      "command": [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--permission-mode",
        "dontAsk"
      ],
      "timeout_seconds": 900
    },
    {
      "name": "kimi",
      "enabled": true,
      "priority": 80,
      "command": ["kimi", "--print"],
      "timeout_seconds": 900
    }
  ]
}
```

- [ ] **Step 5: Run entry-point tests and verify they pass**

Run:

```powershell
python -m unittest tests.test_main -v
```

Expected: three tests report `ok` and the command ends with `OK`.

- [ ] **Step 6: Remove API-specific configuration and dependency**

Delete `.env.example`. Replace `requirements.txt` with an empty file because the runtime uses only the Python standard library.

Append this generated-data rule to `.gitignore`:

```gitignore
worker/runs/
```

- [ ] **Step 7: Rewrite the README for the multi-CLI CTF workflow**

Replace `README.md` with:

````markdown
# Multi-CLI CTF Workers

Run Codex CLI, Claude Code, Kimi CLI, and additional command-line agents concurrently against one CTF task.

Each provider receives the same prompt and challenge path, runs in a separate writable directory, and produces independent logs and artifacts. Results are displayed by configured priority after every provider finishes.

## Requirements

- Python 3.10 or newer
- At least one supported CLI installed and authenticated

The orchestrator does not read API keys. Authentication remains owned by each CLI.

## Provider Setup

Default providers are configured in `worker/providers.json`.

- Codex CLI uses `codex exec` with an ephemeral workspace-write sandbox.
- Claude Code uses non-interactive print mode with `dontAsk` permissions.
- Kimi CLI uses print mode.

CLI arguments can change between releases. Run each installed CLI's `--help` command and update its `command` array when necessary. Set `enabled` to `false` to skip a provider.

`priority` controls display order only. All enabled providers run concurrently.

## Run

```powershell
python main.py --challenge E:\ctf\challenge --prompt "Analyze the challenge and recover the flag."
```

Omit `--prompt` to enter one task interactively:

```powershell
python main.py --challenge E:\ctf\challenge
```

Use another configuration file when needed:

```powershell
python main.py --config E:\ctf\providers.json --challenge E:\ctf\challenge
```

## Results

Every invocation creates:

```text
worker/runs/<run-id>/
  codex/
    stdout.txt
    stderr.txt
  claude/
    stdout.txt
    stderr.txt
  kimi/
    stdout.txt
    stderr.txt
  results.json
```

Provider directories are writable and isolated from one another. The challenge directory is passed as read-only input by convention, while enforcement depends on each CLI's sandbox and permission flags. This is process and working-directory isolation, not container-grade isolation.

Provider failures do not cancel other providers. A missing executable is reported as `unavailable`, a non-zero exit as `failed`, and an expired timeout as `timeout`.

The process exits with code `0` when at least one provider succeeds and code `1` when all providers fail or no providers are enabled.

## Test

Tests use fake local CLIs and do not consume model quota:

```powershell
python -m unittest discover -s tests -v
```
````

- [ ] **Step 8: Commit the entry point and documentation when Git metadata is valid**

```powershell
git add main.py worker/providers.json tests/test_main.py README.md requirements.txt .gitignore
git add -u .env.example
git commit -m "feat: add multi-CLI CTF orchestrator"
```

Expected: one commit replacing the OpenAI API entry point and documenting CLI-owned authentication.

### Task 5: Full Verification and Design Alignment

**Files:**
- Verify: `main.py`
- Verify: `worker/config.py`
- Verify: `worker/executor.py`
- Verify: `worker/models.py`
- Verify: `worker/providers.json`
- Verify: `worker/runner.py`
- Verify: `tests/test_config.py`
- Verify: `tests/test_executor.py`
- Verify: `tests/test_runner.py`
- Verify: `tests/test_main.py`
- Verify: `README.md`

- [ ] **Step 1: Run the entire test suite**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: seventeen tests report `ok`; final output is `OK`.

- [ ] **Step 2: Compile every Python module**

Run:

```powershell
python -m py_compile main.py worker\__init__.py worker\models.py worker\config.py worker\executor.py worker\runner.py tests\test_config.py tests\test_executor.py tests\test_runner.py tests\test_main.py
```

Expected: exit code `0` with no output.

- [ ] **Step 3: Validate the default JSON configuration without launching a model**

Run:

```powershell
python -c "from pathlib import Path; from worker.config import load_provider_configs; providers=load_provider_configs(Path('worker/providers.json')); print([(p.name, p.priority, p.enabled) for p in providers])"
```

Expected:

```text
[('codex', 100, True), ('claude', 90, True), ('kimi', 80, True)]
```

- [ ] **Step 4: Verify command discovery without submitting prompts**

Run:

```powershell
Get-Command codex,claude,kimi -ErrorAction SilentlyContinue | Select-Object Name,Source
```

Expected in the current environment: Codex is listed; Claude Code and Kimi CLI may be absent and will later produce `unavailable` results until installed.

- [ ] **Step 5: Verify the user-facing command help**

Run:

```powershell
python main.py --help
```

Expected: usage output documents `--challenge`, `--config`, and `--prompt`, with exit code `0`.

- [ ] **Step 6: Review generated-file boundaries**

Run:

```powershell
Get-Content .gitignore
```

Expected: output includes `worker/runs/`, `.venv/`, `__pycache__/`, and `*.py[cod]`.

- [ ] **Step 7: Commit any verification corrections when Git metadata is valid**

```powershell
git status --short
```

Expected: no uncommitted changes after prior commits. If verification required a correction, stage only the corrected files and commit with a message describing that correction.
