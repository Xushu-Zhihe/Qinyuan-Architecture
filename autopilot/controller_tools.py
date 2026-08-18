PLATFORM_HTTP_TOOL = {
    "type": "function",
    "function": {
        "name": "platform_http_request",
        "description": "Send one platform HTTP request during the current phase.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["method", "url_or_path"],
            "properties": {
                "method": {"type": "string"},
                "url_or_path": {"type": "string"},
                "query": {"type": ["object", "null"]},
                "headers": {"type": ["object", "null"]},
                "json_body": {"type": ["object", "array", "string", "null"]},
                "form_body": {"type": ["object", "null"]},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
            },
        },
    },
}

DOWNLOAD_ATTACHMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "download_attachment",
        "description": "Download one platform attachment into backend staging.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url_or_path", "destination_relative_path"],
            "properties": {
                "url_or_path": {"type": "string"},
                "destination_relative_path": {"type": "string"},
                "headers": {"type": ["object", "null"]},
            },
        },
    },
}

EXTRACT_TASK_FIELDS_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_task_fields",
        "description": "Extract task fields from a response using JSON paths.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["response", "mapping"],
            "properties": {
                "response": {"type": "object"},
                "mapping": {"type": "object"},
            },
        },
    },
}

PREVIEW_SUBMISSION_TOOL = {
    "type": "function",
    "function": {
        "name": "preview_submission",
        "description": "Freeze the exact HTTP request for this submission attempt.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["request"],
            "properties": {"request": {"type": "object"}},
        },
    },
}

SUBMIT_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_platform_result",
        "description": "Send a previously frozen submission request.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["frozen_plan_fingerprint"],
            "properties": {"frozen_plan_fingerprint": {"type": "string"}},
        },
    },
}

GET_SOLVER_RESULT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_solver_result",
        "description": "Read the selected GoalResult for the current solver project.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id"],
            "properties": {"project_id": {"type": "string"}},
        },
    },
}

LIST_SOLVER_FACTS_TOOL = {
    "type": "function",
    "function": {
        "name": "list_solver_facts",
        "description": "List bounded supporting facts already attached to the selected GoalResult.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["project_id"],
            "properties": {
                "project_id": {"type": "string"},
                "relation": {"type": ["string", "null"]},
                "cursor": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
}

SKIP_TOOL = {
    "type": "function",
    "function": {
        "name": "skip_platform_task",
        "description": "Send one platform request that releases the current task.",
        "parameters": PLATFORM_HTTP_TOOL["function"]["parameters"],
    },
}

PHASE_TOOLS = {
    "fetch": (PLATFORM_HTTP_TOOL, DOWNLOAD_ATTACHMENT_TOOL, EXTRACT_TASK_FIELDS_TOOL),
    "submit": (GET_SOLVER_RESULT_TOOL, LIST_SOLVER_FACTS_TOOL, PREVIEW_SUBMISSION_TOOL, SUBMIT_RESULT_TOOL),
    "skip": (SKIP_TOOL,),
    "plan": (),
}
