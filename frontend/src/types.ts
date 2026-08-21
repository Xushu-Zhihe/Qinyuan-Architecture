export type BlackboardNodeType = 'goal' | 'origin' | 'fact' | 'hint'
export type WorkspaceMode = 'browse' | 'edit'
export type GraphTool = 'select' | 'connect'
export type IntentStatus = 'planned' | 'running' | 'completed' | 'failed' | 'cancelled'

export interface ProvenanceOutput {
  intent_id: string
  execution_id: string
}

export interface BlackboardNode {
  id: string
  type: BlackboardNodeType
  content: string
  state: string
  generation: number
  created_at: string
  sources: string[]
  produced_by: ProvenanceOutput | null
}

export interface IntentExecution {
  id: string
  intent_edge_id: string
  provider: string
  status: string
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  stdout_path: string
  stderr_path: string
  work_dir: string
  error_message: string | null
  queued_at?: string | null
  queue_seconds?: number
  duration_seconds?: number | null
  retry_count?: number
  retry_of_execution_id?: string | null
  cancel_reason?: string | null
  wait_reasons?: string[]
  model?: string
  cli_version?: string
  token_usage?: object | string
  logs_retained?: boolean
}

export interface PlanningAttempt {
  id: string
  project_id: string
  provider: string
  status: string
  snapshot_sequence: number
  queued_at: string
  started_at: string | null
  finished_at: string | null
  queue_seconds: number
  duration_seconds: number | null
  stdout_path: string
  stderr_path: string
  work_dir: string
  decision_reason: string | null
  intent_edge_id: string | null
  retry_count: number
  wait_reasons: string[]
  model: string
  cli_version: string
  token_usage: object | string
  logs_retained: boolean
}

export interface ProviderHealth {
  provider: string
  status: string
  consecutive_failures: number
  last_error: string | null
  checked_at: string
  cooldown_until: string | null
  updated_at: string
  cli_version: string
  stream_format?: string
  streaming_supported?: boolean
}

export interface RecoverySummary {
  planning_attempts: number
  executions: number
  total: number
  latest_at: string | null
}

export interface BlackboardEdge {
  id: string
  type: string
  from_node: string
  to_node: string
  generation: number
  created_at: string
  content: string | null
  sources: string[]
  status?: IntentStatus
  executions?: IntentExecution[]
}

export interface ProjectSummary {
  id: string
  name: string
  mode: 'manual' | 'run'
  status: string
  challenge_path: string | null
  providers: string[]
  agent_limit: number
  created_at: string
  updated_at: string
  last_opened_at: string
  fact_count?: number
  intent_count?: number
  highest_generation?: number
  final_answer?: string | null
  goal_version?: number
  recovery?: RecoverySummary
}

export interface LayoutSnapshot {
  revision: number
  nodes: Record<string, { x: number; y: number }>
  edges: Record<string, unknown>
  viewport: Record<string, number>
}

export interface ProjectSnapshot {
  project: ProjectSummary
  nodes: BlackboardNode[]
  edges: BlackboardEdge[]
  layout: LayoutSnapshot
  candidates: string[]
  highest_generation: number
  sequence: number
  planning_attempts: PlanningAttempt[]
  provider_health: ProviderHealth[]
  recovery: RecoverySummary
}

export interface ProjectEvent {
  project_id: string
  sequence: number
  type: string
  source: string
  payload: Record<string, unknown>
  created_at: string
}

export interface ExecutionLogs {
  stdout: string
  stderr: string
  all?: string
}

export interface LogPageState {
  offset: number
  next_offset: number
  total_bytes: number
  has_more: boolean
  retained: boolean
}

export interface ExecutionLogPage extends ExecutionLogs {
  pagination: {
    limit_bytes: number
    stdout: LogPageState
    stderr: LogPageState
    all: LogPageState
  }
}

export interface SyncedExecutionLogs {
  logs: ExecutionLogs
  offsets: { stdout: number; stderr: number; all: number }
}

export interface RuntimeTask {
  kind: 'planning' | 'execution'
  id: string
  project_id: string
  project_name: string
  provider: string
  status: string
  queued_at: string
  started_at: string | null
  intent_id?: string
}

export interface PendingTask {
  project_id: string
  provider: string
  kind: 'planning' | 'execution'
  priority: number
  queue_seconds: number
  wait_reasons: string[]
}

export interface RuntimeProject extends ProjectSummary {
  provider_health: ProviderHealth[]
  active_task_count: number
  recovery: RecoverySummary
  event_metrics: {
    subscribers: number
    queue_depth: number
    queue_capacity: number
    published_events: number
    dropped_events: number
    backfilled_events: number
    log_events_last_minute: number
  }
}

export interface RuntimeSnapshot {
  limits: {
    max_global_running: number
    max_project_running: number
    max_global_planning: number
    max_project_planning: number
    aging_seconds: number
  }
  coordinator: {
    running: {
      global: number
      planning: number
      execution: number
      by_project: Record<string, number>
      by_provider: Record<string, number>
    }
    pending: {
      total: number
      planning: number
      execution: number
      items: PendingTask[]
    }
  }
  projects: RuntimeProject[]
  active_tasks: RuntimeTask[]
}

export interface AutoHistoryEntry {
  external_id: string
  name: string
  project_id: string
  answer: string
  status: string
  message: string | null
  result_ref?: string | null
  result_fact_id?: string | null
  reason_code?: string | null
  attempt_count?: number
  accepted_at?: string | null
  finished_at?: string | null
  attempts?: Array<Record<string, unknown>>
}

