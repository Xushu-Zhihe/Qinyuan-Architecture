# Blackboard Orchestrator Design

## Goal

Extend the multi-CLI CTF runner with a round-based blackboard architecture.
Agents collaborate through a shared, traceable graph whose nodes represent the
goal, original problem statement, known facts, next-step intents, and human
hints.

The system must preserve source provenance, prevent agents from mutating
human-owned input, merge duplicate facts without losing independent support,
and resolve final answers through agreement rather than configured provider
priority.

## Definitions

- **Goal:** The outcome the run must achieve.
- **Origin:** The challenge statement and conditions known before agent work.
- **Fact:** A proposition discovered or asserted during analysis.
- **Intent:** One agent's stated next action.
- **Hint:** Guidance supplied by a human before or between rounds.

All five concepts are graph nodes. Goal, Origin, and Hint nodes are owned by
the human input path. Agents may propose only Fact and Intent nodes.

## Scope

This change covers:

- Multi-round concurrent execution of all enabled CLI agents.
- A shared graph snapshot read by every agent in a round.
- SQLite persistence for graph state, provenance, rounds, and events.
- JSON graph snapshots for agent input and human inspection.
- Structured agent updates extracted from CLI stdout.
- Human hints supplied initially or between rounds.
- Duplicate Fact consolidation with retained provenance.
- Final-answer consensus and conflict handling.
- Deterministic tests using fake local CLIs.

This change does not cover:

- A network service or long-running job queue.
- Multiple orchestrator processes writing to the same run database.
- A dedicated graph database such as Neo4j.
- Embedding-based semantic deduplication.
- Automatic correctness grading against a remote CTF platform.
- Installing or authenticating third-party CLIs.

## Package Boundaries

Collaboration logic belongs in a new `orchestrator` package. The existing
`worker` package remains a provider-neutral CLI execution layer.

```text
main.py
orchestrator/
  __init__.py
  models.py
  store.py
  protocol.py
  scheduler.py
worker/
  __init__.py
  models.py
  config.py
  executor.py
  providers.json
  runs/
tests/
```

Responsibilities:

- `main.py` parses Goal, Origin, initial Hints, maximum rounds, challenge path,
  and provider configuration; it then presents the final run outcome.
- `orchestrator/models.py` defines graph, provenance, proposed update, round,
  and orchestration result structures.
- `orchestrator/store.py` owns the SQLite schema, transactions, graph queries,
  Fact consolidation, event recording, and snapshot export.
- `orchestrator/protocol.py` renders an agent prompt from a graph snapshot and
  parses and validates the structured update in stdout.
- `orchestrator/scheduler.py` creates the run, executes concurrent rounds,
  collects human Hints, merges valid updates, and evaluates consensus.
- `worker/config.py` and `worker/providers.json` continue to describe CLI
  commands, timeouts, enablement, and presentation priority.
- `worker/executor.py` executes one CLI request in one directory and returns
  raw process results. It does not understand blackboard semantics.

The existing `worker/runner.py` is replaced by the orchestration scheduler.

## Run Layout

Each invocation creates:

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
      claude/
        stdout.txt
        stderr.txt
      kimi/
        stdout.txt
        stderr.txt
