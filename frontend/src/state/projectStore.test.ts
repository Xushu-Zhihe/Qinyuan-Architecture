import { describe, expect, it } from 'vitest'

import { createProjectState, reduceProjectEvent } from './projectStore'

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
})
