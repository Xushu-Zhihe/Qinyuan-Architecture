import { describe, expect, it } from 'vitest'

import {
  MAX_LOG_LINES,
  createProjectState,
  reduceProjectEvent,
} from './projectStore'

describe('project event reducer', () => {
  it('requests a snapshot when websocket sequence has a gap', () => {
    const state = createProjectState({ sequence: 5 })

    const next = reduceProjectEvent(state, {
      project_id: 'project-1',
      sequence: 7,
      type: 'graph.node_added',
      source: 'codex',
      payload: { node_id: 'fact-1', type: 'fact' },
      created_at: '2026-07-12T00:00:00Z',
    })

    expect(next.needsRefresh).toBe(true)
    expect(next.sequence).toBe(5)
  })

  it('appends log chunks only to the matching execution', () => {
    const state = createProjectState({
      sequence: 8,
      logs: {
        'exec-a': { stdout: 'before\n', stderr: '' },
        'exec-b': { stdout: 'other\n', stderr: 'error\n' },
      },
    })

    const next = reduceProjectEvent(state, {
      project_id: 'project-1',
      sequence: 9,
      type: 'intent.log_chunk',
      source: 'codex',
      payload: { execution_id: 'exec-a', stream: 'stdout', text: 'working\n' },
      created_at: '2026-07-12T00:00:00Z',
    })

    expect(next.logs['exec-a'].stdout).toContain('working')
    expect(next.logs['exec-b']).toEqual(state.logs['exec-b'])
  })

  it('keeps only the most recent in-memory log lines', () => {
    const text = Array.from({ length: MAX_LOG_LINES + 5 }, (_, index) => `line-${index}`).join('\n')
    const next = reduceProjectEvent(createProjectState({ sequence: 3 }), {
      project_id: 'project-1',
      sequence: 4,
      type: 'intent.log_chunk',
      source: 'codex',
      payload: { execution_id: 'exec-a', stream: 'stdout', text },
      created_at: '2026-07-12T00:00:00Z',
    })

    expect(next.logs['exec-a'].stdout).not.toContain('line-0\n')
    expect(next.logs['exec-a'].stdout).toContain(`line-${MAX_LOG_LINES + 4}`)
    expect(next.logs['exec-a'].stdout.split('\n')).toHaveLength(MAX_LOG_LINES)
  })
})
