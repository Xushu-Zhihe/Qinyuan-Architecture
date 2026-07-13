---
name: blackboard-result-protocol
description: Use when executing a blackboard Intent, producing BLACKBOARD_RESULT output, or correcting protocol_error failures such as fact proposal must be an object.
---

# Blackboard Result Protocol

## Core Rule

Finish with exactly one single-line `BLACKBOARD_RESULT: ` record whose suffix is strict JSON accepted by the project parser. Finding an answer does not bypass validation.

## Required Shape

The JSON object must contain exactly:

- `intent_id`: the assigned Intent ID.
- `facts`: objects shaped `{"ref":"unique-temp-ref","content":"non-empty fact"}`.
- `edges`: objects shaped `{"type":"supports","from":"node-or-ref","to":"node-or-ref"}`.
- `goal_achieved`: JSON boolean.
- `final_answer`: non-empty string when achieved, otherwise string or null.

Allowed edge types: `supports`, `contradicts`, `derived_from`, `addresses`, `similar_to`, `equivalent_to`.

Edges may reference existing graph node IDs or temporary fact refs declared in the same result. Use empty arrays when no facts or edges are needed.

## Workflow

1. Execute only the assigned Intent. Do not plan another Intent.
2. Write the JSON object to `blackboard-result.json` without the protocol prefix.
3. Run the bundled validator with the assigned Intent ID and every existing node ID referenced by an edge:

   `python <skill-directory>/scripts/validate_result.py --intent-id <assigned-id> --existing-node-id <node-id> --file blackboard-result.json`

4. Fix every validation error and rerun until it prints `valid`.
5. Emit one final line: `BLACKBOARD_RESULT: ` followed by the compact JSON file content.

## Valid Example

```text
BLACKBOARD_RESULT: {"intent_id":"intent-1","facts":[{"ref":"flag-fact","content":"flag = flag{example}"}],"edges":[{"type":"supports","from":"flag-fact","to":"goal-1"}],"goal_achieved":true,"final_answer":"flag{example}"}
```

## Invalid Forms

- `BLACKBOARD_RESULT: intent_id="intent-1" facts=[]` is key=value text, not JSON.
- `"facts":["flag found"]` fails with `fact proposal must be an object`.
- `"edges":[["fact-1","goal-1"]]` fails because every edge must be an object with `type`, `from`, and `to`.
- Do not wrap the final protocol line in a Markdown fence.
