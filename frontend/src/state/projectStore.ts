import type {
  BlackboardEdge,
  BlackboardNode,
  ProjectEvent,
  ProjectSnapshot,
} from '../types'

export interface ProjectState {
  sequence: number
  needsRefresh: boolean
  snapshot: ProjectSnapshot | null
  logs: Record<string, { stdout: string; stderr: string }>
}

export const MAX_LOG_LINES = 2000

export function capLogText(text: string, maxLines = MAX_LOG_LINES): string {
  const lines = text.split('\n')
  return lines.length <= maxLines ? text : lines.slice(-maxLines).join('\n')
}

export function createProjectState(
  initial: Partial<ProjectState> = {},
): ProjectState {
  return {
    sequence: 0,
    needsRefresh: false,
    snapshot: null,
    logs: {},
    ...initial,
  }
}

function appendLog(
  state: ProjectState,
  payload: Record<string, unknown>,
): ProjectState {
  const executionId = String(payload.execution_id)
  const stream = payload.stream === 'stderr' ? 'stderr' : 'stdout'
  const current = state.logs[executionId] ?? { stdout: '', stderr: '' }
  return {
    ...state,
    logs: {
      ...state.logs,
      [executionId]: {
        ...current,
        [stream]: capLogText(current[stream] + String(payload.text ?? '')),
      },
    },
  }
}

export function reduceProjectEvent(
  state: ProjectState,
  event: ProjectEvent,
): ProjectState {
  if (event.sequence !== state.sequence + 1) {
    return { ...state, needsRefresh: true }
  }
  let next: ProjectState = {
    ...state,
    sequence: event.sequence,
  }
  if (event.type === 'intent.log_chunk') {
    next = appendLog(next, event.payload)
  } else {
    next = { ...next, needsRefresh: true }
  }
  return next
}

export function replaceSnapshot(
  state: ProjectState,
  snapshot: ProjectSnapshot,
): ProjectState {
  return {
    ...state,
    sequence: snapshot.sequence,
    needsRefresh: false,
    snapshot,
  }
}

export function upsertNode(
  nodes: BlackboardNode[],
  node: BlackboardNode,
): BlackboardNode[] {
  return [...nodes.filter(item => item.id !== node.id), node]
}

export function upsertEdge(
  edges: BlackboardEdge[],
  edge: BlackboardEdge,
): BlackboardEdge[] {
  return [...edges.filter(item => item.id !== edge.id), edge]
}
