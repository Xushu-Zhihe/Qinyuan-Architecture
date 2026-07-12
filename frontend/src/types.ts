export type BlackboardNodeType = 'goal' | 'origin' | 'fact' | 'hint'
export type WorkspaceMode = 'browse' | 'edit'
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
