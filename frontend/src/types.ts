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
