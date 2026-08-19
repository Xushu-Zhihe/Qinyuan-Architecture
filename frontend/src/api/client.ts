import type {
  ExecutionLogs,
  ExecutionLogPage,
  IntentExecution,
  LayoutSnapshot,
  PlanningAttempt,
  ProjectSnapshot,
  ProjectSummary,
  RuntimeSnapshot,
  SyncedExecutionLogs,
  AutopilotSnapshot,
  AutopilotStartPayload,
  ControllerInput,
  CurrentTaskResult,
  PlatformValidationPayload,
  StructuredPlatformInput,
  StructuredPlatformTemplate,
  ValidationResult,
} from '../types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail))
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

async function syncLogs(path: string): Promise<SyncedExecutionLogs> {
  let stdoutOffset = 0
  let stderrOffset = 0
  let allOffset = 0
  let stdout = ''
  let stderr = ''
  let all = ''
  while (true) {
    const separator = path.includes('?') ? '&' : '?'
    const page = await request<ExecutionLogPage>(
      `${path}${separator}stdout_offset=${stdoutOffset}&stderr_offset=${stderrOffset}&all_offset=${allOffset}&limit=1048576`,
    )
    stdout += page.stdout
    stderr += page.stderr
    all += page.all ?? ''
    stdoutOffset = page.pagination.stdout.next_offset
    stderrOffset = page.pagination.stderr.next_offset
    allOffset = page.pagination.all.next_offset
    if (!page.pagination.stdout.has_more && !page.pagination.stderr.has_more && !page.pagination.all.has_more) {
      return {
        logs: { stdout, stderr, all },
        offsets: { stdout: stdoutOffset, stderr: stderrOffset, all: allOffset },
      }
    }
  }
}

export const api = {
  listProjects: () => request<ProjectSummary[]>('/api/projects'),
  getRuntime: () => request<RuntimeSnapshot>('/api/runtime'),
  createProject: (payload: object) =>
    request<ProjectSummary>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  deleteProject: (projectId: string) =>
    request<void>(`/api/projects/${projectId}`, { method: 'DELETE' }),
  getGraph: (projectId: string, includeArchived = false) =>
    request<ProjectSnapshot>(`/api/projects/${projectId}/graph?include_archived=${includeArchived}`),
  addNode: (projectId: string, payload: Record<string, unknown>) =>
    request(`/api/projects/${projectId}/nodes`, { method: 'POST', body: JSON.stringify(payload) }),
  archiveNode: (projectId: string, nodeId: string) =>
    request<void>(`/api/projects/${projectId}/nodes/${nodeId}`, { method: 'DELETE' }),
  restoreNode: (projectId: string, nodeId: string) =>
    request<void>(`/api/projects/${projectId}/nodes/${nodeId}/restore`, { method: 'POST' }),
  addEdge: (projectId: string, payload: Record<string, unknown>) =>
    request(`/api/projects/${projectId}/edges`, { method: 'POST', body: JSON.stringify(payload) }),
  updateEdge: (projectId: string, edgeId: string, payload: Record<string, unknown>) =>
    request(`/api/projects/${projectId}/edges/${edgeId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteEdge: (projectId: string, edgeId: string) =>
    request<void>(`/api/projects/${projectId}/edges/${edgeId}`, { method: 'DELETE' }),
  saveLayout: (projectId: string, payload: LayoutSnapshot) =>
    request<LayoutSnapshot>(`/api/projects/${projectId}/layout`, { method: 'PUT', body: JSON.stringify(payload) }),
  startProject: (projectId: string) =>
    request(`/api/projects/${projectId}/start`, { method: 'POST' }),
  stopProject: (projectId: string) =>
    request<ProjectSummary>(`/api/projects/${projectId}/stop`, { method: 'POST' }),
  reopenProject: (projectId: string) =>
    request<ProjectSummary>(`/api/projects/${projectId}/reopen`, { method: 'POST' }),
  retryExecution: (projectId: string, executionId: string) =>
    request<IntentExecution>(`/api/projects/${projectId}/executions/${executionId}/retry`, { method: 'POST' }),
  executionLogs: (projectId: string, executionId: string) =>
    request<ExecutionLogs>(`/api/projects/${projectId}/executions/${executionId}/logs`),
  syncExecutionLogs: (projectId: string, executionId: string) =>
    syncLogs(`/api/projects/${projectId}/executions/${executionId}/logs`),
  planningLogs: (projectId: string, attemptId: string) =>
    request<ExecutionLogs>(`/api/projects/${projectId}/planning-attempts/${attemptId}/logs`),
  syncPlanningLogs: (projectId: string, attemptId: string) =>
    syncLogs(`/api/projects/${projectId}/planning-attempts/${attemptId}/logs`),
  planningAttempts: (projectId: string) =>
    request<PlanningAttempt[]>(`/api/projects/${projectId}/planning-attempts`),
  auditExportUrl: (projectId: string) => `/api/projects/${projectId}/audit/export`,
  getAutopilot: () => request<AutopilotSnapshot>('/api/autopilot'),
  listAutopilotTemplates: () => request<StructuredPlatformTemplate[]>('/api/autopilot/templates'),
  saveAutopilotTemplate: (payload: { name: string; platform: StructuredPlatformInput }) =>
    request<StructuredPlatformTemplate>('/api/autopilot/templates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteAutopilotTemplate: (templateId: string) =>
    request<void>(`/api/autopilot/templates/${templateId}`, { method: 'DELETE' }),
  startAutopilot: (payload: AutopilotStartPayload) =>
    request<AutopilotSnapshot>('/api/autopilot/start', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  stopAutopilot: () => request<AutopilotSnapshot>('/api/autopilot/stop', { method: 'POST' }),
  testAutopilotController: (payload: ControllerInput) =>
    request<ValidationResult>('/api/autopilot/test-controller', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  validateAutopilotPlatform: (payload: PlatformValidationPayload) =>
    request<ValidationResult>('/api/autopilot/validate-platform', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  validateAutopilotPrompt: (controller: ControllerInput, prompt: string) =>
    request<ValidationResult>('/api/autopilot/validate-prompt', {
      method: 'POST',
      body: JSON.stringify({ controller, prompt }),
    }),
  testAutopilotPlatformHealth: (platform: StructuredPlatformInput) =>
    request<ValidationResult>('/api/autopilot/test-platform-health', {
      method: 'POST',
      body: JSON.stringify({ mode: 'structured', platform }),
    }),
  getAutopilotCurrentTask: (platform: StructuredPlatformInput) =>
    request<CurrentTaskResult>('/api/autopilot/current-task', {
      method: 'POST',
      body: JSON.stringify({ mode: 'structured', platform }),
    }),
}
