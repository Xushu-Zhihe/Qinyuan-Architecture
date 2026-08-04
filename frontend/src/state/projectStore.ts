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
  logs: Record<string, { stdout: string; stderr: string; all?: string }>
  logOffsets: Record<string, { stdout: number; stderr: number; all: number }>
  logSyncNeeded: Record<string, true>
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
    logOffsets: {},
    logSyncNeeded: {},
    ...initial,
  }
}

function appendLog(
  state: ProjectState,
  payload: Record<string, unknown>,
): ProjectState {
  const recordValue = payload.execution_id ?? payload.planning_attempt_id
  if (recordValue === undefined || recordValue === null) return state
  const executionId = String(recordValue)
  const stream = payload.stream === 'stderr' ? 'stderr' : 'stdout'
  const current = state.logs[executionId] ?? { stdout: '', stderr: '', all: '' }
  const offsets = state.logOffsets[executionId] ?? { stdout: 0, stderr: 0, all: 0 }
  const byteOffset = Number(payload.byte_offset)
  const byteLength = Number(payload.byte_length)
  const combinedByteOffset = Number(payload.combined_byte_offset)
  const combinedByteLength = Number(payload.combined_byte_length)
  const text = typeof payload.text === 'string' ? payload.text : null
  if (text === null) {
    return {
      ...state,
      logSyncNeeded: { ...state.logSyncNeeded, [executionId]: true },
    }
  }
  if (
    (Number.isFinite(byteOffset) && byteOffset !== offsets[stream])
    || (Number.isFinite(combinedByteOffset) && combinedByteOffset !== offsets.all)
  ) {
    return {
      ...state,
      logSyncNeeded: { ...state.logSyncNeeded, [executionId]: true },
    }
  }
  const mergedText = `[${stream}]\n${text}`
  return {
    ...state,
    logs: {
      ...state.logs,
      [executionId]: {
        ...current,
        [stream]: capLogText(current[stream] + text),
        all: capLogText((current.all ?? '') + mergedText),
      },
    },
    logOffsets: {
      ...state.logOffsets,
      [executionId]: {
        ...offsets,
        [stream]: offsets[stream] + (Number.isFinite(byteLength)
          ? byteLength
          : new TextEncoder().encode(text).length),
        all: offsets.all + (Number.isFinite(combinedByteLength)
          ? combinedByteLength
          : new TextEncoder().encode(mergedText).length),
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
  if (event.type === 'intent.log_chunk' || event.type === 'planning.log_chunk') {
    next = appendLog(next, event.payload)
  } else {
    next = { ...next, needsRefresh: true }
  }
  return next
}

export function replaceRecordLogs(
  state: ProjectState,
  recordId: string,
  logs: { stdout: string; stderr: string; all?: string },
  offsets: { stdout: number; stderr: number; all: number },
): ProjectState {
  const current = state.logOffsets[recordId] ?? { stdout: 0, stderr: 0, all: 0 }
  const raced = current.stdout > offsets.stdout || current.stderr > offsets.stderr || current.all > offsets.all
  const syncNeeded = { ...state.logSyncNeeded }
  if (raced) syncNeeded[recordId] = true
  else delete syncNeeded[recordId]
  return {
    ...state,
    logs: {
      ...state.logs,
      [recordId]: {
        stdout: capLogText(logs.stdout),
        stderr: capLogText(logs.stderr),
        all: capLogText(logs.all ?? ''),
      },
    },
    logOffsets: { ...state.logOffsets, [recordId]: offsets },
    logSyncNeeded: syncNeeded,
  }
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