```

SQLite is the sole source of truth. JSON snapshots are derived artifacts and
may be regenerated from the database. Raw CLI output remains in ordinary log
files.

## Graph Model

### Nodes

Every node has:

- A unique orchestrator-assigned ID.
- A type: `goal`, `origin`, `fact`, `intent`, or `hint`.
- Canonical content.
- Creation round.
- State such as `active` or `superseded`.
- One or more provenance records.

Goal and Origin nodes are immutable roots created in round zero. Hint nodes
are created only through the human input path. Fact nodes are immutable
propositions. An Intent node represents one agent's next action and may be
superseded by that same agent's later Intent.

### Provenance

Source is not stored as a single lossy field on a consolidated Fact. A
separate provenance record associates a node with:

- The source identity, such as `human`, `codex`, `claude`, or `kimi`.
- The round in which that source supplied the content.
- The original, unnormalized content.

This allows one canonical Fact node to retain support from multiple agents.
Goal, Origin, Hint, and Intent nodes normally have one source; consolidated
Facts may have many.

### Edges

Directed edges have a unique ID, relation type, source, and creation round.
The initial supported relations are:

- `supports`
- `contradicts`
- `derived_from`
- `addresses`
- `plans_from`
- `supersedes`
- `similar_to`
- `equivalent_to`

Agents may propose relations involving existing node IDs and temporary IDs
for their own new nodes. The scheduler creates `supersedes` edges when an
agent replaces its current Intent. Human Hints receive an `addresses` edge to
the Goal by default.

Unknown relation types and edges with missing endpoints are rejected.

## SQLite Persistence

The database contains at least these logical tables:

- `runs`: run identity, status, maximum rounds, final answer, and timestamps.
- `rounds`: round number, lifecycle status, snapshot path, and outcome.
- `nodes`: canonical graph nodes and node state.
- `node_provenance`: source, round, and original content for each node.
- `edges`: typed, sourced graph relationships with foreign keys to nodes.
- `agent_results`: process status, protocol status, paths, duration, and final
  answer claim for each provider in each round.
- `events`: append-only records for node creation, provenance addition, edge
  creation, Intent replacement, Hint insertion, consensus, and run status.

Foreign keys are enabled. A whole round's valid graph updates are committed in
one transaction. An internal persistence error rolls back the complete round.

## Fact Consolidation

Fact consolidation is conservative because textual similarity is not proof of
semantic equivalence.

### Exact Normalized Match

Before fingerprinting, Fact text receives Unicode normalization, surrounding
whitespace removal, and internal whitespace collapsing. Case and meaningful
punctuation remain intact because CTF flags, hashes, paths, algorithms, and
identifiers may be case-sensitive.

Facts with the same fingerprint use one canonical node. The store appends a
new provenance record instead of creating a duplicate node. The snapshot
renders the canonical Fact once with all supporting sources.

### Possible Similarity

High textual similarity may create a `similar_to` edge or compact snapshot
cluster, but never causes an automatic hard merge. For example, `AES-CBC` and
`AES-ECB` must not be merged merely because their strings are similar.

Hard consolidation beyond an exact normalized match requires an explicit
`equivalent_to` relationship established by subsequent agent analysis. Until
then, both propositions remain independently visible and traceable.

## Agent Protocol

Every agent receives the same round-start JSON snapshot and instructions that
Goal, Origin, and Hint nodes are read-only. The agent may write ordinary
analysis to stdout, but the final structured proposal must be one line with
this prefix:

```text
BLACKBOARD_UPDATE: {"nodes": [...], "edges": [...], "goal_achieved": false, "final_answer": null}
```

The parser scans from the bottom of stdout and uses the last syntactically and
semantically valid prefixed record.

Agent-proposed nodes use temporary references and may have only `fact` or
`intent` types. Agent-proposed edges may reference snapshot node IDs or
temporary references in the same update. The orchestrator, not the agent,
assigns persistent IDs, source, and round.

`goal_achieved=true` requires a non-empty `final_answer`. A malformed or
unauthorized update is rejected as a whole; no partial graph mutation occurs.

## Round Execution

1. Parse and validate `--goal`, `--origin`, repeatable `--hint`,
   `--max-rounds`, provider configuration, and the optional challenge path.
2. Create the run database and write Goal, Origin, and initial Hint nodes in a
   transaction.
3. Export `round-000.json`.
4. At each round start, export one immutable snapshot.
5. Render one request from that snapshot for each enabled provider.
6. Execute all providers concurrently in separate round/provider directories.
7. Parse each provider's structured update while preserving its raw logs.
8. Validate all updates independently.
9. Merge all valid updates in deterministic provider-name order within one
   SQLite transaction. Configured priority does not affect graph truth.
10. Evaluate final-answer consensus after the whole round is committed.
11. If no consensus is reached and another round remains, collect an optional
    human Hint when stdin is interactive. Blank input continues unchanged.
12. Stop on consensus, no usable providers, or the configured round limit.

Non-interactive environments skip between-round Hint prompts. Initial Hints
remain available through repeated `--hint` arguments.

## Consensus and Final Answers

Provider priority is not an epistemic ranking. It may control presentation
order only and never selects facts or final answers.

Final answers are normalized conservatively and grouped by exact value. The
run reaches `achieved` when at least two independent available agents submit
the same answer. When only one agent is available, its valid achieved claim is
sufficient.

If achieved claims conflict, all candidates and their sources are persisted
and the next round explicitly presents the conflict for further verification.
No configured provider wins a tie. If the maximum round is reached while
conflicts remain, the run ends as `unresolved` and reports every candidate.

Process success alone does not mean the Goal was achieved:

- `achieved` produces exit code 0.
- `unresolved`, `max_rounds_reached`, no usable providers, or no valid achieved
  claim produces exit code 1.

## Error Handling

- Provider process failure, timeout, or unavailability is isolated from other
  providers.
- A zero exit code without a valid structured update records process status
  `success` and protocol status `protocol_error` but changes no graph state.
- Unknown node references, illegal relation types, empty content, spoofed
  source data, human-owned node creation, or an incomplete achieved claim
  reject that provider's complete update.
- One rejected update does not prevent other valid same-round updates.
- Database failure rolls back the complete graph merge for that round and
  aborts the run rather than exposing partial state.
- Snapshot export failure prevents provider launch for that round.
- Intent state changes and scheduler-generated `supersedes` edges are atomic.
- Conflicting Facts coexist and may be linked with `contradicts`.

## CLI Changes

The entry point replaces the generic prompt with explicit blackboard input:

```powershell
python main.py --goal "Recover the flag" --origin "<challenge statement>" --hint "Inspect the config" --challenge E:\ctf\challenge --max-rounds 5
```

`--goal` and `--origin` are independent required concepts. When omitted in an
interactive terminal, the CLI asks for them separately. Empty values are
rejected. `--hint` is repeatable. `--max-rounds` must be a positive integer
and defaults to 5.

The old `--prompt` option is removed rather than silently assigning ambiguous
Goal or Origin semantics.

## Testing

Tests use `unittest`, temporary SQLite databases, and fake local CLIs. They do
not invoke real models or require network access.

Coverage includes:

- Database schema, foreign keys, graph persistence, and transaction rollback.
- Immutable Goal and Origin nodes and human-only Hint creation.
- Node and edge provenance.
- Intent replacement, state changes, and `supersedes` edges.
- Prompt rendering from a complete graph snapshot.
- Structured update extraction from otherwise free-form stdout.
- Rejection of malformed JSON, unknown references, illegal edges, spoofed
  sources, and unauthorized node types.
- Exact Fact consolidation with multiple preserved sources.
- Conservative handling of similar but semantically different Facts.
- Compact snapshot rendering of consolidated Facts.
- Concurrent provider execution against one identical round snapshot.
- Isolation of provider and protocol failures.
- Initial and between-round human Hints.
- Same-answer consensus, single-provider completion, conflicting candidates,
  and maximum-round unresolved results.
- CLI argument validation, output paths, final status, and exit codes.

Existing provider configuration and subprocess safety tests remain, adjusted
only for the executor's request-oriented interface and round-specific output
directories.

## Documentation Changes

`README.md` will document the graph concepts, source provenance, round flow,
SQLite database, JSON snapshots, structured provider protocol, Fact
consolidation limits, consensus rules, CLI usage, and result layout.

