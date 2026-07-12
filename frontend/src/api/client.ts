import type {
  ExecutionLogs,
  LayoutSnapshot,
  ProjectSnapshot,
  ProjectSummary,
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

export const api = {
  listProjects: () => request<ProjectSummary[]>('/api/projects'),
  createProject: (payload: Record<string, unknown>) =>
    request<ProjectSummary>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
  getGraph: (projectId: string) =>
    request<ProjectSnapshot>(`/api/projects/${projectId}/graph`),
  addNode: (projectId: string, payload: Record<string, unknown>) =>
    request(`/api/projects/${projectId}/nodes`, { method: 'POST', body: JSON.stringify(payload) }),
  addEdge: (projectId: string, payload: Record<string, unknown>) =>
    request(`/api/projects/${projectId}/edges`, { method: 'POST', body: JSON.stringify(payload) }),
  saveLayout: (projectId: string, payload: LayoutSnapshot) =>
    request<LayoutSnapshot>(`/api/projects/${projectId}/layout`, { method: 'PUT', body: JSON.stringify(payload) }),
  startProject: (projectId: string) =>
    request(`/api/projects/${projectId}/start`, { method: 'POST' }),
  stopProject: (projectId: string) =>
    request<ProjectSummary>(`/api/projects/${projectId}/stop`, { method: 'POST' }),
  executionLogs: (projectId: string, executionId: string) =>
    request<ExecutionLogs>(`/api/projects/${projectId}/executions/${executionId}/logs`),
}
