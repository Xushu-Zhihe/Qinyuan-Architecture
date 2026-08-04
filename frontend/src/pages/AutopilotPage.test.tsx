import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { AutopilotSnapshot } from '../types'
import { AutopilotPage } from './AutopilotPage'

vi.mock('../api/client', () => ({
  api: {
    getAutopilot: vi.fn(),
    startAutopilot: vi.fn(),
    stopAutopilot: vi.fn(),
  },
}))

const empty: AutopilotSnapshot = { session: null, providers: ['fake'] }

describe('AutopilotPage', () => {
  beforeEach(() => {
    vi.mocked(api.getAutopilot).mockResolvedValue(empty)
    vi.mocked(api.startAutopilot).mockResolvedValue({
      ...empty,
      session: {
        id: 'auto-1',
        status: 'running',
        controller_provider: 'fake',
        solver_providers: ['fake'],
        available_providers: ['fake'],
        agent_limit: 1,
        max_tasks: 2,
        completed_tasks: 0,
        current_external_id: null,
        current_task_name: null,
        current_project_id: null,
        last_message: null,
        last_error: null,
        history: [],
      },
    })
  })

  it('keeps platform prompt in memory and submits it only on start', async () => {
    render(<AutopilotPage onBack={() => {}} onOpenProject={() => {}} />)

    await screen.findByText('连接配置')
    await userEvent.click(screen.getByRole('button', { name: '显示平台 prompt' }))
    await userEvent.type(screen.getByPlaceholderText('粘贴平台 API、token 和答题接口说明'), 'token=secret')
    await userEvent.click(screen.getByRole('button', { name: '启动全自动' }))

    expect(api.startAutopilot).toHaveBeenCalledWith(expect.objectContaining({
      platform_prompt: 'token=secret',
      controller_provider: 'fake',
      solver_providers: ['fake'],
    }))
  })
})
