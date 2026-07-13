# Agent Progression and Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Codex and Claude full VM-scoped permissions and keep agents progressing after the first Intent without planning races or idle polling.

**Architecture:** Extend provider configuration with an independent planning timeout. Make planning side-effect-free by prompt contract, validate plans against their captured graph, discard stale plans without failure, and wait for persisted graph changes after idle.

**Tech Stack:** Python 3.10, asyncio, SQLite, unittest, JSON provider configuration.

---

### Task 1: Lock provider permissions and planning timeout into tests

**Files:**
- Modify: `tests/test_config.py`
- Modify: `worker/models.py`
- Modify: `worker/config.py`
- Modify: `worker/providers.json`

- [ ] Add tests that load `worker/providers.json`, require Codex's `--dangerously-bypass-approvals-and-sandbox`, require Claude's `--dangerously-skip-permissions`, reject the former restrictive flags, and verify a positive `planning_timeout_seconds`.
- [ ] Run `python -m unittest tests.test_config -v` and confirm the new assertions fail because the field and flags are absent.
- [ ] Add `planning_timeout_seconds` to `ProviderConfig`, parse it with a backward-compatible default, and update the two provider commands.
- [ ] Rerun `python -m unittest tests.test_config -v` and confirm it passes.

### Task 2: Make planning bounded and side-effect-free

**Files:**
- Modify: `tests/test_protocol.py`
- Modify: `tests/test_supervisor.py`
- Modify: `orchestrator/protocol.py`
- Modify: `orchestrator/supervisor.py`

- [ ] Add protocol assertions requiring the planning request to forbid tool calls and the execution request to classify provider/environment failures as diagnostics rather than Facts.
- [ ] Add a supervisor test asserting the planning call receives `planning_timeout_seconds` while execution retains `timeout_seconds`.
- [ ] Run the focused tests and confirm the new assertions fail.
- [ ] Render the new contracts and pass a `dataclasses.replace` copy of the provider into planning with the bounded timeout.
- [ ] Rerun the focused tests and confirm they pass.

### Task 3: Replan stale bootstrap output without failure

**Files:**
- Modify: `tests/test_supervisor.py`
- Modify: `orchestrator/supervisor.py`

- [ ] Add a regression where another graph writer adds the first Fact while a provider is planning from origin; with an iteration budget of one, assert the stale attempt does not consume that budget and the provider replans from the Fact without a failed status.
- [ ] Run the regression and confirm the current code reports failure or fails to execute the second plan.
- [ ] Parse against the captured node map, compare it with the current map under the write lock, and return `stale` on mismatch without incrementing failures.
- [ ] Rerun the regression and confirm it passes.

### Task 4: Stop idle replanning until the graph changes

**Files:**
- Modify: `tests/test_blackboard_store.py`
- Modify: `tests/test_supervisor.py`
- Modify: `orchestrator/store.py`
- Modify: `orchestrator/supervisor.py`

- [ ] Add a store test proving only persisted `graph.*` events advance `graph_sequence()`.
- [ ] Add a supervisor test that observes one idle planning call, waits longer than the poll interval, mutates the graph, and observes exactly one subsequent call.
- [ ] Run the regressions and confirm `graph_sequence` is missing and the current loop replans unchanged work.
- [ ] Implement `graph_sequence()` and an interruptible graph-change wait used by the idle path.
- [ ] Rerun the regressions and confirm they pass.

### Task 5: Verify and commit

**Files:**
- Review all modified files.

- [ ] Run `python -m unittest discover -v`.
- [ ] Run `python -m compileall orchestrator worker web tests main.py web_main.py`.
- [ ] Run `npm test -- --run` and `npm run build` in `frontend/`.
- [ ] Parse `worker/providers.json` with Python and inspect `git diff --check` plus `git diff`.
- [ ] Stage only the intended files, excluding `worksapce/`, and commit with message `fix: keep agents progressing after first intent`.
