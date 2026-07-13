# Agent Progression and Permissions Design

## Goal

Allow Codex and Claude to operate with unrestricted CLI permissions inside an externally isolated virtual machine, while preventing the planning loop from stalling after the first completed Intent.

## Chosen approach

Use each CLI's explicit unsafe/full-access flag in `worker/providers.json`. Keep the worker process boundary and per-provider working directories unchanged; the virtual machine, rather than the CLI sandbox, becomes the security boundary.

Make planning a bounded, side-effect-free phase. The planner prompt will explicitly forbid tool use and execution work, and each provider will have a shorter `planning_timeout_seconds` distinct from its execution timeout.

Treat a plan as valid only for the graph node set from which it was produced. If another agent changes the active nodes while planning is in flight, discard the stale plan without incrementing the provider failure counter or consuming the iteration budget, then immediately replan from the new graph. This prevents a valid bootstrap-origin plan from becoming a protocol failure merely because another provider added the first Fact.

When a provider returns `idle`, do not invoke the expensive planner again against an unchanged graph. Wait until the graph changes or cancellation occurs, polling the persisted graph revision so human/API edits also wake an idle agent. Provider permission failures remain visible in execution logs but the execution prompt tells agents not to publish those environment-specific failures as reusable challenge Facts.

## Data and control flow

1. Capture the active-node map and graph revision before invoking the planner.
2. Invoke the planner with the provider's planning timeout and a no-tools contract.
3. Parse against the captured active-node map.
4. Under the supervisor write lock, compare the current active-node map with the captured map.
5. If it changed, return `stale` and replan without recording failure; otherwise create or merge the Intent and execute it.
6. If the planner returns `idle`, wait until `BlackboardStore.graph_sequence()` advances before planning again.

## Error handling

- Planning timeout and invalid protocol remain provider failures and retain the existing three-failure degradation threshold.
- Stale planning output is normal concurrency, not failure.
- Full-access CLI flags do not bypass virtual-machine firewall, routing, or OS-account restrictions.
- Existing provider configurations without `planning_timeout_seconds` remain valid and default to the smaller of 120 seconds or their execution timeout.

## Verification

- Configuration tests assert the default Codex and Claude commands use the full-access flags and omit the restrictive flags.
- Protocol tests assert planning forbids tools and execution forbids environment failures as Facts.
- Supervisor tests reproduce the stale-bootstrap race and verify successful replanning.
- Supervisor tests verify an idle provider is not reinvoked until the graph changes.
- Full Python and frontend suites, Python compilation, JSON parsing, and frontend production build must pass before commit.
