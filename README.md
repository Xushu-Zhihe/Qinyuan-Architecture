# Multi-CLI CTF Blackboard

Run Codex CLI, Claude Code, Kimi CLI, and additional command-line agents in
concurrent rounds against one shared CTF blackboard graph.

## Blackboard Concepts

- Goal: the outcome the run must achieve.
- Origin: the challenge statement and known starting conditions.
- Fact: an agent-discovered proposition with one or more sources.
- Intent: one agent's next planned action.
- Hint: human guidance supplied initially or between rounds.

Every concept is a sourced graph node. Relationships such as `supports`,
`contradicts`, `derived_from`, and `plans_from` are stored as sourced edges.

## Requirements

- Python 3.10 or newer
- At least one configured CLI installed and authenticated

The orchestrator uses only the Python standard library. Each CLI owns its own
authentication.

## Run

```powershell
python main.py --goal "Recover the flag" --origin "Analyze the supplied binary and config.dat" --challenge E:\ctf\challenge
```

Add initial human Hints and change the round limit:

```powershell
python main.py --goal "Recover the flag" --origin "Challenge statement" --hint "Inspect config.dat" --hint "Check for reused IVs" --max-rounds 7 --challenge E:\ctf\challenge
```

When Goal or Origin is omitted, an interactive terminal asks for it. Between
unresolved rounds, an interactive run accepts another Hint; blank input
continues without one.

## Agent Protocol

Every provider receives the same JSON graph snapshot at the start of a round.
It may write ordinary analysis, but its final structured line must begin with:

```text
BLACKBOARD_UPDATE:
```

The JSON object may propose Fact and Intent nodes, sourced edges, a
goal-completion claim, and a final answer. Agents cannot create Goal, Origin,
or Hint nodes and cannot choose their stored source identity.

## Persistence

Each run creates:

```text
worker/runs/<run-id>/
  blackboard.db
  snapshots/
    round-000.json
    round-001.json
  rounds/
    001/
      codex/
        stdout.txt
        stderr.txt
```

`blackboard.db` is the source of truth. JSON files are reproducible snapshots
used for agent input and inspection.

Exact normalized duplicate Facts share one graph node while retaining every
independent source. Textually similar Facts are not automatically merged
because small wording differences may be technically significant.

## Completion

Provider priority controls presentation only. With multiple available agents,
two independent agents must submit the same final answer to reach consensus.
Conflicting answers remain visible and trigger another round. A single
available agent may complete the run alone.

The process exits with code `0` only when the Goal is achieved. Unresolved
conflicts, exhausted rounds, or no usable providers exit with code `1`.

## Provider Setup

Provider commands, enablement, presentation priority, and timeouts are
configured in `worker/providers.json`. Requests travel through stdin with
`shell=False`, and every provider runs in its own writable round directory.

## Test

Tests use fake local CLIs and do not consume model quota:

```powershell
python -m unittest discover -s tests -v
```
