import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { ProjectSnapshot } from '../types'
import { DiagnosticsPanel } from './DiagnosticsPanel'

const snapshot: ProjectSnapshot = {
  project: {
    id: 'project-1', name: 'Diagnostics', mode: 'run', status: 'idle',
    challenge_path: null, providers: ['codex', 'claude'], agent_limit: 2,
    created_at: '2026-07-30T00:00:00Z', updated_at: '2026-07-30T00:00:00Z',
    last_opened_at: '2026-07-30T00:00:00Z',
  },
  nodes: [],
  edges: [{
    id: 'intent-1', type: 'intent', from_node: 'origin', to_node: 'goal',
    generation: 1, created_at: '2026-07-30T00:00:00Z', content: 'Inspect',
    sources: ['codex'], status: 'failed', executions: [
      {
        id: 'execution-codex', intent_edge_id: 'intent-1', provider: 'codex',
        status: 'interrupted', started_at: '2026-07-30T00:00:00Z', finished_at: '2026-07-30T00:00:01Z',
        exit_code: null, stdout_path: 'stdout', stderr_path: 'stderr', work_dir: 'work', error_message: 'server restarted',
      },
      {
        id: 'execution-claude', intent_edge_id: 'intent-1', provider: 'claude',
        status: 'completed', started_at: '2026-07-30T00:00:00Z', finished_at: '2026-07-30T00:00:01Z',
        exit_code: 0, stdout_path: 'stdout', stderr_path: 'stderr', work_dir: 'work', error_message: null,
      },
    ],
  }],
  planning_attempts: [{
    id: 'planning-1', project_id: 'project-1', provider: 'codex', status: 'valid',
    snapshot_sequence: 1, queued_at: '2026-07-30T00:00:00Z', started_at: '2026-07-30T00:00:00Z',
    finished_at: '2026-07-30T00:00:01Z', queue_seconds: 0, duration_seconds: 1,
    stdout_path: 'stdout', stderr_path: 'stderr', work_dir: 'work', decision_reason: 'valid',
    intent_edge_id: 'intent-1', retry_count: 0, wait_reasons: [], model: 'unknown',
    cli_version: 'unknown', token_usage: 'unknown', logs_retained: true,
  }],
  provider_health: [{
    provider: 'codex', status: 'auth_failed', consecutive_failures: 1,
    last_error: 'authentication required', checked_at: '2026-07-30T00:00:00Z',
    cooldown_until: null, updated_at: '2026-07-30T00:00:00Z', cli_version: 'unknown',
  }],
  recovery: { planning_attempts: 0, executions: 1, total: 1, latest_at: '2026-07-30T00:00:01Z' },
  layout: { revision: 0, nodes: {}, edges: {}, viewport: {} },
  candidates: [], highest_generation: 1, sequence: 3,
}

describe('DiagnosticsPanel', () => {
  it('filters executions and exposes explicit retry and health states', async () => {
    const onRetry = vi.fn()
    render(<DiagnosticsPanel snapshot={snapshot} onSelectExecution={() => {}} onSelectPlanning={() => {}} onSelectIntent={() => {}} onRetry={onRetry} />)

    expect(screen.getByText('认证失败')).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Provider'), 'codex')
    expect(screen.getByText('execution-codex')).toBeInTheDocument()
    expect(screen.queryByText('execution-claude')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '重试 execution-codex' }))
    expect(onRetry).toHaveBeenCalledWith(snapshot.edges[0].executions?.[0])
  })

  it('opens planning logs and locates its intent', async () => {
    const onSelectPlanning = vi.fn()
    const onSelectIntent = vi.fn()
    render(<DiagnosticsPanel snapshot={snapshot} onSelectExecution={() => {}} onSelectPlanning={onSelectPlanning} onSelectIntent={onSelectIntent} onRetry={() => {}} />)

    await userEvent.click(screen.getByRole('tab', { name: /Planning/ }))
    await userEvent.click(screen.getByText('planning-1'))
    expect(onSelectPlanning).toHaveBeenCalledWith(snapshot.planning_attempts[0])
    await userEvent.click(screen.getByRole('button', { name: '定位 intent-1' }))
    expect(onSelectIntent).toHaveBeenCalledWith('intent-1')
  })
})
