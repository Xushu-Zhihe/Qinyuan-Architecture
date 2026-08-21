# Platform Manifest: Example Competition Platform

- Manifest ID: `example-platform-v1`
- Version: `1`
- Content hash: `74f45196500c77e8023909d0be5e0ab21891563f3157d02728d50b7548d365e3`
- Base URL: `https://platform.example/api`

## Operations

### `fetch-question` — Fetch the next question

- Kind: `fetch`
- Request: `GET https://platform.example/api/questions/next`
- Side effect: `False`
- Body mode: `none`
- Response type: `json`
- Description: The platform returns the next available question and its attachments.
- Success condition: {"expected": null, "kind": "status_code", "path": null, "status_codes": [200], "text": null}

### `get-current-question` — Read the current question

- Kind: `current`
- Request: `GET https://platform.example/api/questions/{{question_id}}`
- Side effect: `False`
- Body mode: `none`
- Response type: `json`
- Depends on: `fetch-question`

### `download-attachment` — Download a question attachment

- Kind: `attachment`
- Request: `GET https://platform.example/api/attachments/{{attachment_id}}`
- Side effect: `False`
- Body mode: `none`
- Response type: `bytes`
- Depends on: `fetch-question`

### `submit-answer` — Submit the solved answer

- Kind: `submit`
- Request: `POST https://platform.example/api/questions/{{question_id}}/submit`
- Side effect: `True`
- Body mode: `json`
- Response type: `json`
- Depends on: `get-current-question`
- Success condition: {"expected": true, "kind": "json_path_equals", "path": "accepted", "status_codes": [], "text": null}
- Answer mapping: `raw` (1 field(s))

### `skip-question` — Skip the current question

- Kind: `skip`
- Request: `POST https://platform.example/api/questions/{{question_id}}/skip`
- Side effect: `True`
- Body mode: `json`
- Response type: `json`

### `abandon-question` — Abandon the current question

- Kind: `abandon`
- Request: `DELETE https://platform.example/api/questions/{{question_id}}/lease`
- Side effect: `True`
- Body mode: `none`
- Response type: `json`

## Sources

- `Contest manual, platform API chapter` (high)