export interface ControllerSummary {
  configured: boolean
  base_url: string
  model: string
}

export interface PlatformTaskSnapshot {
  external_id: string | null
  name: string | null
  status: string | null
  project_id: string | null
}

export interface GoalResultSnapshot {
  result_ref: string | null
  fact_id: string | null
  preview: string | null
}

export interface ProviderCapability {
  request_protocol_versions: number[]
  result_protocol_versions: number[]
  skill_mode: 'none' | 'inline' | 'required' | 'tool'
  available_skills: string[]
  validates_result_locally: boolean
  supports_result_fact: boolean
  schema_version: string | null
  fixture_digest: string | null
  auto_ready: boolean
  status: string
}

export interface AutoSession {
  id: string
  status: string
  mode?: 'prompt' | 'structured'
  controller_provider?: string | null
  controller?: ControllerSummary | null
  solver_providers: string[]
  available_providers: string[]
  agent_limit: number
  max_tasks: number
  completed_tasks: number
  processed_tasks?: number
  current_external_id: string | null
  current_task_name: string | null
  current_project_id: string | null
  skip_requested?: boolean
  finish_after_current_task_requested?: boolean
  current_task_started_at?: string | null
  current_task_deadline_at?: string | null
  current_task_remaining_seconds?: number | null
  last_message: string | null
  last_error: string | null
  current_task_status?: string | null
  reason_code?: string | null
  current_goal_result?: GoalResultSnapshot | null
  current_attempt?: {
    phase: string | null
    retry_count: number
    replan_count: number
    plan_fingerprint: string | null
  }
  history: AutoHistoryEntry[]
}

export interface AutopilotSnapshot {
  session: AutoSession | null
  providers: string[]
  provider_capabilities?: Record<string, ProviderCapability>
  controller_health?: { status: string; last_error: string | null; checked_at: string | null; model: string | null }
}

export interface ControllerAdvancedInput {
  timeout_seconds: number
  max_rounds: number
  max_transport_retries: number
  temperature: number | null
  max_output_tokens: number | null
  reasoning_effort: string | null
  extra_headers: Record<string, string>
  extra_body: Record<string, unknown>
}

export interface ControllerInput {
  api_key: string
  base_url: string
  model: string
  advanced: ControllerAdvancedInput
}

export interface EndpointInput {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  query: Record<string, unknown>
  headers: Record<string, string>
  body: unknown
  form: Record<string, string> | null
  timeout_seconds: number
  max_retries: number
  retry_status_codes: number[]
}

export interface OutcomeConditionInput {
  path?: string | null
  equals?: unknown
  status_codes?: number[]
}

export interface StructuredPlatformInput {
  base_url: string
  auth: {
    type: 'bearer' | 'header' | 'query' | 'cookie'
    token: string
    header_name: string
    prefix: string
    parameter_name: string
  }
  default_headers: Record<string, string>
  fetch_task: EndpointInput
  current_task: EndpointInput | null
  task_info: EndpointInput | null
  submit_result: EndpointInput
  skip_task: EndpointInput | null
  mapping: {
    task_list_path: string | null
    external_id_path: string
    name_path: string
    goal_path: string
    origin_path: string
    origin_endpoint_path: string | null
    goal_format: 'plain' | 'flag_objective'
    hints_path: string | null
    hints_item_path: string | null
    attachments_path: string | null
    cursor_path: string | null
    lease_id_path: string | null
  }
  outcomes: {
    fetch: { finished: OutcomeConditionInput | null; empty: OutcomeConditionInput | null }
    submit: { accepted: OutcomeConditionInput | null; rejected: OutcomeConditionInput | null; retryable: OutcomeConditionInput }
    skip: { succeeded: OutcomeConditionInput | null }
  }
  health_check: EndpointInput | null
  task_info_mode: 'always' | 'when_incomplete'
  task_info_failure_action: 'fail_task' | 'use_fetch_payload' | 'skip_task'
  task_info_delay_seconds: number
  initial_cursor: string | null
}

export interface StructuredPlatformTemplate {
  id: string
  name: string
  created_at: string
  updated_at: string
  platform: StructuredPlatformInput
}

interface AutopilotStartCommon {
  controller: ControllerInput
  solver_providers: string[]
  agent_limit: number
  max_tasks: number
  task_timeout_minutes: number
  poll_interval_seconds: number
  rejected_action: 'pause' | 'replan' | 'skip' | 'fail'
  solver_failure_action: 'skip' | 'fail'
  stop_active_task_action: 'leave' | 'skip'
  max_replans_after_rejection: number
  max_transport_retries: number
}

export type AutopilotStartPayload =
  | (AutopilotStartCommon & { mode: 'prompt'; platform: { prompt: string } })
  | (AutopilotStartCommon & { mode: 'structured'; platform: StructuredPlatformInput })

export type PlatformValidationPayload =
  | { mode: 'prompt'; platform: { prompt: string } }
  | { mode: 'structured'; platform: StructuredPlatformInput; sample_response?: unknown }

export interface ValidationResult {
  ok: boolean
  status: 'valid' | 'warning' | 'invalid'
  category: string
  model?: string
  warnings?: string[]
  missing?: string[]
  mapping_preview?: { status: string; errors?: string[]; task?: Record<string, unknown>; configured_outcomes?: Record<string, boolean> }
}

export interface CurrentTaskResult extends ValidationResult {
  message?: string | null
  task: {
    external_id: string
    name: string
    goal: string
    origin: string
    hints: string[]
    lease_id: string | null
  } | null
}
