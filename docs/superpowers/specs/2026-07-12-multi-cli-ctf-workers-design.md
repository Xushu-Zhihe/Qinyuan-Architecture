# Multi-CLI CTF Workers Design

## Goal

Replace the direct OpenAI SDK call with a configurable worker system that runs Codex CLI, Claude Code, Kimi CLI, and future command-line agents concurrently for CTF tasks.

The system accepts one task prompt and an optional CTF challenge directory. Every enabled provider runs in its own working directory. All provider results are retained and returned in configured priority order.

## Scope

This change covers:

- Non-interactive CLI execution through Python subprocesses.
- Concurrent execution of all enabled providers.
- Provider configuration through `worker/providers.json`.
- Separate working, logging, and artifact directories for every provider invocation.
- A shared challenge directory treated as read-only input.
- Structured result persistence and console output.
- Unit tests that use fake CLIs and do not consume model quota.

This change does not cover:

- Combining or judging model answers.
- Automatically applying provider changes back to the challenge directory.
- Container-based isolation.
- Installing, authenticating, or upgrading third-party CLIs.
- A persistent job queue or long-running worker daemon.

## Architecture

The repository will use the following structure:

```text
main.py
worker/
  __init__.py
  config.py
  executor.py
  models.py
  providers.json
  runner.py
  runs/
tests/
```

Responsibilities:

- `main.py` parses the challenge directory, reads the task prompt, invokes the runner, prints provider results, and returns the aggregate exit code.
- `worker/config.py` loads and validates provider configuration.
- `worker/models.py` defines provider configuration and result data structures.
- `worker/executor.py` executes one provider as a non-interactive subprocess.
- `worker/runner.py` creates the run layout, launches providers concurrently, sorts results, and writes `results.json`.
- `worker/providers.json` contains enabled state, priority, command arguments, and timeout for each CLI.
- `worker/runs/` contains generated execution state and is excluded from version control.

The executor interface will remain provider-neutral so a future Docker executor can replace the subprocess executor without changing `main.py` or the result contract.

## Provider Configuration

`worker/providers.json` will contain a list of provider objects:

```json
{
  "providers": [
    {
      "name": "codex",
      "enabled": true,
      "priority": 100,
      "command": ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--ephemeral", "-C", "{work_dir}", "-"],
      "timeout_seconds": 900
    },
    {
      "name": "claude",
      "enabled": true,
      "priority": 90,
      "command": ["claude", "-p", "--output-format", "text"],
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

The implementation will replace only documented path placeholders such as `{challenge_dir}` and `{work_dir}` inside individual command arguments. The task prompt will always be sent through standard input and will never be interpolated into a shell command.

Configuration validation will reject malformed JSON, duplicate provider names, empty commands, invalid priorities, and non-positive timeouts. Providers are enabled independently. A missing executable is a runtime `unavailable` result rather than a configuration error.

Exact Claude Code and Kimi CLI arguments may be adjusted in JSON after those tools are installed because their non-interactive and permission flags vary by version. Codex will receive appropriate non-interactive and native sandbox arguments in the default configuration.

## Execution Flow

1. The user starts `python main.py --challenge <directory>` and supplies a CTF task prompt.
2. `main.py` resolves and validates the optional challenge path.
3. The runner loads all enabled providers and creates `worker/runs/<run-id>/`.
4. The runner creates one work directory per provider.
5. The runner starts all provider subprocesses concurrently with `asyncio.gather()`.
6. Each subprocess uses its provider work directory as `cwd`, receives the prompt through standard input, and captures standard output and standard error.
7. Each execution writes `stdout.txt` and `stderr.txt` in its provider directory.
8. After all providers finish, the runner sorts results by descending priority and writes `results.json`.
9. `main.py` prints every result in the same priority order.

Priority affects presentation order only. A successful high-priority provider does not cancel lower-priority work.

## Run Layout

Each invocation creates an isolated directory tree:

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

The provider directory is the only working directory managed by the orchestrator for that provider. Providers may create additional files beneath it.

The challenge directory is passed as an input path and is never modified by orchestrator code. Strong read-only enforcement depends on each CLI's native permission or sandbox flags because Codex CLI, Claude Code, and Kimi CLI do not expose one uniform sandbox interface. The subprocess executor provides process separation, distinct working directories, argument-safe invocation, and provider-specific sandbox configuration; it does not claim container-grade isolation.

## Process Safety

The executor will call subprocess APIs with `shell=False` and a command argument list. This prevents prompt text and CTF paths from being interpreted as PowerShell, `cmd.exe`, or POSIX shell syntax.

On Windows, the executor will resolve the configured executable with `shutil.which()` before process creation. This selects executable npm `.cmd` shims instead of same-name extensionless shell scripts that Windows `CreateProcess` cannot launch directly.

The prompt is written to standard input exactly as text. Paths are expanded only into individual configured arguments. Environment inheritance remains the default so each installed CLI can access its existing authentication, while the orchestrator does not read or persist credentials.

Timeout handling terminates the timed-out provider and its child process tree where the operating system supports it. Other providers continue unaffected.

## Result Contract

Each provider produces a structured result with at least these fields:

```json
{
  "provider": "codex",
  "priority": 100,
  "status": "success",
  "exit_code": 0,
  "duration_seconds": 12.4,
  "output": "Candidate flag: flag{example}",
  "stdout_path": "worker/runs/20260712T020000Z/codex/stdout.txt",
  "stderr_path": "worker/runs/20260712T020000Z/codex/stderr.txt",
  "work_dir": "worker/runs/20260712T020000Z/codex"
}
```

Supported statuses are:

- `success`: the CLI exited with code zero.
- `failed`: the CLI exited with a non-zero code.
- `timeout`: the configured timeout elapsed.
- `unavailable`: the configured executable could not be found or started.

Standard output is the provider's primary output. Standard error is retained in its file and included in failure diagnostics. Empty successful output remains a success because some coding agents may communicate through artifacts in the provider work directory.

The main process exits with code zero when at least one provider succeeds. It exits with code one when no provider succeeds or when no providers are enabled.

## Error Handling

Provider failures are isolated. One missing, timed-out, or failed CLI never cancels another provider.

Startup and configuration errors that prevent the run from being defined, such as invalid JSON or an invalid challenge path, are reported before any subprocess starts and produce exit code one.

All completed provider outcomes are persisted even when every provider fails. This gives the user one stable place to inspect stderr, exit codes, durations, and partial artifacts.

## Testing

Tests will use Python's standard `unittest` library and temporary directories. Fake CLI scripts will stand in for real providers so verification is deterministic and does not use network access, credentials, or model quota.

Coverage will include:

- Valid and invalid provider configuration.
- Duplicate provider names and invalid timeouts.
- Concurrent execution rather than sequential execution.
- Descending priority result order.
- Exact prompt delivery through standard input, including shell metacharacters and non-ASCII CTF text.
- Distinct current working directories for every provider.
- Missing executable handling.
- Timeout and non-zero exit handling.
- Partial success and total failure exit behavior.
- `stdout.txt`, `stderr.txt`, and `results.json` persistence.

Real-provider verification will be limited to command discovery or help output. Automated tests will not submit prompts to Codex, Claude Code, or Kimi CLI.

## Documentation Changes

`README.md` will explain:

- Required Python version.
- How to install and authenticate each CLI separately.
- How to edit `worker/providers.json`.
- How to invoke the orchestrator with a CTF challenge directory.
- Where run results and provider artifacts are stored.
- The difference between work-directory isolation and container-grade isolation.

The OpenAI SDK dependency and API-key example will be removed because the application will no longer call an LLM API directly.
