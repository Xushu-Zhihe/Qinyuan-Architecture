import { render, screen, waitFor } from '@testing-library/react'
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
    testAutopilotController: vi.fn(),
    validateAutopilotPlatform: vi.fn(),
    validateAutopilotPrompt: vi.fn(),
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
        mode: 'prompt',
        controller_provider: null,
        controller: { base_url: 'https://controller.example/v1', model: 'controller-model' },
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
    await userEvent.clear(screen.getByRole('textbox', { name: 'Base URL' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Base URL' }), 'https://controller.example/v1')
    await userEvent.type(screen.getByRole('textbox', { name: '模型' }), 'controller-model')
    await userEvent.type(screen.getByLabelText('API key'), 'controller-secret')
    await userEvent.type(screen.getByRole('textbox', { name: '平台说明 prompt' }), 'platform instructions')
    await userEvent.click(screen.getByRole('button', { name: '启动全自动' }))

    expect(api.startAutopilot).toHaveBeenCalledWith({
      mode: 'prompt',
      controller: {
        api_key: 'controller-secret',
        base_url: 'https://controller.example/v1',
        model: 'controller-model',
        advanced: {
          timeout_seconds: 300,
          max_rounds: 20,
          temperature: null,
          max_output_tokens: null,
          reasoning_effort: null,
          extra_headers: {},
          extra_body: {},
        },
      },
      platform: { prompt: 'platform instructions' },
      solver_providers: ['fake'],
      agent_limit: 1,
      max_tasks: 10,
      task_timeout_minutes: 60,
      poll_interval_seconds: 1,
      rejected_action: 'fail',
      solver_failure_action: 'fail',
      stop_active_task_action: 'leave',
      max_replans_after_rejection: 1,
      max_transport_retries: 2,
    })
    await waitFor(() => {
      expect(screen.getByLabelText('API key')).toHaveValue('')
      expect(screen.getByRole('textbox', { name: '平台说明 prompt' })).toHaveValue('')
    })
  })
})
