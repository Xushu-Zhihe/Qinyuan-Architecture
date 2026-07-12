import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { GraphToolbar } from '../components/GraphToolbar'
import {
  advanceClickConnection,
  buildFlowEdges,
  buildReconnectCommand,
  shouldFitAfterNodeChange,
} from './BlackboardGraph'

describe('blackboard graph editing rules', () => {
  it('locks semantic editing in browse mode', () => {
    render(<GraphToolbar mode="browse" activeTool="select" onSelect={() => {}} onAddFact={() => {}} onConnect={() => {}} onFit={() => {}} />)
    expect(screen.getByRole('button', { name: '添加 Fact' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '连接节点' })).toBeDisabled()
  })

  it('builds reconnect command only in edit mode', () => {
    expect(buildReconnectCommand('browse', 'intent-1', 'fact-1', 'goal-2')).toBeNull()
    expect(buildReconnectCommand('edit', 'intent-1', 'fact-1', 'goal-2')).toEqual({
      edgeId: 'intent-1',
      source: 'fact-1',
      target: 'goal-2',
    })
  })

  it('maps intent relationships to the custom intent edge', () => {
    const edges = buildFlowEdges([
      {
        id: 'intent-1',
        type: 'intent',
        from_node: 'fact-1',
        to_node: 'goal-1',
        generation: 2,
        created_at: 'now',
        content: 'Verify key',
        sources: ['codex'],
        status: 'running',
        executions: [],
      },
    ])
    expect(edges[0].type).toBe('intent')
    expect(edges[0].source).toBe('fact-1')
    expect(edges[0].target).toBe('goal-1')
  })

  it('builds an intent connection from two node clicks in connect mode', () => {
    expect(advanceClickConnection('select', null, 'fact-1')).toEqual({
      source: null,
      command: null,
    })
    expect(advanceClickConnection('connect', null, 'fact-1')).toEqual({
      source: 'fact-1',
      command: null,
    })
    expect(advanceClickConnection('connect', 'fact-1', 'goal-1')).toEqual({
      source: null,
      command: { source: 'fact-1', target: 'goal-1' },
    })
  })

  it('fits the graph when newly produced nodes arrive', () => {
    expect(shouldFitAfterNodeChange(2, 3)).toBe(true)
    expect(shouldFitAfterNodeChange(3, 3)).toBe(false)
    expect(shouldFitAfterNodeChange(0, 3)).toBe(false)
  })
})
