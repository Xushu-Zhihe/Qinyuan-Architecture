import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { IntentExecution } from '../types'
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
})
