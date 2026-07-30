import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { IntentExecution, PlanningAttempt } from '../types'
import { LogConsole } from './LogConsole'

const execution: IntentExecution = {
  id: 'exec-a',
  intent_edge_id: 'intent-1',
  provider: 'codex',
  status: 'completed',
  started_at: '2026-07-12T00:00:00Z',
  finished_at: '2026-07-12T00:00:03Z',
  exit_code: 0,
  stdout_path: 'executions/exec-a/stdout.txt',
  stderr_path: 'executions/exec-a/stderr.txt',
  work_dir: 'executions/exec-a/work',
  error_message: null,
}

const planning: PlanningAttempt = {
  id: 'planning-a', project_id: 'project-1', provider: 'codex', status: 'valid',
  snapshot_sequence: 4, queued_at: '2026-07-12T00:00:00Z', started_at: '2026-07-12T00:00:00Z',
  finished_at: '2026-07-12T00:00:02Z', queue_seconds: 0, duration_seconds: 2,
  stdout_path: 'stdout', stderr_path: 'stderr', work_dir: 'work', decision_reason: 'valid',
  intent_edge_id: 'intent-1', retry_count: 0, wait_reasons: [], model: 'unknown',
  cli_version: 'unknown', token_usage: 'unknown', logs_retained: true,
}

describe('LogConsole', () => {
  it('loads stored logs for a selected execution and switches streams', async () => {
    const user = userEvent.setup()
    const loadLogs = vi.fn().mockResolvedValue({ stdout: 'saved output\n', stderr: 'saved error\n' })
    render(
      <LogConsole
        projectId="project-1"
        execution={execution}
        liveLogs={{ stdout: '', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )

    expect(await screen.findByText('saved output', { exact: false })).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'stderr' }))

    expect(screen.getByText('saved error', { exact: false })).toBeInTheDocument()
    expect(loadLogs).toHaveBeenCalledWith('project-1', 'exec-a')
  })

  it('clears only the current view and keeps stored logs available after reselection', async () => {
    const user = userEvent.setup()
    const loadLogs = vi.fn().mockResolvedValue({ stdout: 'persistent output\n', stderr: '' })
    const { rerender } = render(
      <LogConsole
        projectId="project-1"
        execution={execution}
        liveLogs={{ stdout: '', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )
    await screen.findByText('persistent output', { exact: false })

    await user.click(screen.getByRole('button', { name: '清空当前视图' }))
    expect(screen.queryByText('persistent output', { exact: false })).not.toBeInTheDocument()

    rerender(
      <LogConsole
        projectId="project-1"
        execution={null}
        liveLogs={{ stdout: '', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )
    rerender(
      <LogConsole
        projectId="project-1"
        execution={execution}
        liveLogs={{ stdout: '', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )

    await waitFor(() => expect(screen.getByText('persistent output', { exact: false })).toBeInTheDocument())
  })

  it('does not duplicate live text already present in saved logs', async () => {
    const loadLogs = vi.fn().mockResolvedValue({ stdout: 'single copy\n', stderr: '' })
    const { container } = render(
      <LogConsole
        projectId="project-1"
        execution={execution}
        liveLogs={{ stdout: 'single copy\n', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )

    await screen.findByText('single copy', { exact: false })
    const text = container.querySelector('.log-output')?.textContent ?? ''
    expect(text.match(/single copy/g)).toHaveLength(1)
  })

  it('loads planning attempt logs through the planning endpoint', async () => {
    const loadLogs = vi.fn()
    vi.spyOn(api, 'planningLogs').mockResolvedValue({ stdout: 'planning output\n', stderr: '' })
    render(
      <LogConsole
        projectId="project-1"
        execution={null}
        planningAttempt={planning}
        liveLogs={{ stdout: '', stderr: '' }}
        loadLogs={loadLogs}
      />,
    )

    expect(await screen.findByText('planning output', { exact: false })).toBeInTheDocument()
    expect(api.planningLogs).toHaveBeenCalledWith('project-1', 'planning-a')
    expect(loadLogs).not.toHaveBeenCalled()
  })
})
