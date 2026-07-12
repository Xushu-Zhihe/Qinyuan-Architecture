import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectSnapshot } from '../types'
import { InspectorPanel } from './InspectorPanel'

const snapshot: ProjectSnapshot = {
  project: {
    id: 'project-1',
    name: 'JWT investigation',
    mode: 'run',
    status: 'running',
    challenge_path: null,
    providers: ['codex', 'claude'],
    agent_limit: 2,
    created_at: '2026-07-12T00:00:00Z',
    updated_at: '2026-07-12T00:00:00Z',
    last_opened_at: '2026-07-12T00:00:00Z',
  },
  nodes: [
    {
      id: 'goal-1',
      type: 'goal',
      content: 'Recover the flag',
      state: 'active',
      generation: 0,
      created_at: '2026-07-12T00:00:00Z',
      sources: ['human'],
      produced_by: null,
    },
    {
      id: 'fact-1',
      type: 'fact',
      content: 'JWT uses HS256',
      state: 'active',
      generation: 2,
      created_at: '2026-07-12T00:01:00Z',
      sources: ['codex'],
      produced_by: { intent_id: 'intent-27', execution_id: 'exec-codex' },
    },
  ],
  edges: [
    {
      id: 'intent-27',
      type: 'intent',
      from_node: 'goal-1',
      to_node: 'fact-1',
      generation: 2,
      created_at: '2026-07-12T00:00:30Z',
      content: 'Verify the signing algorithm',
      sources: ['codex', 'claude'],
      status: 'running',
      executions: [
        {
          id: 'exec-codex',
          intent_edge_id: 'intent-27',
          provider: 'codex',
          status: 'running',
          started_at: '2026-07-12T00:00:30Z',
          finished_at: null,
          exit_code: null,
          stdout_path: 'executions/exec-codex/stdout.txt',
          stderr_path: 'executions/exec-codex/stderr.txt',
          work_dir: 'executions/exec-codex/work',
          error_message: null,
        },
        {
          id: 'exec-claude',
          intent_edge_id: 'intent-27',
          provider: 'claude',
          status: 'completed',
          started_at: '2026-07-12T00:00:35Z',
          finished_at: '2026-07-12T00:01:20Z',
          exit_code: 0,
          stdout_path: 'executions/exec-claude/stdout.txt',
          stderr_path: 'executions/exec-claude/stderr.txt',
          work_dir: 'executions/exec-claude/work',
          error_message: null,
        },
      ],
    },
  ],
  layout: { revision: 0, nodes: {}, edges: {}, viewport: {} },
  candidates: [],
  highest_generation: 2,
  sequence: 8,
}

describe('InspectorPanel', () => {
  it('opens the producing intent execution for a selected fact', () => {
    const onSelectExecution = vi.fn()
    render(
      <InspectorPanel
        selection={{ kind: 'node', value: snapshot.nodes[1] }}
        snapshot={snapshot}
        onSelectExecution={onSelectExecution}
      />,
    )

    expect(screen.getByText('Intent intent-27')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Codex' })).toHaveAttribute('aria-selected', 'true')
    expect(onSelectExecution).toHaveBeenCalledWith(snapshot.edges[0].executions?.[0])
  })

  it('switches between executions without changing the selected intent', async () => {
    const user = userEvent.setup()
    const onSelectExecution = vi.fn()
    render(
      <InspectorPanel
        selection={{ kind: 'edge', value: snapshot.edges[0] }}
        snapshot={snapshot}
        onSelectExecution={onSelectExecution}
      />,
    )

    await user.click(screen.getByRole('tab', { name: 'Claude' }))

    expect(screen.getByRole('tab', { name: 'Claude' })).toHaveAttribute('aria-selected', 'true')
    expect(onSelectExecution).toHaveBeenLastCalledWith(snapshot.edges[0].executions?.[1])
  })
})
